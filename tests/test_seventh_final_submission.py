"""Terminal tool turns are atomic protocol commits, not prose-quality gates.

Controlled HTTP fixtures exercise the real adapter and runtime. They do not
certify model research quality or contact a provider.
"""
import json

import httpx
import pytest

from app import agents
from app.model import ModelClient, ModelError


def call(name, args, identity):
    return {"type": "function_call", "call_id": identity, "name": name,
            "arguments": json.dumps(args, ensure_ascii=False)}


def response(*calls):
    return httpx.Response(200, json={"status": "completed", "output": list(calls)})


def configure(monkeypatch, handler, **overrides):
    monkeypatch.setenv("FINAL_SUBMISSION_TEST_KEY", "controlled-test-only")
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(agents, "ModelClient", lambda config, before_request=None:
                        ModelClient(config, before_request, transport))
    return {"model_base_url": "https://model.example/v1", "model_name": "protocol-fixture",
            "model_key_env": "FINAL_SUBMISSION_TEST_KEY", "model_protocol": "responses",
            "model_native_search": False, "model_retries": 0, "max_model_calls": 8,
            "max_agent_turns": 4, "agent_timeout_seconds": 2,
            "analysis_timeout_seconds": 4, **overrides}


class Tools:
    def __init__(self):
        self.saved = []

    async def save_material(self, material):
        self.saved.append(material)
        return {"id": "m-side-effect", **material}


def assert_rejected_results(payload, expected_ids):
    results = {item["call_id"]: json.loads(item["output"]) for item in payload["input"]
               if item.get("type") == "function_call_output"}
    assert expected_ids <= results.keys()
    for identity in expected_ids:
        assert results[identity]["error_code"] == "invalid_terminal_submission"
        assert results[identity]["accepted"] is False
        assert results[identity]["failed"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["duplicate", "terminal_then_work", "work_then_terminal"])
async def test_invalid_terminal_turn_is_atomic_then_can_correct(monkeypatch, case):
    tools, events, payloads = Tools(), [], []
    rejected = [call("finish_analysis", {"report": f"Uncommitted candidate {i}"}, f"final-{i}")
                for i in range(3)]
    if case != "duplicate":
        rejected = [rejected[0], call("save_material", {"material": {"text": "Must not be saved"}}, "save")]
        if case == "work_then_terminal":
            rejected.reverse()

    async def handler(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        if len(payloads) == 1:
            return response(*rejected)
        assert_rejected_results(payload, {item["call_id"] for item in rejected})
        assert not tools.saved
        assert not any(event["type"] == "agent_completed" for event in events)
        return response(call("finish_analysis", {"report": "资料不足，暂不作方向判断。"}, "corrected"))

    async def emit(event):
        events.append(event)

    result = await agents.run_research({"task_id": "terminal-atomic"},
                                      configure(monkeypatch, handler), tools, emit)
    assert result["report"] == "资料不足，暂不作方向判断。"
    assert len(payloads) == 2 and not tools.saved
    assert len([event for event in events if event["type"] == "agent_completed"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("limits", [{"max_agent_turns": 1}, {"max_model_calls": 1}])
async def test_invalid_terminal_cannot_publish_when_correction_budget_exhausted(monkeypatch, limits):
    events, payloads = [], []

    async def handler(request):
        payloads.append(json.loads(request.content))
        return response(call("finish_analysis", {"report": "First uncommitted candidate"}, "first"),
                        call("finish_analysis", {"report": "Last uncommitted candidate"}, "last"))

    async def emit(event):
        events.append(event)

    with pytest.raises(ModelError):
        await agents.run_research({"task_id": "terminal-no-budget"},
                                  configure(monkeypatch, handler, **limits), Tools(), emit)
    assert len(payloads) == 1
    assert not any(event["type"] == "agent_completed" for event in events)
    results = [event["payload"]["result"] for event in events if event["type"] == "tool_result"]
    assert len(results) == 2
    assert all(result.get("error_code") == "invalid_terminal_submission" for result in results)


@pytest.mark.asyncio
async def test_invalid_terminal_clears_old_reference_candidate(monkeypatch):
    payloads, events = [], []
    tools = Tools()

    async def resolve_references(args):
        return {**args, "reference_summary": {"unresolved": 1}}

    tools.resolve_references = resolve_references

    async def handler(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        if len(payloads) == 1:
            return response(call("finish_analysis", {"report": "Earlier citation candidate",
                                                      "material_ids": ["unknown"]}, "citation"))
        if len(payloads) == 2:
            return response(call("finish_analysis", {"report": "Ambiguous first"}, "first"),
                            call("finish_analysis", {"report": "Ambiguous last"}, "last"))
        assert_rejected_results(payload, {"first", "last"})
        raise httpx.ReadTimeout("controlled correction failure", request=request)

    async def emit(event):
        events.append(event)

    with pytest.raises(ModelError):
        await agents.run_research({"task_id": "terminal-no-old-candidate"},
                                  configure(monkeypatch, handler), tools, emit)
    assert len(payloads) == 3
    assert not any(event["type"] == "agent_completed" for event in events)


@pytest.mark.asyncio
async def test_child_terminal_uses_same_single_submission_contract(monkeypatch):
    payloads, events = [], []

    async def handler(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        if len(payloads) == 1:
            return response(call("submit_report", {"summary": "First uncommitted"}, "first"),
                            call("submit_report", {"summary": "Last uncommitted"}, "last"))
        assert_rejected_results(payload, {"first", "last"})
        return response(call("submit_report", {"summary": "可见来源未披露日期，保持未知。"}, "corrected"))

    async def emit(event):
        events.append(event)

    runtime = agents.ResearchRuntime({"task_id": "child-terminal"},
                                     configure(monkeypatch, handler), Tools(), emit)
    try:
        spawned = await runtime._spawn({"task": "Check available source"})
        child = runtime.children[spawned["agent_id"]]
        await child.task_handle
        assert child.status == "completed"
        assert len(child.reports) == 1
        assert child.reports[0]["summary"] == "可见来源未披露日期，保持未知。"
        assert len(payloads) == 2
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("report", ["未知。", "资料不足，暂不作方向判断。", "研究说明：存在占位数据，应保留缺口。"])
async def test_single_terminal_report_has_no_length_or_keyword_gate(monkeypatch, report):
    payloads = []

    async def handler(request):
        payloads.append(json.loads(request.content))
        return response(call("finish_analysis", {"report": report}, "single"))

    async def emit(event):
        pass

    result = await agents.run_research({"task_id": "terminal-valid"},
                                      configure(monkeypatch, handler), Tools(), emit)
    assert result["report"] == report
    assert len(payloads) == 1
