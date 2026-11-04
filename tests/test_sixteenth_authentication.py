"""Authentication rejection uses the durable configuration gate, not timer retries.

Real ModelClient/Agent/Service/Store with scripted HTTP and isolated credentials;
this is scheduling/transport-contract evidence, not a real vendor auth probe.
"""
import asyncio
import json

import httpx
import pytest

from app.model import ModelClient, ModelConfigurationError
from app.service import Service
from app.store import Store
from tests.test_fourteenth_chain import setup_app, final, call, response
from tests.test_ninth_calculation_context import seed


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [401, 403])
@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
async def test_authentication_rejection_is_configuration_error_once_and_redacted(status, protocol):
    requests = []

    def reject(request):
        requests.append(request)
        return httpx.Response(status, text='reflected-private-key forbidden-private-body',
            headers={'x-request-id': 'safe-request-id'})

    client = ModelClient({'model_name': 'authentication-fixture',
        'model_base_url': 'https://fixture.invalid/v1', 'model_protocol': protocol,
        '_model_api_key': 'fixture-only-private-key',
        'model_native_search': False, 'model_retries': 3}, transport=httpx.MockTransport(reject))
    try:
        with pytest.raises(ModelConfigurationError) as error:
            await client.complete(client.session('test', {}), [])
        assert error.value.code == f'upstream_http_{status}'
        assert str(status) in str(error.value)
        assert len(requests) == len(client.requests) == 1
        event = client.requests[0]
        assert event['error_code'] == f'upstream_http_{status}'
        assert event['request_accepted'] is False
        assert event['retry_suppressed'] == 'authentication_configuration_rejected'
        assert 'private' not in str(error.value) + json.dumps(event)
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [401, 403])
async def test_authentication_waits_across_scheduler_ticks_restart_and_new_demands_until_credential_revision(
        tmp_path, monkeypatch, frozen_clock, status):
    frozen_clock.set('2026-11-02T14:00:00+08:00')
    requests = []
    restored = False

    def handler(request):
        context = seed(json.loads(request.content))
        requests.append({'job_id': context['job_id'],
                         'account_id': (context.get('account') or {}).get('id')})
        if not restored:
            return httpx.Response(status, text='reflected-private-key forbidden-private-body')
        assert request.headers['authorization'] == 'Bearer rotated-fixture-only-key'
        return response(call('finish_analysis', final(scope=['account' if context.get('account') else 'public'],
            covered_demand_ids=[item['id'] for item in context.get('business_demands', [])])))

    app, store, account = setup_app(tmp_path, monkeypatch, handler,
        scheduled_research_enabled=True, market_refresh_enabled=False,
        forecast_evaluation_enabled=False, model_retries=3,
        scan_interval_minutes=1, analysis_interval_minutes=1)
    # New ordinary scheduled jobs need an explicit securities scope. The
    # cash-only setup job alone is not authorization for every future window.
    store.save_universe({'watchlist': [], 'candidates': ['SZ:159278']})
    account = store.account(account['id'])
    initial_job_ids = {job['id'] for job in store.jobs(account['id'])}
    service = app.state.service

    async def tick(worker):
        await worker.tick()
        tasks = {*worker.active.values(), *worker.maintenance}
        if worker.global_task:
            tasks.add(worker.global_task)
        if tasks:
            await asyncio.gather(*tasks)

    await tick(service)
    old_job = store.job(account['latest_job_id'])
    old_run = store.global_runs()[0]
    assert len(requests) == 2  # One account and one public HTTP; no HTTP retry.
    assert old_job['state'] == old_run['state'] == 'waiting_config'
    assert store.analyses(account['id']) == []
    original_fingerprint = old_job['config_snapshot']['_model_configuration_fingerprint']
    for _ in range(3):
        await tick(service)
    # Ordinary non-model settings and restart cannot clear the rejected identity.
    store.set_meta('settings', {**store.settings(), 'quote_cache_seconds': 45})
    service.resume_waiting_config()
    reopened = Store(store.path)
    reopened._credential_store = store._credential_store
    reopened.recover(config_ready=True)
    restarted = Service(reopened)
    for _ in range(3):
        frozen_clock.advance(seconds=61)
        await tick(restarted)
    assert len(requests) == 2
    assert {job['id'] for job in reopened.jobs(account['id'])} == initial_job_ids
    assert len(reopened.global_runs()) == 1
    assert reopened.job(old_job['id'])['state'] == reopened.global_run(old_run['id'])['state'] == 'waiting_config'
    demands = reopened.demands(limit=1000)
    assert len([d for d in demands if d['kind'] == 'scheduled']) == 4
    assert len([d for d in demands if d['kind'] == 'candidate_scan']) == 4
    assert all(d['state'] == 'assigned' and d['job_id'] in {old_job['id'], old_run['id']} for d in demands)
    assert any('waiting' in d['detail'] for d in demands)
    with reopened.connect() as db:
        persisted = [json.loads(row[0]) for row in db.execute('SELECT body FROM events')]
    attempts = [r for event in persisted for r in event.get('payload', {}).get('requests', [])]
    assert len(attempts) == 2
    assert all(r['error_code'] == f'upstream_http_{status}' and r['request_accepted'] is False for r in attempts)
    assert 'private' not in json.dumps(persisted)

    # The real provider-save path changes credential_revision and the frozen
    # team fingerprint. The external key vault and catalog alone are fixtures.
    provider = reopened.provider(store._fixture_provider_id)
    revised = await reopened.save_provider({'api_key': 'rotated-fixture-only-key'}, provider['id'],
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
            'data': [{'id': 'fourteenth-controlled'}]})))
    assert revised['credential_revision'] == provider['credential_revision'] + 1
    restored = True
    assert restarted.resume_waiting_config()
    assert reopened.job(old_job['id'])['state'] == 'queued'
    assert reopened.global_run(old_run['id'])['state'] == 'superseded'
    await tick(restarted)
    resumed = reopened.job(old_job['id'])
    assert resumed['state'] == 'completed', resumed.get('error')
    assert resumed['attempts'] == 2
    assert resumed['config_snapshot']['_model_configuration_fingerprint'] != original_fingerprint
    assert len(reopened.analyses(account['id'])) == 1
    assert len([run for run in reopened.global_runs() if run['state'] == 'completed']) == 1
    assert len(requests) == 4
    # Later account demands were not part of the old frozen read scope. Its
    # recovery must not claim that the model saw them; they stay uncovered.
    remaining = reopened.demands(limit=1000)
    assert len([d for d in remaining if d['state'] == 'uncovered']) == 3
    assert all(d['state'] in {'completed', 'uncovered'} for d in remaining)
    for _ in range(3):
        await tick(restarted)
    assert len(requests) == 4
    # A genuinely new window can run under the repaired credentials.
    frozen_clock.advance(seconds=61)
    await tick(restarted)
    assert len(requests) == 6
    assert reopened.jobs(account['id'])[0]['state'] == 'completed'
    assert reopened.global_runs()[0]['state'] == 'completed'
