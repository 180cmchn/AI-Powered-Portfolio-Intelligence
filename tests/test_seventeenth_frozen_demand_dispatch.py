"""Recovered account jobs must not absorb demands outside their frozen scope."""
import json

import httpx
import pytest

from app.domain import AccountInput
from app.service import Service
from app.store import Store
from tests.test_fourteenth_chain import setup_app, final, call, response
from tests.test_ninth_calculation_context import seed


@pytest.fixture
def account_queue(tmp_path, frozen_clock):
    frozen_clock.set('2026-11-02T14:00:00+08:00')
    store = Store(tmp_path / 'queue.sqlite3')
    store.set_meta('settings', {**store.settings(), 'debounce_seconds': 0,
        'scheduled_research_enabled': False, 'market_refresh_enabled': False,
        'forecast_evaluation_enabled': False})
    account = store.save_account(AccountInput(name='dispatch', cash='100', holdings=[], research_scope='cash_only'))
    return store, account


def freeze_job(store, account_id, job_id):
    assert [job['id'] for job in store.claim_jobs(account_id=account_id)] == [job_id]
    return store.task_demands(job_id, account_id)


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal', ['completed', 'failed'])
async def test_recovered_frozen_queue_dispatches_late_demands_after_terminal(
        tmp_path, frozen_clock, monkeypatch, terminal):
    frozen_clock.set('2026-11-02T14:00:00+08:00')
    scopes_seen = []
    symbol = 'SZ:159278'

    def handler(request):
        context = seed(json.loads(request.content))
        assert context['account']['id'] == account_id
        demand_ids = [row['id'] for row in context.get('business_demands', [])]
        scopes_seen.append((context['job_id'], demand_ids))
        if context['job_id'] == old_id and terminal == 'failed':
            return httpx.Response(400, text='controlled research failure')
        return response(call('finish_analysis', final(scope=['account', symbol], covered_demand_ids=demand_ids)))

    _, store, placeholder = setup_app(tmp_path, monkeypatch, handler, scheduled_research_enabled=False,
        market_refresh_enabled=False, forecast_evaluation_enabled=False, model_retries=0)
    # A holding gives both jobs a security scope without starting public research.
    store.set_job_state(placeholder['latest_job_id'], 'cancelled')
    account = store.save_account(AccountInput(name='recovery', cash='100',
        holdings=[{'symbol': symbol, 'quantity': '1'}]))
    account_id, old_id = account['id'], account['latest_job_id']
    original = store.request_analysis(account_id, 'scheduled', 'initial-window')
    old_reason = store.job(old_id)['reason']
    frozen = freeze_job(store, account_id, old_id)
    assert [row['id'] for row in frozen['items']] == [original['id']]

    reopened = Store(store.path)
    reopened._credential_store = store._credential_store
    store = reopened
    store.recover(config_ready=True)
    recovered = store.job(old_id)
    assert recovered['state'] == 'queued' and recovered['recovery_attempts'] == 1

    frozen_clock.advance(seconds=1)
    price = store.request_analysis(account_id, 'price_change', 'new-price')
    frozen_clock.advance(seconds=1)
    review = store.request_analysis(account_id, 'recommendation_review', 'new-review')
    late_ids = [price['id'], review['id']]
    by_id = {row['id']: row for row in store.demands(account_id)}
    for demand_id in late_ids:
        row = by_id[demand_id]
        assert (row['state'], row['job_id']) == ('pending', None)
        assert row['detail'] == 'deferred: queued research scope already frozen'
        assert row['updated_at'] == review['created_at']
    assert by_id[price['id']]['updated_at'] > price['updated_at']
    assert store.account(account_id)['latest_job_id'] == old_id
    assert store.job(old_id)['reason'] == old_reason
    assert store.task_demands(old_id, account_id) == frozen

    # Isolate tick-driven dispatch from the independent recommendation sweep.
    monkeypatch.setattr(store, 'review_recommendations', lambda *args, **kwargs: None)
    service = Service(store)
    await service.tick()
    await service.active[old_id]
    assert service.global_task is None
    old_job = store.job(old_id)
    assert old_job['state'] == terminal, old_job['error']
    assert old_job['reason'] == old_reason
    assert scopes_seen == [(old_id, [original['id']])]
    after_terminal = {row['id']: row for row in store.demands(account_id)}
    assert after_terminal[original['id']]['state'] == terminal
    assert [after_terminal[demand_id] for demand_id in late_ids] == [by_id[demand_id] for demand_id in late_ids]

    # No new event is needed; the next worker tick dispatches the pending backlog.
    await service.tick()
    new_id = store.account(account_id)['latest_job_id']
    assert new_id != old_id and store.job(new_id)['input_version'] == account['version']
    assert {row['id']: row['state'] for row in store.demands(job_id=new_id)} == dict.fromkeys(late_ids, 'assigned')
    await service.active[new_id]
    new_job = store.job(new_id)
    assert new_job['state'] == 'completed', new_job['error']
    assert scopes_seen == [(old_id, [original['id']]), (new_id, late_ids)]
    assert new_job['result']['demand_coverage']['items'] == [
        {'id': demand_id, 'state': 'completed'} for demand_id in late_ids]
    assert all(row['detail'] == 'explicitly covered in published report' for row in store.demands(job_id=new_id))


@pytest.mark.parametrize('state,detail', [
    ('queued', 'merged before execution'),
    ('waiting_config', 'merged: waiting for model configuration'),
])
def test_same_version_merge_contract(account_queue, state, detail):
    store, account = account_queue
    account_id, job_id = account['id'], account['latest_job_id']
    first = store.request_analysis(account_id, 'scheduled', 'initial-window')
    if state == 'waiting_config':
        frozen = freeze_job(store, account_id, job_id)
        store.set_job_state(job_id, 'waiting_config', 'controlled configuration rejection')

    late = store.request_analysis(account_id, 'price_change', 'new-price')
    assert (late['state'], late['job_id'], late['input_version'], late['detail']) == (
        'assigned', job_id, account['version'], detail)
    job = store.job(job_id)
    assert job['state'] == state
    assert job['reason'] == 'scheduled; price_change'
    assert [job['id'] for job in store.jobs(account_id)] == [job_id]
    page = store.task_demands(job_id, account_id)
    if state == 'waiting_config':
        assert page == frozen
    else:
        assert [row['id'] for row in page['items']] == [first['id'], late['id']]


def test_new_input_version_replaces_recovered_frozen_job(account_queue):
    store, account = account_queue
    account_id, old_id = account['id'], account['latest_job_id']
    first = store.request_analysis(account_id, 'scheduled', 'initial-window')
    freeze_job(store, account_id, old_id)
    store.recover(config_ready=True)
    assert store.job(old_id)['state'] == 'queued'

    updated = store.save_account({'name': 'new input', 'expected_version': account['version']}, account_id)
    new_id = updated['latest_job_id']
    late = store.request_analysis(account_id, 'recommendation_review', 'new-version-review')
    assert new_id != old_id and updated['version'] == account['version'] + 1
    assert store.job(old_id)['state'] == 'superseded'
    assert store.job(old_id)['reason'] == 'scheduled'
    new_job = store.job(new_id)
    assert new_job['state'] == 'queued' and new_job['input_version'] == updated['version']
    assert (late['state'], late['job_id'], late['input_version']) == ('assigned', new_id, updated['version'])
    page = store.task_demands(new_id, account_id)
    assert [(row['id'], row['input_version']) for row in page['items']] == [
        (demand_id, updated['version']) for demand_id in (first['id'], late['id'])]
