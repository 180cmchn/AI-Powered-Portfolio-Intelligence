"""Production scheduler tests with explicit historical fixture prices and clocks."""
import asyncio
from datetime import datetime
import time

import pytest

from app.calendar import SHANGHAI
from app.service import Service
from app.store import Store


class HistoricalPrices:
    def __init__(self):
        self.calls = []

    async def history(self, symbol, **options):
        self.calls.append((symbol, options))
        rows = [{"time": options["start"], "close": "100", "volume": "100"}]
        if options["end"] != options["start"]:
            rows.append({"time": options["end"], "close": "101", "volume": "100"})
        return {"status": "ok", "symbol": symbol, "source": "controlled fixture", "adjustment": options["adjust"], "rows": rows}


@pytest.fixture
def configured(tmp_path, monkeypatch):
    store = Store(tmp_path / "scheduler-fixture.sqlite3")
    store.set_meta("settings", {"scheduled_research_enabled": False, "market_refresh_enabled": True,
        "forecast_evaluation_enabled": True, "debounce_seconds": 0, "model_name": "", "model_key_env": "MISSING_FIXTURE_KEY"})
    account = store.save_account({"name": "Fixture account", "holdings": [{"symbol": "SZ:159278", "quantity": "100", "cost": "100"}], "preferences": {}})
    prices = HistoricalPrices()
    monkeypatch.setattr("app.providers.MarketProvider", lambda config: prices)
    service = Service(store)
    return store, account, prices, service


async def drain(service):
    pending = list(service.maintenance)
    if pending:
        await asyncio.gather(*pending)


def fixed_forecast(store, account, at, horizons=(1,)):
    store.save_forecast_settings(account["id"], {"horizons": list(horizons), "labels": [f"{n}日" for n in horizons]})
    batch = store.create_forecast_batch(account["id"], at=at, kind="test")
    store.bind_forecast_job(batch["id"], account["latest_job_id"])
    job = store.job(account["latest_job_id"])
    with store.connect() as db:
        ids = store.publish_forecasts(db, job, {"report": "controlled fixture", "forecasts": [{
            "security_id": "SZ:159278", "horizon": n, "direction": "up", "reason": "fixture"} for n in horizons]}, "fixture-report", at)
    return ids


@pytest.mark.asyncio
async def test_pausing_models_keeps_quote_and_evaluation_scheduler(configured):
    store, account, prices, service = configured
    calls = []
    async def refresh(*args, **kwargs):
        calls.append("quote")
    service.refresh_quotes = refresh
    evaluated = []
    async def tick(config, *, at, create_batches):
        evaluated.append({"at": at, "create_batches": create_batches, "model_name": config["model_name"]})
        return {"evaluated": [], "batches": []}
    store.forecast_tick = tick
    at = datetime(2026, 10, 29, 10, tzinfo=SHANGHAI)
    await service.scheduler_tick(at)
    await drain(service)
    assert calls == ["quote"]
    assert evaluated == [{"at": at, "create_batches": False, "model_name": ""}]
    assert len(store.jobs(account["id"])) == 1  # no paid scheduler research
    # Same timestamp does not duplicate either maintenance unit.
    await service.scheduler_tick(at)
    await drain(service)
    assert calls == ["quote"] and len(evaluated) == 1


@pytest.mark.asyncio
async def test_lunch_does_not_score_todays_close_but_after_close_does(configured):
    store, account, prices, service = configured
    forecast_id = fixed_forecast(store, account, datetime(2026, 10, 28, 16, tzinfo=SHANGHAI))[0]
    await service.scheduler_tick(datetime(2026, 10, 29, 12, tzinfo=SHANGHAI))
    await drain(service)
    assert prices.calls == []
    assert store.forecast_detail(forecast_id)["evaluations"] == []
    await service.scheduler_tick(datetime(2026, 10, 29, 16, tzinfo=SHANGHAI))
    await drain(service)
    detail = store.forecast_detail(forecast_id)
    assert detail["evaluations"][0]["hit"] == 1
    assert {options["end"] for _, options in prices.calls} == {"2026-10-29"}


@pytest.mark.asyncio
async def test_weekend_wakeup_scores_old_exact_session_without_regeneration(configured):
    store, account, prices, service = configured
    forecast_id = fixed_forecast(store, account, datetime(2026, 10, 29, 16, tzinfo=SHANGHAI))[0]
    # Device sleeps through Friday and wakes on Sunday. No current quote or LLM.
    await service.scheduler_tick(datetime(2026, 11, 1, 10, tzinfo=SHANGHAI))
    await drain(service)
    assert store.forecast_detail(forecast_id)["evaluations"][0]["status"] == "evaluated"
    assert {options["end"] for _, options in prices.calls} == {"2026-10-30"}
    assert len(store.jobs(account["id"])) == 1


@pytest.mark.asyncio
async def test_new_year_calendar_gap_does_not_block_other_due_samples(configured):
    store, account, prices, service = configured
    first, missing = fixed_forecast(store, account, datetime(2026, 12, 30, 16, tzinfo=SHANGHAI), (1, 5))
    # Current calendar deliberately has no 2027 coverage. The 2026 target is still
    # known and can be evaluated after restart; the 5-day target must wait.
    await service.scheduler_tick(datetime(2027, 1, 4, 12, tzinfo=SHANGHAI))
    await drain(service)
    assert store.forecast_detail(first)["evaluations"][0]["hit"] == 1
    detail = store.forecast_detail(missing)
    assert detail["evaluations"] == [] and detail["evaluation_task"]["last_error"] == "calendar_gap"
    assert {options["end"] for _, options in prices.calls} == {"2026-12-31"}


def complete_initial_job(store, account):
    job = store.claim_jobs()[0]
    assert store.publish(job["id"], {"report": "prior fixture report"}, None)
    assert store.account(account["id"])["analysis_state"] == "current"


@pytest.mark.asyncio
@pytest.mark.parametrize("market_enabled", [False, True])
async def test_sleep_gap_obeys_model_and_market_switches(configured, market_enabled):
    store, account, prices, service = configured
    complete_initial_job(store, account)
    store.set_meta("settings", {**store.settings(), "market_refresh_enabled": market_enabled})
    refreshed = []
    async def refresh(*args, **kwargs):
        refreshed.append(True)
    service.refresh_quotes = refresh
    original_scheduler = service.scheduler_tick
    async def controlled_scheduler():
        await original_scheduler(datetime(2026, 10, 29, 12, tzinfo=SHANGHAI))
    service.scheduler_tick = controlled_scheduler
    service.last_tick = time.monotonic() - 181
    service.last_wall_tick = time.time() - 181
    await service.tick()
    await drain(service)
    assert len(refreshed) == int(market_enabled)
    assert len(store.jobs(account["id"])) == 1
    assert not any(d["kind"] == "resume_refresh" for d in store.demands(account["id"]))
    await service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("market_enabled", [False, True])
async def test_restart_stale_cache_obeys_model_and_market_switches(configured, market_enabled):
    store, account, prices, service = configured
    complete_initial_job(store, account)
    store.set_meta("settings", {**store.settings(), "market_refresh_enabled": market_enabled})
    store.set_meta("last_quote_refresh", "2000-01-01T15:00:00+08:00")
    refreshed = []
    async def refresh(*args, **kwargs):
        refreshed.append(True)
    async def hold_worker():
        await asyncio.Event().wait()
    service.refresh_quotes, service.loop = refresh, hold_worker
    await service.start()
    await drain(service)
    await service.stop()
    assert len(refreshed) == int(market_enabled)
    assert len(store.jobs(account["id"])) == 1
    assert store.account(account["id"])["analysis_state"] == "current"


@pytest.mark.asyncio
async def test_explicitly_pausing_all_stops_each_automatic_branch(configured):
    store, account, prices, service = configured
    store.set_meta("settings", {**store.settings(), "scheduled_research_enabled": False,
        "market_refresh_enabled": False, "forecast_evaluation_enabled": False})
    async def forbidden(*args, **kwargs):
        raise AssertionError("explicitly paused maintenance was called")
    service.refresh_quotes, store.forecast_tick = forbidden, forbidden
    await service.scheduler_tick(datetime(2026, 10, 29, 10, tzinfo=SHANGHAI))
    await drain(service)
    assert len(store.jobs(account["id"])) == 1


@pytest.mark.asyncio
async def test_pausing_evaluation_does_not_stop_enabled_new_batches(configured):
    store, account, prices, service = configured
    store.set_meta("settings", {**store.settings(), "scheduled_research_enabled": True, "forecast_evaluation_enabled": False})
    store.save_forecast_settings(account["id"], {"enabled": True})
    async def forbidden(*args, **kwargs):
        raise AssertionError("disabled scoring must not run")
    store.evaluate_due_forecasts = forbidden
    await service.scheduler_tick(datetime(2026, 10, 29, 16, tzinfo=SHANGHAI))
    await drain(service)
    batches = [b for b in store.forecast_batches(account["id"]) if b["kind"] == "formal"]
    assert len(batches) == 1 and batches[0]["job_id"]
    assert store.job(batches[0]["job_id"])["reason"] == "forecast_batch:" + batches[0]["id"]
