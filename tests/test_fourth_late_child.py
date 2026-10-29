from tests.helpers_sixth import configure_test_team
"""Late child completion is durable and final updates stay within existing limits."""
import asyncio
import json
from copy import deepcopy

import httpx
import pytest

from app import agents
from app.domain import AccountInput
from app.model import ModelClient, ModelError
from app.service import Service
from app.store import Store


def config(monkeypatch, **extra):
    monkeypatch.setenv("LATE_CHILD_TEST_KEY", "controlled-not-real")
    return {"model_base_url": "https://model.example/v1", "model_name": "late-child-fixture",
            "model_key_env": "LATE_CHILD_TEST_KEY", "model_protocol": "responses",
            "model_native_search": True, "model_retries": 0, "max_model_calls": 30,
            "max_agent_turns": 18, "max_concurrent_agents": 2, "agent_timeout_seconds": 3,
            "analysis_timeout_seconds": 5, "synthesis_request_timeout_seconds": 1, **extra}


def response(tool, args):
    return httpx.Response(200, json={"status": "completed", "output": [{"type": "function_call",
        "call_id": tool, "name": tool, "arguments": json.dumps(args)}]})


def install(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(agents, "ModelClient", lambda cfg, before_request=None: ModelClient(cfg, before_request, transport))


class Tools:
    def __init__(self):
        self.checkpoints = []

    async def save_checkpoint(self, checkpoint):
        self.checkpoints.append(deepcopy(checkpoint))


@pytest.mark.asyncio
async def test_child_checkpoint_survives_parent_inflight_failure_and_manual_resume(tmp_path, monkeypatch):
    cfg = config(monkeypatch, quote_provider="manual", debounce_seconds=0, synthesis_max_calls=4)
    store = Store(tmp_path / "late-child.sqlite3")
    store.set_meta("settings", cfg)
    configure_test_team(store, cfg)
    account = store.save_account(AccountInput(name="Late child recovery", holdings=[]))
    parent_started = asyncio.Event()
    manager_requests = 0
    recovered = []
    first = store.claim_jobs(account_id=account["id"])[0]

    async def handler(request):
        nonlocal manager_requests
        payload = json.loads(request.content)
        seed = json.loads(payload["input"][0]["content"])
        if seed.get("synthesis_checkpoint"):
            recovered.append(payload)
            assert all(tool["type"] != "web_search" and tool.get("name") != "spawn_research" for tool in payload["tools"])
            assert "Completed while parent HTTP was pending" in json.dumps(payload)
            return response("finish_analysis", {"report": "Recovered from visible child checkpoint; source date remains unknown."})
        if seed.get("task"):
            await parent_started.wait()
            return response("submit_report", {"summary": "Completed while parent HTTP was pending", "open_questions": ["Source date remains unknown"]})
        manager_requests += 1
        if manager_requests == 1:
            return response("spawn_research", {"role_id": "filing_researcher", "task": "Inspect original source"})
        parent_started.set()
        for _ in range(100):
            if store.checkpoint_status(first["id"])["available"]:
                break
            await asyncio.sleep(.005)
        assert store.checkpoint_status(first["id"])["available"]
        raise httpx.ReadTimeout("controlled parent request failure", request=request)

    install(monkeypatch, handler)
    service = Service(store)
    await service.execute(first)
    assert store.job(first["id"])["state"] == "failed"
    assert not store.analyses(account["id"])
    with store.connect() as db:
        body = json.loads(db.execute("SELECT body FROM synthesis_checkpoints WHERE job_id=?", (first["id"],)).fetchone()[0])
    assert body["reports"][0]["summary"] == "Completed while parent HTTP was pending"
    assert body["unresolved_tasks"] == []
    assert "encrypted_content" not in json.dumps(body)
    store.enqueue(account["id"], "manual")
    second = store.claim_jobs(account_id=account["id"])[0]
    await service.execute(second)
    assert store.job(second["id"])["state"] == "completed"
    assert len(recovered) == 1 and manager_requests == 2


@pytest.mark.parametrize("case", ["one_call_left", "plaintext_final", "no_calls_left", "no_turns_left", "update_failure"])
@pytest.mark.asyncio
async def test_late_final_update_is_bounded_and_unincorporated_results_are_explicit(case, monkeypatch):
    cfg = config(monkeypatch, max_agent_turns=1 if case == "no_turns_left" else 18)
    parent_started, child_started, child_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    payloads, events, tools = [], [], Tools()
    manager_requests = 0
    if case == "update_failure":
        resolutions = 0
        async def resolve_references(args):
            nonlocal resolutions
            resolutions += 1
            return {**args, "reference_summary": {"unresolved": int(resolutions == 1)}}
        tools.resolve_references = resolve_references

    async def emit(event):
        events.append(event)
        if event["type"] == "agent_completed" and event["agent_id"] != "manager":
            assert tools.checkpoints  # Checkpoint persisted before completion is announced.
            child_done.set()

    async def handler(request):
        nonlocal manager_requests
        payload = json.loads(request.content)
        seed = json.loads(payload["input"][0]["content"])
        payloads.append(payload)
        if seed.get("task"):
            child_started.set()
            await parent_started.wait()
            return response("submit_report", {"summary": "Late visible issuer report", "material_ids": ["source-1"],
                "open_questions": ["Publication date remains unknown"]})
        manager_requests += 1
        if manager_requests == 1:
            if case == "update_failure":
                # Retain citation feedback from one legal terminal turn. The
                # child completes during the next request, triggering a final
                # update whose failure must not fall back to this stale draft.
                return response("finish_analysis", {"report": "Earlier final with citation gap",
                                                     "material_ids": ["missing"]})
            parent_started.set()
            await child_done.wait()
            if case == "plaintext_final":
                return httpx.Response(200, json={"status": "completed", "output": [{"type": "message", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": "Child has not delivered; issuer conclusion remains unknown."}]}]})
            return response("finish_analysis", {"report": "Child has not delivered; issuer conclusion remains unknown."})
        if case == "update_failure" and manager_requests == 2:
            parent_started.set()
            await child_done.wait()
            return response("finish_analysis", {"report": "Child has not delivered; issuer conclusion remains unknown."})
        if case == "update_failure":
            assert manager_requests == 3
            assert {tool.get("name") for tool in payload["tools"]} == {"finish_analysis"}
            assert "Late visible issuer report" in json.dumps(payload)
            assert "Publication date remains unknown" in json.dumps(payload)
            raise httpx.ReadTimeout("controlled final update failure", request=request)
        assert manager_requests == 2
        assert {tool.get("name") for tool in payload["tools"]} == {"finish_analysis"}
        assert "Late visible issuer report" in json.dumps(payload)
        assert "Publication date remains unknown" in json.dumps(payload)
        return response("finish_analysis", {"report": "Updated report incorporates the late issuer findings; publication date remains unknown.", "material_ids": ["source-1"]})

    install(monkeypatch, handler)
    runtime = agents.ResearchRuntime({"task_id": "late-bounded"}, cfg, tools, emit)
    runtime.closeout_waited = True  # The bounded wait already expired, as in the real race.
    created = await runtime._spawn({"task": "Issuer evidence", "title": "Issuer child"})
    runtime.calls = 26 if case == "update_failure" else (28 if case == "no_calls_left" else 27)
    if case == "no_calls_left":
        await child_started.wait()  # Its in-flight request uses slot 29; parent final uses 30.
    if case == "update_failure":
        with pytest.raises(ModelError, match="网络请求失败"):
            await runtime.run()
        assert tools.checkpoints and not any(e["type"] == "agent_completed" and e["agent_id"] == "manager" for e in events)
    else:
        result = await runtime.run()
        assert result["agents"][0]["status"] == "completed"
        if case in {"one_call_left", "plaintext_final"}:
            assert result["model_calls"] == 30 and manager_requests == 2
            assert result["late_child_update_performed"] is True
            assert result["report"].startswith("Updated report")
            assert not result.get("unincorporated_child_reports")
        else:
            assert manager_requests == 1
            assert result["model_calls"] == (30 if case == "no_calls_left" else 29)
            assert result["unincorporated_child_reports"][0]["agent_id"] == created["agent_id"]
            assert result["unincorporated_child_reports"][0]["material_ids"] == ["source-1"]
            assert "正文尚未吸收" in result["report"] and result["uncertainties"]
    assert runtime.calls <= 30 and len(payloads) <= (4 if case == "update_failure" else 3)


@pytest.mark.asyncio
async def test_second_late_child_does_not_start_an_unbounded_final_update_chain(monkeypatch):
    cfg = config(monkeypatch)
    parent_started, update_started = asyncio.Event(), asyncio.Event()
    child_done = {"First": asyncio.Event(), "Second": asyncio.Event()}
    tools, events = Tools(), []
    manager_requests = 0

    async def emit(event):
        events.append(event)
        if event["type"] == "agent_completed" and event["agent_id"] != "manager":
            child_done[runtime.children[event["agent_id"]].title].set()

    async def handler(request):
        nonlocal manager_requests
        payload = json.loads(request.content)
        seed = json.loads(payload["input"][0]["content"])
        if seed.get("task"):
            await (parent_started if seed["task"] == "First" else update_started).wait()
            return response("submit_report", {"summary": seed["task"] + " report", "open_questions": ["Remaining disclosure gap"]})
        manager_requests += 1
        if manager_requests == 1:
            parent_started.set()
            await child_done["First"].wait()
            return response("finish_analysis", {"report": "Initial report without the first late delivery."})
        assert manager_requests == 2
        update_started.set()
        await child_done["Second"].wait()
        return response("finish_analysis", {"report": "First child incorporated; second child not yet reviewed."})

    install(monkeypatch, handler)
    runtime = agents.ResearchRuntime({"task_id": "late-chain"}, cfg, tools, emit)
    runtime.closeout_waited = True
    await runtime._spawn({"task": "First", "title": "First"})
    second = await runtime._spawn({"task": "Second", "title": "Second"})
    runtime.calls = 25
    result = await runtime.run()
    assert manager_requests == 2 and result["model_calls"] == 29
    assert result["late_child_update_performed"] is True
    assert result["unincorporated_child_reports"][0]["agent_id"] == second["agent_id"]
    assert any(len(checkpoint["reports"]) == 1 and checkpoint["unresolved_tasks"] for checkpoint in tools.checkpoints)
    assert len(tools.checkpoints[-1]["reports"]) == 2 and tools.checkpoints[-1]["unresolved_tasks"] == []


@pytest.mark.asyncio
async def test_child_checkpoint_update_preserves_declared_manager_evidence(monkeypatch):
    cfg = config(monkeypatch)
    tools = Tools()
    async def emit(event):
        pass
    async def handler(request):
        return response("submit_report", {"summary": "Child completed report", "material_ids": ["child-source"],
            "open_questions": ["Child source publication time unknown"]})
    install(monkeypatch, handler)
    runtime = agents.ResearchRuntime({"task_id": "current-task", "input_version": 9}, cfg, tools, emit)
    declaration = {"summary": "Visible manager findings", "material_ids": ["manager-source"],
                   "open_questions": ["Manager disclosure gap remains"]}
    try:
        await runtime._save_checkpoint(declaration)
        child = await runtime._spawn({"task": "Existing supplemental research"})
        await runtime.children[child["agent_id"]].task_handle
        saved = tools.checkpoints[-1]
        assert saved["manager_summary"] == declaration
        assert set(saved["material_ids"]) == {"manager-source", "child-source"}
        assert saved["open_questions"] == ["Manager disclosure gap remains", "Child source publication time unknown"]
        assert saved["task_id"] == "current-task" and saved["input_version"] == 9
        replacement = {"summary": "Updated explicit declaration", "material_ids": [], "open_questions": ["Updated gap"]}
        await runtime._save_checkpoint(replacement)
        assert tools.checkpoints[-1]["manager_summary"] == replacement
        assert "manager-source" not in tools.checkpoints[-1]["material_ids"]
    finally:
        await runtime.close()
