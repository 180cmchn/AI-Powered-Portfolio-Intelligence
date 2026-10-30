"""Production SQLite, queue, Service and ResearchTools; prices/model are explicit fixtures."""
from ledger_helpers import save_snapshot_correction
import asyncio
from collections import Counter
from datetime import datetime, timedelta
import json

import pytest

from app.calendar import SHANGHAI
from app.service import Service
from app.store import Store
from helpers_sixth import configure_test_team

A, B = 'SZ:159278', 'SZ:159279'


@pytest.fixture
def env(tmp_path, monkeypatch):
    clock = [datetime(2026, 10, 28, 16, tzinfo=SHANGHAI)]
    for module in ('app.store', 'app.forecasts', 'app.forecast_windows', 'app.service'):
        monkeypatch.setattr(module + '.now', lambda: clock[0].isoformat())
    import app.forecasts as forecasts
    local = forecasts._local
    monkeypatch.setattr(forecasts, '_local', lambda at=None: clock[0] if at is None else local(at))
    store = Store(tmp_path / 'isolated.sqlite3')
    store.set_meta('settings', {'debounce_seconds': 0, 'market_refresh_enabled': False})
    configure_test_team(store, {'model_name': 'protocol-fixture', 'reasoning_effort': 'medium'})
    account = make_account(store, [A, B])
    return store, account, clock


def make_account(store, symbols, name='A'):
    account = store.save_account({'name': name, 'cash': '10000', 'holdings': [
        {'symbol': symbol, 'quantity': '10000'} for symbol in symbols]})
    store.save_forecast_settings(account['id'], {'enabled': True})
    return account


class Prices:
    def __init__(self, missing=(), delay=0):
        self.missing, self.delay, self.calls = set(missing), delay, []

    async def history(self, symbol, **options):
        self.calls.append(symbol)
        if self.delay:
            await asyncio.sleep(self.delay)
        return {'symbol': symbol, 'source': 'offline exact-date fixture', 'status': 'ok',
                'adjustment': options['adjust'], 'rows': [] if symbol in self.missing else [
                    {'time': options['start'], 'close': '100', 'volume': '1000'},
                    *([{'time': options['end'], 'close': '102', 'volume': '1000'}] if options['end'] != options['start'] else [])]}


async def tick(store, clock, prices, **config):
    return await store.forecast_tick({'forecast_evaluation_enabled': False, **config}, at=clock[0], provider=prices)


def claim(store, clock, account):
    with store.connect() as db:
        db.execute('UPDATE jobs SET due_at=? WHERE account_id=?', (clock[0].isoformat(), account['id']))
    return store.claim_jobs(account_id=account['id'])[0]


def directions(context, direction='up'):
    return {'report': 'Explicit offline protocol model fixture: fresh reasoning invocation',
            'forecasts': [{'symbol': item['symbol'], 'horizon': item['horizon'],
                'direction': direction, 'reason': 'protocol test judgement, not an actual forecast'}
                for item in context['forecast_batch']['items'] if item['direction_eligible']]}


def service_for(store, monkeypatch, runner):
    service = Service(store, runner=runner)
    async def quotes(symbols=None): return {}
    monkeypatch.setattr(service, 'refresh_quotes', quotes)
    return service


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['queued', 'running'])
async def test_partial_ready_keeps_collecting_and_tool_delivers_new_evidence(env, monkeypatch, state):
    store, account, clock = env
    prices = Prices([B])
    await tick(store, clock, prices)
    batch = store.forecast_batches()[0]
    original_job = batch['job_id']
    assert batch['preparation'] == {'ready': 1, 'waiting_data': 1}
    assert Counter(prices.calls) == {A: 1, B: 1}
    assert {item['state'] for item in store.forecast_job_context(original_job)['items'] if item['symbol'] == B} == {'waiting_data'}
    entered, release = asyncio.Event(), asyncio.Event()
    delivered = []
    async def runner(context, cfg, tools, emit):
        entered.set(); await release.wait()
        delivered.append(await tools.list_forecast_targets())
        context['forecast_batch'] = delivered[-1]
        return directions(context)
    task = None
    if state == 'running':
        task = asyncio.create_task(service_for(store, monkeypatch, runner).execute(claim(store, clock, account)))
        await entered.wait()
    clock[0] += timedelta(minutes=1, seconds=1)
    prices.missing.clear()
    await tick(store, clock, prices)
    assert Counter(prices.calls) == {A: 1, B: 2}
    assert store.forecast_batches()[0]['job_id'] == original_job
    assert store.forecast_batches()[0]['preparation'] == {'ready': 2}
    if task:
        release.set(); await task
        rows = delivered[-1]['items']
        assert all(item['baseline']['shared_evidence_id'] for item in rows)
        assert {item['symbol'] for item in rows if item['direction_eligible']} == {A, B}
        assert store.job(original_job)['state'] == 'completed'
        assert store.forecast_statistics()['expected'] == 6
        assert len([r for r in store.forecasts_page()['items'] if r['forecast_id']]) == 6


@pytest.mark.asyncio
async def test_41_offline_symbols_cross_tick_restart_and_fair_accounts(env):
    store, account, clock = env
    symbols = [f'SZ:{100000 + n}' for n in range(41)]
    save_snapshot_correction(store, {'expected_version': account['version'], 'name': 'A', 'cash': '10000', 'holdings': [
        {'symbol': symbol, 'quantity': '1'} for symbol in symbols]}, account['id'])
    prices = Prices()
    await tick(store, clock, prices)
    assert len(prices.calls) == 40
    batch = store.forecast_batches()[0]
    assert batch['job_id'] and batch['expected_count'] == 123
    assert batch['preparation'] == {'ready': 40, 'unattempted': 1}
    store = Store(store.path)
    clock[0] += timedelta(minutes=1)
    await tick(store, clock, prices)
    assert len(prices.calls) == 41 and len(set(prices.calls)) == 41
    assert store.forecast_batches()[0]['id'] == batch['id']
    assert store.forecast_batches()[0]['preparation'] == {'ready': 41}
    assert store.forecasts_page()['total'] == 123
    context = store.forecast_job_context(store.forecast_batches()[0]['job_id'], limit=200)
    assert all(item['direction_eligible'] for item in context['items'])


@pytest.mark.asyncio
async def test_partial_publish_supplement_is_new_service_judgment_same_batch(env, monkeypatch):
    store, account, clock = env
    prices, seen = Prices([B]), []
    async def runner(context, cfg, tools, emit):
        seen.append((context['job_id'], context['forecast_batch']))
        return directions(context, 'up' if len(seen) == 1 else 'down')
    service = service_for(store, monkeypatch, runner)
    await tick(store, clock, prices)
    batch = store.forecast_batches()[0]
    await service.execute(claim(store, clock, account))
    initial = [store.forecast_detail(r['forecast_id']) for r in store.forecasts_page()['items'] if r['forecast_id']]
    assert len(initial) == 3 and {f['symbol'] for f in initial} == {A}
    clock[0] += timedelta(minutes=2)
    prices.missing.clear()
    await tick(store, clock, prices)
    current = store.forecast_batches()[0]
    assert current['id'] == batch['id'] and current['job_id'] != batch['job_id']
    assert current['deadline'] == batch['deadline'] and current['input_version'] == batch['input_version']
    await service.execute(claim(store, clock, account))
    assert len(seen) == 2
    assert all(not item['direction_eligible'] for item in seen[1][1]['items'] if item['symbol'] == A)
    saved = [store.forecast_detail(r['forecast_id']) for r in store.forecasts_page()['items']]
    assert {f['direction'] for f in saved if f['symbol'] == A} == {'up'}
    assert {f['direction'] for f in saved if f['symbol'] == B} == {'down'}
    assert {f['published_at'] for f in saved if f['symbol'] == B} == {clock[0].isoformat()}
    assert [store.forecast_detail(f['id']) for f in initial] == initial
    for _ in range(3):
        await tick(store, clock, prices)
    assert len(store.forecast_attempts(batch_id=batch['id'])) == 2
    assert len(seen) == 2 and len(store.forecast_batches()) == 1
    assert store.forecast_statistics()['expected'] == 6


@pytest.mark.asyncio
async def test_all_missing_has_bounded_backoff_and_no_paid_jobs(env):
    store, account, clock = env
    prices = Prices([A, B])
    await tick(store, clock, prices)
    for _ in range(3):
        await tick(store, clock, prices)
    assert len(prices.calls) == 2 and not store.forecast_batches()[0]['job_id']
    for seconds in (61, 121, 241, 481, 601):
        clock[0] += timedelta(seconds=seconds)
        await tick(store, clock, prices)
    assert Counter(prices.calls) == {A: 5, B: 5}
    assert store.forecast_batches()[0]['preparation'] == {'terminal': 2}
    assert not store.forecast_batches()[0]['job_id']
    assert all('终止' in r['reason'] for r in store.forecasts_page()['items'])


@pytest.mark.asyncio
async def test_multiple_accounts_rotate_even_with_one_request_budget(env):
    store, account, clock = env
    other = make_account(store, ['SH:600519'], 'B')
    prices = Prices([A, B, 'SH:600519'])
    for _ in range(3):
        await tick(store, clock, prices, forecast_baseline_batch_size=1)
        clock[0] += timedelta(seconds=1)
    assert Counter(prices.calls) == {A: 1, B: 1, 'SH:600519': 1}
    assert store.forecasts_page()['total'] == 9


@pytest.mark.asyncio
async def test_cancelled_cycle_cleans_lease_and_restart_keeps_attempts(env):
    store, account, clock = env
    prices = Prices(delay=1)
    await tick(store, clock, prices, forecast_cycle_timeout_seconds=.05)
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM forecast_preparations WHERE lease_until IS NOT NULL").fetchone()[0] == 0
    store = Store(store.path)
    clock[0] += timedelta(minutes=2)
    ready = Prices()
    await tick(store, clock, ready)
    context = store.forecast_job_context(store.forecast_batches()[0]['job_id'])
    assert all(item['preparation']['attempts'] == 2 for item in context['items'])


@pytest.mark.asyncio
async def test_completion_after_cutoff_cannot_qualify_or_enqueue(env):
    store, account, clock = env
    clock[0] = clock[0].replace(hour=17, minute=59, second=59, microsecond=980000)
    await tick(store, clock, Prices(delay=.05))
    batch = store.forecast_batches()[0]
    assert not batch['job_id'] and batch['preparation'] == {'expired': 2}
    assert all(row['state'] == 'missed' and not row['forecast_id'] for row in store.forecasts_page()['items'])
    assert all(v.get('status') != 'ready' for v in batch['baseline_evidence'].values())


@pytest.mark.asyncio
async def test_account_change_during_fetch_invalidates_original_batch(env):
    store, account, clock = env
    began, release = asyncio.Event(), asyncio.Event()
    class Controlled(Prices):
        async def history(self, symbol, **options):
            began.set(); await release.wait()
            return await super().history(symbol, **options)
    task = asyncio.create_task(tick(store, clock, Controlled()))
    await began.wait()
    save_snapshot_correction(store, {'expected_version': account['version'], 'name': 'A', 'cash': '10000', 'holdings': [
        {'symbol': A, 'quantity': '20000'}]}, account['id'])
    release.set(); await task
    batch = store.forecast_batches()[0]
    assert batch['state'] == 'failed' and batch['symbols'] == [A, B]
    assert batch['input_version'] == account['version'] and not batch['job_id']
    assert batch['preparation'] == {'terminal': 2}
    assert store.forecasts_page()['total'] == 6


@pytest.mark.asyncio
async def test_failed_initial_gets_only_one_newly_ready_supplement(env, monkeypatch):
    store, account, clock = env
    prices = Prices([B])
    calls = []
    async def runner(context, cfg, tools, emit):
        calls.append(context['job_id'])
        if len(calls) == 1:
            raise ConnectionError('controlled initial model failure')
        return directions(context)
    service = service_for(store, monkeypatch, runner)
    await tick(store, clock, prices)
    original = store.forecast_batches()[0]
    await service.execute(claim(store, clock, account))
    assert store.job(original['job_id'])['state'] == 'failed'
    clock[0] += timedelta(minutes=2)
    prices.missing.clear()
    await tick(store, clock, prices)
    new = store.forecast_batches()[0]
    assert new['job_id'] != original['job_id']
    await service.execute(claim(store, clock, account))
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM forecast_research_scopes WHERE purpose='supplement'").fetchone()[0] == 1
    assert len(calls) == 2 and len(store.forecast_attempts(batch_id=original['id'])) == 2
    assert all(row['forecast_id'] for row in store.forecasts_page()['items'])


@pytest.mark.asyncio
async def test_supplement_budget_exhaustion_keeps_specific_uncovered_reason(env, monkeypatch):
    store, account, clock = env
    c = 'SZ:159280'
    store.save_universe({'watchlist': [], 'candidates': [c]})
    prices = Prices([B, c])
    async def runner(context, cfg, tools, emit): return directions(context)
    service = service_for(store, monkeypatch, runner)
    await tick(store, clock, prices)
    await service.execute(claim(store, clock, account))
    clock[0] += timedelta(minutes=2)
    prices.missing.remove(B)
    await tick(store, clock, prices)
    # C still pending: one coalescing interval must pass before another team.
    assert store.forecast_batches()[0]['research_state'] == 'completed'
    clock[0] += timedelta(seconds=61)
    await tick(store, clock, prices)
    await service.execute(claim(store, clock, account))
    clock[0] += timedelta(minutes=3)
    prices.missing.clear()
    await tick(store, clock, prices)
    batch = store.forecast_batches()[0]
    assert batch['research_state'] == 'completed' and len(batch['attempts']) == 2
    rows = [r for r in store.forecasts_page()['items'] if r['symbol'] == c]
    assert all(not r['forecast_id'] and '预算已用尽' in r['reason'] for r in rows)
    assert batch['expected_count'] == 9 and batch['preparation'] == {'ready': 3}


@pytest.mark.asyncio
async def test_concurrent_ticks_do_not_duplicate_baseline_requests(env):
    store, account, clock = env
    prices = Prices(delay=.03)
    await asyncio.gather(tick(store, clock, prices), tick(store, clock, prices))
    assert Counter(prices.calls) == {A: 1, B: 1}
    batch = store.forecast_batches()[0]
    assert len([j for j in store.jobs(account['id']) if j['reason'].startswith('forecast_batch:')]) == 1
    assert batch['preparation'] == {'ready': 2}


@pytest.mark.asyncio
async def test_published_abstain_not_replaced_and_rejected_direction_requires_new_judgment(env, monkeypatch):
    store, account, clock = env
    prices = Prices([B])
    runs = []
    async def runner(context, cfg, tools, emit):
        runs.append(context['job_id'])
        # Deliberately attempts a direction without B's exact baseline on run 1.
        # The server rejects B; it must never auto-resurrect this old direction.
        return {'report': 'protocol fixture', 'forecasts': [
            {'symbol': A, 'horizon': 1, 'direction': 'abstain', 'reason': 'visible uncertainty'},
            {'symbol': B, 'horizon': 1, 'direction': 'up' if len(runs) == 1 else 'down', 'reason': 'fresh fixture judgment'}]}
    service = service_for(store, monkeypatch, runner)
    await tick(store, clock, prices)
    await service.execute(claim(store, clock, account))
    a = next(r for r in store.forecasts_page()['items'] if r['symbol'] == A and r['horizon'] == 1)
    assert a['direction'] == 'abstain'
    b = next(r for r in store.forecasts_page()['items'] if r['symbol'] == B and r['horizon'] == 1)
    assert b['forecast_id'] is None and b['state'] == 'waiting_data'
    clock[0] += timedelta(minutes=2)
    prices.missing.clear()
    await tick(store, clock, prices)
    assert next(r for r in store.forecasts_page()['items'] if r['symbol'] == B and r['horizon'] == 1)['forecast_id'] is None
    await service.execute(claim(store, clock, account))
    final = store.forecasts_page()['items']
    assert next(r for r in final if r['symbol'] == A and r['horizon'] == 1)['forecast_id'] == a['forecast_id']
    assert next(r for r in final if r['symbol'] == B and r['horizon'] == 1)['direction'] == 'down'
    assert len(runs) == 2


@pytest.mark.asyncio
async def test_calendar_correction_invalidates_score_without_rewriting_target(env, monkeypatch, tmp_path):
    store, account, clock = env
    async def runner(context, cfg, tools, emit): return directions(context)
    await tick(store, clock, Prices())
    await service_for(store, monkeypatch, runner).execute(claim(store, clock, account))
    forecast_id = next(r['forecast_id'] for r in store.forecasts_page()['items'] if r['symbol'] == A and r['horizon'] == 1)
    clock[0] = datetime(2026, 10, 30, 16, tzinfo=SHANGHAI)
    await store.evaluate_due_forecasts({}, at=clock[0], provider=Prices())
    original = store.forecast_detail(forecast_id)
    assert original['effective_evaluation']['status'] == 'evaluated' and original['target_date'] == '2026-10-29'
    calendar = tmp_path / 'calendar.json'
    calendar.write_text(json.dumps({'coverage': {'start': '2026-10-01', 'end': '2026-12-31'},
        'source': 'explicit corrected calendar fixture', 'holidays': ['2026-10-29']}))
    store.reconcile_forecast_windows(at=clock[0], calendar_path=calendar)
    row = next(r for r in store.forecasts_page()['items'] if r['forecast_id'] == forecast_id)
    assert row['evaluation_status'] == 'waiting_data' and row['hit'] is None
    prices = Prices()
    await store.evaluate_due_forecasts({'calendar_path': calendar}, at=clock[0], provider=prices)
    corrected = store.forecast_detail(forecast_id)
    assert corrected['target_date'] == original['target_date']
    assert corrected['direction'] == original['direction'] and corrected['published_at'] == original['published_at']
    assert corrected['evaluations'][0] == original['evaluations'][0]
    latest = corrected['effective_evaluation']
    assert latest['status'] == 'unassessable' and latest['reason'] == 'calendar_target_changed'
    assert latest['evidence']['calendar_validation']['target_date'] == '2026-10-30'
    assert not prices.calls
    assert next(g for g in store.forecast_statistics()['groups'] if g['horizon'] == 1)['evaluated'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('retry_reason', ['manual_retry', 'retry:forecast_batch:api-original'])
async def test_explicit_retry_can_finish_unpublished_horizons_without_revising_first(env, monkeypatch, retry_reason):
    store, account, clock = env
    runs = []
    async def runner(context, cfg, tools, emit):
        runs.append(context['job_id'])
        result = directions(context)
        if len(runs) == 1:
            result['forecasts'] = result['forecasts'][:1]
        return result
    service = service_for(store, monkeypatch, runner)
    await tick(store, clock, Prices())
    batch = store.forecast_batches()[0]
    await service.execute(claim(store, clock, account))
    first = next(r for r in store.forecasts_page()['items'] if r['forecast_id'])
    original = store.forecast_detail(first['forecast_id'])
    clock[0] += timedelta(minutes=2)
    retry = store.enqueue(account['id'], retry_reason)
    assert store.forecast_job_context(retry['id'])['id'] == batch['id']
    await service.execute(claim(store, clock, account))
    assert all(r['forecast_id'] for r in store.forecasts_page()['items'])
    assert store.forecast_detail(first['forecast_id']) == original
    assert store.forecast_statistics()['expected'] == 6 and len(runs) == 2


@pytest.mark.asyncio
async def test_after_deadline_no_supplement_and_existing_predictions_unchanged(env, monkeypatch):
    store, account, clock = env
    async def runner(context, cfg, tools, emit): return directions(context)
    prices = Prices([B])
    await tick(store, clock, prices)
    await service_for(store, monkeypatch, runner).execute(claim(store, clock, account))
    batch = store.forecast_batches()[0]
    before = [store.forecast_detail(r['forecast_id']) for r in store.forecasts_page()['items'] if r['forecast_id']]
    clock[0] = clock[0].replace(hour=18, minute=0)
    prices.missing.clear()
    await tick(store, clock, prices)
    assert Counter(prices.calls) == {A: 1, B: 1}
    assert store.forecast_batches()[0]['job_id'] == batch['job_id']
    assert [store.forecast_detail(r['id']) for r in before] == before
    assert all(r['state'] == 'missed' and not r['forecast_id'] for r in store.forecasts_page()['items'] if r['symbol'] == B)


@pytest.mark.asyncio
async def test_exploratory_eligibility_does_not_require_formal_baseline(env, monkeypatch):
    store, account, clock = env
    seen = []
    async def runner(context, cfg, tools, emit):
        page = await tools.list_forecast_targets()
        seen.append(page)
        assert page['kind'] == 'exploratory' and not page['baseline_required_for_direction']
        assert all(item['direction_eligible'] and not item['baseline_ready'] for item in page['items'])
        assert all(item['baseline'] is None and not item['baseline_required_for_direction'] for item in page['items'])
        context['forecast_batch'] = page
        return directions(context)
    await service_for(store, monkeypatch, runner).execute(claim(store, clock, account))
    assert seen and store.job(account['latest_job_id'])['state'] == 'completed'
    assert all(r['direction'] == 'up' for r in store.forecasts_page(kind='exploratory')['items'])
    assert store.forecast_statistics(kind='formal')['expected'] == 0


@pytest.mark.asyncio
async def test_formal_eligibility_matches_exact_publication_baseline_check(env):
    store, account, clock = env
    await tick(store, clock, Prices([B]))
    batch = store.forecast_batches()[0]
    with store.connect() as db:
        evidence = json.loads(db.execute('SELECT baseline_evidence FROM forecast_batches WHERE id=?', (batch['id'],)).fetchone()[0])
        evidence[A]['source'] = None
        db.execute('UPDATE forecast_batches SET baseline_evidence=? WHERE id=?', (json.dumps(evidence), batch['id']))
    page = store.forecast_job_context(batch['job_id'])
    assert page['baseline_required_for_direction']
    assert all(not item['direction_eligible'] for item in page['items'])
    assert all(item['baseline_ready'] and not item['baseline_valid_for_direction'] for item in page['items'] if item['symbol'] == A)
