"""Crash boundaries use production publication/outbox/SQLite routing code."""
import pytest

from app.domain import AccountInput
from app.service import Service
from app.store import Store


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_at", ["before_route", "after_account_demand", "before_ack"])
async def test_completed_public_report_resumes_route_without_model_or_duplicate_work(tmp_path, monkeypatch, crash_at):
    path = tmp_path / "route-recovery.sqlite3"
    store = Store(path)
    store.set_meta("settings", {"quote_provider": "manual", "debounce_seconds": 0})
    a = store.save_account(AccountInput(name="A", holdings=[{"symbol": "SH:600519", "quantity": "100"}]))
    b = store.save_account(AccountInput(name="B", holdings=[{"symbol": "SZ:000001", "quantity": "500"}]))
    material = store.save_material({"scope": "public", "url": "https://issuer.example/event", "content": "controlled external disclosure"}, notify=True)
    calls = []

    async def runner(context, cfg, tools, emit):
        calls.append(context)
        return {"report": "Completed public relevance judgment", "information_impacts": [
            {"material_id": material["id"], "status": "relevant", "symbols": ["SH:600519"], "explanation": "Issuer A only"}]}

    def crash(*args, **kwargs):
        raise RuntimeError("controlled process crash boundary")

    service = Service(store, runner)
    service.notify_material_changes()
    run = store.claim_global()
    if crash_at == "before_route":
        monkeypatch.setattr(service, "route_material_changes", crash)
    elif crash_at == "after_account_demand":
        monkeypatch.setattr(store, "review_recommendations", crash)
    else:
        monkeypatch.setattr(store, "complete_material_notifications", crash)
    await service.execute_global(run)
    assert store.global_run(run["id"])["state"] == "completed"
    assert store.pending_material_notifications() == [material["id"]]
    with store.connect() as db:
        route = db.execute("SELECT state FROM information_routes WHERE material_id=?", (material["id"],)).fetchone()
    assert route["state"] == ("routed" if crash_at == "before_ack" else "ready")

    restarted = Store(path)
    resumed_service = Service(restarted, runner)
    resumed_service.notify_material_changes()
    resumed_service.notify_material_changes()
    assert restarted.pending_material_notifications() == []
    assert restarted.claim_global() is None
    assert len(calls) == 1
    assert len([d for d in restarted.demands(a["id"]) if d["kind"] == "public_information"]) == 1
    assert not [d for d in restarted.demands(b["id"]) if d["kind"] == "public_information"]
    assert restarted.account(a["id"])["version"] == a["version"]
    assert restarted.account(b["id"])["version"] == b["version"]
