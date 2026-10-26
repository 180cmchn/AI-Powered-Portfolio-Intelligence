"""Controlled bytes and production collection/storage; no live search or model."""
import asyncio
import threading

import pytest

from app import providers
from app.domain import AccountInput
from app.service import ResearchTools
from app.store import Store


URL = "https://controlled.example.test/fourth.html"


@pytest.fixture
def store(tmp_path):
    providers._DOCUMENT_CACHE.clear()
    value = Store(tmp_path / "fourth-materials.sqlite3")
    yield value
    providers._DOCUMENT_CACHE.clear()


def raw(content, **kwargs):
    return {"scope": "public", "url": URL, "text": content, "document_id": content,
            "access": "full_text", "truncated": False, **kwargs}


def observe(store, content, **kwargs):
    token = store.begin_material_observation(URL)
    return store.save_material(raw(content, **kwargs), notify=True, observation_id=token)


def test_late_old_observation_ignores_source_clock_and_preserves_rollback(store):
    first = observe(store, "A")
    older = store.begin_material_observation(URL)
    second = observe(store, "B")
    store.complete_material_notifications([first["id"], second["id"]])
    late = store.save_material(raw("A", fetched_at="2099-12-31", published_at="2099-12-31"),
                               notify=True, observation_id=older)
    assert late["id"] == first["id"]
    assert late["observation_result_status"] == "late_observation"
    assert not late["_updated"] and not late["is_latest_document_observation"]
    assert not store.pending_material_notifications()
    rollback = observe(store, "A", fetched_at="1900-01-01")
    assert rollback["version"] == 3 and rollback["previous_id"] == second["id"]
    assert rollback["change_kind"] == "original_content_changed"
    assert rollback["id"] != first["id"]
    assert store.pending_material_notifications() == [rollback["id"]]


def test_late_previously_unseen_bytes_stay_retrievable_without_latest_or_notice(store):
    older = store.begin_material_observation(URL)
    current = observe(store, "B")
    store.complete_material_notifications([current["id"]])
    late = store.save_material(raw("previously unseen A"), notify=True, observation_id=older)
    assert late["version"] == 0 and late["previous_id"] is None
    assert late["change_kind"] == "original_content_observed"
    assert not late["_updated"] and not late["is_latest_document_observation"]
    assert store.material_range(late["id"])["text"] == "previously unseen A"
    index = {item["id"]: item for item in store.material_index()}
    assert index[current["id"]]["is_latest_document_observation"]
    assert not index[late["id"]]["is_latest_document_observation"]
    assert not store.pending_material_notifications()


def test_unknown_or_forged_order_never_claims_correction(store):
    first = observe(store, "A")
    store.complete_material_notifications([first["id"]])
    invalid = store.begin_material_observation("https://controlled.example.test/other")
    for content, keyword in (("B", None), ("C", "made-up-token"), ("D", invalid)):
        item = store.save_material(raw(content, observation_sequence=99999,
            observation_status="ordered", _observation_id=invalid), notify=True, observation_id=keyword)
        assert item["observation_status"] == "unverified_order"
        assert item["version"] == 0 and not item["_updated"]
        assert "observation_sequence" not in item
        assert item["change_kind"] == "original_content_observed"
    assert not store.pending_material_notifications()


def test_restart_old_cache_ranges_and_token_reuse_cannot_advance(store):
    token = store.begin_material_observation(URL)
    first = store.save_material(raw("A", text="first page"), observation_id=token)
    second = observe(store, "B")
    reopened = Store(store.path)
    cached = reopened.save_material(raw("A", text="different page", cache_hit=True,
        returned_range={"start_page": 8, "end_page": 8}), notify=True, observation_id=token)
    assert cached["document_version_id"] == first["document_version_id"]
    assert cached["id"] != first["id"] and cached["version"] == 1
    assert cached["observation_result_status"] == "late_observation"
    assert cached["change_kind"] == "original_content_observed"
    assert reopened.material_range(cached["id"])["returned_range"]["start_page"] == 8
    forged = reopened.save_material(raw("C"), notify=True, observation_id=token)
    assert forged["version"] == 0 and forged["observation_status"] == "unverified_order"
    rollback = observe(reopened, "A")
    assert rollback["version"] == 3 and rollback["previous_id"] == second["id"]


@pytest.mark.asyncio
async def test_real_provider_research_tools_out_of_order_parsing_cache_restart(store, monkeypatch):
    calls = 0
    entered, release = threading.Event(), threading.Event()
    original = providers._extract_document
    async def fetch(url, config):
        nonlocal calls
        calls += 1
        return (b"<article>B</article>" if calls == 3 else b"<article>A</article>"), "text/html", url
    def extract(body, mime, config, **options):
        if config.get("controlled_slow_parse"):
            entered.set()
            assert release.wait(5), "controlled delayed parser not released"
        return original(body, mime, config, **options)
    monkeypatch.setattr(providers, "_fetch_public", fetch)
    monkeypatch.setattr(providers, "_extract_document", extract)
    account = store.save_account(AccountInput(name="controlled collection"))
    job = store.job(account["latest_job_id"])
    tools = ResearchTools(store, job, {})
    first = await tools.read(URL)
    slow_tools = ResearchTools(store, job, {"controlled_slow_parse": True})
    late_task = asyncio.create_task(slow_tools.read(URL, refresh=True))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        current = await tools.read(URL, refresh=True)
    finally:
        release.set()
    late = await late_task
    assert calls == 3
    assert late["material_id"] == first["material_id"]
    assert store.material(current["material_id"])["version"] == 2
    reopened = Store(store.path)
    cached = await ResearchTools(reopened, job, {}).read(URL)
    assert calls == 3 and cached["material_id"] == current["material_id"]
    rollback = await ResearchTools(reopened, job, {}).read(URL, refresh=True)
    assert calls == 4 and store.material(rollback["material_id"])["version"] == 3


@pytest.mark.asyncio
async def test_parent_alias_union_identity_permissions_and_unresolved_gaps(store):
    a = store.save_account(AccountInput(name="A"))
    b = store.save_account(AccountInput(name="B"))
    tools = ResearchTools(store, store.job(a["latest_job_id"]), {})
    source_a = observe(store, "first original")
    source_b = observe(store, "changed original")
    private = store.save_material({"scope": "account", "text": "B private secret", "title": "secret title"}, account_id=b["id"])
    base = {"url": URL, "text": "same extraction"}
    first = await tools.save_material({**base, "source_material_ids": [source_a["id"]]})
    second = await tools.save_material({**base, "source_material_ids": [source_b["id"]]})
    canonical = await tools.save_material({**base, "material_ids": [source_b["id"]]})
    assert first["id"] != second["id"] == canonical["id"]
    parents = sorted([source_a["id"], source_b["id"], private["id"], "missing-parent"])
    mixed = await tools.save_material({**base, "material_ids": [source_b["id"], source_a["id"], private["id"]],
        "source_material_ids": ["missing-parent", source_a["id"]]})
    again = await tools.save_material({**base, "source_material_ids": list(reversed(parents)) + parents})
    assert mixed["id"] == again["id"]
    assert mixed["material_ids"] == mixed["source_material_ids"] == parents
    result = await tools.resolve_references({"source_material_ids": [mixed["id"]]})
    refs = {ref["material_id"]: ref for ref in result["references"][0]["origin_references"]}
    assert refs[source_a["id"]]["document_version"] == 1
    assert refs[source_b["id"]]["document_version"] == 2
    assert refs[private["id"]]["reason"] == refs["missing-parent"]["reason"] == "unknown_or_inaccessible"
    assert "secret" not in str(result)
    assert not store.material_accessible(private["id"], a["id"])
    assert store.material(first["id"])["material_ids"] == [source_a["id"]]


@pytest.mark.asyncio
async def test_native_sources_are_parent_evidence_with_only_visible_scope(store):
    tools = ResearchTools(store, {"id": "controlled-public-job", "account_id": None}, {})
    rows = await tools.record_native_search("child-1", [{"type": "web_search_call"}], [
        {"url": URL, "title": "link only"},
        {"url": URL + "?excerpt", "title": "excerpt", "snippet": "visible excerpt"}], [])
    claim = await tools.save_material({"text": "generated claim", "source_material_ids": [row["id"] for row in rows]})
    result = await tools.resolve_references({"material_ids": [claim["id"]]})
    refs = {ref["material_id"]: ref for ref in result["references"][0]["origin_references"]}
    assert refs[rows[0]["id"]]["status"] == "source_link"
    assert refs[rows[1]["id"]]["document_identity_kind"] == "visible_excerpt_sha256"
    assert refs[rows[1]["id"]]["range"]["read_completeness"] == "visible_excerpt"
    for row in rows:
        assert "document_id" not in store.material(row["id"])
        assert row["representation_kind"] == "native_search_source"
    assert claim["id"] not in store.pending_material_notifications()
    citations = await tools.resolve_references({"report": f"Links [one]({URL}#citation) [two]({URL}?excerpt)",
        "native_annotations": [{"url": URL + "?excerpt"}]})
    mapped = {ref["material_id"]: ref for ref in citations["references"]}
    assert mapped[rows[0]["id"]]["status"] == "source_link"
    assert mapped[rows[0]["id"]]["citation_url"] == URL + "#citation"
    assert mapped[rows[1]["id"]]["status"] == "resolved"
    assert citations["native_annotations"][0]["references"][0]["material_id"] == rows[1]["id"]


def test_native_citation_url_lookup_never_uses_inaccessible_private_sources(store):
    private = store.save_material({"scope": "account", "representation": "native_search_source",
        "url": URL, "access": "search_excerpt", "text": "private snippet", "title": "private title"}, account_id="B")
    result = store.resolve_result_references({"report": "Citation " + URL}, account_id="A")
    assert result["references"] == [{"url": URL, "status": "source_link", "reason": "not_located_in_saved_material"}]
    assert private["id"] not in str(result) and "private" not in str(result)
