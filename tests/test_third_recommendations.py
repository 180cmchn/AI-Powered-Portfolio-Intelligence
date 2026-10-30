"""Third-round deterministic checks using the production SQLite lifecycle."""
from ledger_helpers import save_snapshot_correction
from datetime import datetime, timedelta

import pytest

from app.domain import AccountInput, TZ
from app.store import Store


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / "recommendations.sqlite3")
    store.set_meta("settings", {"debounce_seconds": 0, "quote_provider": "manual"})
    return store


def publish(store, account_id=None, reason="manual", **result):
    if account_id:
        store.enqueue(account_id, reason)
    job = store.claim_jobs()[0]
    assert store.publish(job["id"], {"report": "明确标识的受控测试报告", **result}, None)
    return job


def suggestion(security="SH:600519", **fields):
    return {"security_id": security, "action": "watch", "basis": "受控测试依据",
            "price_conditions": [{"operator": "gte", "value": "110"}], **fields}


def test_plain_report_keeps_observing_and_deduplicates_followup(store):
    account = store.save_account(AccountInput(name="受控账户", holdings=[]))
    publish(store, suggestions=[suggestion()])
    rid = store.recommendations(account["id"])[0]["id"]
    publish(store, account["id"])
    stamp = datetime.now(TZ)
    quotes = {"SH:600519": {"status": "ok", "price": "120", "as_of": stamp.isoformat()}}
    store.review_recommendations(quotes, stamp)
    item = store.recommendations(account["id"])[0]
    assert item["price_state"] == "met"
    assert item["review_status"] == "needs_review"
    assert item["id"] == rid
    assert store.demands(account["id"])
    followup = store.claim_jobs()[0]
    assert store.publish(followup["id"], {"report": "复核报告再次省略结构化处理"}, None)
    for _ in range(5):
        store.review_recommendations(quotes, stamp)
    assert not store.claim_jobs(), "同一未完成复核不能无限自动重算"
    unresolved = store.recommendations(account["id"])[0]
    assert unresolved["review_status"] == "needs_review"
    assert unresolved["review_followup"]["state"] == "waiting_review"
    retry = store.retry_recommendation_review(account["id"], rid)
    assert retry["state"] == "assigned"


def test_active_pagination_is_not_crowded_out_by_history(store):
    account = store.save_account(AccountInput(name="分页受控账户", holdings=[]))
    publish(store, suggestions=[suggestion()])
    first = store.recommendations(account["id"])[0]["id"]
    for index in range(107):
        publish(store, account["id"], reason="manual_security:SZ:000002",
                suggestions=[suggestion("SZ:000002", basis=f"历史受控记录 {index}")])
    page = store.recommendations_page(account["id"], view="active", limit=30)
    assert page["total"] == 2
    assert first in {item["id"] for item in page["items"]}
    assert page["counts"]["history"] == 106
    assert store.recommendations(account["id"], limit=2)[1]["id"] == first
    history = store.recommendations_page(account["id"], view="history", limit=30)
    assert history["total"] == 106 and history["next_offset"] == 30
    assert all(item["state"] in ("revoked", "expired") for item in history["items"])


def test_maintain_and_update_bind_verified_current_account_version(store):
    account = store.save_account(AccountInput(name="版本受控账户", holdings=[]))
    publish(store, suggestions=[suggestion()])
    rid = store.recommendations(account["id"])[0]["id"]
    save_snapshot_correction(store, AccountInput(name="已修改持仓", holdings=[{"symbol": "SH:600519", "quantity": "1"}]), account["id"])
    assert store.recommendation(account["id"], rid)["review_status"] == "needs_review"
    job = store.claim_jobs()[0]
    assert store.publish(job["id"], {"report": "已复核新持仓", "maintained_suggestion_ids": [rid], "input_version": 999}, None)
    item = store.recommendation(account["id"], rid)
    assert item["input_version"] == 2 and item["review_status"] == "valid"
    assert item["job_id"] == job["id"]
    publish(store, account["id"], suggestions=[suggestion(id=rid, input_version=999, basis="明确更新依据")])
    item = store.recommendation(account["id"], rid)
    assert item["revision"] == 2 and item["input_version"] == 2 and item["basis"] == "明确更新依据"
    assert store.recommendations_page(account["id"], view="all")["total"] == 1
    publish(store, account["id"], revoked_suggestion_ids=[rid])
    assert store.recommendation(account["id"], rid)["review_status"] == "revoked"
    assert store.recommendations_page(account["id"])["total"] == 0


def test_local_scope_expiry_and_account_isolation(store):
    first = store.save_account(AccountInput(name="受控 A", holdings=[]))
    publish(store, suggestions=[suggestion(), suggestion("SZ:000002", valid_until=(datetime.now(TZ) + timedelta(hours=1)).isoformat())])
    a = next(item for item in store.recommendations(first["id"]) if item["security_id"] == "SH:600519")
    second = store.save_account(AccountInput(name="受控 B", holdings=[]))
    publish(store)
    publish(store, first["id"], reason="manual_security:SZ:000002", report="只研究 B，省略结构化建议")
    unchanged = store.recommendation(first["id"], a["id"])
    assert unchanged["review_status"] == "valid" and unchanged["updated_at"] == a["updated_at"]
    with pytest.raises(KeyError):
        store.recommendation(second["id"], a["id"])
    publish(store, second["id"], maintained_suggestion_ids=[a["id"]], revoked_suggestion_ids=[a["id"]], suggestions=[suggestion(id=a["id"])])
    assert store.recommendation(first["id"], a["id"])["review_status"] == "valid"
    assert store.recommendations_page(second["id"])["total"] == 0
    store.review_recommendations(stamp=datetime.now(TZ) + timedelta(hours=2))
    assert store.recommendation(first["id"], a["id"])["review_status"] == "valid"
    history = store.recommendations_page(first["id"], view="history")
    assert history["total"] == 1 and history["items"][0]["review_status"] == "expired"
    with pytest.raises(ValueError):
        store.retry_recommendation_review(first["id"], history["items"][0]["id"])


def test_active_pages_and_direct_event_context_cover_beyond_first_page(store):
    account = store.save_account(AccountInput(name="多页受控账户", holdings=[]))
    publish(store, suggestions=[suggestion(f"SH:{600000 + n}") for n in range(65)])
    page, ids, offsets = store.recommendations_page(account["id"], limit=20), set(), []
    while True:
        offsets.append(page["offset"])
        ids.update(item["id"] for item in page["items"])
        assert page["total"] == 65
        if page["next_offset"] is None:
            break
        page = store.recommendations_page(account["id"], limit=20, offset=page["next_offset"])
    assert len(ids) == 65 and offsets == [0, 20, 40, 60]
    overflow = store.recommendations_page(account["id"], limit=20, offset=100)
    assert overflow["items"] == [] and overflow["total"] == 65 and not overflow["has_more"]
    old = next(item for item in store.recommendations(account["id"], limit=100) if item["security_id"] == "SH:600000")
    stamp = datetime.now(TZ)
    store.review_recommendations({"SH:600000": {"status": "ok", "price": "120", "as_of": stamp.isoformat()}}, stamp)
    job = store.claim_jobs()[0]
    context = store.recommendation_context(account["id"], job["id"], limit=30)
    assert old["id"] not in {item["id"] for item in context["items"]}
    assert old["id"] in {item["id"] for item in context["event_linked_items"]}
    assert context["total"] == 65 and context["next_offset"] == 30
    assert len(store.recommendation_symbols(account["id"])) == 65
    assert store.recommendations_page(account["id"], view="triggered")["total"] == 1
    assert store.recommendations_page(account["id"], view="needs_review")["total"] == 1


def test_restart_and_repeated_omissions_do_not_create_new_review_work(store):
    account = store.save_account(AccountInput(name="重启受控账户", holdings=[]))
    publish(store, suggestions=[suggestion()])
    rid = store.recommendations(account["id"])[0]["id"]
    publish(store, account["id"])
    store.dispatch_demands()
    follow = store.claim_jobs()[0]
    assert store.publish(follow["id"], {"report": "复核未给出明确处理"}, None)
    before_demands = store.demands(account["id"])
    before_events = store.recommendation_events(rid)
    reopened = Store(store.path)
    assert reopened.demands(account["id"]) == before_demands
    assert reopened.recommendation_events(rid) == before_events
    for _ in range(3):
        publish(reopened, account["id"])
        reopened.dispatch_demands()
        assert not reopened.claim_jobs()
    assert len(reopened.demands(account["id"])) == len(before_demands)
    assert reopened.recommendation(account["id"], rid)["review_followup"]["state"] == "waiting_review"


def test_material_event_replay_cannot_undo_explicit_review(store):
    account = store.save_account(AccountInput(name="事件重放受控账户", holdings=[]))
    publish(store, suggestions=[suggestion()])
    rid = store.recommendations(account["id"])[0]["id"]
    store.review_recommendations(material_ids=["controlled-public-document"])
    job = store.claim_jobs()[0]
    assert store.publish(job["id"], {"report": "复核材料后维持", "maintained_suggestion_ids": [rid]}, None)
    count = len(store.recommendation_events(rid))
    for _ in range(4):
        store.review_recommendations(material_ids=["controlled-public-document"])
    assert store.recommendation(account["id"], rid)["review_status"] == "valid"
    assert not store.claim_jobs() and len(store.recommendation_events(rid)) == count


def test_material_event_deduplication_survives_changed_notification_batches(store):
    account = store.save_account(AccountInput(name="变更通知批次受控账户", holdings=[]))
    publish(store, suggestions=[suggestion()])
    rid = store.recommendations(account["id"])[0]["id"]
    store.review_recommendations(material_ids=["controlled-material-A", "controlled-material-B"])
    job = store.claim_jobs()[0]
    assert store.publish(job["id"], {"report": "已复核 A 和 B", "maintained_suggestion_ids": [rid]}, None)
    demands_before = store.demands(account["id"])
    events_before = store.recommendation_events(rid)
    store.review_recommendations(material_ids=["controlled-material-A"])
    assert store.recommendation(account["id"], rid)["review_status"] == "valid"
    assert store.demands(account["id"]) == demands_before
    assert store.recommendation_events(rid) == events_before
    assert not store.claim_jobs()
    store.review_recommendations(material_ids=["controlled-material-A", "controlled-material-C"])
    demands = store.demands(account["id"])
    added = [d for d in demands if d["id"] not in {prior["id"] for prior in demands_before}]
    assert len(added) == 1 and added[0]["event_key"].endswith(":material:controlled-material-C")
    assert "controlled-material-A" not in added[0]["reason"]
    assert store.recommendation(account["id"], rid)["review_status"] == "needs_review"
    reopened = Store(store.path)
    reopened.review_recommendations(material_ids=["controlled-material-C", "controlled-material-B", "controlled-material-A"])
    assert len(reopened.demands(account["id"])) == len(demands)


@pytest.mark.parametrize("quote_status,time_known", [("ok", True), ("closed", True), ("ok", False), ("error_cached", True)])
def test_stale_quote_does_not_claim_price_or_judgment_validity(store, quote_status, time_known):
    account = store.save_account(AccountInput(name="过期行情受控账户", holdings=[]))
    publish(store, suggestions=[suggestion()])
    stamp = datetime.now(TZ)
    quote = {"SH:600519": {"status": quote_status, "price": "120",
                           "as_of": (stamp - timedelta(days=1)).isoformat() if time_known else None}}
    store.review_recommendations(quote, stamp)
    item = store.recommendations(account["id"])[0]
    assert item["price_state"] == "stale" and item["review_status"] == "needs_review"
    assert store.recommendations_page(account["id"], view="triggered")["total"] == 0
    assert item["review_followup"] is None
    assert not store.demands(account["id"]) and not store.claim_jobs()
    assert store.analyses(account["id"])[0]["status"] == "current"
    events = store.recommendation_events(item["id"])
    reopened = Store(store.path)
    for _ in range(3):
        reopened.review_recommendations(quote, stamp)
    assert not reopened.demands(account["id"]) and not reopened.claim_jobs()
    assert reopened.recommendation_events(item["id"]) == events


def test_previously_fresh_quote_becoming_stale_creates_one_durable_review(store):
    account = store.save_account(AccountInput(name="真实行情退化受控账户", holdings=[]))
    publish(store, suggestions=[suggestion()])
    rid = store.recommendations(account["id"])[0]["id"]
    stamp = datetime.now(TZ)
    quote = {"SH:600519": {"status": "ok", "price": "100", "as_of": stamp.isoformat()}}
    store.review_recommendations(quote, stamp)
    assert store.recommendation(account["id"], rid)["price_state"] == "not_met"
    assert not store.demands(account["id"])
    stale_at = stamp + timedelta(seconds=store.settings()["quote_stale_seconds"] + 1)
    store.review_recommendations(quote, stale_at)
    demand = store.demands(account["id"])[0]
    assert demand["state"] == "assigned" and demand["event_key"].endswith(":stale:v1")
    reopened = Store(store.path)
    for _ in range(3):
        reopened.review_recommendations(quote, stale_at)
    assert reopened.demands(account["id"]) == [demand]
    follow = reopened.claim_jobs()[0]
    assert reopened.publish(follow["id"], {"report": "受控复核维持", "maintained_suggestion_ids": [rid]}, None)
    reopened.review_recommendations(quote, stale_at)
    assert not reopened.claim_jobs() and len(reopened.demands(account["id"])) == 1
    assert reopened.recommendation(account["id"], rid)["price_state"] == "stale"


@pytest.mark.parametrize("new_event", ["manual", "input", "material", "price_crossing"])
def test_initial_stale_baseline_keeps_real_review_triggers(store, new_event):
    account = store.save_account(AccountInput(name="首次旧行情后真实事件", holdings=[]))
    publish(store, suggestions=[suggestion()])
    rid = store.recommendations(account["id"])[0]["id"]
    stamp = datetime.now(TZ)
    store.review_recommendations({"SH:600519": {"status": "closed", "price": "120",
                                               "as_of": (stamp - timedelta(days=1)).isoformat()}}, stamp)
    assert not store.demands(account["id"])
    if new_event == "manual":
        store.retry_recommendation_review(account["id"], rid)
    elif new_event == "input":
        save_snapshot_correction(store, AccountInput(name="明确修改后受控账户", holdings=[]), account["id"])
    elif new_event == "material":
        store.review_recommendations(material_ids=["controlled-new-material-after-stale-baseline"])
    else:
        store.review_recommendations({"SH:600519": {"status": "ok", "price": "120", "as_of": stamp.isoformat()}}, stamp)
    assert store.demands(account["id"]) and len(store.claim_jobs()) == 1


@pytest.mark.asyncio
async def test_production_research_tools_paginate_and_enforce_account_scope(store):
    from app.service import ResearchTools
    first = store.save_account(AccountInput(name="工具 A", holdings=[]))
    job = publish(store, suggestions=[suggestion("SH:600000"), suggestion("SH:600001"), suggestion("SH:600002")])
    tools = ResearchTools(store, job, {})
    page = await tools.list_recommendations(limit=2)
    assert page["total"] == 3 and page["next_offset"] == 2
    next_page = await tools.list_recommendations(limit=2, offset=page["next_offset"])
    assert len(next_page["items"]) == 1 and next_page["next_offset"] is None
    rid = page["items"][0]["id"]
    assert (await tools.get_recommendation(rid))["account_id"] == first["id"]
    second = store.save_account(AccountInput(name="工具 B", holdings=[]))
    foreign = ResearchTools(store, store.job(second["latest_job_id"]), {})
    with pytest.raises(KeyError):
        await foreign.get_recommendation(rid)
    public = ResearchTools(store, {"id": "public-controlled-fixture"}, {})
    assert (await public.list_recommendations())["total"] == 0
    with pytest.raises(KeyError):
        await public.get_recommendation(rid)


def test_legacy_migration_preserves_id_and_unknown_price_observation(store):
    from app.store import dumps, loads
    account = store.save_account(AccountInput(name="迁移受控账户", holdings=[]))
    publish(store, suggestions=[suggestion()])
    rid = store.recommendations(account["id"])[0]["id"]
    with store.connect() as db:
        row = db.execute("SELECT body FROM recommendations WHERE id=?", (rid,)).fetchone()
        body = loads(row[0])
        body.pop("review_status")
        body.pop("price_state")
        body["state"] = body["status"] = "needs_review"
        db.execute("UPDATE recommendations SET body=?,state='needs_review' WHERE id=?", (dumps(body), rid))
    reopened = Store(store.path)
    migrated = reopened.recommendation(account["id"], rid)
    assert migrated["id"] == rid and migrated["price_state"] == "unknown"
    assert migrated["review_status"] == "needs_review" and "observed_price" not in migrated
    demands = reopened.demands(account["id"])
    assert len(demands) == 1 and demands[0]["event_key"].endswith("legacy:v1")
    again = Store(store.path)
    assert again.demands(account["id"]) == demands
    assert again.recommendation(account["id"], rid)["updated_at"] == migrated["updated_at"]


def test_replacement_lineage_and_untrusted_runtime_fields(store):
    account = store.save_account(AccountInput(name="替代关系受控账户", holdings=[]))
    publish(store, suggestions=[suggestion(price_crossing="untrusted", review_event_key="foreign-key")])
    original = store.recommendations(account["id"])[0]
    assert "price_crossing" not in original and "review_event_key" not in original
    publish(store, account["id"], suggestions=[suggestion(basis="新版本依据")])
    new = store.recommendations_page(account["id"])["items"][0]
    historical = store.recommendation(account["id"], original["id"])
    assert new["replaces_ids"] == [original["id"]]
    assert historical["replaced_by_id"] == new["id"] and historical["state"] == "revoked"


@pytest.mark.parametrize("resolution", ["maintain", "update", "replace"])
def test_inflight_price_event_is_not_consumed_by_an_older_research_context(store, resolution):
    account = store.save_account(AccountInput(name="运行期间新事件受控账户", holdings=[]))
    publish(store, suggestions=[suggestion()])
    original = store.recommendations_page(account["id"])["items"][0]
    store.enqueue(account["id"], "manual")
    running = store.claim_jobs()[0]
    before = store.recommendation_context(account["id"], running["id"])
    assert before["items"][0]["price_state"] == "unknown"
    stamp = datetime.now(TZ)
    quote = {"SH:600519": {"price": "120", "status": "ok", "as_of": stamp.isoformat()}}
    store.review_recommendations(quote, stamp)
    event = store.demands(account["id"])[0]
    assert event["state"] == "pending" and event["job_id"] is None
    result = {"report": "基于事件发生前已取得上下文形成的报告"}
    if resolution == "maintain":
        result["maintained_suggestion_ids"] = [original["id"]]
    else:
        result["suggestions"] = [suggestion(**({"id": original["id"]} if resolution == "update" else {}))]
    assert store.publish(running["id"], result, None)
    after = next(d for d in store.demands(account["id"]) if d["id"] == event["id"])
    assert after["state"] == "pending", "后到事件未被旧任务研究，不可静默完成"
    active = store.recommendations_page(account["id"])["items"][0]
    assert active["review_status"] == "needs_review"
    store.dispatch_demands()
    follow = store.claim_jobs()[0]
    linked = store.recommendation_context(account["id"], follow["id"])["event_linked_items"]
    assert active["id"] in {item["id"] for item in linked}
    covered = [row['id'] for row in store.task_demands(follow['id'], account['id'])['items']]
    assert store.publish(follow["id"], {"report": "已处理本轮新价格事件", "maintained_suggestion_ids": [active["id"]],
                                      'covered_demand_ids': covered}, None)
    assert store.recommendation(account["id"], active["id"])["review_status"] == "valid"
    assert all(d["state"] == "completed" for d in store.demands(account["id"]))
    store.review_recommendations(quote, stamp)
    store.dispatch_demands()
    assert not store.claim_jobs(), "被实际复核的同一价格事件不应再次自触发"
