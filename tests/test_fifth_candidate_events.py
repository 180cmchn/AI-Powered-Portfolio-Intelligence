"""Controlled SZ:159278 events exercise production outbox delivery, not real news."""
import asyncio

import pytest

from app.domain import AccountInput
from app.service import Service
from app.store import Store, loads


ETF = "SZ:159278"


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "fifth-candidate-events.sqlite3")
    value.set_meta("settings", {"quote_provider": "manual", "debounce_seconds": 0})
    return value


def event(store, key="controlled-etf-source", **decision):
    material = store.save_material({"scope": "public", "url": "https://controlled.example/" + key,
        "content": "Controlled relevance fixture; not an actual ETF announcement."}, notify=True)
    return material["id"], {"information_impacts": [{"material_id": material["id"], "status": "relevant",
        "symbols": [ETF], "explanation": "Controlled public Agent judgment for exact security ID", **decision}]}


def delivered(store, account):
    return [item for item in store.demands(account["id"]) if item["kind"] == "public_information"]


def route_row(store, mid):
    with store.connect() as db:
        row = dict(db.execute("SELECT * FROM information_routes WHERE material_id=?", (mid,)).fetchone())
    row["decision"] = loads(row["decision"])
    return row


def test_cash_only_159278_candidate_gets_durable_research_without_buying(store):
    account = store.save_account(AccountInput(name="ETF 候选观察", cash="20000"))
    store.save_universe({"watchlist": [], "candidates": [ETF]})
    mid, result = event(store)
    Service(store).route_material_changes("controlled-public", result, [mid])
    assert len(delivered(store, account)) == 1
    assert store.account(account["id"])["holdings"] == []
    assert store.account(account["id"])["cash"] == "20000"
    assert store.pending_material_notifications() == []
    assert route_row(store, mid)["decision"]["account_ids"] == [account["id"]]


@pytest.mark.parametrize("decision", [
    {"status": "uncertain"}, {"status": "relevant", "symbols": []},
    {"status": "irrelevant", "symbols": [{}]},
])
def test_unknown_relevance_is_retained_without_broadcast_or_retry_loop(store, decision):
    account = store.save_account(AccountInput(name="待查账户", holdings=[{"symbol": ETF, "quantity": "10000"}]))
    mid, result = event(store, **decision)
    service = Service(store)
    service.notify_material_changes()
    service.route_material_changes("controlled-public", result, [mid])
    assert delivered(store, account) == []
    assert store.pending_material_notifications() == [mid]
    assert route_row(store, mid)["state"] == "awaiting_retry"
    for _ in range(3):
        service.notify_material_changes()
    assert len([d for d in store.demands() if d["kind"] == "public_information"]) == 1


def test_unknown_sector_does_not_match_every_account(store):
    account = store.save_account(AccountInput(name="未分类 ETF", holdings=[{"symbol": ETF, "quantity": "10000"}]))
    mid, result = event(store, symbols=[], sectors=["汽车"])
    Service(store).route_material_changes("controlled-public", result, [mid])
    assert delivered(store, account) == []
    assert store.pending_material_notifications() == [mid]
    assert route_row(store, mid)["state"] == "awaiting_retry"


def test_sector_match_uses_candidate_metadata_and_leaves_other_sectors_out(store):
    account = store.save_account(AccountInput(name="行业候选"))
    store.save_universe({"watchlist": [], "candidates": [ETF]})
    store.put_cache("securities", ETF, {"symbol": ETF, "sector": "机器人"})
    mid, result = event(store, symbols=[], sectors=["汽车"])
    Service(store).route_material_changes("controlled-irrelevant-sector", result, [mid])
    assert delivered(store, account) == []
    assert store.pending_material_notifications() == []
    second, matching = event(store, "controlled-sector-match", symbols=[], sectors=["机器人"])
    Service(store).route_material_changes("controlled-relevant-sector", matching, [second])
    assert len(delivered(store, account)) == 1


def test_holdings_active_suggestions_and_candidates_are_union_not_all_accounts(store):
    holder = store.save_account(AccountInput(name="持仓", holdings=[{"symbol": ETF, "quantity": "10000"}]))
    suggestion = store.save_account(AccountInput(name="建议"))
    unrelated = store.save_account(AccountInput(name="其它证券", holdings=[{"symbol": "SH:600519", "quantity": "100"}]))
    jobs = store.claim_jobs()
    for job in jobs:
        body = {"report": "Controlled published account report"}
        if job["account_id"] == suggestion["id"]:
            body["suggestions"] = [{"security_id": ETF, "action": "watch", "basis": "Controlled suggestion"}]
        store.publish(job["id"], body, None)
    mid, result = event(store)
    Service(store).route_material_changes("controlled-public", result, [mid])
    assert len(delivered(store, holder)) == len(delivered(store, suggestion)) == 1
    assert delivered(store, unrelated) == []


def test_removed_candidate_and_watchlist_alone_do_not_expand_research_scope(store):
    account = store.save_account(AccountInput(name="范围变化"))
    store.save_universe({"watchlist": [], "candidates": [ETF]})
    mid, result = event(store, "before-scope-change")
    store.save_universe({"watchlist": [ETF], "candidates": []})
    Service(store).route_material_changes("controlled-public", result, [mid])
    assert delivered(store, account) == []
    assert store.pending_material_notifications() == []
    store.save_universe({"watchlist": [], "candidates": [ETF]})
    second, second_result = event(store, "after-scope-change")
    Service(store).route_material_changes("controlled-public-2", second_result, [second])
    assert len(delivered(store, account)) == 1


@pytest.mark.parametrize("crash_at", ["first_demand", "before_ack"])
def test_candidate_delivery_replays_after_crash_without_duplicate_research(store, monkeypatch, crash_at):
    accounts = [store.save_account(AccountInput(name=name, cash="20000")) for name in ("A", "B")]
    store.save_universe({"watchlist": [], "candidates": [ETF]})
    mid, result = event(store)
    service = Service(store)
    service.notify_material_changes()
    if crash_at == "first_demand":
        original = store.request_analysis
        def crash(account_id, *args, **kwargs):
            result = original(account_id, *args, **kwargs)
            raise RuntimeError("controlled crash after durable demand")
        monkeypatch.setattr(store, "request_analysis", crash)
    else:
        def crash(*args):
            raise RuntimeError("controlled crash before acknowledgement")
        monkeypatch.setattr(store, "complete_material_notifications", crash)
    with pytest.raises(RuntimeError, match="controlled crash"):
        service.route_material_changes("controlled-public", result, [mid])
    assert store.pending_material_notifications() == [mid]
    reopened = Store(store.path)
    Service(reopened).notify_material_changes()
    Service(reopened).notify_material_changes()
    assert reopened.pending_material_notifications() == []
    assert [len(delivered(reopened, account)) for account in accounts] == [1, 1]
    Service(reopened).route_material_changes("controlled-public", result, [mid])
    second, reprint = event(reopened, "controlled-reprint", event_key="same-controlled-disclosure")
    third, same_event = event(reopened, "controlled-reprint-2", event_key="same-controlled-disclosure")
    Service(reopened).route_material_changes("controlled-public", reprint, [second])
    Service(reopened).route_material_changes("controlled-public", same_event, [third])
    assert [len(delivered(reopened, account)) for account in accounts] == [2, 2]


def test_later_public_research_can_resolve_retained_uncertain_event(store):
    account = store.save_account(AccountInput(name="候选观察"))
    store.save_universe({"watchlist": [], "candidates": [ETF]})
    mid, result = event(store, status="uncertain")
    service = Service(store)
    service.route_material_changes("controlled-incomplete", result, [mid])
    store.request_analysis(None, "manual", "controlled-explicit-followup")
    run = store.claim_global()
    assert mid in store.public_information_for_run(run["id"])
    result["information_impacts"][0]["status"] = "relevant"
    service.route_material_changes(run["id"], result, [mid])
    assert len(delivered(store, account)) == 1
    assert store.pending_material_notifications() == []


def test_ack_replay_keeps_delivered_plan_when_candidate_scope_changes(store, monkeypatch):
    account = store.save_account(AccountInput(name="候选后加入"))
    mid, result = event(store)
    def crash(*args):
        raise RuntimeError("controlled crash before acknowledgement")
    monkeypatch.setattr(store, "complete_material_notifications", crash)
    with pytest.raises(RuntimeError, match="controlled crash"):
        Service(store).route_material_changes("controlled-public", result, [mid])
    previous = route_row(store, mid)["decision"]
    assert previous["account_ids"] == []
    store.save_universe({"watchlist": [], "candidates": [ETF]})
    reopened = Store(store.path)
    Service(reopened).notify_material_changes()
    assert delivered(reopened, account) == []
    assert reopened.pending_material_notifications() == []
    assert route_row(reopened, mid)["decision"] == previous
    assert any(d["kind"] == "universe_changed" for d in reopened.demands())


def test_later_relevance_refinement_cannot_redeliver_same_material_to_account(store):
    holder = store.save_account(AccountInput(name="已关联持仓", holdings=[{"symbol": ETF, "quantity": "10000"}]))
    store.save_account(AccountInput(name="行业待查", holdings=[{"symbol": "SH:600519", "quantity": "100"}]))
    mid, result = event(store, sectors=["机器人"])
    service = Service(store)
    service.route_material_changes("controlled-partial", result, [mid])
    assert len(delivered(store, holder)) == 1
    assert store.pending_material_notifications() == [mid]
    # A later public Agent may refine the semantic event identity while filling
    # the unresolved mapping. The same material/account pair is already covered.
    result["information_impacts"][0].update(event_key="controlled-newly-identified-event", sectors=[])
    service.route_material_changes("controlled-refined", result, [mid])
    assert len(delivered(store, holder)) == 1
    assert store.pending_material_notifications() == []
    reprint, repeated = event(store, "controlled-refined-reprint", event_key="controlled-newly-identified-event")
    service.route_material_changes("controlled-reprint", repeated, [reprint])
    assert len(delivered(store, holder)) == 1


@pytest.mark.asyncio
async def test_worker_routes_public_159278_judgment_to_cash_only_account(store):
    account = store.save_account(AccountInput(name="正常后台候选", cash="20000"))
    store.set_meta("settings", {**store.settings(), "scheduled_research_enabled": False})
    store.save_universe({"watchlist": [], "candidates": [ETF]})
    mid, result = event(store)
    seen = []
    async def runner(context, cfg, tools, emit):
        seen.append(context)
        return {"report": "Controlled research, not actual news", **(result if context.get("scope") == "global" else {})}
    service = Service(store, runner)
    await service.tick()
    await asyncio.gather(service.global_task, *list(service.active.values()))
    public = next(context for context in seen if context.get("scope") == "global")
    assert public["candidates"] == [ETF]
    assert mid in {item["id"] for item in public["pending_information"]}
    assert store.global_runs()[0]["state"] == "completed"
    assert len(delivered(store, account)) == 1
    assert store.pending_material_notifications() == []
