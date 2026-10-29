"""Production Store/queue regressions with a controlled clock and price adapter."""
import asyncio
from datetime import datetime, timedelta
import json

import pytest

from app.calendar import SHANGHAI
from app.store import Store
from app.forecast_prices import ClosingPriceSession


@pytest.fixture
def clock(monkeypatch):
    stamp = [datetime(2026, 10, 28, 10, tzinfo=SHANGHAI)]
    for module in ("app.store", "app.forecasts", "app.forecast_windows"):
        monkeypatch.setattr(module + ".now", lambda: stamp[0].isoformat())
    return stamp


def account(store, name="A"):
    return store.save_account({"name": name, "cash": "10000", "holdings": [{"symbol": "SZ:159278", "quantity": "10000", "cost": "1"}], "preferences": {}})


class Prices:
    def __init__(self, delay=0):
        self.calls, self.delay = [], delay

    async def history(self, symbol, **opts):
        self.calls.append((symbol, opts))
        if self.delay:
            await asyncio.sleep(self.delay)
        rows = [{"time": opts["start"], "close": "100", "volume": "100"}]
        if opts["start"] != opts["end"]:
            rows.append({"time": opts["end"], "close": "101", "volume": "100"})
        return {"symbol": symbol, "status": "ok", "rows": rows, "source": "controlled historical fixture", "adjustment": opts["adjust"]}


@pytest.mark.asyncio
async def test_entire_window_offline_is_visible_idempotent_without_forecasts(tmp_path, clock):
    store = Store(tmp_path / "window.sqlite3")
    a = account(store)
    store.save_forecast_settings(a["id"], {"enabled": True})
    await store.forecast_tick({}, at=clock[0], provider=Prices())
    clock[0] = clock[0].replace(hour=19)
    await store.forecast_tick({}, at=clock[0], provider=Prices())
    row = store.forecast_window_runs(a["id"])["items"][0]
    assert row["status"] == "missed" and row["scope_status"] == "known_snapshot" and row["expected_count"] == 3
    assert row["discovered_at"].startswith("2026-10-28T10:")
    assert row["updated_at"].startswith("2026-10-28T19:")
    assert row["detail"]["model_attribution"] == "unknown"
    assert store.forecasts_page()["total"] == 0 and store.forecast_batches() == []
    store = Store(store.path)
    await store.forecast_tick({}, at=clock[0], provider=Prices())
    assert store.forecast_window_runs(a["id"])["total"] == 1
    clock[0] = datetime(2026, 10, 29, 16, tzinfo=SHANGHAI)
    result = await store.forecast_tick({}, at=clock[0], provider=Prices())
    assert len(result["batches"]) == 1
    assert result["batches"][0]["base_date"] == "2026-10-29"
    assert store.forecasts_page()["total"] == 3
    assert store.forecast_window_statistics(a["id"])["states"]["missed"] == 1


def test_known_enabled_unknown_scope_does_not_invent_denominator(tmp_path, clock):
    store = Store(tmp_path / "unknown-scope.sqlite3")
    a = account(store)
    store.save_forecast_settings(a["id"], {"enabled": True})
    with store.connect() as db:
        row = db.execute("SELECT id,body FROM forecast_window_configurations WHERE account_id=? ORDER BY id DESC LIMIT 1", (a["id"],)).fetchone()
        body = json.loads(row["body"]); body["scope_status"] = "unknown"; body["symbols"] = None
        db.execute("UPDATE forecast_window_configurations SET body=? WHERE id=?", (json.dumps(body), row["id"]))
    clock[0] = clock[0].replace(hour=19)
    store.reconcile_forecast_windows(at=clock[0])
    row = store.forecast_window_runs(a["id"])["items"][0]
    assert row["status"] == "missed" and row["expected_count"] is None
    assert row["scope_status"] == "unknown" and store.forecasts_page()["total"] == 0


def test_pause_not_created_weekend_and_calendar_gap_are_distinct(tmp_path, clock):
    store = Store(tmp_path / "states.sqlite3")
    a = account(store)
    store.save_forecast_settings(a["id"], {"enabled": True})
    store.set_meta("settings", {"scheduled_research_enabled": False})
    clock[0] = clock[0].replace(hour=19)
    store.reconcile_forecast_windows(at=clock[0], scheduled_enabled=False)
    assert store.forecast_window_runs(a["id"])["items"][0]["status"] == "disabled"
    clock[0] = datetime(2026, 10, 29, 19, tzinfo=SHANGHAI)
    b = account(store, "B")
    store.reconcile_forecast_windows(at=clock[0], scheduled_enabled=False)
    assert next(r for r in store.forecast_window_runs(b["id"])["items"] if r["planned_date"] == "2026-10-28")["status"] == "not_created"
    clock[0] = datetime(2026, 10, 31, 19, tzinfo=SHANGHAI)
    store.reconcile_forecast_windows(at=clock[0], scheduled_enabled=False)
    assert store.forecast_window_runs(a["id"])["items"][0]["status"] == "non_trading_day"
    clock[0] = datetime(2027, 1, 4, 19, tzinfo=SHANGHAI)
    store.reconcile_forecast_windows(at=clock[0], scheduled_enabled=False)
    assert store.forecast_window_runs(a["id"])["items"][0]["status"] == "calendar_unknown"
    assert store.forecast_window_statistics(a["id"])["known_expected_windows"] == 0


def test_enable_and_rule_changes_during_window_do_not_erase_obligation(tmp_path, clock):
    store = Store(tmp_path / "changes.sqlite3")
    a = account(store)
    store.save_forecast_settings(a["id"], {"enabled": True})
    clock[0] = clock[0].replace(hour=16)
    store.reconcile_forecast_windows(at=clock[0])
    assert store.forecast_window_runs(a["id"])["items"][0]["status"] == "pending"
    store.save_forecast_settings(a["id"], {"enabled": False})
    clock[0] = clock[0].replace(hour=19)
    store.reconcile_forecast_windows(at=clock[0])
    assert store.forecast_window_runs(a["id"])["items"][0]["status"] == "missed"
    # Tomorrow's later window applies from its actual settings change.
    clock[0] = datetime(2026, 10, 29, 10, tzinfo=SHANGHAI)
    store.save_forecast_settings(a["id"], {"enabled": True, "start_time": "17:00", "deadline_time": "19:00"})
    store.reconcile_forecast_windows(at=clock[0].replace(hour=16))
    assert store.forecast_window_runs(a["id"])["items"][0]["status"] == "scheduled"
    store.reconcile_forecast_windows(at=clock[0].replace(hour=20))
    assert store.forecast_window_runs(a["id"])["items"][0]["status"] == "missed"


def test_upgrade_observation_does_not_backdate_enable_state(tmp_path, clock):
    store = Store(tmp_path / "migration.sqlite3")
    a = account(store)
    store.save_forecast_settings(a["id"], {"enabled": True})
    with store.connect() as db:
        db.execute("DELETE FROM forecast_window_configurations")
        db.execute("DELETE FROM meta WHERE key='forecast_windows_history_started_at'")
    clock[0] = clock[0].replace(hour=19)
    store = Store(store.path)
    store.reconcile_forecast_windows(at=clock[0])
    row = store.forecast_window_runs(a["id"])["items"][0]
    assert row["status"] == "history_unknown" and row["expected_count"] is None
    assert store.forecasts_page()["total"] == 0


def publish(store, a, at):
    store.save_forecast_settings(a["id"], {"horizons": [1], "labels": ["one"]})
    batch = store.create_forecast_batch(a["id"], kind="test", at=at)
    store.bind_forecast_job(batch["id"], a["latest_job_id"])
    job = store.job(a["latest_job_id"])
    with store.connect() as db:
        return store.publish_forecasts(db, job, {"forecasts": [{"symbol": "SZ:159278", "horizon": 1, "direction": "up", "reason": "controlled fixture"}]}, "fixture-report", at)[0]


@pytest.mark.asyncio
async def test_shared_exact_price_evidence_across_accounts_and_fresh_corrections(tmp_path, clock):
    store = Store(tmp_path / "shared.sqlite3")
    forecasts = [publish(store, account(store, str(i)), clock[0].replace(hour=16)) for i in range(4)]
    prices = Prices(delay=0.001)
    due = datetime(2026, 10, 29, 16, tzinfo=SHANGHAI)
    assert set(await store.evaluate_due_forecasts({}, at=due, provider=prices)) == set(forecasts)
    assert len(prices.calls) == 2  # shared raw + same-snapshot adjusted pair
    evidence_ids = {store.forecast_detail(f)["evaluations"][0]["evidence"]["shared_evidence_id"] for f in forecasts}
    assert len(evidence_ids) == 1
    for f in forecasts:
        assert store.forecast_detail(f)["evaluation_task"]["next_attempt"].startswith("2026-11-05")
        store.request_forecast_recheck(f, at=due)
    await store.evaluate_due_forecasts({}, at=due, provider=prices)
    assert len(prices.calls) == 4  # a new cycle really checks the supplier again
    assert all(len(store.forecast_detail(f)["evaluations"]) == 1 for f in forecasts)


@pytest.mark.asyncio
async def test_first_scoring_reserved_ahead_of_large_review_queue(tmp_path, clock):
    store = Store(tmp_path / "priority.sqlite3")
    ids = [publish(store, account(store, str(i)), clock[0].replace(hour=16)) for i in range(12)]
    due = datetime(2026, 10, 29, 16, tzinfo=SHANGHAI)
    await store.evaluate_due_forecasts({}, at=due, provider=Prices(), limit=10)
    with store.connect() as db:
        reviewed = {r[0] for r in db.execute("SELECT forecast_id FROM forecast_evaluations")}
        first = set(ids) - reviewed
        db.execute("UPDATE forecast_evaluation_tasks SET next_attempt='2026-10-01T00:00:00+08:00' WHERE state='evaluated'")
    result = await store.evaluate_due_forecasts({"forecast_evaluation_batch_size": 4, "forecast_review_batch_size": 2}, at=due, provider=Prices())
    assert first <= set(result) and len(set(result) & reviewed) == 2


@pytest.mark.asyncio
async def test_slow_history_cannot_delay_new_batch_or_ready_job(tmp_path, clock):
    store = Store(tmp_path / "nonblocking.sqlite3")
    a, b = account(store), account(store, "B")
    publish(store, a, clock[0].replace(hour=16))
    store.save_forecast_settings(b["id"], {"enabled": True})
    clock[0] = datetime(2026, 10, 29, 16, tzinfo=SHANGHAI)
    blocked, release = asyncio.Event(), asyncio.Event()
    class SlowReviews(Prices):
        async def history(self, symbol, **opts):
            if opts["start"] != opts["end"]:
                blocked.set(); await release.wait()
            return await super().history(symbol, **opts)
    task = asyncio.create_task(store.forecast_tick({}, at=clock[0], provider=SlowReviews()))
    await blocked.wait()
    for _ in range(50):
        batches = store.forecast_batches(b["id"])
        if batches and batches[0]["job_id"]:
            break
        await asyncio.sleep(0.005)
    assert len(batches) == 1 and batches[0]["job_id"]
    assert not task.done()  # model task is durable while history remains blocked
    release.set(); await task


@pytest.mark.asyncio
async def test_deadline_checked_after_price_request_and_cancellation_releases_leases(tmp_path, clock):
    store = Store(tmp_path / "deadline.sqlite3")
    a = account(store)
    store.save_forecast_settings(a["id"], {"enabled": True})
    store.create_forecast_batch(a["id"], at=clock[0].replace(hour=17, minute=50))
    at = clock[0].replace(hour=17, minute=59, second=59, microsecond=980000)
    result = await store.forecast_tick({}, at=at, provider=Prices(delay=0.04))
    assert result["batches"] == []
    assert {r["state"] for r in store.forecasts_page()["items"]} == {"missed"}
    assert len(store.jobs(a["id"])) == 1
    b = account(store, "B")
    f = publish(store, b, clock[0].replace(hour=16))
    result = await store.evaluate_due_forecasts({"forecast_cycle_timeout_seconds": 0.05}, at=datetime(2026, 10, 29, 16, tzinfo=SHANGHAI), provider=Prices(delay=1))
    assert result == [] and store.forecast_detail(f)["evaluations"] == []
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM forecast_evaluation_leases").fetchone()[0] == 0


def test_exact_enable_in_window_and_scope_changes_are_auditable(tmp_path, clock):
    store = Store(tmp_path / "scope-change.sqlite3")
    a = account(store)
    clock[0] = clock[0].replace(hour=16)
    store.save_forecast_settings(a["id"], {"enabled": True})
    store.reconcile_forecast_windows(at=clock[0])
    assert store.forecast_window_runs(a["id"])["items"][0]["status"] == "pending"
    clock[0] = clock[0].replace(minute=30)
    store.save_universe({"watchlist": [], "candidates": ["SH:600519"]})
    clock[0] = clock[0].replace(hour=19)
    store.reconcile_forecast_windows(at=clock[0])
    row = store.forecast_window_runs(a["id"])["items"][0]
    assert row["status"] == "missed" and row["scope_status"] == "changed_during_window"
    assert row["expected_count"] is None
    assert len(row["detail"]["configuration_ids"]) == 2
    assert row["detail"]["gap_discovered_at"].startswith("2026-10-28T19:")


@pytest.mark.asyncio
async def test_manual_recheck_operates_when_automatic_review_budget_zero(tmp_path, clock):
    store = Store(tmp_path / "manual-review.sqlite3")
    f = publish(store, account(store), clock[0].replace(hour=16))
    due = datetime(2026, 10, 29, 16, tzinfo=SHANGHAI)
    await store.evaluate_due_forecasts({}, at=due, provider=Prices())
    store.request_forecast_recheck(f, at=due)
    assert await store.evaluate_due_forecasts({"forecast_review_batch_size": 0}, at=due, provider=Prices()) == [f]
    assert len(store.forecast_detail(f)["evaluations"]) == 1


@pytest.mark.asyncio
async def test_one_failed_record_does_not_block_other_accounts(tmp_path, clock):
    store = Store(tmp_path / "fairness.sqlite3")
    a, b = account(store), account(store, "B")
    ids = [publish(store, a, clock[0].replace(hour=16)), publish(store, b, clock[0].replace(hour=16))]
    due = datetime(2026, 10, 29, 16, tzinfo=SHANGHAI)
    # A failed request is isolated and leaves B able to finish in the same cycle.
    original = store.record_forecast_evaluation
    def fail_a(forecast_id, *args, **kwargs):
        if forecast_id == ids[0]:
            raise ValueError("controlled per-record failure")
        return original(forecast_id, *args, **kwargs)
    store.record_forecast_evaluation = fail_a
    assert await store.evaluate_due_forecasts({}, at=due, provider=Prices()) == [ids[1]]
    assert store.forecast_detail(ids[0])["evaluation_task"]["last_error"] == "ValueError"


@pytest.mark.asyncio
async def test_production_scheduler_records_disabled_day_with_both_switches_off(tmp_path, clock, monkeypatch):
    from app.service import Service
    store = Store(tmp_path / "paused-production.sqlite3")
    a = account(store)
    store.save_forecast_settings(a["id"], {"enabled": True})
    store.set_meta("settings", {"scheduled_research_enabled": False, "forecast_evaluation_enabled": False, "market_refresh_enabled": False})
    async def forbidden(*args, **kwargs):
        raise AssertionError("paused maintenance must not contact a supplier")
    monkeypatch.setattr(store, "forecast_tick", forbidden)
    service = Service(store)
    clock[0] = clock[0].replace(hour=19)
    await service.scheduler_tick(clock[0])
    assert store.forecast_window_runs(a["id"])["items"][0]["status"] == "disabled"
    assert store.forecast_batches(a["id"]) == []
    assert len(store.jobs(a["id"])) == 1


def test_window_statistics_respect_same_date_filters_as_page(tmp_path, clock):
    store = Store(tmp_path / "window-filter.sqlite3")
    a = account(store)
    store.save_forecast_settings(a["id"], {"enabled": True})
    store.reconcile_forecast_windows(at=clock[0].replace(hour=19))
    store.reconcile_forecast_windows(at=datetime(2026, 10, 31, 19, tzinfo=SHANGHAI))
    stats = store.forecast_window_statistics(a["id"], start_date="2026-10-31", end_date="2026-10-31")
    page = store.forecast_window_runs(a["id"], start_date="2026-10-31", end_date="2026-10-31")
    assert stats["states"] == {"non_trading_day": 1}
    assert sum(stats["states"].values()) == page["total"] == 1
    assert stats["known_expected_windows"] == 0


@pytest.mark.asyncio
async def test_repeated_failure_backoff_does_not_starve_next_account(tmp_path, clock):
    store = Store(tmp_path / "repeat-failure.sqlite3")
    a, b = account(store), account(store, "B")
    fa, fb = publish(store, a, clock[0].replace(hour=16)), publish(store, b, clock[0].replace(hour=16))
    with store.connect() as db:
        db.execute("UPDATE forecast_evaluation_tasks SET next_attempt='2026-10-01T00:00:00+08:00' WHERE forecast_id=?", (fa,))
    original = store.record_forecast_evaluation
    attempts = []
    def fail_a(forecast_id, *args, **kwargs):
        attempts.append(forecast_id)
        if forecast_id == fa:
            raise ValueError("controlled repeated failure")
        return original(forecast_id, *args, **kwargs)
    store.record_forecast_evaluation = fail_a
    config = {"forecast_evaluation_batch_size": 1, "forecast_review_batch_size": 0}
    due = datetime(2026, 10, 29, 16, tzinfo=SHANGHAI)
    assert await store.evaluate_due_forecasts(config, at=due, provider=Prices()) == []
    assert await store.evaluate_due_forecasts(config, at=due + timedelta(seconds=1), provider=Prices()) == [fb]
    assert attempts == [fa, fb]
    assert store.forecast_detail(fa)["evaluation_task"]["next_attempt"].startswith("2026-10-29T16:15:")


@pytest.mark.asyncio
async def test_late_baseline_does_not_rewrite_published_batch(tmp_path, clock):
    store = Store(tmp_path / "late-baseline.sqlite3")
    a = account(store)
    store.save_universe({"watchlist": [], "candidates": ["SH:600519"]})
    store.save_forecast_settings(a["id"], {"enabled": True})
    clock[0] = clock[0].replace(hour=16)
    started, release = asyncio.Event(), asyncio.Event()
    class DelayedSymbol(Prices):
        async def history(self, symbol, **opts):
            if symbol == "SZ:159278":
                started.set(); await release.wait()
            return await super().history(symbol, **opts)
    task = asyncio.create_task(store.forecast_tick({}, at=clock[0], provider=DelayedSymbol()))
    await started.wait()
    for _ in range(50):
        batch = store.forecast_batches(a["id"])[0]
        if batch["job_id"]:
            break
        await asyncio.sleep(0.005)
    assert batch["job_id"] and "SH:600519" in batch["baseline_evidence"]
    job = store.job(batch["job_id"])
    with store.connect() as db:
        store.publish_forecasts(db, job, {"forecasts": [{"symbol": "SH:600519", "horizon": 1, "direction": "up", "reason": "controlled"}]}, "fixture-report", clock[0])
    before = store.forecast_batches(a["id"])[0]["baseline_evidence"]
    release.set(); await task
    after = store.forecast_batches(a["id"])[0]["baseline_evidence"]
    assert after == before and "SZ:159278" not in after
