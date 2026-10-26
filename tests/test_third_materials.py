"""Round-three controlled material identity/delivery regressions; no network or model."""
import pytest

from app.domain import AccountInput
from app.service import ResearchTools, Service
from app.store import Store, dumps


@pytest.fixture
def store(tmp_path, monkeypatch):
    for module in ("app.materials", "app.store", "app.scheduling"):
        monkeypatch.setattr(module + ".now", lambda: "2026-10-25T09:30:00+08:00")
    return Store(tmp_path / "controlled-materials.sqlite3")


def document(text, **extra):
    return {"scope": "public", "url": "https://controlled.example.test/report.pdf",
            "document_id": "controlled-original-byte-fingerprint-v1", "text": text,
            "access": "excerpt", "offset": 0, "truncated": True,
            "requested_range": {"start_page": 1, "end_page": 20}, **extra}


def collected(store, material, **options):
    """Controlled collector starts a trusted observation before saving bytes."""
    token = store.begin_material_observation(material["url"])
    return store.save_material(material, observation_id=token, **options)


def test_read_lengths_share_document_version_and_preserve_representations(store):
    short = store.save_material(document("x" * 12000), notify=True)
    long = store.save_material(document("x" * 40000), notify=True)
    again = store.save_material(document("x" * 12000), notify=True)
    assert [short["version"], long["version"], again["version"]] == [1, 1, 1]
    assert short["id"] == again["id"] != long["id"]
    assert short["document_version_id"] == long["document_version_id"]
    assert not long["_updated"] and not long["_document_created"]
    assert store.pending_material_notifications() == [short["id"]]
    assert store.material_range(long["id"], 30000, 100)["text"] == "x" * 100


def test_pages_offsets_are_retrievable_under_same_original_version(store):
    page1 = store.save_material(document("page-one", returned_range={"start_page": 1, "end_page": 1}))
    page7 = store.save_material(document("page-seven", offset=90,
        requested_range={"start_page": 7, "end_page": 8}, returned_range={"start_page": 7, "end_page": 7}))
    assert page1["document_version_id"] == page7["document_version_id"]
    fragment = store.material_range(page7["id"])
    assert fragment["offset"] == 90 and fragment["returned_range"]["start_page"] == 7
    assert fragment["text"] == "page-seven" and fragment["source_truncated"]


def test_existing_material_can_register_one_notification_after_agent_read(store):
    one = store.save_material(document("agent-read"), notify=False)
    two = store.save_material(document("agent-read"), notify=True)
    assert two["id"] == one["id"] and not two["_created"]
    assert store.pending_material_notifications() == [one["id"]]
    Service(store).notify_material_changes()
    assert store.pending_material_notifications() == [one["id"]]
    Service(store).route_material_changes("controlled-public-report", {}, [one["id"]])
    for _ in range(3):
        store.save_material(document("agent-read"), notify=True)
    assert not store.pending_material_notifications()
    assert len([d for d in store.demands() if d["kind"] == "public_information"]) == 1


def test_original_summary_and_model_extraction_have_independent_identity(store):
    raw = store.save_material(document("原文"), notify=True)
    summary = store.save_material({"scope": "public", "url": raw["url"],
        "content": "摘要", "access": "search_excerpt"}, notify=True)
    changed = store.save_material({"scope": "public", "url": raw["url"],
        "content": "摘要变化", "access": "search_excerpt"}, notify=True)
    extraction = store.save_material({**document("Agent 提取"), "representation": "agent_extraction"}, notify=True)
    generated = store.save_material({"scope": "public", "url": raw["url"],
        "content": "Agent 总结", "classification": "agent_analysis", "access": "generated_report"}, notify=True)
    assert raw["version"] == summary["version"] == extraction["version"] == generated["version"] == 1
    assert changed["version"] == 2 and changed["change_kind"] == "summary_changed"
    assert summary["document_identity_kind"] == "representation_sha256"
    assert raw["document_identity_kind"] == "raw_bytes_sha256"
    assert extraction["document_version_id"] != raw["document_version_id"]
    assert extraction["id"] not in store.pending_material_notifications()
    assert generated["id"] not in store.pending_material_notifications()


def test_real_original_change_even_same_url_publication_and_excerpt(store):
    first = collected(store, document("same page", published_at="2026-10-20"), notify=True)
    second = collected(store, document("same page", published_at="2026-10-20",
        document_id="controlled-original-byte-fingerprint-v2"), notify=True)
    assert second["version"] == 2 and second["previous_id"] == first["id"]
    assert second["change_kind"] == "original_content_changed"
    assert second["document_version_id"] != first["document_version_id"]
    assert len(store.pending_material_notifications()) == 2


def test_notification_partial_delivery_recovers_without_duplicate_demands(store, monkeypatch):
    first = store.save_account(AccountInput(name="controlled-account-one"))
    second = store.save_account(AccountInput(name="controlled-account-two"))
    saved = store.save_material(document("controlled notification"), notify=False)
    store.save_material(document("controlled notification"), notify=True)
    original = store.request_analysis
    def interrupt(account_id, *args, **kwargs):
        if account_id == second["id"]:
            raise RuntimeError("controlled interruption after another target persisted")
        return original(account_id, *args, **kwargs)
    monkeypatch.setattr(store, "request_analysis", interrupt)
    Service(store).notify_material_changes()
    with pytest.raises(RuntimeError, match="controlled interruption"):
        Service(store).route_material_changes("controlled-public-report", {}, [saved["id"]])
    assert store.pending_material_notifications() == [saved["id"]]
    restarted = Store(store.path)
    Service(restarted).notify_material_changes()
    Service(restarted).route_material_changes("controlled-public-report", {}, [saved["id"]])
    Service(restarted).route_material_changes("controlled-public-report", {}, [saved["id"]])
    demands = [d for d in restarted.demands() if d["kind"] == "public_information"]
    assert sorted(d["account_id"] for d in demands) == sorted(["", first["id"], second["id"]])
    assert not restarted.pending_material_notifications()


@pytest.mark.asyncio
async def test_agent_then_scan_real_service_path_reuses_original_and_backfills(store, monkeypatch):
    from app import providers
    account = store.save_account(AccountInput(name="controlled-real-code-path"))
    cfg = {**store.settings(), "news_queries": ["controlled fixture"], "read_max_chars": 40000}
    store.set_meta("settings", cfg)
    async def fetch(url, config):
        return b"<article>" + b"x" * 40000 + b"</article>", "text/html", url
    monkeypatch.setattr(providers, "_fetch_public", fetch)
    providers._DOCUMENT_CACHE.clear()
    tools = ResearchTools(store, store.job(account["latest_job_id"]), cfg)
    first = await tools.read(document("")["url"])
    assert not store.pending_material_notifications()
    service = Service(store)
    scan = await service.news_scan()
    assert scan["state"] == "queued"
    public = ResearchTools(store, {"id": "controlled-public-job", "account_id": None}, cfg)
    await public.read(document("")["url"], offset=12000)
    await public.record_native_search("child", [{"type": "web_search_call"}], [
        {"url": document("")["url"], "snippet": "controlled search summary"}], [])
    service.notify_material_changes()
    assert len(store.pending_material_notifications()) == 2
    service.route_material_changes("controlled-public-report", {}, store.pending_material_notifications())
    second = await tools.read(document("")["url"])
    assert second["material_id"] == first["material_id"]
    original_reads = [m for m in store.material_index() if m["representation_kind"] == "original_document"]
    assert len(original_reads) == 2 and {m["char_count"] for m in original_reads} == {12000}
    assert {m["document_version"] for m in original_reads} == {1}
    demands = [d for d in store.demands(account["id"]) if d["kind"] == "public_information"]
    assert len(demands) == 2  # one summary and one original, independently typed
    for _ in range(5):
        await service.news_scan()
    assert len([d for d in store.demands(account["id"]) if d["kind"] == "public_information"]) == 2


def test_cached_old_original_is_not_misreported_as_publisher_reversion(store):
    first = collected(store, document("old"), notify=True)
    second = collected(store, document("new", document_id="controlled-v2"), notify=True)
    cached = store.save_material(document("old", cache_hit=True, fetched_at="2026-10-25T08:00:00+08:00"), notify=True)
    assert cached["id"] == first["id"] and not cached["_document_created"]
    reverted = collected(store, document("old", cache_hit=False), notify=True)
    assert reverted["version"] == 3 and reverted["previous_id"] == second["id"]


def test_migration_preserves_legacy_ids_unknown_identity_and_delivered_notifications(store):
    # Controlled v2 fixture: observed text has no saved raw-byte hash or full-read status.
    legacy = {"id": "controlled-legacy-id", "scope": "public", "url": "https://controlled.example.test/legacy",
              "content": "legacy observed text", "version": 7, "source_id": "controlled-old-source-key",
              "content_hash": "controlled-old-text-hash-is-not-original-byte-hash"}
    short = {**document("x" * 12000), "id": "controlled-short-id", "version": 1}
    long = {**document("x" * 40000), "id": "controlled-long-id", "version": 2,
            "previous_id": short["id"], "change_summary": "controlled legacy false length correction"}
    with store.connect() as db:
        for body in (legacy, short, long):
            db.execute("INSERT INTO materials VALUES(?,?,?,?)", (body["id"], body["id"], dumps(body), "2026-10-24T09:00:00+08:00"))
            db.execute("INSERT INTO material_catalog VALUES(?,?,?,?,?,?,?,?,?,?)",
                (body["id"], body.get("source_id", "controlled-v2-source"), body["version"], body.get("previous_id"),
                 body.get("content_hash", "controlled-v2-text-hash"), "", "public", "old index", "{}", len(body.get("content", body.get("text", "")))))
        db.execute("INSERT INTO material_notifications VALUES(?,'pending',?,NULL)", (short["id"], "2026-10-24"))
        db.execute("INSERT INTO material_notifications VALUES(?,'notified',?,?)", (long["id"], "2026-10-24", "2026-10-24"))
        db.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
    upgraded = Store(store.path)
    old = upgraded.material(legacy["id"])
    assert old["id"] == legacy["id"] and old["content"] == legacy["content"]
    assert not old.get("document_id") and old["document_identity_kind"] == "representation_sha256"
    assert old["read_completeness"] == "unknown" and old["legacy_version"] == 7
    assert upgraded.material(short["id"])["document_version_id"] == upgraded.material(long["id"])["document_version_id"]
    assert upgraded.material(long["id"])["legacy_previous_id"] == short["id"]
    assert upgraded.material(long["id"])["previous_id"] is None
    assert "change_summary" not in upgraded.material(long["id"])
    assert not upgraded.pending_material_notifications()
    reopened = Store(store.path)
    assert reopened.material(legacy["id"]) == old
    assert len(reopened.material_index()) == 3
    reopened.save_material(document("x" * 40000), notify=True)
    assert not reopened.pending_material_notifications()


def test_transaction_abort_leaves_no_orphan_version_or_notification(store, monkeypatch):
    original = store._catalog
    def fail(*args):
        raise RuntimeError("controlled transaction abort")
    monkeypatch.setattr(store, "_catalog", fail)
    with pytest.raises(RuntimeError, match="controlled transaction abort"):
        store.save_material(document("controlled failure"), notify=True)
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM material_document_versions").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM materials").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM material_notifications").fetchone()[0] == 0
    monkeypatch.setattr(store, "_catalog", original)
    assert store.save_material(document("controlled failure"), notify=True)["version"] == 1


def test_fetch_metadata_alone_never_versions_content_and_summary_only_is_labeled(store):
    first = store.save_material(document("controlled unchanged", fetched_at="2026-10-24", cache_hit=False), notify=True)
    changed = store.save_material(document("controlled unchanged", fetched_at="2026-10-25", retrieved_at="2026-10-25", cache_hit=True), notify=True)
    assert first["id"] == changed["id"] and not changed["_created"]
    assert len(store.pending_material_notifications()) == 1
    summary = store.save_material({"scope": "public", "url": first["url"], "summary": "only a summary"})
    assert summary["representation_kind"] == "summary"
    assert summary["document_identity_kind"] == "representation_sha256"
    assert summary["read_completeness"] == "unknown"
