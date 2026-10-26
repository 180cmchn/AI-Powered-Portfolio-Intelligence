"""Deterministic context/protocol tests; no claims about real model quality."""
import json

import httpx
import pytest

from app.agents import ResearchRuntime, RESEARCH_TOOLS, MANAGER_TOOLS
from app.context import compact, recall
from app.model import ModelClient, ModelSession, ToolCall


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("CONTEXT_TEST_KEY", "test-only-key")
    return {"model_name": "protocol-fixture", "model_key_env": "CONTEXT_TEST_KEY",
            "model_retries": 0, "max_context_chars": 10000, "max_tool_result_chars": 2000}


def test_compaction_keeps_visible_message_phase_status_but_not_hidden_fields():
    session = ModelSession("Research", history=[{"role": "user", "content": "Initial research"}, {
        "type": "message", "role": "assistant", "phase": "commentary", "status": "completed",
        "content": [{"type": "output_text", "text": "Still researching. " * 200}],
        "reasoning_content": "DO_NOT_ARCHIVE_OPAQUE_DATA"}])
    assert compact(session, 1500)
    saved = json.loads(session.archive[0]["payload"])
    assert saved["phase"] == "commentary" and saved["status"] == "completed"
    assert "DO_NOT_ARCHIVE_OPAQUE_DATA" not in json.dumps(session.archive)


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["responses", "chat_completions"])
async def test_large_history_compacts_whole_turns_and_recovers_old_refs(config, protocol):
    payloads = []

    async def handler(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        if protocol == "responses":
            return httpx.Response(200, json={"output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "继续研究"}]}], "usage": {"input_tokens": 30, "output_tokens": 10, "total_tokens": 40}})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "继续研究"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40}})

    client = ModelClient({**config, "model_protocol": protocol}, transport=httpx.MockTransport(handler))
    session = client.session("研究资料", {"account": {"id": "a", "version": 3}})
    try:
        for index in range(12):
            tool_call = ToolCall(f"call-{index}", "get_material", {"material_id": f"m-{index}"})
            if protocol == "responses":
                session.history += [{"type": "reasoning", "encrypted_content": "PRIVATE_OPAQUE_STATE"},
                                    {"type": "function_call", "call_id": tool_call.id, "name": tool_call.name, "arguments": json.dumps(tool_call.arguments)}]
            else:
                session.history.append({"role": "assistant", "content": None, "reasoning_content": "PRIVATE_OPAQUE_STATE", "tool_calls": [{"id": tool_call.id, "type": "function", "function": {"name": tool_call.name, "arguments": json.dumps(tool_call.arguments)}}]})
            session.pending_calls = [tool_call]
            client.add_results(session, [(tool_call, {"id": f"m-{index}", "url": f"https://example.org/{index}", "page": 28, "access": "partial", "text": "客观事实与待查问题。" * 160})])
            await client.complete(session, [])
        assert session.compactions > 0
        assert len(json.dumps(payloads[-1], ensure_ascii=False)) < 14000
        found = recall(session, query="https://example.org/0")
        assert found["total"]
        fetched = recall(session, record_id=found["items"][0]["record_id"], length=12000)
        assert "m-0" in fetched["text"] and any(ref.get("page") == 28 for ref in found["items"][0]["references"])
        assert "PRIVATE_OPAQUE_STATE" not in json.dumps(session.archive)
        assert all("_context_checkpoint" not in json.dumps(payload) for payload in payloads)
        # Every surviving output has a matching call; compaction never orphans one.
        for payload in payloads:
            if protocol == "responses":
                calls = {m["call_id"] for m in payload["input"] if m.get("type") == "function_call"}
                assert all(m["call_id"] in calls for m in payload["input"] if m.get("type") == "function_call_output")
            else:
                calls = {c["id"] for m in payload["messages"] for c in m.get("tool_calls", [])}
                assert all(m["tool_call_id"] in calls for m in payload["messages"] if m.get("role") == "tool")
        assert client.usage_summary(client.requests)["usage"]["total_tokens"] == 480
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_huge_tool_result_retrievable_in_chunks_without_context_failure(config):
    client = ModelClient(config)
    session = client.session("研究", {})
    try:
        original = {"id": "long-material", "url": "https://example.org/report.pdf", "page": 29,
                    "text": "始" + "数据" * 100000 + "尾部关键值 978.2"}
        call = ToolCall("call", "get_material", {})
        client.add_results(session, [(call, original)])
        compact_result = json.loads(session.history[-1]["output"])
        assert compact_result["truncated_for_context"]
        assert len(session.history[-1]["output"]) < 2400
        record = recall(session, record_id=compact_result["record_id"], start=compact_result["total_chars"] - 100, length=100)
        assert "978.2" in record["text"] and record["next_start"] is None
        assert record["truncated"]
        assert not recall(ModelSession("another account"), query="long-material")["items"]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_request_usage_observes_real_attempts_without_secrets(config):
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, text="test-only-key hidden-response")
        return httpx.Response(200, json={"output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
                                        "usage": {"input_tokens": 12, "output_tokens": 3, "input_tokens_details": {"cached_tokens": 4}, "provider_secret": "hidden-response"}})

    client = ModelClient({**config, "model_retries": 1}, transport=httpx.MockTransport(handler))
    try:
        await client.complete(client.session("test", {}), [])
        summary = client.usage_summary(client.requests)
        assert summary["requests"] == 2 and summary["failed_requests"] == 1
        assert summary["usage"] == {"input_tokens": 12, "output_tokens": 3}
        assert all(r["duration_ms"] >= 0 for r in client.requests)
        assert client.requests[1]["usage"]["input_tokens_details"]["cached_tokens"] == 4
        assert "hidden-response" not in json.dumps(client.requests)
        assert "test-only-key" not in json.dumps(client.requests)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_runtime_paginates_and_global_cannot_submit_account_suggestions(config):
    class Tools:
        async def list_materials(self, query="", limit=20, offset=0):
            return [{"id": f"m-{i}", "summary": "索引"} for i in range(offset, offset + limit)]

    async def emit(event):
        pass

    runtime = ResearchRuntime({"scope": "global", "account": None}, config, Tools(), emit)
    try:
        result = await runtime._dispatch("manager", "list_materials", {"limit": 10, "offset": 20})
        assert result["items"][0]["id"] == "m-20" and result["next_offset"] == 30
        with pytest.raises(ValueError, match="全局研究"):
            await runtime._dispatch("manager", "finish_analysis", {"report": "公共报告", "suggestions": [{"security_id": "SH:600000"}]})
        accepted = await runtime._dispatch("manager", "finish_analysis", {"report": "公共报告", "research_records": [{"kind": "market", "title": "研究", "reason": "公开数据"}]})
        assert accepted["accepted"]
        names = {tool["name"] for tool in RESEARCH_TOOLS}
        assert {"pdf_metadata", "search_pdf", "read_material_range", "recall_context", "history", "fundamentals", "sector_history"} <= names
        finish = next(tool for tool in MANAGER_TOOLS if tool["name"] == "finish_analysis")
        assert {"suggestions", "revoked_suggestion_ids", "research_records"} <= finish["parameters"]["properties"].keys()
        with pytest.raises(ValueError, match="全局研究"):
            await runtime._dispatch("manager", "finish_analysis", {"report": "公共报告", "revoked_suggestion_ids": ["old-suggestion"]})
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_finish_preserves_explicit_suggestion_revocations(config, monkeypatch):
    from app import agents

    async def handler(request):
        return httpx.Response(200, json={"output": [{"type": "function_call", "call_id": "finish", "name": "finish_analysis",
            "arguments": json.dumps({"report": "新公告推翻原依据，仅撤销指定证券建议。", "revoked_suggestion_ids": ["suggestion-old-1"]})}]})

    monkeypatch.setattr(agents, "ModelClient", lambda cfg, before_request=None: ModelClient(cfg, before_request, httpx.MockTransport(handler)))

    async def emit(event):
        pass

    result = await agents.run_research({"account": {"id": "account-a", "version": 3}, "target_security": {"symbol": "SH:600000"}}, config, object(), emit)
    assert result["revoked_suggestion_ids"] == ["suggestion-old-1"]
    assert "suggestions" not in result
    assert result["account_id"] == "account-a" and result["input_version"] == 3
