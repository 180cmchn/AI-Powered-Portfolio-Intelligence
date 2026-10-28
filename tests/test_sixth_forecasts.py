"""Controlled-clock and historical-price fixtures, never real forward accuracy."""
import json
import sqlite3
import asyncio
from datetime import datetime

import pytest

from app.calendar import advance_trading_days, market_state, SHANGHAI
from app.forecasts import classify_return
from app.forecast_prices import closing_pair
from app.store import Store


AT = datetime(2026, 10, 28, 16, tzinfo=SHANGHAI)


@pytest.fixture
def account(tmp_path):
    store = Store(tmp_path / "fixture.sqlite3")
    account = store.save_account({"name": "historical fixture", "holdings": [{"symbol": "SZ:159278", "quantity": "100", "cost": "1"}], "preferences": {}})
    return store, account


def batch_job(account, *, kind="test", horizons=None, at=AT):
    store, account = account
    if horizons:
        store.save_forecast_settings(account["id"], {"horizons": horizons, "labels": [f"{n}日" for n in horizons]})
    batch = store.create_forecast_batch(account["id"], kind=kind, at=at)
    store.bind_forecast_job(batch["id"], account["latest_job_id"])
    with store.connect() as db:
        db.execute("UPDATE jobs SET state='running' WHERE id=?", (account["latest_job_id"],))
    return batch, store.job(account["latest_job_id"])


def publish(store, job, *, direction="up", horizon=1, at=AT, extra=None):
    result = {"report": "Controlled test fixture", "forecasts": [{"security_id": "SZ:159278", "horizon": horizon,
        "direction": direction, "reason": "fixture thesis", "material_ids": []}]}
    if extra:
        result.update(extra)
    with store.connect() as db:
        return store.publish_forecasts(db, job, result, "fixture-report", published_at=at)


@pytest.mark.parametrize("price,direction", [("100.5", "sideways"), ("99.5", "sideways"),
    ("100.50000000001", "up"), ("99.49999999999", "down"), ("100", "sideways"), ("101", "up")])
def test_decimal_boundaries(price, direction):
    assert classify_return("100", price)["actual_direction"] == direction


def test_calendar_excludes_base_and_holidays():
    assert advance_trading_days("2026-09-30", 1)["target_date"] == "2026-10-08"
    assert advance_trading_days("2026-10-28", 5)["target_date"] == "2026-11-04"
    assert advance_trading_days("2026-10-28", 20)["target_date"] == "2026-11-25"
    assert advance_trading_days("2026-12-31", 1)["status"] == "calendar_gap"


def test_calendar_directory_extension_and_gaps(tmp_path):
    (tmp_path / "calendar_2026.json").write_text(json.dumps({"coverage": {"start": "2026-12-30", "end": "2026-12-31"}, "source": "fixture-2026", "holidays": []}))
    (tmp_path / "calendar_2027.json").write_text(json.dumps({"coverage": {"start": "2027-01-01", "end": "2027-01-10"}, "source": "fixture-2027", "holidays": ["2027-01-01"]}))
    assert advance_trading_days("2026-12-31", 1, tmp_path)["target_date"] == "2027-01-04"
    assert market_state(datetime(2027, 1, 4, 10, tzinfo=SHANGHAI), tmp_path)["trading_day"] is True


def test_initial_disabled_and_expected_scope_full(account):
    store, acc = account
    assert store.forecast_settings(acc["id"])["enabled"] is False
    batch, job = batch_job(account)
    assert batch["expected_count"] == 3
    page = store.forecast_job_context(job["id"], limit=1)
    assert page["has_more"] and page["next_offset"] == 1
    assert store.forecasts_page(kind="test")["total"] == 3


def test_first_success_lock_abstention_missing_and_rules(account):
    store, acc = account
    batch, job = batch_job(account)
    first = publish(store, job)
    again = publish(store, job, direction="down")
    assert first == again
    assert store.forecast_detail(first[0])["direction"] == "up"
    with store.connect() as db:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE forecasts SET direction='down' WHERE id=?", (first[0],))
    publish(store, job, direction="abstain", horizon=5)
    store.save_forecast_settings(acc["id"], {"horizons": [2, 7, 30], "labels": ["短线", "中线", "长线"], "threshold": "0.02"})
    assert store.forecast_detail(first[0])["rule"]["threshold"] == "0.005"
    # Editing rules during the day cannot open a second fixed batch.
    assert store.create_forecast_batch(acc["id"], at=AT, kind="test")["id"] == batch["id"]
    stats = store.forecast_statistics(kind="test")
    assert len(stats["groups"]) == 3
    assert all(g["accuracy"] is None for g in stats["groups"])
    assert next(g for g in stats["groups"] if g["horizon"] == 5)["states"] == {"abstain": 1}
    assert store.forecast_statistics()["expected"] == 0


def test_after_deadline_cannot_backfill(account):
    store, _ = account
    _, job = batch_job(account)
    assert publish(store, job, at=datetime(2026, 10, 29, 16, tzinfo=SHANGHAI)) == []
    assert {x["state"] for x in store.forecasts_page()["items"]} == {"missed"}


def test_model_cannot_select_dates_or_foreign_scope(account):
    store, _ = account
    _, job = batch_job(account)
    with store.connect() as db:
        with pytest.raises(ValueError, match="身份和时间"):
            store.publish_forecasts(db, job, {"forecasts": [{"symbol": "SZ:159278", "horizon": 1, "direction": "up", "reason": "x", "published_at": "2000-01-01"}]}, "report", AT)
    with pytest.raises(ValueError, match="范围"):
        publish(store, job, horizon=2)


def evidence(price="101", **changes):
    return {"symbol": "SZ:159278", "base_date": "2026-10-28", "target_date": "2026-10-29",
            "status": "ready", "base_price": "100", "target_price": price,
            "source": "controlled historical fixture", "basis": "raw_close", "price_policy": "same_source_pair_v1", **changes}


def test_evaluation_maturity_idempotence_and_revision_rollback(account):
    store, _ = account
    _, job = batch_job(account)
    forecast_id = publish(store, job)[0]
    assert store.record_forecast_evaluation(forecast_id, evidence(), at=datetime(2026, 10, 29, 12, tzinfo=SHANGHAI)) is None
    at = datetime(2026, 10, 29, 16, tzinfo=SHANGHAI)
    first = store.record_forecast_evaluation(forecast_id, evidence(), at=at)
    assert store.record_forecast_evaluation(forecast_id, evidence(), at=at) == first
    store.record_forecast_evaluation(forecast_id, evidence("98"), at=at)
    assert len(store.forecast_detail(forecast_id)["evaluations"]) == 2
    stats = next(g for g in store.forecast_statistics(kind="test")["groups"] if g["horizon"] == 1)
    assert stats["accuracy"] == 0 and stats["evaluated"] == 1
    # A genuine supplier correction back to its old value is a third revision.
    store.record_forecast_evaluation(forecast_id, evidence(), at=at)
    assert len(store.forecast_detail(forecast_id)["evaluations"]) == 3
    stats = next(g for g in store.forecast_statistics(kind="test")["groups"] if g["horizon"] == 1)
    assert stats["accuracy"] == 1 and stats["evaluated"] == 1


def test_exact_date_rejects_current_quote_substitution(account):
    store, _ = account
    _, job = batch_job(account)
    forecast_id = publish(store, job)[0]
    with pytest.raises(ValueError, match="精确日期"):
        store.record_forecast_evaluation(forecast_id, evidence(target_date="2026-10-30"), at=datetime(2026, 10, 30, 16, tzinfo=SHANGHAI))


class HistoricalFixture:
    def __init__(self, *, missing=False, adjusted=True, action=False, no_trade=False):
        self.missing, self.adjusted, self.action, self.no_trade = missing, adjusted, action, no_trade
        self.calls = []

    async def history(self, symbol, **kwargs):
        self.calls.append((symbol, kwargs))
        if kwargs["adjust"] == "hfq" and not self.adjusted:
            return {"status": "unavailable", "symbol": symbol, "source": "fixture"}
        base, end = ("200", "202") if self.action and kwargs["adjust"] == "hfq" else ("100", "50.5") if self.action else ("100", "101")
        rows = [{"time": kwargs["start"], "close": base, "volume": "100"}]
        if kwargs["end"] != kwargs["start"] and not self.missing:
            rows.append({"time": kwargs["end"], "close": end, "volume": "0" if self.no_trade else "100"})
        return {"symbol": symbol, "source": "fixture", "status": "ok", "adjustment": kwargs["adjust"], "rows": rows}


@pytest.mark.asyncio
async def test_history_pair_action_and_missing_handling():
    pair = await closing_pair(HistoricalFixture(action=True), "SZ:159278", "2026-10-28", "2026-10-29")
    assert pair["basis"] == "same_snapshot_hfq"
    assert classify_return(pair["base_price"], pair["target_price"])["actual_direction"] == "up"
    assert (await closing_pair(HistoricalFixture(missing=True), "SZ:159278", "2026-10-28", "2026-10-29"))["reason"] == "exact_date_missing"
    assert (await closing_pair(HistoricalFixture(adjusted=False), "SZ:159278", "2026-10-28", "2026-10-29"))["reason"] == "comparable_adjustment_unavailable"
    assert (await closing_pair(HistoricalFixture(no_trade=True), "SZ:159278", "2026-10-28", "2026-10-29"))["status"] == "unassessable"


@pytest.mark.asyncio
async def test_offline_model_still_evaluates_after_sleep(account):
    store, _ = account
    _, job = batch_job(account)
    forecast_id = publish(store, job)[0]
    fixture = HistoricalFixture()
    result = await store.forecast_tick({"scheduled_research_enabled": False, "model_name": "", "model_key_env": "MISSING"},
        at=datetime(2026, 11, 2, 10, tzinfo=SHANGHAI), provider=fixture, create_batches=False)
    assert result["evaluated"] == [forecast_id]
    assert store.forecast_detail(forecast_id)["evaluations"][0]["hit"] == 1
    assert {call[1]["end"] for call in fixture.calls} == {"2026-10-29"}


@pytest.mark.asyncio
async def test_lunch_and_weekend_cannot_spawn_fixed_batch(account):
    store, acc = account
    store.save_forecast_settings(acc["id"], {"enabled": True})
    for at in [datetime(2026, 10, 28, 12, tzinfo=SHANGHAI), datetime(2026, 10, 31, 16, tzinfo=SHANGHAI)]:
        result = await store.forecast_tick({}, at=at, provider=HistoricalFixture())
        assert result["batches"] == []
    result = await store.forecast_tick({}, at=AT, provider=HistoricalFixture())
    assert result["batches"][0]["expected_count"] == 3
    again = await store.forecast_tick({}, at=AT, provider=HistoricalFixture())
    assert again["batches"][0]["id"] == result["batches"][0]["id"]


def test_publish_hook_atomic_and_cancelled_job_excluded(account, monkeypatch):
    import app.forecasts as forecasts
    monkeypatch.setattr(forecasts, "_local", lambda at=None: AT if at is None else at)
    store, acc = account
    _, job = batch_job(account)
    result = {"report": "fixture", "forecasts": [{"security_id": "SZ:159278", "horizon": 1, "direction": "up", "reason": "fixture"}]}
    assert store.publish(job["id"], result, AT.isoformat()) is True
    detail = store.forecasts_page(kind="test")["items"][0]
    forecast = store.forecast_detail(detail["forecast_id"])
    assert store.analysis(forecast["report_id"])["report"] == "fixture"
    assert store.publish(job["id"], result, AT.isoformat()) is True
    assert sum(bool(x["forecast_id"]) for x in store.forecasts_page(kind="test")["items"]) == 1


def test_exploration_is_separate_pre_run_baseline(account):
    store, acc = account
    job_id = acc["latest_job_id"]
    context = store.prepare_exploratory_forecast_job(job_id, datetime(2026, 10, 29, 10, tzinfo=SHANGHAI))
    assert context["kind"] == "exploratory" and context["base_date"] == "2026-10-28"
    assert context["created_at"].startswith("2026-10-29T10:")
    publish(store, store.job(job_id), at=datetime(2026, 10, 29, 11, tzinfo=SHANGHAI))
    assert store.forecast_statistics()["expected"] == 0
    assert store.forecast_statistics(kind="exploratory")["expected"] == 3


def test_invalid_forecast_rolls_back_report_transaction(account, monkeypatch):
    import app.forecasts as forecasts
    monkeypatch.setattr(forecasts, "_local", lambda at=None: AT if at is None else at)
    store, acc = account
    _, job = batch_job(account)
    with pytest.raises(ValueError):
        store.publish(job["id"], {"report": "must roll back", "forecasts": [{"security_id": "SZ:159278", "horizon": 999, "direction": "up", "reason": "x"}]}, AT.isoformat())
    assert store.analyses(acc["id"]) == []
    assert store.job(job["id"])["state"] == "running"


def test_cancelled_publication_cannot_create_forecast(account, monkeypatch):
    import app.forecasts as forecasts
    monkeypatch.setattr(forecasts, "_local", lambda at=None: AT if at is None else at)
    store, _ = account
    _, job = batch_job(account)
    store.set_job_state(job["id"], "cancelled")
    assert store.publish(job["id"], {"report": "late", "forecasts": [{"security_id": "SZ:159278", "horizon": 1, "direction": "up", "reason": "x"}]}, AT.isoformat()) is False
    assert all(x["forecast_id"] is None for x in store.forecasts_page()["items"])


def test_network_refresh_does_not_remove_valid_denominator(account):
    store, _ = account
    _, job = batch_job(account)
    forecast_id = publish(store, job)[0]
    at = datetime(2026, 10, 29, 16, tzinfo=SHANGHAI)
    store.record_forecast_evaluation(forecast_id, evidence(), at=at)
    store.record_forecast_evaluation(forecast_id, evidence(status="waiting_data", reason="history_unavailable"), at=at)
    detail = store.forecast_detail(forecast_id)
    assert len(detail["evaluations"]) == 1 and len(detail["price_observations"]) == 2
    group = next(g for g in store.forecast_statistics(kind="test")["groups"] if g["horizon"] == 1)
    assert group["evaluated"] == 1 and group["hits"] == 1


@pytest.mark.asyncio
async def test_calendar_gap_only_affects_missing_horizon_and_can_recover(account, tmp_path):
    store, acc = account
    calendar = tmp_path / "calendar_fixture.json"
    calendar.write_text(json.dumps({"coverage": {"start": "2026-10-28", "end": "2026-10-29"}, "source": "fixture", "holidays": []}))
    batch = store.create_forecast_batch(acc["id"], at=AT, kind="test", calendar_path=calendar)
    store.bind_forecast_job(batch["id"], acc["latest_job_id"])
    job = store.job(acc["latest_job_id"])
    one, five = publish(store, job, horizon=1)[0], publish(store, job, horizon=5)[0]
    assert store.forecast_detail(one)["target_date"] == "2026-10-29"
    assert store.forecast_detail(five)["target_date"] is None
    calendar.write_text(json.dumps({"coverage": {"start": "2026-10-28", "end": "2026-12-31"}, "source": "fixture extension", "holidays": []}))
    await store.evaluate_due_forecasts({"calendar_path": calendar}, at=datetime(2026, 11, 5, 16, tzinfo=SHANGHAI), provider=HistoricalFixture())
    detail = store.forecast_detail(five)
    assert detail["target_date"] is None  # original ledger remains immutable
    assert detail["effective_target_date"] == "2026-11-04"
    assert detail["evaluations"][0]["status"] == "evaluated"


def test_task_replacement_preserves_fixed_scope_and_changed_input_fails(account):
    store, acc = account
    batch, old_job = batch_job(account)
    with store.connect() as db:
        db.execute("UPDATE jobs SET state='superseded' WHERE id=?", (old_job["id"],))
        store.reconcile_forecast_jobs(db, acc["id"], "replacement-job", acc["version"], at=AT)
        row = db.execute("SELECT job_id,rule_version,expected_count FROM forecast_batches WHERE id=?", (batch["id"],)).fetchone()
        assert row["job_id"] == "replacement-job" and row["expected_count"] == 3 and row["rule_version"] == batch["rule_version"]
        # Restore the old binding to test a changed-input replacement independently.
        db.execute("UPDATE forecast_batches SET job_id=? WHERE id=?", (old_job["id"], batch["id"]))
        store.reconcile_forecast_jobs(db, acc["id"], "new-version-job", acc["version"] + 1, at=AT)
    rows = store.forecasts_page()["items"]
    assert {r["state"] for r in rows} == {"failed"}
    assert all("输入版本" in r["reason"] for r in rows)


def test_context_contains_page_not_complete_team_prompts(account):
    store, _ = account
    _, job = batch_job(account)
    context = store.forecast_job_context(job["id"], limit=1)
    assert "team_snapshot" not in context and "symbols" not in context
    assert context["expected_count"] == 3 and len(context["items"]) == 1


@pytest.mark.asyncio
async def test_missing_all_baselines_retains_scope_and_waits_without_paid_job(account):
    store, acc = account
    store.save_forecast_settings(acc["id"], {"enabled": True})
    class Missing:
        async def history(self, symbol, **kwargs):
            return {"symbol": symbol, "status": "empty", "rows": [], "source": "fixture"}
    result = await store.forecast_tick({}, at=AT, provider=Missing())
    assert result["batches"] == [] and len(store.jobs(acc["id"])) == 1
    batches = store.forecast_batches(acc["id"])
    assert len(batches) == 1 and batches[0]["state"] == "waiting_data"
    assert store.forecasts_page()["total"] == 3
    assert {r["state"] for r in store.forecasts_page()["items"]} == {"waiting_data"}
    ready = await store.forecast_tick({}, at=datetime(2026, 10, 28, 17, tzinfo=SHANGHAI), provider=HistoricalFixture())
    assert ready["batches"][0]["id"] == batches[0]["id"]
    assert ready["batches"][0]["symbols"] == batches[0]["symbols"]


@pytest.mark.asyncio
async def test_missing_baseline_cutoff_records_missed_not_invisible(account):
    store, acc = account
    store.save_forecast_settings(acc["id"], {"enabled": True})
    class Missing:
        async def history(self, symbol, **kwargs):
            return {"symbol": symbol, "status": "empty", "rows": [], "source": "fixture"}
    await store.forecast_tick({}, at=AT, provider=Missing())
    store.expire_forecast_batches(datetime(2026, 10, 28, 19, tzinfo=SHANGHAI))
    assert {r["state"] for r in store.forecasts_page()["items"]} == {"missed"}
    assert all("基准" in r["reason"] for r in store.forecasts_page()["items"])


def test_atomic_batch_enqueue_rolls_back_crash_and_is_idempotent(account, monkeypatch):
    store, acc = account
    batch = store.create_forecast_batch(acc["id"], at=AT, kind="test", baseline_evidence={"SZ:159278": {"status": "ready"}})
    original = store._enqueue
    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("controlled crash before binding")
    monkeypatch.setattr(store, "_enqueue", crash)
    with pytest.raises(RuntimeError):
        store.enqueue_forecast_batch(batch["id"], at=AT)
    assert len(store.jobs(acc["id"])) == 1
    assert store.forecast_batches(acc["id"])[0]["job_id"] is None
    assert store.job(acc["latest_job_id"])["state"] == "queued"
    monkeypatch.setattr(store, "_enqueue", original)
    job = store.enqueue_forecast_batch(batch["id"], at=AT)
    assert store.enqueue_forecast_batch(batch["id"], at=AT)["id"] == job["id"]
    assert len(store.jobs(acc["id"])) == 2
    assert store.forecast_batches(acc["id"])[0]["job_id"] == job["id"]


def test_exploration_published_after_target_is_retained_but_not_scored(account):
    store, acc = account
    store.prepare_exploratory_forecast_job(acc["latest_job_id"], datetime(2026, 10, 29, 10, tzinfo=SHANGHAI))
    at = datetime(2026, 10, 29, 16, tzinfo=SHANGHAI)
    forecast_id = publish(store, store.job(acc["latest_job_id"]), at=at)[0]
    store.record_forecast_evaluation(forecast_id, evidence(), at=at)
    detail = store.forecast_detail(forecast_id)
    assert detail["direction"] == "up" and detail["evaluations"][0]["status"] == "unassessable"
    assert detail["evaluations"][0]["reason"] == "published_after_target_close"
    assert next(g for g in store.forecast_statistics(kind="exploratory")["groups"] if g["horizon"] == 1)["accuracy"] is None


@pytest.mark.asyncio
async def test_concurrent_evaluation_has_one_inflight_price_fetch(account):
    store, _ = account
    _, job = batch_job(account)
    forecast_id = publish(store, job)[0]
    started, release = asyncio.Event(), asyncio.Event()
    class SlowHistory(HistoricalFixture):
        async def history(self, symbol, **kwargs):
            started.set()
            await release.wait()
            return await super().history(symbol, **kwargs)
    provider = SlowHistory()
    at = datetime(2026, 10, 29, 16, tzinfo=SHANGHAI)
    first = asyncio.create_task(store.evaluate_due_forecasts({}, at=at, provider=provider))
    await started.wait()
    assert await store.evaluate_due_forecasts({}, at=at, provider=provider) == []
    release.set()
    assert await first == [forecast_id]
    assert len(provider.calls) == 2  # raw and same-snapshot adjustment, one evaluation
    assert len(store.forecast_detail(forecast_id)["evaluations"]) == 1
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM forecast_evaluation_leases").fetchone()[0] == 0


def test_report_publication_accepts_system_resolved_forecast_citations(account, monkeypatch):
    import app.forecasts as forecasts
    monkeypatch.setattr(forecasts, "_local", lambda at=None: AT if at is None else at)
    store, acc = account
    _, job = batch_job(account)
    raw = store.save_material({"url": "https://example.com/raw", "scope": "public", "content": "raw filing", "access": "full"}, job["id"], acc["id"])
    other = store.save_account({"name": "Other private account", "holdings": [], "preferences": {}})
    private = store.save_material({"url": "https://example.com/private", "scope": "account", "content": "SECRET_PRIVATE_CONCLUSION", "representation": "agent_extraction"}, other["latest_job_id"], other["id"])
    result = {"report": "fixture with citations", "forecasts": [{"security_id": "SZ:159278", "horizon": 1,
        "direction": "up", "reason": "fixture", "material_ids": [raw["id"], private["id"]]}]}
    assert store.publish(job["id"], result, AT.isoformat())
    forecast_id = next(r["forecast_id"] for r in store.forecasts_page()["items"] if r["forecast_id"])
    detail = store.forecast_detail(forecast_id)
    references = detail["citations"]["references"]
    assert {r["status"] for r in references} == {"resolved", "unresolved"}
    assert "SECRET_PRIVATE_CONCLUSION" not in json.dumps(detail)
