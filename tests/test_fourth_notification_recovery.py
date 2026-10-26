"""Crash boundaries use production publication/outbox/SQLite routing code."""
import asyncio

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


@pytest.mark.asyncio
@pytest.mark.parametrize("resume_trigger", ["manual", "external_event"])
async def test_failed_public_discovery_does_not_schedule_itself_again(tmp_path, resume_trigger):
    store = Store(tmp_path / "self-discovery.sqlite3")
    store.set_meta("settings", {"quote_provider": "manual", "scheduled_research_enabled": False})
    store.request_analysis(None, "manual", "controlled-initial-public")
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def runner(context, cfg, tools, emit):
        calls.append(context)
        if len(calls) == 1:
            await tools.record_native_search("child", [{"type": "web_search_call", "status": "completed"}], [
                {"url": "https://issuer.example/discovered", "snippet": "controlled native source"}], [])
            entered.set()
            await release.wait()
            raise RuntimeError("controlled synthesis exhausted")
        return {"report": "Controlled completed retry", "information_impacts": [
            {"material_id": item["id"], "status": "irrelevant", "explanation": "Controlled explicit relevance result"}
            for item in context["pending_information"]]}

    service = Service(store, runner)
    await service.tick()  # Normal worker dispatch, not a direct execute call.
    await asyncio.wait_for(entered.wait(), 2)
    original = store.global_runs()[0]
    owned = [d for d in store.demands() if d['kind'] == 'public_information']
    assert len(owned) == 1 and owned[0]['state'] == 'assigned' and owned[0]['job_id'] == original['id']
    service.last_recommendation_check = 0
    await service.tick()  # Notification polling while discovery's run is active.
    assert not [d for d in store.demands() if d['account_id'] == '' and d['state'] == 'pending']
    release.set()
    await service.global_task
    assert store.global_run(original['id'])['state'] == 'failed'
    pending = store.pending_material_notifications()
    assert len(pending) == 1
    for _ in range(3):
        service.last_recommendation_check = 0
        await service.tick()
    assert len(store.global_runs()) == 1 and len(calls) == 1

    restarted = Store(store.path)
    restarted.recover(config_ready=True)
    resumed = Service(restarted, runner)
    await resumed.tick()
    assert len(restarted.global_runs()) == 1
    assert restarted.pending_material_notifications() == pending
    with restarted.connect() as db:
        route = dict(db.execute('SELECT * FROM information_routes WHERE material_id=?', (pending[0],)).fetchone())
    assert route['state'] == 'awaiting_retry' and route['public_run_id'] == original['id']
    if resume_trigger == 'manual':
        restarted.request_analysis(None, 'manual', 'controlled-explicit-retry')
    else:
        restarted.save_material({'scope': 'public', 'url': 'https://issuer.example/new-event',
                                  'content': 'controlled genuinely new event'}, notify=True)
        resumed.last_recommendation_check = 0
    await resumed.tick()
    await resumed.global_task
    assert len(calls) == 2
    assert pending[0] in {item['id'] for item in calls[-1]['pending_information']}
    assert restarted.global_runs()[0]['state'] == 'completed'
    assert not restarted.pending_material_notifications()
    resumed.last_recommendation_check = 0
    await resumed.tick()
    assert len(restarted.global_runs()) == 2


@pytest.mark.asyncio
async def test_failed_discovery_backlog_cannot_hide_new_external_event(tmp_path):
    store = Store(tmp_path / 'backlog.sqlite3')
    store.set_meta('settings', {'quote_provider': 'manual', 'scheduled_research_enabled': False})
    store.request_analysis(None, 'manual', 'controlled-original')
    old = store.claim_global()
    mids = [store.save_material({'scope': 'public', 'url': f'https://issuer.example/{n}',
                                 'content': 'controlled earlier discovery'}, notify=True)['id'] for n in range(101)]
    store.bind_public_information(old['id'], mids)
    store.finish_global(old['id'], 'failed', error='controlled failure')
    new = store.save_material({'scope': 'public', 'url': 'https://issuer.example/new',
                              'content': 'controlled new external event'}, notify=True)
    contexts = []
    async def runner(context, cfg, tools, emit):
        contexts.append(context)
        return {'report': 'controlled relevance', 'information_impacts': [
            {'material_id': item['id'], 'status': 'irrelevant', 'explanation': 'controlled scope decision'}
            for item in context['pending_information']]}
    service = Service(store, runner)
    await service.tick()
    await service.global_task
    assert contexts[0]['pending_information'][0]['id'] == new['id']
    assert new['id'] not in store.pending_material_notifications()
    assert len(store.pending_material_notifications()) == 2  # Retained, never silently acknowledged.


def test_interrupted_public_discoveries_recover_in_one_bounded_execution(tmp_path):
    store = Store(tmp_path / 'interrupted-discovery.sqlite3')
    store.request_analysis(None, 'manual', 'controlled-initial')
    old = store.claim_global()
    material = store.save_material({'scope': 'public', 'url': 'https://issuer.example/interrupted',
                                    'content': 'controlled discovered source'}, notify=True)
    store.bind_public_information(old['id'], [material['id']])
    restarted = Store(store.path)
    restarted.recover(config_ready=True)
    Service(restarted).notify_material_changes()
    retry = restarted.claim_global()
    assert retry and retry['attempts'] == 2
    assert {d['job_id'] for d in restarted.demands()} == {retry['id']}
    assert restarted.claim_global() is None
    assert restarted.pending_material_notifications() == [material['id']]
