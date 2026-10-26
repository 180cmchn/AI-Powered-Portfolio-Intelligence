"""Protocol substitutes and production runtime tests; not real-network evidence."""
import asyncio
import json

import httpx
import pytest

from app import agents
from app.model import ModelBudgetExceeded, ModelClient, ModelError, native_search_fingerprint, native_search_status, probe_native_search


@pytest.fixture
def config(monkeypatch, request):
    monkeypatch.setenv("FOURTH_MODEL_KEY", "test-credential-not-real")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    return {"model_base_url": "https://model.example/v1", "model_name": request.node.name,
            "model_key_env": "FOURTH_MODEL_KEY", "model_protocol": "responses",
            "model_native_search": True, "model_retries": 0, "max_agent_turns": 12,
            "model_request_timeout_seconds": 1, "analysis_timeout_seconds": 3,
            "synthesis_max_calls": 2, "search_provider": "disabled"}


def call(name, args=None, identifier=None):
    return {"type": "function_call", "call_id": identifier or name, "name": name, "arguments": json.dumps(args or {})}


def message(text, annotations=None):
    return {"type": "message", "role": "assistant", "id": "visible-message", "content": [
        {"type": "output_text", "text": text, "annotations": annotations or []}]}


def response(*items):
    return httpx.Response(200, headers={"x-request-id": "request-observable"}, json={
        "status": "completed", "id": "response-observable", "output": list(items),
        "usage": {"input_tokens": 10, "output_tokens": 8}})


SEARCH = {"type": "web_search_call", "id": "ws-visible", "status": "completed", "action": {
    "type": "search", "query": "company report", "sources": [
        {"url": "https://issuer.example/report", "title": "Issuer report"},
        {"url": "https://issuer.example/excerpt", "title": "Excerpt", "snippet": "Visible source excerpt"}]}}
ANNOTATION = {"type": "url_citation", "url": "https://issuer.example/report", "title": "Issuer report", "start_index": 0, "end_index": 5}
CONTEXT = {"task_id": "test-task", "account": {"id": "a", "version": 4}, "input_version": 4,
           "metrics": {"securities_value": "1000"}, "data_as_of": "2026-10-26T10:00:00+08:00"}


@pytest.mark.asyncio
async def test_native_and_local_calls_share_session_without_search_key(config):
    payloads = []
    opaque = {"type": "reasoning", "encrypted_content": "not-visible-reasoning", "summary": []}

    async def handler(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        assert {"type": "web_search"} in payload["tools"]
        assert "web_search_call.action.sources" in payload["include"]
        assert request.headers["authorization"] == "Bearer test-credential-not-real"
        if len(payloads) == 1:
            return response(opaque, SEARCH, message("Source", [ANNOTATION]), call("calculate", {"values": [2, 3]}))
        assert opaque in payload["input"] and SEARCH in payload["input"]
        assert any(item.get("type") == "function_call_output" for item in payload["input"])
        return response(message("Final"))

    client = ModelClient(config, transport=httpx.MockTransport(handler))
    session = client.session("Research", CONTEXT)
    session.agent_id, session.phase = "research-1", "research"
    try:
        turn = await client.complete(session, [{"name": "calculate", "parameters": {"type": "object"}}])
        assert [call.name for call in turn.calls] == ["calculate"]
        assert turn.native_events[0]["id"] == "ws-visible"
        assert turn.sources[0] == {"url": "https://issuer.example/report", "title": "Issuer report"}
        assert turn.sources[1]["snippet"] == "Visible source excerpt"
        assert turn.annotations[0]["start_index"] == 0
        assert client.native_status()["status"] == "observed"
        client.add_results(session, [(turn.calls[0], {"result": 5})])
        assert (await client.complete(session, [])).text == "Final"
        request = session.requests[0]
        assert request["agent_id"] == "research-1" and request["stage"] == "research"
        assert request["input_chars"] > 0 and request["output_bytes"] > 0
        assert request["request_id"] == "request-observable" and request["attempt"] == 1
        assert "not-visible-reasoning" not in json.dumps(session.requests)
        assert "test-credential-not-real" not in json.dumps(session.requests)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_native_only_intermediate_is_not_final_report(config):
    client = ModelClient(config, transport=httpx.MockTransport(lambda request: response(SEARCH)))
    try:
        turn = await client.complete(client.session("Research", {}), [])
        assert turn.continuation_required and not turn.calls and not turn.text
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", [{"phase": "commentary"}, {"status": "in_progress"}, {"status": "incomplete"}])
async def test_explicit_intermediate_message_continues_and_replays_metadata(config, marker):
    intermediate = {**message("I will inspect the report", [ANNOTATION]), **marker}
    seen = []

    def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        if len(seen) == 1:
            return response(SEARCH, intermediate)
        assert intermediate in payload["input"]
        return response({**message("Completed final research"), "phase": "final_answer", "status": "completed"})

    client = ModelClient(config, transport=httpx.MockTransport(handler))
    session = client.session("Research", CONTEXT)
    try:
        first = await client.complete(session, [])
        assert first.continuation_required and first.text == ""
        assert first.sources and first.annotations
        second = await client.complete(session, [])
        assert second.text == "Completed final research" and not second.continuation_required
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_commentary_is_excluded_from_same_response_final_text(config):
    client = ModelClient(config, transport=httpx.MockTransport(lambda request: response(
        {**message("Working update"), "phase": "commentary"},
        {**message("Final answer"), "phase": "final_answer"})))
    try:
        turn = await client.complete(client.session("Research", {}), [])
        assert turn.text == "Final answer" and not turn.continuation_required
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_commentary_only_does_not_complete_runtime_and_remains_budget_bounded(config, monkeypatch):
    events = []
    install(monkeypatch, lambda request: response({**message("Still checking"), "phase": "commentary"}))

    async def emit(event):
        events.append(event)

    with pytest.raises(ModelBudgetExceeded):
        await agents.run_research(CONTEXT, {**config, "max_model_calls": 2}, Tools(), emit)
    assert not any(event["type"] == "agent_completed" for event in events)


@pytest.mark.asyncio
async def test_phase_omitted_or_null_remains_compatible_final_text(config):
    for extra in ({}, {"phase": None}):
        client = ModelClient(config, transport=httpx.MockTransport(lambda request: response({**message("Ordinary final"), **extra})))
        try:
            turn = await client.complete(client.session("Research", {}), [])
            assert turn.text == "Ordinary final" and not turn.continuation_required
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_incomplete_final_keeps_finished_search_sources_without_publishing_partial(config, monkeypatch):
    tools, events = Tools(), []
    install(monkeypatch, lambda request: httpx.Response(200, json={"status": "incomplete",
        "output": [SEARCH, message("HALF A REPORT"), call("finish_analysis", {"report": "HALF A REPORT"})]}))

    async def emit(event):
        events.append(event)

    with pytest.raises(ModelError, match="未完成"):
        await agents.run_research(CONTEXT, config, tools, emit)
    assert tools.native and tools.native[0][2][0]["url"] == "https://issuer.example/report"
    assert not any(event["type"] == "agent_completed" for event in events)
    assert "HALF A REPORT" not in json.dumps(events)


@pytest.mark.asyncio
@pytest.mark.parametrize("code,body,expected", [
    (400, {"error": {"message": "web_search unsupported"}}, "unsupported"),
    (401, {"error": {"message": "bad authorization"}}, "authentication_failed"),
    (503, {"error": {"message": "unavailable"}}, "request_failed"),
])
async def test_probe_distinguishes_failures_without_fallback(config, code, body, expected):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(code, json=body)

    result = await probe_native_search(config, transport=httpx.MockTransport(handler))
    assert result["status"] == expected and not result["ok"]
    assert seen == ["https://model.example/v1/responses"]


@pytest.mark.asyncio
async def test_ignored_native_tool_or_freely_generated_link_is_unverified(config):
    result = await probe_native_search(config, transport=httpx.MockTransport(
        lambda request: response(message("I searched https://issuer.example/report", [ANNOTATION]))))
    assert result["status"] == "unverified" and not result["ok"]
    assert result["sources"] and not result["native_events"]


def test_explicit_disable_and_protocol_boundaries(config, monkeypatch):
    assert native_search_status({**config, "model_native_search": False})["status"] == "disabled"
    assert native_search_status({**config, "model_protocol": "chat_completions"})["status"] == "unsupported"
    assert native_search_status(config)["status"] == "unverified"
    fingerprint = native_search_fingerprint(config)
    monkeypatch.setenv("FOURTH_MODEL_KEY", "changed")
    assert fingerprint != native_search_fingerprint(config)


class Tools:
    def __init__(self):
        self.native = []
        self.checkpoints = []

    async def record_native_search(self, agent_id, events, sources, annotations):
        self.native.append((agent_id, events, sources, annotations))
        return [{"id": "native-source", "url": sources[0]["url"], "access": "native_search_link"}]

    async def save_checkpoint(self, checkpoint):
        self.checkpoints.append(checkpoint)

    async def calculate(self, operation, values):
        return {"result": "5"}


def install(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(agents, "ModelClient", lambda cfg, before_request=None: ModelClient(cfg, before_request, transport))


@pytest.mark.asyncio
async def test_manager_and_child_native_sources_enter_visible_recovery_checkpoint(config, monkeypatch):
    tools, events = Tools(), []
    state = {"child": None, "child_done": False, "manager_calls": 0, "child_calls": 0}

    async def emit(event):
        events.append(event)
        if event["type"] == "agent_created":
            state["child"] = event["agent_id"]
        if event["type"] == "agent_completed" and event["agent_id"] != "manager":
            state["child_done"] = True

    async def handler(request):
        payload = json.loads(request.content)
        assert {"type": "web_search"} in payload["tools"]
        initial = json.loads(payload["input"][0]["content"])
        if "task" in initial:
            state["child_calls"] += 1
            if state["child_calls"] == 1:
                return response(SEARCH)  # must continue, not mark child complete
            assert "native-source" in json.dumps(payload)
            return response(call("submit_report", {"summary": "Visible child result", "material_ids": ["native-source"], "open_questions": ["Unknown publication time"]}))
        state["manager_calls"] += 1
        if not state["child"]:
            return response(SEARCH, call("spawn_research", {"task": "Find company report"}))
        if not state["child_done"]:
            return response(call("wait_agents", {"timeout_seconds": 0.05}))
        raise httpx.ReadTimeout("test bounded synthesis timeout", request=request)

    install(monkeypatch, handler)
    with pytest.raises(ModelError):
        await agents.run_research(CONTEXT, config, tools, emit)
    assert {row[0] for row in tools.native} == {"manager", state["child"]}
    assert tools.checkpoints and tools.checkpoints[-1]["reports"][0]["summary"] == "Visible child result"
    assert tools.checkpoints[-1]["material_ids"] == ["native-source"]
    assert "Unknown publication time" in tools.checkpoints[-1]["open_questions"]
    assert not any(event["type"] == "agent_completed" and event["agent_id"] == "manager" for event in events)
    assert any(event["type"] == "model_error" and event["payload"]["requests"][0]["stage"] == "synthesis" for event in events)
    assert "encrypted_content" not in json.dumps(tools.checkpoints)


@pytest.mark.asyncio
async def test_recovery_uses_fresh_context_existing_results_no_research_calls(config, monkeypatch):
    checkpoint = {"task_id": "old-task", "phase": "synthesis", "reports": [{"summary": "Recovered fact"}], "material_ids": ["m-1"]}
    seen, events = [], []

    def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        assert all(tool["type"] != "web_search" and tool.get("name") != "spawn_research" for tool in payload["tools"])
        assert "Recovered fact" in json.dumps(payload) and '"version": 4' in json.loads(payload["input"][0]["content"]).__str__().replace("'", '"')
        return response(call("finish_analysis", {"report": "Complete recovered final report", "material_ids": ["m-1"]}))

    async def emit(event):
        events.append(event)

    install(monkeypatch, handler)
    result = await agents.run_research({**CONTEXT, "synthesis_checkpoint": checkpoint}, config, Tools(), emit)
    assert result["synthesis_recovered"] is True and result["input_version"] == 4
    assert len(seen) == 1 and result["model_calls"] == 1
    assert any(event["type"] == "synthesis_resumed" for event in events)


@pytest.mark.asyncio
async def test_recovery_stops_at_finite_request_budget(config, monkeypatch):
    install(monkeypatch, lambda request: response(call("calculate", {"operation": "sum", "values": [2, 3]})))

    async def emit(event):
        pass

    with pytest.raises(ModelBudgetExceeded, match="汇总恢复"):
        await agents.run_research({**CONTEXT, "synthesis_checkpoint": {"phase": "synthesis"}}, config, Tools(), emit)


@pytest.mark.asyncio
async def test_explicit_synthesis_compacts_context_at_resolved_function_boundary(config, monkeypatch):
    tools, payloads = Tools(), []

    def handler(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        if len(payloads) == 1:
            return response(call("prepare_synthesis", {"summary": "Completed visible research", "material_ids": ["m-visible"]}))
        assert all(item.get("type") != "function_call_output" for item in payload["input"])
        assert all(tool.get("type") != "web_search" for tool in payload["tools"])
        assert "Completed visible research" in json.dumps(payload)
        return response(call("finish_analysis", {"report": "Final report"}))

    async def emit(event):
        pass

    install(monkeypatch, handler)
    result = await agents.run_research(CONTEXT, config, tools, emit)
    assert result["report"] == "Final report" and len(payloads) == 2
    assert tools.checkpoints[0]["material_ids"] == ["m-visible"]


@pytest.mark.asyncio
async def test_failed_followup_remains_unresolved_beside_previous_report(config, monkeypatch):
    tools, events = Tools(), []
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return response(call("submit_report", {"summary": "Original completed report"}))
        payload = json.loads(request.content)
        assert "Verify subsequent correction" in json.dumps(payload)
        raise httpx.ReadTimeout("controlled follow-up failure", request=request)

    async def emit(event):
        events.append(event)

    install(monkeypatch, handler)
    runtime = agents.ResearchRuntime(CONTEXT, config, tools, emit)
    try:
        result = await runtime._spawn({"task": "Read initial disclosure", "title": "Disclosure"})
        child = runtime.children[result["agent_id"]]
        await child.task_handle
        assert child.status == "completed" and child.reports[0]["research_round"] == 1
        await runtime._dispatch("manager", "follow_up", {"agent_id": child.id, "task": "Verify subsequent correction"})
        await child.task_handle
        assert child.status == "failed" and not child.inbox  # delivered, but failed
        checkpoint = await runtime._save_checkpoint()
        assert checkpoint["reports"][0]["summary"] == "Original completed report"
        assert checkpoint["reports"][0]["research_round"] == 1
        unresolved = checkpoint["unresolved_tasks"][0]
        assert unresolved["round"] == 2 and unresolved["status"] == "failed"
        assert unresolved["task"] == "Read initial disclosure"
        assert unresolved["pending_messages"] == ["Verify subsequent correction"]
        assert "ReadTimeout" in unresolved["error"]
        assert "encrypted_content" not in json.dumps(checkpoint)
    finally:
        await runtime.close()
