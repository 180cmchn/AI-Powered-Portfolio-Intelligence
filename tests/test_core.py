"""Deterministic persistence/integration tests; these are not LLM-quality evaluations."""
from ledger_helpers import snapshot_payload
from ledger_helpers import save_snapshot_correction
import asyncio
from datetime import datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.domain import AccountInput, TZ, metrics, now
from app.main import create_app
from app.service import ResearchTools, Service
from app.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.sqlite3")
    s.set_meta("settings", {"debounce_seconds": 0, "quote_provider": "manual"})
    return s


def payload(name="甲", qty="100", cash=None, cost=None):
    return AccountInput(name=name, cash=cash, holdings=[{"symbol": "SH:600519", "quantity": qty, "cost_price": cost}])


def quote(store, price="10.125"):
    store.put_cache("quotes", "SH:600519", {"symbol": "SH:600519", "price": price, "source": "test_fixture", "as_of": now(), "status": "ok"})


def test_account_isolation_edit_versions_and_metrics(store):
    quote(store)
    a, b = store.save_account(payload(qty="100")), store.save_account(payload("乙", "350", cash="0", cost="8"))
    svc = Service(store)
    av, bv = svc.account(a["id"]), svc.account(b["id"])
    assert av["metrics"]["securities_value"] == "1012.50"
    assert bv["metrics"]["securities_value"] == "3543.75"
    assert av["metrics"]["total_assets"] is None
    assert av["metrics"]["unrealized_pnl"] is None
    assert av["metrics"]["known_cost_pnl"] is None
    assert bv["metrics"]["total_assets"] == "3543.75"
    assert bv["metrics"]["unrealized_pnl"] == "743.75"
    before_b = store.account(b["id"])
    changed = save_snapshot_correction(store, payload(qty="200"), a["id"])
    assert changed["version"] == 2
    assert svc.account(a["id"])["metrics"]["securities_value"] == "2025.00"
    assert store.account(b["id"]) == before_b
    assert changed["cash"] is None
    assert store.job(changed["latest_job_id"])["input_version"] == 2


def test_new_result_finishes_before_old_cannot_overwrite(store):
    a = store.save_account(payload())
    old = store.claim_jobs()[0]
    changed = save_snapshot_correction(store, payload(qty="300"), a["id"])
    new = store.claim_jobs()[0]
    assert store.publish(new["id"], {"report": "new result"}, None)
    assert not store.publish(old["id"], {"report": "late old result"}, None)
    current = [r for r in store.analyses(a["id"]) if r["status"] == "current"]
    assert len(current) == 1 and current[0]["report"] == "new result"
    assert store.account(a["id"])["latest_job_id"] == changed["latest_job_id"]
    assert store.previous_analysis(a["id"])["report"] == "new result"
    assert store.publish(new["id"], {"report": "duplicate"}, None)
    assert store.job(new["id"])["state"] == "completed"
    assert store.previous_analysis(a["id"])["report"] == "new result"


def test_same_version_latest_job_fence_and_cancellation(store):
    a = store.save_account(payload())
    old = store.claim_jobs()[0]
    store.enqueue(a["id"], "manual")
    new = store.claim_jobs()[0]
    store.set_job_state(new["id"], "cancelled")
    assert not store.publish(old["id"], {"report": "old"}, None)
    assert not store.publish(new["id"], {"report": "cancelled result"}, None)
    assert not any(r["status"] == "current" for r in store.analyses(a["id"]))


def test_debounce_and_duplicate_event(store):
    a = store.save_account(payload())
    for n in range(1, 5):
        a = save_snapshot_correction(store, payload(qty=str(n)), a["id"])
    assert len(store.claim_jobs()) == 1
    first = store.enqueue(a["id"], "news", event_key="same-news")
    second = store.enqueue(a["id"], "news", event_key="same-news")
    assert first["id"] == second["id"]


def test_monitor_union_and_deleted_account(store):
    a, b = store.save_account(payload()), store.save_account(payload("乙"))
    store.delete_account(a["id"])
    assert "SH:600519" in store.monitored()
    store.set_meta("universe", {"watchlist": ["SH:600519"], "candidates": ["SH:510300"]})
    store.delete_account(b["id"])
    assert store.monitored() == ["SH:510300", "SH:600519"]


def test_recovery_only_current_and_inputs_survive(store):
    a = store.save_account(payload())
    old = store.claim_jobs()[0]
    a = save_snapshot_correction(store, payload(qty="500"), a["id"])
    new = store.claim_jobs()[0]
    store.recover()
    assert store.job(old["id"])["state"] == "superseded"
    assert store.job(new["id"])["state"] == "queued"
    assert store.account(a["id"])["holdings"][0]["quantity"] == "500"


def test_missing_quote_does_not_invent_total_weights_or_returns(store):
    a = store.save_account(payload(cash="0"))
    data = Service(store).account(a["id"])["metrics"]
    assert data["cash"] == "0"
    assert data["total_assets"] is None
    assert data["securities_value"] is None
    assert data["positions"][0]["weight_pct"] is None
    assert data["unrealized_pnl"] is None


@pytest.mark.asyncio
async def test_materials_preserve_classifications_missing_fields_and_privacy(store):
    a, b = store.save_account(payload()), store.save_account(payload("乙"))
    ja, jb = store.job(a["latest_job_id"]), store.job(b["latest_job_id"])
    ta, tb = ResearchTools(store, ja, {}), ResearchTools(store, jb, {})
    fact = await ta.save_material({"classification": "fact", "content": "报告披露收入 10 亿元", "url": "https://example.org/report"})
    opinion = await ta.save_material({"classification": "external_opinion", "content": "作者建议立即加仓", "url": "https://example.org/report"})
    failed = await ta.save_material({"url": "https://example.org/unreadable", "access": "failed", "error": "HTTP 403"})
    rows = await ta.list_materials()
    assert {r["id"] for r in rows} == {fact["id"], opinion["id"], failed["id"]}
    assert fact["published_at"] is None
    assert not await tb.list_materials()  # model-authored account research stays scoped
    public = store.save_material({"scope": "public", "content": "公共原文", "url": "https://example.org/public"})
    assert (await tb.list_materials())[0]["id"] == public["id"]


def test_old_materials_retrievable_after_list_window(store):
    old = store.save_material({"content": "old source", "scope": "public"})
    for n in range(305):
        store.save_material({"content": str(n), "scope": "public"})
    assert store.material(old["id"])["content"] == "old source"
    assert store.materials(query=old["id"])[0]["id"] == old["id"]


@pytest.mark.asyncio
async def test_worker_failure_keeps_saved_input_and_retry(store):
    async def fail(context, cfg, research_tools, emit):
        raise TimeoutError()
    a = store.save_account(payload(qty="900"))
    svc = Service(store, runner=fail)
    await svc.execute(store.claim_jobs()[0])
    assert store.job(a["latest_job_id"])["state"] == "failed"
    assert store.account(a["id"])["holdings"][0]["quantity"] == "900"
    retried = store.enqueue(a["id"], "retry")
    assert retried["input_version"] == a["version"]


@pytest.mark.asyncio
async def test_worker_actual_local_pipeline_with_test_double(store):
    quote(store)
    async def researcher(context, cfg, research_tools, emit):
        await emit({"type": "test_only", "message": "deterministic integration double"})
        return {"report": f"账户 {context['account']['name']} 市值 {context['metrics']['securities_value']}"}
    a, b = store.save_account(payload(qty="100")), store.save_account(payload("乙", "200"))
    svc = Service(store, runner=researcher)
    await asyncio.gather(*(svc.execute(j) for j in store.claim_jobs()))
    assert "1012.50" in store.analyses(a["id"])[0]["report"]
    assert "2025.00" in store.analyses(b["id"])[0]["report"]


@pytest.mark.asyncio
async def test_target_security_and_missing_model_preflight(store):
    a = store.save_account(payload())
    service = Service(store)
    started = asyncio.Event()
    finish = asyncio.Event()
    async def pending_quotes(symbols=None):
        started.set()
        await finish.wait()
    service.refresh_quotes = pending_quotes
    await asyncio.wait_for(service.execute(store.claim_jobs()[0]), timeout=1)
    assert store.job(a["latest_job_id"])["state"] == "waiting_config"
    await asyncio.wait_for(started.wait(), timeout=1)
    finish.set()
    await asyncio.gather(*list(service.maintenance))

    captured = {}
    async def capture(context, cfg, tools, emit):
        captured.update(context)
        return {"report": "deterministic specified-security pipeline test"}
    async def refresh(symbols=None):
        captured["refreshed"] = symbols
        store.put_cache("quotes", "SH:510300", {"price": "4.107", "as_of": now(), "status": "ok"})
    service.runner, service.refresh_quotes = capture, refresh
    store.enqueue(a["id"], "manual_security:SH:510300")
    await service.execute(store.claim_jobs()[0])
    assert "SH:510300" in captured["refreshed"]
    assert captured["target_security"]["quote"]["price"] == "4.107"


@pytest.mark.asyncio
async def test_startup_refreshes_expired_completed_analysis(store):
    a = store.save_account(payload())
    job = store.claim_jobs()[0]
    store.publish(job["id"], {"report": "previous"}, None)
    store.set_meta("last_quote_refresh", "2026-01-01T15:00:00+08:00")
    service = Service(store)
    async def noop(*args, **kwargs):
        return {}
    service.refresh_quotes = noop
    await service.start()
    await service.stop()
    assert store.job(store.account(a["id"])["latest_job_id"])["reason"] == "service_restart_refresh"
    assert store.analyses(a["id"])[0]["status"] == "stale"


@pytest.mark.asyncio
async def test_scheduler_calendar_dedup_and_unknown_year(store):
    a = store.save_account(payload())
    svc = Service(store)
    await svc.scheduler_tick(datetime(2026, 10, 1, 10, 0, tzinfo=TZ))
    assert len(store.jobs(a["id"])) == 1
    await svc.scheduler_tick(datetime(2027, 10, 22, 10, 0, tzinfo=TZ))
    assert len(store.jobs(a["id"])) == 1
    await svc.scheduler_tick(datetime(2026, 10, 22, 15, 16, tzinfo=TZ))
    await svc.scheduler_tick(datetime(2026, 10, 22, 15, 17, tzinfo=TZ))
    assert sum(j["reason"] == "close_summary" for j in store.jobs(a["id"])) == 1


def test_backup_integrity_and_mode_isolation(store, tmp_path):
    a = store.save_account(payload())
    target = store.backup(tmp_path / "backup.sqlite3")
    restored = Store(target)
    assert restored.account(a["id"])["holdings"] == a["holdings"]
    with pytest.raises(ValueError):
        Store(target, "demo")


def test_api_validation_conflict_and_local_boundary(tmp_path):
    app = create_app(tmp_path / "api.sqlite3", worker=False)
    app.state.store.set_meta("settings", {"quote_provider": "manual"})
    with TestClient(app) as c:
        bad = c.post("/api/accounts", json={"name": "bad", "holdings": [{"symbol": "60x519", "quantity": "1"}]})
        assert bad.status_code == 422
        bad = c.post("/api/accounts", json={"name": "bad", "cash": "NaN"})
        assert bad.status_code == 422
        r = c.post("/api/accounts", json=payload().model_dump())
        assert r.status_code == 201
        a = r.json()
        changed = {**payload(qty="200").model_dump(), "expected_version": 1}
        assert c.put(f"/api/accounts/{a['id']}", json=snapshot_payload(changed)).status_code == 200
        assert c.put(f"/api/accounts/{a['id']}", json=snapshot_payload(changed)).status_code == 409
        assert c.post("/api/backup", headers={"origin": "https://evil.example"}).status_code == 403
        assert c.post("/api/demo/seed").status_code == 409
        assert c.get('/api/status').json()['model_configured'] is False


@pytest.mark.parametrize("quantity", ["-1", "NaN", "Infinity", "1.25", True])
def test_quantity_is_shares_not_lots_and_finite(quantity):
    with pytest.raises(ValueError):
        payload(qty=quantity)
