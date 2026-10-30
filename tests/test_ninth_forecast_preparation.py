"""Real scheduler/SQLite/capacity queues; providers are bounded offline fixtures."""
from ledger_helpers import save_snapshot_correction
import asyncio
from collections import Counter
from datetime import timedelta
import sqlite3

import pytest

from app.forecast_prices import ClosingPriceSession
from app.store import Store
from test_eighth_forecast_preparation import env, A, B, Prices, tick, claim, make_account


def preparations(store):
    with store.connect() as db:
        return [dict(r) for r in db.execute('SELECT * FROM forecast_preparations ORDER BY symbol')]


@pytest.mark.asyncio
@pytest.mark.parametrize('concurrency', [1, 8])
async def test_eight_targets_one_second_cycles_count_actual_starts_not_waiters(env, concurrency):
    store, account, clock = env
    symbols = [f'SZ:{159270 + n}' for n in range(8)]
    save_snapshot_correction(store, {'expected_version': account['version'], 'name': 'A', 'cash': '10000', 'holdings': [
        {'symbol': s, 'quantity': '1'} for s in symbols]}, account['id'])
    prices = Prices(delay=.65)
    metrics = []
    for n in range(5):
        result = await tick(store, clock, prices, forecast_evaluation_concurrency=concurrency,
            forecast_cycle_timeout_seconds=1, forecast_request_timeout_seconds=2,
            forecast_baseline_batch_size=40)
        metrics.append(result['price_requests'])
        rows = preparations(store)
        assert sum(r['attempts'] for r in rows) == len(prices.calls)
        assert not any(r['state'] == 'terminal' for r in rows)
        assert all(not r['lease_token'] and not r['lease_until'] for r in rows)
        clock[0] += timedelta(seconds=601)
    if concurrency == 1:
        assert any(m['capacity_wait_cancelled'] for m in metrics)
        assert sum(r['state'] == 'ready' for r in rows) == 5
        assert all(r['attempts'] < 5 for r in rows if r['state'] != 'ready')
    else:
        assert len(prices.calls) == 8 and all(r['attempts'] == 1 and r['state'] == 'ready' for r in rows)
    store = Store(store.path)  # queued progress survives restart
    for n in range(3):
        await tick(store, clock, prices, forecast_evaluation_concurrency=concurrency,
            forecast_cycle_timeout_seconds=1, forecast_request_timeout_seconds=2)
        clock[0] += timedelta(seconds=601)
    assert all(r['state'] == 'ready' for r in preparations(store))
    assert sum(r['attempts'] for r in preparations(store)) == len(prices.calls)


@pytest.mark.asyncio
async def test_real_request_timeouts_exhaust_exactly_five_attempts(env):
    store, account, clock = env
    prices = Prices(delay=.2)
    for _ in range(6):
        await tick(store, clock, prices, forecast_evaluation_concurrency=1,
            forecast_cycle_timeout_seconds=1, forecast_request_timeout_seconds=.05)
        clock[0] += timedelta(seconds=601)
    assert Counter(prices.calls) == {A: 5, B: 5}
    assert all(r['state'] == 'terminal' and r['attempts'] == 5 and r['reason'] == 'baseline_retry_budget_exhausted' for r in preparations(store))


@pytest.mark.asyncio
async def test_late_result_cannot_write_or_clear_replacement_lease(env):
    store, account, clock = env
    entered, release = asyncio.Event(), asyncio.Event()
    class Controlled(Prices):
        async def history(self, symbol, **options):
            if symbol == A:
                entered.set()
                await release.wait()
            return await super().history(symbol, **options)
    task = asyncio.create_task(tick(store, clock, Controlled()))
    await entered.wait()
    with store.connect() as db:
        db.execute("UPDATE forecast_preparations SET lease_token='replacement',lease_until=?,reason='replacement-owned' WHERE symbol=?",
            ((clock[0] + timedelta(minutes=2)).isoformat(), A))
    release.set()
    await task
    row = next(r for r in preparations(store) if r['symbol'] == A)
    assert row['lease_token'] == 'replacement' and row['reason'] == 'replacement-owned'
    assert row['state'] == 'preparing' and row['ready_at'] is None
    assert A not in store.forecast_batches()[0]['baseline_evidence']


@pytest.mark.asyncio
async def test_shared_fetch_survives_one_consumer_cancel_and_counts_supplier_once(env):
    store, account, clock = env
    entered, release = asyncio.Event(), asyncio.Event()
    class Controlled(Prices):
        async def history(self, symbol, **options):
            entered.set()
            await release.wait()
            return await super().history(symbol, **options)
    prices = Controlled()
    session = ClosingPriceSession(store, prices, concurrency=1)
    starts = []
    first = asyncio.create_task(session.pair(A, '2026-10-28', '2026-10-28', on_start=lambda: starts.append('first')))
    second = asyncio.create_task(session.pair(A, '2026-10-28', '2026-10-28', on_start=lambda: starts.append('second')))
    await entered.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError): await first
    release.set()
    result = await second
    assert result['status'] == 'ready' and result['shared_evidence_id']
    assert starts == ['first', 'second'] and prices.calls == [A]
    assert session.metrics['requests_started'] == session.metrics['requests_completed'] == 1
    await session.close()


@pytest.mark.asyncio
async def test_scoring_capacity_wait_does_not_consume_new_baseline_budget(env):
    store, account, clock = env
    await tick(store, clock, Prices())
    current = claim(store, clock, account)
    assert store.publish(current['id'], {'report': 'controlled due forecast', 'forecasts': [
        {'symbol': A, 'horizon': 1, 'direction': 'up', 'reason': 'fixture'}]}, clock[0].isoformat())
    clock[0] += timedelta(days=1)
    prices = Prices(delay=.65)
    result = await store.forecast_tick({'forecast_evaluation_enabled': True, 'forecast_evaluation_concurrency': 1,
        'forecast_cycle_timeout_seconds': 1, 'forecast_request_timeout_seconds': 2}, at=clock[0], provider=prices)
    rows = [r for r in preparations(store) if r['batch_id'] == next(b['id'] for b in store.forecast_batches() if b['base_date'] == '2026-10-29')]
    assert all(r['attempts'] == 0 and r['state'] == 'unattempted' for r in rows)
    assert result['price_requests']['requests_started'] == 1
    with store.connect() as db:
        evaluation = db.execute('SELECT attempts,last_error FROM forecast_evaluation_tasks').fetchone()
        assert evaluation['attempts'] == 1 and evaluation['last_error'] == 'request_started_result_unknown'


def test_failed_v9_migration_rolls_back_columns_tables_marker_and_version(env, monkeypatch):
    store, account, clock = env
    with store.connect() as db:
        db.execute('DROP TABLE forecast_sample_attempts')
        db.execute('DROP TABLE forecast_research_scope_samples')
        db.execute('ALTER TABLE forecast_preparations DROP COLUMN lease_token')
        db.execute('ALTER TABLE forecast_preparations DROP COLUMN request_started_at')
        db.execute("DELETE FROM meta WHERE key='forecast_sample_identity_v9'")
        db.execute("UPDATE meta SET value='8' WHERE key='schema_version'")
    original = Store.migrate_forecast_identity
    def fail(self, db):
        original(self, db)
        raise RuntimeError('controlled migration failure')
    with monkeypatch.context() as patch:
        patch.setattr(Store, 'migrate_forecast_identity', fail)
        with pytest.raises(RuntimeError, match='controlled migration'):
            Store(store.path)
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == '8'
        assert not db.execute("SELECT 1 FROM meta WHERE key='forecast_sample_identity_v9'").fetchone()
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='forecast_sample_attempts'").fetchone()
        assert not {'lease_token', 'request_started_at'} & {r[1] for r in db.execute('PRAGMA table_info(forecast_preparations)')}
    assert Store(store.path).meta('schema_version') == 11


@pytest.mark.asyncio
async def test_scoring_late_response_cannot_replace_new_lease_or_record_score(env):
    store, account, clock = env
    await tick(store, clock, Prices())
    current = claim(store, clock, account)
    assert store.publish(current['id'], {'report': 'controlled forecast', 'forecasts': [
        {'symbol': A, 'horizon': 1, 'direction': 'up', 'reason': 'fixture'}]}, clock[0].isoformat())
    forecast_id = next(r['forecast_id'] for r in store.forecasts_page()['items'] if r['forecast_id'])
    clock[0] += timedelta(days=1)
    entered, release = asyncio.Event(), asyncio.Event()
    class Controlled(Prices):
        async def history(self, symbol, **options):
            entered.set()
            await release.wait()
            return await super().history(symbol, **options)
    task = asyncio.create_task(store.evaluate_due_forecasts({}, at=clock[0], provider=Controlled()))
    await entered.wait()
    with store.connect() as db:
        db.execute("UPDATE forecast_evaluation_leases SET token='replacement' WHERE forecast_id=?", (forecast_id,))
    release.set()
    assert await task == []
    detail = store.forecast_detail(forecast_id)
    assert detail['evaluations'] == [] and detail['price_observations'] == []
    with store.connect() as db:
        assert db.execute('SELECT token FROM forecast_evaluation_leases WHERE forecast_id=?', (forecast_id,)).fetchone()[0] == 'replacement'


@pytest.mark.asyncio
async def test_crashed_fifth_started_request_does_not_gain_sixth_after_restart(env):
    store, account, clock = env
    batch = store.create_forecast_batch(account['id'], at=clock[0])
    with store.connect() as db:
        db.execute("""UPDATE forecast_preparations SET state='preparing',attempts=5,lease_token='crashed',
            lease_until=?,request_started_at=?,next_attempt_at=NULL WHERE batch_id=? AND symbol=?""",
            ((clock[0] - timedelta(seconds=1)).isoformat(), (clock[0] - timedelta(minutes=2)).isoformat(), batch['id'], A))
    store = Store(store.path)
    prices = Prices()
    await tick(store, clock, prices)
    assert prices.calls == [B]
    target = next(r for r in preparations(store) if r['symbol'] == A)
    assert target['attempts'] == 5 and target['state'] == 'terminal' and not target['lease_token']
    assert all('终止' in r['reason'] for r in store.forecasts_page(symbol=A)['items'])


@pytest.mark.asyncio
async def test_shared_request_resubscribe_after_sole_consumer_cancel_starts_new_generation(env):
    store, account, clock = env
    entered, cancelled, release_old = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Controlled(Prices):
        def __init__(self):
            super().__init__()
            self.starts = 0
        async def history(self, symbol, **options):
            self.starts += 1
            if self.starts == 1:
                entered.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    cancelled.set()
                    await release_old.wait()  # old cancellation deliberately stays pending
                    raise
            return await super().history(symbol, **options)
    prices, starts = Controlled(), []
    session = ClosingPriceSession(store, prices, concurrency=1)
    first = asyncio.create_task(session.pair(A, '2026-10-28', '2026-10-28', on_start=lambda: starts.append('first')))
    await entered.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await cancelled.wait()
    second = asyncio.create_task(session.pair(A, '2026-10-28', '2026-10-28', on_start=lambda: starts.append('second')))
    await asyncio.sleep(0)
    # Waiting on the old task's capacity release must not count as a new start.
    assert starts == ['first'] and prices.starts == 1 and not second.done()
    release_old.set()
    result = await second
    assert result['status'] == 'ready'
    assert starts == ['first', 'second'] and prices.starts == 2
    assert session.metrics['requests_started'] == 2 and session.metrics['requests_completed'] == 1
    await session.close()
