from tests.helpers_sixth import configure_test_team
"""Production Service + Runtime + SQLite recovery; only model HTTP is scripted."""
import json
from copy import deepcopy
from datetime import datetime, timedelta

import httpx
import pytest

from app import agents
from app.domain import AccountInput, TZ
from app.model import ModelClient
from app.service import Service
from app.store import Store


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("RECOVERY_MODEL_KEY", "recovery-test-not-real")
    store = Store(tmp_path / "recovery.sqlite3")
    store.set_meta("settings", {"model_name": "recovery-fixture", "model_base_url": "https://model.example/v1",
        "model_key_env": "RECOVERY_MODEL_KEY", "model_protocol": "responses", "model_native_search": True,
        "quote_provider": "manual", "debounce_seconds": 0, "model_retries": 0,
        "model_request_timeout_seconds": 1, "synthesis_request_timeout_seconds": 1,
        "agent_timeout_seconds": 3, "analysis_timeout_seconds": 5, "synthesis_max_calls": 2})
    configure_test_team(store, store.settings())
    return store


def call(name, args=None):
    return {"type": "function_call", "call_id": name, "name": name, "arguments": json.dumps(args or {})}


def response(*output):
    return httpx.Response(200, json={"id": "fixture-response", "status": "completed", "output": list(output)})


def install(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(agents, "ModelClient", lambda cfg, before_request=None: ModelClient(cfg, before_request, transport))


@pytest.mark.asyncio
async def test_failed_event_driven_research_manual_retry_resumes_only_synthesis(store, monkeypatch):
    account = store.save_account(AccountInput(name="Recovery A", holdings=[]))
    store.request_analysis(account["id"], "public_information", "immutable-incoming-event")
    state = {"discovered": False, "child_done": False, "native_requests": 0, "recovery_requests": 0}

    async def handler(request):
        payload = json.loads(request.content)
        initial = json.loads(payload["input"][0]["content"])
        if initial.get("synthesis_checkpoint"):
            state["recovery_requests"] += 1
            assert all(tool["type"] != "web_search" and tool.get("name") != "spawn_research" for tool in payload["tools"])
            assert "Visible finished child report" in json.dumps(payload)
            return response(call("finish_analysis", {"report": "Complete recovered account report", "material_ids": initial["synthesis_checkpoint"]["material_ids"]}))
        state["native_requests"] += 1
        if "task" in initial:
            state["child_done"] = True
            return response(call("submit_report", {"summary": "Visible finished child report", "open_questions": ["Publication date not known"]}))
        if not state["discovered"]:
            state["discovered"] = True
            return response({"type": "web_search_call", "id": "native-source-call", "status": "completed", "action": {
                "type": "search", "query": "company disclosure", "sources": [{"url": "https://issuer.example/report", "title": "Discovered source"}]}},
                call("spawn_research", {"role_id": "filing_researcher", "task": "Inspect existing visible report"}))
        if not state["child_done"]:
            return response(call("wait_agents", {"timeout_seconds": 0.05}))
        raise httpx.ReadTimeout("controlled synthesis failure", request=request)

    install(monkeypatch, handler)
    service = Service(store)
    first = store.claim_jobs(account_id=account["id"])[0]
    await service.execute(first)
    assert store.job(first["id"])["state"] == "failed"
    assert store.job(first["id"])["synthesis_checkpoint"]["available"]
    assert not store.analyses(account["id"])
    assert store.meta("native_search_capability")["status"] == "request_failed"
    with store.connect() as db:
        body = json.loads(db.execute("SELECT body FROM synthesis_checkpoints WHERE job_id=?", (first["id"],)).fetchone()[0])
    assert body["reports"][0]["summary"] == "Visible finished child report"
    assert body["material_ids"] and "encrypted_content" not in json.dumps(body)
    source = store.material(body["material_ids"][0])
    assert source["access"] == "source_link"
    before = state["native_requests"]
    store.enqueue(account["id"], "manual")
    second = store.claim_jobs(account_id=account["id"])[0]
    await service.execute(second)
    assert store.job(second["id"])["state"] == "completed"
    assert state["native_requests"] == before and state["recovery_requests"] == 1
    report = store.analyses(account["id"])[0]
    assert report["status"] == "current" and report["result"]["synthesis_recovered"]
    assert report["result"]["input_version"] == account["version"]
    assert any(event["type"] == "synthesis_resumed" for event in store.job(second["id"], events=True)["events"])
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM synthesis_checkpoints").fetchone()[0] == 1
        assert db.execute("SELECT resume_count FROM synthesis_checkpoints").fetchone()[0] == 2  # Success consumes the source checkpoint.


def checkpoint_fixture(store, *, scope="account"):
    account = store.save_account(AccountInput(name="Guard A", holdings=[]))
    job = store.claim_jobs(account_id=account["id"])[0]
    source = store.save_material({"url": "https://issuer.example/source", "text": "Observed source", "scope": "public"})
    quote = {"price": "10", "as_of": "2026-10-26T15:00:00+08:00", "source": "controlled", "status": "closed"}
    context = {"scope": scope, "account": account if scope == "account" else None,
        "universe_version": store.universe_snapshot()["universe_version"], "metrics": {"positions": []},
        "candidate_quotes": {"SH:600519": deepcopy(quote)}, "quotes": {"SZ:000001": deepcopy(quote)}}
    body = {"phase": "synthesis", "task_id": job["id"], "reports": [{"summary": "Finished report"}],
        "material_ids": [source["id"]]}
    store.save_synthesis_checkpoint(job, body, context)
    store.set_job_state(job["id"], "failed", "controlled failure")
    return account, job, context, source


@pytest.mark.parametrize("change", ["account", "universe", "event", "ttl", "candidate_quote", "global_quote", "missing_source"])
def test_checkpoint_refuses_changed_inputs_or_missing_source(store, change):
    account, job, context, source = checkpoint_fixture(store)
    if change == "account":
        account = store.save_account(AccountInput(id=account["id"], version=account["version"], name="Guard A", holdings=[], cash="99"))
        context["account"] = account
        job = store.job(account["latest_job_id"])
    elif change == "universe":
        store.save_universe({"watchlist": [], "candidates": ["SZ:000001"]})
        context["universe_version"] = store.universe_snapshot()["universe_version"]
    elif change == "event":
        store.save_material({"scope": "public", "url": "https://issuer.example/new-event", "text": "New event"}, notify=True)
    elif change == "ttl":
        with store.connect() as db:
            db.execute("UPDATE synthesis_checkpoints SET created_at=?", ((datetime.now(TZ) - timedelta(hours=2)).isoformat(),))
    elif change == "candidate_quote":
        context["candidate_quotes"]["SH:600519"]["price"] = "11"
    elif change == "global_quote":
        context["quotes"]["SZ:000001"]["price"] = "11"
    elif change == "missing_source":
        with store.connect() as db:
            db.execute("DELETE FROM materials WHERE id=?", (source["id"],))
    assert store.reusable_synthesis_checkpoint(job, context) is None


def test_same_checkpoint_resume_cap_survives_restart_and_failed_retry_jobs(store):
    account, job, context, _ = checkpoint_fixture(store)
    for attempt in (1, 2):
        store.enqueue(account["id"], "manual")
        retry = store.claim_jobs(account_id=account["id"])[0]
        checkpoint = store.reusable_synthesis_checkpoint(retry, context)
        assert checkpoint["checkpoint_job_id"] == job["id"]
        assert checkpoint["resume_attempt"] == attempt
        store.set_job_state(retry["id"], "failed", "controlled repeated synthesis failure")
        store = Store(store.path)
    store.enqueue(account["id"], "manual")
    retry = store.claim_jobs(account_id=account["id"])[0]
    assert store.reusable_synthesis_checkpoint(retry, context) is None
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM synthesis_checkpoints").fetchone()[0] == 1


def test_other_account_cannot_reuse_checkpoint(store):
    _, _, context, _ = checkpoint_fixture(store)
    account_b = store.save_account(AccountInput(name="Guard B", holdings=[]))
    job_b = store.claim_jobs(account_id=account_b["id"])[0]
    assert store.reusable_synthesis_checkpoint(job_b, {**context, "account": account_b}) is None
