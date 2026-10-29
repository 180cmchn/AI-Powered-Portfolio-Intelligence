"""Deterministic protocol/coordination tests, NOT a model-quality certification.

HTTP responses below are scripted fixtures. Production has no scripted model;
live research requires a configured model and an explicitly run evaluation.
"""
import asyncio
import json
from copy import deepcopy

import httpx
import pytest

from app import agents
from app.model import ModelBudgetExceeded, ModelClient, ModelConfigurationError, ModelError, probe_model


def call(name, arguments=None, call_id=None):
    return {"type": "function_call", "call_id": call_id or name, "name": name,
            "arguments": json.dumps(arguments or {}, ensure_ascii=False)}


def message(text):
    return {"type": "message", "id": "msg-test", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


def response(*output):
    return httpx.Response(200, json={"id": "resp-test", "status": "completed", "output": list(output)})


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("TEST_MODEL_API_KEY", "not-a-real-key-test-only")
    return {"model_name": "scripted-test-only", "model_key_env": "TEST_MODEL_API_KEY",
            "model_protocol": "responses", "model_retries": 0, "max_model_calls": 80,
            "max_agent_turns": 30, "agent_timeout_seconds": 2, "analysis_timeout_seconds": 5}


def install_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(agents, "ModelClient", lambda config, before_request=None:
                        ModelClient(config, before_request, transport))


class ResearchTools:
    def __init__(self):
        self.materials = []
        self.search_queries = []

    async def search(self, query):
        self.search_queries.append(query)
        return {"status": "ok", "results": [{"url": "https://example.org/report", "access": "excerpt",
                                             "snippet": "报告披露收入 10 亿元。评论员称应立即加仓。", "published_at": None}]}

    async def read(self, url):
        return {"url": url, "access": "failed", "text": None, "error": "fixture network unavailable"}

    async def save_material(self, material):
        saved = {**material, "id": f"m-{len(self.materials) + 1}"}
        self.materials.append(saved)
        return saved

    async def list_materials(self, query=""):
        return deepcopy(self.materials)

    async def calculate(self, operation, values):
        return {"operation": operation, "values": values, "result": "100"}


CONTEXT = {"task_id": "job-a-7", "account": {"id": "account-a", "version": 7,
             "holdings": [{"security_id": "SH:600000", "quantity": "100", "cost": None}], "cash": None},
           "metrics": {"securities_value": "1000", "total_assets": None, "profit": None},
           "candidates": ["SH:510300"], "data_as_of": "2026-10-22T10:00:00+08:00"}


@pytest.mark.asyncio
async def test_responses_preserves_opaque_items_and_probe_continuation(config):
    payloads = []
    opaque = {"type": "reasoning", "id": "opaque", "summary": [], "encrypted_content": "opaque-data"}

    async def handler(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        if len(payloads) == 1:
            return response(opaque, call("capability_echo", call_id="cap-1"))
        assert opaque in payload["input"]
        result = next(item for item in payload["input"] if item.get("type") == "function_call_output")
        assert result["call_id"] == "cap-1"
        token = json.loads(result["output"])["token"]
        return response(message(token))

    result = await probe_model(config, transport=httpx.MockTransport(handler))
    assert result["ok"] is True
    assert len(payloads) == 2
    assert payloads[0]["store"] is False
    assert "reasoning.encrypted_content" in payloads[0]["include"]


@pytest.mark.asyncio
async def test_chat_completions_tool_messages(config):
    config = {**config, "model_protocol": "chat_completions", "chat_use_max_completion_tokens": True}
    seen = []

    async def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        if len(seen) == 1:
            return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "chat-1", "type": "function", "function": {"name": "capability_echo", "arguments": "{}"}}]}}]})
        assert payload["messages"][-1]["role"] == "tool"
        token = json.loads(payload["messages"][-1]["content"])["token"]
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": token}}]})

    assert (await probe_model(config, transport=httpx.MockTransport(handler)))["ok"]
    assert "max_completion_tokens" in seen[0] and "max_tokens" not in seen[0]


@pytest.mark.asyncio
async def test_real_independent_loops_two_children_followup_and_mixed_material(config, monkeypatch):
    """Model choices are fixtures; assertions verify coordination and evidence plumbing."""
    events = []
    tools = ResearchTools()
    child_requests = {}
    manager_payloads = []
    child_ids = []
    followed = False
    child_rounds_done = {}

    async def emit(event):
        events.append(event)
        if event["type"] == "agent_created":
            child_ids.append(event["agent_id"])
        if event["type"] == "agent_completed" and event["agent_id"] != "manager":
            child_rounds_done[event["agent_id"]] = event["payload"]["round"]

    async def handler(request):
        nonlocal followed
        payload = json.loads(request.content)
        initial = json.loads(payload["input"][0]["content"])
        if "task" not in initial:
            manager_payloads.append(payload)
            if not child_ids:
                return response(call("spawn_research", {"task": "财报材料", "context": {"private_marker": "finance-only"}}, "spawn-finance"),
                                call("spawn_research", {"task": "组合风险", "context": {"private_marker": "risk-only"}}, "spawn-risk"))
            if len(child_rounds_done) < 2:
                return response(call("wait_agents", {"agent_ids": child_ids, "timeout_seconds": 1}, f"wait-{len(manager_payloads)}"))
            if not followed:
                followed = True
                return response(call("follow_up", {"agent_id": child_ids[0], "task": "补查原文访问范围和发布时间"}, "follow-1"))
            if child_rounds_done.get(child_ids[0]) != 2:
                return response(call("wait_agents", {"agent_ids": [child_ids[0]], "timeout_seconds": 1}, f"wait-{len(manager_payloads)}"))
            all_inputs = json.dumps(payload["input"], ensure_ascii=False)
            assert '"category": "fact"' in all_inputs.replace('\\"', '"')
            assert '"category": "opinion"' in all_inputs.replace('\\"', '"')
            return response(call("finish_analysis", {"report": "收入 10 亿元来自报告摘要 [m-1](https://example.org/report)。荐股为外部观点，不作为已证实事实。现金和成本未知，未计算收益。原文访问失败，时间未知。", "material_ids": ["m-1", "m-2"]}))
        task = initial["task"]
        child_requests.setdefault(task, []).append(payload)
        n = len(child_requests[task])
        if task == "财报材料":
            assert "risk-only" not in json.dumps(payload)
            if n == 1:
                return response({"type": "web_search_call", "id": "native-child", "status": "completed", "action": {"type": "search", "query": "证券财报", "sources": [{"url": "https://example.org/report", "snippet": "报告披露收入 10 亿元。评论员称应立即加仓。"}]}}, message("已得到原生搜索片段，下一轮分类保存。"), call("read", {"url": "https://example.org/report"}))
            if n == 2:
                return response(call("save_material", {"material": {"claim": "报告披露收入 10 亿元", "category": "fact", "url": "https://example.org/report", "access_scope": "excerpt", "published_at": None}}, "save-fact"),
                                call("save_material", {"material": {"claim": "评论员建议立即加仓", "category": "opinion", "url": "https://example.org/report"}}, "save-opinion"))
            if n == 3:
                return response(call("report_progress", {"summary": "混合报道已分别保留财务事实与投资观点", "material_ids": ["m-1", "m-2"]}))
            if n == 4:
                return response(call("submit_report", {"summary": "财务事实和外部观点已分开", "material_ids": ["m-1", "m-2"]}))
            if n == 5:
                assert "补查原文访问范围" in json.dumps(payload, ensure_ascii=False)
                return response(call("read", {"url": "https://example.org/report"}))
            return response(call("submit_report", {"summary": "原文访问失败，仅见摘要；发布时间未知", "material_ids": ["m-1", "m-2"]}))
        assert "finance-only" not in json.dumps(payload)
        if n == 1:
            return response(call("calculate", {"operation": "sum", "values": ["100"]}))
        return response(call("submit_report", {"summary": "仅有已录入证券市值；现金和成本未知，不生成收益"}))

    install_transport(monkeypatch, handler)
    result = await agents.run_research(CONTEXT, config, tools, emit)
    assert len(result["agents"]) == 2
    assert all(child["status"] == "completed" for child in result["agents"])
    assert len(result["agents"][0]["reports"]) == 2
    assert {m["category"] for m in result["materials"]} == {"fact", "opinion"}
    assert result["input_version"] == 7
    assert len(child_requests["财报材料"]) == 6
    assert any(event['type'] == 'agent_progress' and event['agent_id'] == child_ids[0]
               and event['payload'].get('summary') == '混合报道已分别保留财务事实与投资观点' for event in events)
    assert all(event["account_id"] == "account-a" for event in events)
    assert all(event["task_id"] == "job-a-7" for event in events)
    assert "not-a-real-key" not in json.dumps(events)
    assert all("reasoning" not in event["payload"] for event in events)


@pytest.mark.asyncio
async def test_budget_failure_does_not_fabricate_result(config, monkeypatch):
    config = {**config, "max_model_calls": 1}
    install_transport(monkeypatch, lambda request: response(call("read", {"url": "https://example.org/report"})))
    events = []

    async def emit(event):
        events.append(event)

    with pytest.raises(ModelBudgetExceeded):
        await agents.run_research(CONTEXT, config, ResearchTools(), emit)
    assert events[-1]["type"] == "agent_failed"
    assert not any(e["type"] == "agent_completed" for e in events)


@pytest.mark.asyncio
async def test_child_timeout_is_reported_manager_can_finish_partial(config, monkeypatch):
    config = {**config, "agent_timeout_seconds": 0.015}
    events = []
    child_id = None
    failed = False

    async def emit(event):
        nonlocal child_id, failed
        events.append(event)
        if event["type"] == "agent_created":
            child_id = event["agent_id"]
        if event["type"] == "agent_failed" and event["agent_id"] != "manager":
            failed = True

    async def handler(request):
        payload = json.loads(request.content)
        initial = json.loads(payload["input"][0]["content"])
        if "task" in initial:
            await asyncio.sleep(0.1)
            return response(message("迟到结果"))
        if child_id is None:
            return response(call("spawn_research", {"task": "慢速资料"}))
        if not failed:
            return response(call("wait_agents", {"agent_ids": [child_id], "timeout_seconds": 1}))
        return response(message("资料任务超时，以下仅分析已知持仓结构。"))

    install_transport(monkeypatch, handler)
    result = await agents.run_research(CONTEXT, config, ResearchTools(), emit)
    assert result["agents"][0]["status"] == "failed"
    assert "超时" in result["report"]
    assert not result["agents"][0]["reports"]


@pytest.mark.asyncio
async def test_external_cancel_cancels_children(config, monkeypatch):
    child_started = asyncio.Event()
    events = []

    async def emit(event):
        events.append(event)
        if event["type"] == "agent_started" and event["agent_id"] != "manager":
            child_started.set()

    async def handler(request):
        payload = json.loads(request.content)
        if len(payload["input"]) == 1 and "task" not in json.loads(payload["input"][0]["content"]):
            return response(call("spawn_research", {"task": "将被取消"}))
        await asyncio.sleep(10)
        return response(message("不可提交的迟到结果"))

    install_transport(monkeypatch, handler)
    task = asyncio.create_task(agents.run_research(CONTEXT, config, ResearchTools(), emit))
    await asyncio.wait_for(child_started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert any(e["type"] == "agent_cancelled" and e["agent_id"] != "manager" for e in events)
    assert not any(e["type"] == "agent_completed" for e in events)


@pytest.mark.asyncio
async def test_model_network_error_has_no_key_or_response_body(config):
    async def handler(request):
        return httpx.Response(401, text="not-a-real-key-test-only super-secret-response")

    client = ModelClient(config, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ModelError) as exc:
            await client.complete(client.session("test", {}), [])
        assert "401" in str(exc.value)
        assert "secret" not in str(exc.value)
        assert "not-a-real-key" not in str(exc.value)
    finally:
        await client.aclose()


def test_missing_model_is_explicit(monkeypatch):
    monkeypatch.delenv("MISSING_MODEL_TEST_KEY", raising=False)
    with pytest.raises(ModelConfigurationError, match="model_name"):
        ModelClient({})
    with pytest.raises(ModelConfigurationError, match="缺少环境变量"):
        ModelClient({"model_name": "chosen-model", "model_key_env": "MISSING_MODEL_TEST_KEY"})


@pytest.mark.asyncio
async def test_two_runtime_contexts_are_isolated(config, monkeypatch):
    contexts = []

    async def handler(request):
        payload = json.loads(request.content)
        context = json.loads(payload["input"][0]["content"])
        contexts.append(context)
        account = context["account"]
        return response(message(f"{account['id']}: {account['holdings'][0]['quantity']} 股"))

    install_transport(monkeypatch, handler)

    async def emit(event):
        pass

    second = deepcopy(CONTEXT)
    second["account"]["id"] = "account-b"
    second["account"]["holdings"][0]["quantity"] = "300"
    second["task_id"] = "job-b-7"
    a, b = await asyncio.gather(agents.run_research(CONTEXT, config, ResearchTools(), emit),
                                agents.run_research(second, config, ResearchTools(), emit))
    assert a["report"] == "account-a: 100 股"
    assert b["report"] == "account-b: 300 股"
    assert a["account_id"] != b["account_id"]
    assert CONTEXT["account"]["holdings"][0]["quantity"] == "100"


@pytest.mark.asyncio
async def test_cancelled_pending_tool_can_resume_same_child_session(config, monkeypatch):
    entered = asyncio.Event()
    events = []
    calls = 0

    class SlowTools(ResearchTools):
        async def read(self, url):
            entered.set()
            await asyncio.sleep(10)

    async def emit(event):
        events.append(event)

    async def handler(request):
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        if calls == 1:
            return response(call("read", {"url": "https://example.org/slow"}, "pending-read"))
        pending_result = next(item for item in payload["input"] if item.get("call_id") == "pending-read" and item.get("type") == "function_call_output")
        assert json.loads(pending_result["output"])["failed"] is True
        assert "补充任务" in json.dumps(payload, ensure_ascii=False)
        return response(call("submit_report", {"summary": "已继续同一子会话；此前读取未完成"}))

    install_transport(monkeypatch, handler)
    runtime = agents.ResearchRuntime(CONTEXT, config, SlowTools(), emit)
    try:
        created = await runtime._spawn({"task": "timeout example"})
        child_id = created["agent_id"]
        await asyncio.wait_for(entered.wait(), 1)
        await runtime._dispatch("manager", "cancel_agent", {"agent_id": child_id})
        assert runtime.children[child_id].status == "cancelled"
        await runtime._dispatch("manager", "follow_up", {"agent_id": child_id, "task": "补充任务：仅说明原读取未完成"})
        await runtime.children[child_id].task_handle
        assert runtime.children[child_id].status == "completed"
        assert runtime.children[child_id].round == 2
        assert not runtime.children[child_id].session.pending_calls
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_subagents_really_parallel_but_obey_concurrency_limit(config, monkeypatch):
    config = {**config, "max_concurrent_agents": 2}
    active, maximum = 0, 0

    async def emit(event):
        pass

    async def handler(request):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return response(message("独立子任务结果"))

    install_transport(monkeypatch, handler)
    runtime = agents.ResearchRuntime(CONTEXT, config, ResearchTools(), emit)
    try:
        for title in ("first", "second", "third"):
            await runtime._spawn({"task": title})
        await asyncio.gather(*list(runtime.handles))
        assert maximum == 2
        assert all(child.status == "completed" for child in runtime.children.values())
        assert len({id(child.session) for child in runtime.children.values()}) == 3
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_malformed_or_forbidden_tools_return_errors_without_mutation(config, monkeypatch):
    count = 0
    tools = ResearchTools()

    async def emit(event):
        pass

    async def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            return response(call("calculate", {"operation": "sum", "values": [None]}, "bad-number"),
                            call("save_material", {}, "missing-object"),
                            call("update_account", {"cash": "999999"}, "forbidden"),
                            {"type": "function_call", "call_id": "bad-json", "name": "search", "arguments": "not-json"})
        payload = json.loads(request.content)
        outputs = [json.loads(item["output"]) for item in payload["input"] if item.get("type") == "function_call_output"]
        assert len(outputs) == 4
        assert all(item["failed"] is True for item in outputs)
        return response(message("工具参数错误均已说明，没有修改账户。"))

    install_transport(monkeypatch, handler)
    result = await agents.run_research(CONTEXT, config, tools, emit)
    assert not tools.materials and not tools.search_queries
    assert CONTEXT["account"]["cash"] is None
    assert result["report"].startswith("工具参数错误")
