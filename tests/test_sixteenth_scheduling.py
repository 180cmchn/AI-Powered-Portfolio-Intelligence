"""Sixteenth: real scheduler/worker/ModelClient, only external HTTP/prices are fixtures.

The clock is explicitly historical. These are deterministic production-chain
regressions, not real forward predictions or naturally matured performance.
"""
import asyncio
import json
from datetime import datetime, timedelta

import httpx
import pytest

from app.calendar import SHANGHAI
from app.domain import AccountInput
from app.service import Service, _job_cancellation_source
from app.store import Store
from tests.test_fourteenth_chain import setup_app, final, call, response
from tests.test_fourteenth_deadlines import wait_for_stage
from tests.test_eighth_forecast_preparation import Prices

SYMBOL = 'SZ:159278'


def ready_batch(store, account, at):
    return store.create_forecast_batch(account['id'], at=at, symbols=[SYMBOL],
        baseline_evidence={SYMBOL: {'status': 'ready', 'base_close': '100',
            'base_date': at.date().isoformat(), 'source': 'controlled exact-date fixture'}})


def formal_batch(store, account):
    return next(b for b in store.forecast_batches(account['id']) if b['kind'] == 'formal')


@pytest.mark.asyncio
async def test_scheduler_worker_keeps_inflight_report_then_starts_formal_before_original_cutoff(tmp_path, monkeypatch, frozen_clock):
    frozen_clock.set('2026-11-02T15:29:00+08:00')
    entered, release = asyncio.Event(), asyncio.Event()
    requests, cancelled = [], []
    old_id = None

    async def handler(request):
        body = request.content.decode()
        requests.append(body)
        if old_id and old_id in body and not release.is_set():
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.append(old_id)
                raise
            return response(call('finish_analysis', final(scope=['account'])))
        if account['id'] in body:
            batch = formal_batch(store, account)
            assert batch['id'] in body and 'formal' in body
            return response(call('finish_analysis', final(scope=['account'], forecasts=[
                {'security_id': SYMBOL, 'horizon': horizon, 'direction': 'up' if horizon == 1 else 'abstain',
                 'reason': 'controlled fixture direction/abstention; no real investment assertion'}
                for horizon in (1, 5, 20)])))
        return response(call('finish_analysis', final(scope=['public'])))

    app, store, account = setup_app(tmp_path, monkeypatch, handler,
        model_request_hard_timeout_seconds=30, synthesis_request_hard_timeout_seconds=30,
        analysis_timeout_seconds=90, agent_timeout_seconds=60,
        market_refresh_enabled=False, forecast_evaluation_enabled=False)
    store.save_universe({'watchlist': [], 'candidates': [SYMBOL]})
    account = store.account(account['id'])
    store.save_forecast_settings(account['id'], {'enabled': True})
    prices = Prices()
    monkeypatch.setattr('app.providers.MarketProvider.history', lambda self, symbol, **options: prices.history(symbol, **options))
    service = app.state.service
    old_id = account['latest_job_id']
    await service.tick()
    old_task = service.active[old_id]
    await wait_for_stage(old_task, entered)
    if service.forecast_task:
        await service.forecast_task
    if service.global_task:
        await service.global_task
    frozen_clock.advance(minutes=1)
    await service.tick()
    await service.forecast_task
    batch = formal_batch(store, account)
    original = {key: batch[key] for key in ('id', 'deadline', 'base_date', 'input_version', 'rule', 'symbols')}
    assert batch['state'] == 'waiting_research' and batch['job_id'] is None
    assert batch['research_state'] == 'not_queued'
    assert store.job(old_id)['state'] == 'running'
    assert store.account(account['id'])['latest_job_id'] == old_id
    for _ in range(3):
        await service.tick()
    assert not cancelled
    assert len([r for r in requests if old_id in r]) == 1
    release.set()
    await old_task
    assert store.job(old_id)['state'] == 'completed'
    assert store.analyses(account['id'])[0]['status'] == 'current'
    await service.tick()
    batch = formal_batch(store, account)
    assert {key: batch[key] for key in original} == original
    assert batch['job_id'] != old_id
    await service.active[batch['job_id']]
    assert store.job(batch['job_id'])['state'] == 'completed'
    assert len(store.analyses(account['id'])) == 2
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM forecasts WHERE batch_id=? AND kind=\'formal\'', (batch['id'],)).fetchone()[0] == 3
        assert db.execute('SELECT count(*) FROM forecast_execution_attempts WHERE batch_id=?', (batch['id'],)).fetchone()[0] == 1
    assert not cancelled
    # Repeated ticks never invoke the completed formal team again.
    before = len(requests)
    for _ in range(3):
        await service.tick()
    assert len(requests) == before
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as api:
        assert (await api.get('/api/jobs/'+old_id)).json()['state'] == 'completed'

    # Published forecasts are scored through the normal scheduler with research
    # paused. Historical fixture dates/prices do not imply natural maturation.
    store.set_meta('settings', {**store.settings(), 'scheduled_research_enabled': False,
        'forecast_evaluation_enabled': True})
    frozen_clock.set('2026-11-03T15:31:00+08:00')
    service = Service(Store(store.path))
    await service.scheduler_tick()
    await service.forecast_task
    with store.connect() as db:
        scored = [dict(r) for r in db.execute('SELECT e.* FROM forecast_evaluations e JOIN forecasts f ON f.id=e.forecast_id WHERE f.batch_id=?', (batch['id'],))]
    assert len(scored) == 1 and scored[0]['hit'] == 1
    assert len(requests) == before
    frozen_clock.advance(minutes=2)
    await service.scheduler_tick()
    await service.forecast_task
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM forecast_evaluations e JOIN forecasts f ON f.id=e.forecast_id WHERE f.batch_id=?', (batch['id'],)).fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM forecasts WHERE batch_id=?', (batch['id'],)).fetchone()[0] == 3
    assert len(requests) == before


@pytest.mark.parametrize('state,frozen', [('queued', False), ('waiting_config', False), ('queued', True), ('waiting_config', True), ('running', True), ('waiting_scope', True)])
def test_first_batch_merges_only_unfrozen_compatible_work(tmp_path, frozen_clock, state, frozen):
    at = datetime(2026, 11, 2, 16, tzinfo=SHANGHAI)
    frozen_clock.set(at)
    store = Store(tmp_path/'queue.sqlite3')
    account = store.save_account({'name': 'scope', 'cash': '100', 'holdings': [{'symbol': SYMBOL, 'quantity': '1'}]})
    old_id = account['latest_job_id']
    with store.connect() as db:
        db.execute('UPDATE jobs SET state=?,config_snapshot=?,attempts=? WHERE id=?', (state, '{}' if frozen else None, int(frozen), old_id))
    batch = ready_batch(store, account, at)
    for _ in range(3):
        job = store.enqueue_forecast_batch(batch['id'], at)
    if frozen:
        assert job is None
        assert formal_batch(store, account)['state'] == 'waiting_research'
    else:
        assert job['id'] == old_id
        with store.connect() as db:
            assert db.execute('SELECT reason FROM forecast_job_bindings').fetchone()[0] == 'merged_before_execution'
    assert store.job(old_id)['state'] == state
    assert store.account(account['id'])['latest_job_id'] == old_id
    assert len(store.jobs(account['id'])) == 1


@pytest.mark.parametrize('terminal', ['completed', 'failed', 'cancelled'])
def test_waiting_batch_survives_restart_and_follows_prior_terminal_once(tmp_path, frozen_clock, terminal):
    at = datetime(2026, 11, 2, 16, tzinfo=SHANGHAI)
    frozen_clock.set(at)
    store = Store(tmp_path/'queue.sqlite3')
    account = store.save_account(AccountInput(name='cash', cash='100', holdings=[], research_scope='cash_only'))
    old_id = account['latest_job_id']
    with store.connect() as db:
        db.execute("UPDATE jobs SET state='running' WHERE id=?", (old_id,))
    batch = ready_batch(store, account, at)
    assert store.enqueue_forecast_batch(batch['id'], at) is None
    store = Store(store.path)
    store.recover()
    assert store.job(old_id)['state'] == 'queued'
    assert store.dispatch_waiting_forecast_batches(at) == []
    store.set_job_state(old_id, terminal, 'controlled original task terminal')
    jobs = store.dispatch_waiting_forecast_batches(at)
    assert len(jobs) == 1 and jobs[0]['id'] != old_id
    assert store.job(old_id)['state'] == terminal
    assert store.dispatch_waiting_forecast_batches(at) == []
    assert formal_batch(store, account)['deadline'] == batch['deadline']


def test_waiting_expiry_and_version_change_never_create_late_formal_task(tmp_path, frozen_clock):
    at = datetime(2026, 11, 2, 16, tzinfo=SHANGHAI)
    frozen_clock.set(at)
    store = Store(tmp_path/'queue.sqlite3')
    first = store.save_account(AccountInput(name='expiry', cash='100', holdings=[], research_scope='cash_only'))
    second = store.save_account(AccountInput(name='version', cash='100', holdings=[], research_scope='cash_only'))
    one, two = ready_batch(store, first, at), ready_batch(store, second, at)
    for batch in (one, two):
        assert store.enqueue_forecast_batch(batch['id'], at) is None
    store.save_universe({'watchlist': [], 'candidates': [SYMBOL]})
    store.enqueue_forecast_batch(two['id'], at)
    assert formal_batch(store, second)['state'] == 'failed'
    assert all('输入已变化' in s['reason'] for s in store.forecasts_page(account_id=second['id'])['items'])
    cutoff = datetime.fromisoformat(one['deadline'])
    frozen_clock.set(cutoff)
    assert store.dispatch_waiting_forecast_batches(cutoff) == []
    batch = formal_batch(store, first)
    assert batch['state'] == 'closed' and batch['job_id'] is None
    assert all(s['state'] == 'missed' and '原正式截止' in s['reason'] for s in store.forecasts_page(account_id=first['id'])['items'])


def test_cancelled_formal_execution_does_not_revive_from_later_timer(tmp_path, frozen_clock):
    at = datetime(2026, 11, 2, 16, tzinfo=SHANGHAI)
    frozen_clock.set(at)
    store = Store(tmp_path/'queue.sqlite3')
    account = store.save_account({'name': 'formal', 'cash': '100', 'holdings': [{'symbol': SYMBOL, 'quantity': '1'}]})
    batch = ready_batch(store, account, at)
    job = store.enqueue_forecast_batch(batch['id'], at)
    store.set_job_state(job['id'], 'cancelled', '用户取消')
    store.request_analysis(account['id'], 'scheduled', 'next-normal-timer')
    assert store.enqueue_forecast_batch(batch['id'], at) is None
    assert formal_batch(store, account)['job_id'] == job['id']
    assert store.job(job['id'])['state'] == 'cancelled'


@pytest.mark.asyncio
@pytest.mark.parametrize('replacement,expected', [('manual', 'same_version_replaced'), ('universe', 'input_version_updated')])
async def test_worker_cancellation_reason_is_persisted_not_inferred(tmp_path, monkeypatch, frozen_clock, replacement, expected):
    frozen_clock.set('2026-11-02T14:00:00+08:00')
    entered, release = asyncio.Event(), asyncio.Event()
    observed = []
    async def handler(request):
        if account['latest_job_id'] not in request.content.decode():
            return response(call('finish_analysis', final(scope=['public'])))
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError as exc:
            observed.append(str(exc))
            raise
        return response(call('finish_analysis', final()))
    app, store, account = setup_app(tmp_path, monkeypatch, handler,
        scheduled_research_enabled=False, forecast_evaluation_enabled=False, market_refresh_enabled=False)
    service = app.state.service
    await service.tick()
    old_task = service.active[account['latest_job_id']]
    await wait_for_stage(old_task, entered)
    if replacement == 'manual':
        store.enqueue(account['id'])
    else:
        store.save_universe({'watchlist': [], 'candidates': [SYMBOL]})
    # Cancellation maintenance uses normal worker tick; hold new jobs to avoid
    # generating a second paid fixture while checking the first request.
    with store.connect() as db:
        db.execute("UPDATE jobs SET due_at='2099-01-01T00:00:00+08:00' WHERE state='queued'")
    await service.tick()
    await asyncio.gather(old_task, return_exceptions=True)
    if service.global_task:
        await service.global_task
    saved = store.job(account['latest_job_id'], events=True)
    assert saved['cancellation']['code'] == expected
    assert observed == [expected]
    assert any(e.get('cancelled_by') == expected and e['type'] == 'interrupted' for e in saved['events'])
    assert _job_cancellation_source(saved) == expected
    assert _job_cancellation_source({'state': 'superseded'}) == 'superseded_reason_unknown'
    assert store.analyses(account['id']) == []


@pytest.mark.asyncio
async def test_paused_research_and_scoring_still_expire_waiting_formal_without_model_start(tmp_path, frozen_clock):
    at = datetime(2026, 11, 2, 16, tzinfo=SHANGHAI)
    frozen_clock.set(at)
    store = Store(tmp_path/'paused.sqlite3')
    store.set_meta('settings', {**store.settings(), 'scheduled_research_enabled': False,
        'forecast_evaluation_enabled': False, 'market_refresh_enabled': False})
    account = store.save_account(AccountInput(name='paused', cash='100', holdings=[], research_scope='cash_only'))
    batch = ready_batch(store, account, at)
    assert store.enqueue_forecast_batch(batch['id'], at) is None
    store.set_job_state(account['latest_job_id'], 'cancelled', '用户取消原研究')
    service = Service(store)
    await service.tick()
    assert formal_batch(store, account)['state'] == 'waiting_research'
    assert not service.active
    frozen_clock.set(batch['deadline'])
    await service.tick()
    assert formal_batch(store, account)['state'] == 'closed'
    assert formal_batch(store, account)['job_id'] is None
    assert all(s['state'] == 'missed' for s in store.forecasts_page(account_id=account['id'])['items'])
    assert len(store.jobs(account['id'])) == 1 and not service.active


@pytest.mark.parametrize('symbols,merged', [([SYMBOL], True), ([SYMBOL, 'SH:518880'], False)])
def test_single_security_queued_task_cannot_absorb_wider_formal_scope(tmp_path, frozen_clock, symbols, merged):
    at = datetime(2026, 11, 2, 16, tzinfo=SHANGHAI)
    frozen_clock.set(at)
    store = Store(tmp_path/'scope.sqlite3')
    account = store.save_account({'name': 'two holdings', 'cash': '100', 'holdings': [
        {'symbol': symbol, 'quantity': '1'} for symbol in (SYMBOL, 'SH:518880')]})
    job = store.enqueue(account['id'], reason='manual_security:' + SYMBOL)
    batch = store.create_forecast_batch(account['id'], at=at, symbols=symbols,
        baseline_evidence={symbol: {'status': 'ready'} for symbol in symbols})
    result = store.enqueue_forecast_batch(batch['id'], at=at)
    assert (result is not None) is merged
    assert store.account(account['id'])['latest_job_id'] == job['id']
    assert store.job(job['id'])['state'] == 'queued'
    assert store.delivery_scope(job)['allowed'] == [SYMBOL]
    if not merged:
        assert formal_batch(store, account)['state'] == 'waiting_research'
        assert formal_batch(store, account)['job_id'] is None
