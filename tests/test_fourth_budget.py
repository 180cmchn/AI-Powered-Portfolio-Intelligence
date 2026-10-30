"""A protocol substitute exercises the real runtime and durable evidence resolver."""
import asyncio
import json

import httpx
import pytest

from app import agents
from app.domain import AccountInput
from app.model import ModelBudgetExceeded, ModelClient
from app.context import recall
from app.service import ResearchTools
from app.store import Store


@pytest.mark.asyncio
async def test_searching_model_finishes_inside_original_budget_with_evidence_gap(tmp_path, monkeypatch):
    monkeypatch.setenv("FOURTH_BUDGET_KEY", "controlled-test-key")
    config = {"model_base_url": "https://model.example/v1", "model_name": "budget-regression",
              "model_key_env": "FOURTH_BUDGET_KEY", "model_protocol": "responses",
              "model_native_search": True, "model_retries": 0,
              "max_agent_turns": 6, "max_model_calls": 20, "analysis_timeout_seconds": 5}
    store = Store(tmp_path / "budget.sqlite3")
    store.set_meta("settings", {"quote_provider": "manual", "debounce_seconds": 0})
    account = store.save_account(AccountInput(name="Budget regression", holdings=[]))
    job = store.claim_jobs(account_id=account["id"])[0]
    context = {"job_id": job["id"], "account": account, "input_version": account["version"],
               "metrics": {"positions": []}, "scope": "account"}
    tools = ResearchTools(store, job, config, context)
    requests, budgets, events = [], [], []
    final_report = "已整理可见摘要；原始财报尚未取得，盈利变化仍未知，暂不据此追加仓位。"

    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        turn = len(requests)
        user_messages = [json.loads(item["content"]) for item in payload["input"]
                         if item.get("role") == "user"]
        warnings = [item for item in user_messages if "resource_budget" in item]
        if turn < 4:
            assert not warnings
        else:
            budget = warnings[-1]["resource_budget"]
            assert budget == {"remaining_agent_turns": 7 - turn, "remaining_shared_model_calls": 21 - turn}
            assert "未知保持未知" in warnings[-1]["instruction"]
            budgets.append(budget["remaining_agent_turns"])

        if turn < 6:
            # This model keeps researching even after the first two budget warnings.
            assert {"type": "web_search"} in payload["tools"]
            output = [{"type": "web_search_call", "id": f"search-{turn}", "status": "completed",
                       "action": {"type": "search", "query": "issuer report",
                                  "sources": [{"url": f"https://issuer.example/report-{turn}",
                                               "title": "Source excerpt", "snippet": "A visible source excerpt."}]}}]
        else:
            assert turn == 6  # No seventh request may be borrowed for citation repair.
            assert [(tool["type"], tool.get("name")) for tool in payload["tools"]] == [("function", "finish_analysis")]
            assert "web_search_call.action.sources" not in payload["include"]
            saved_sources = [item for item in user_messages if "native_source_materials" in item]
            assert len(saved_sources) == 5
            valid_id = saved_sources[0]["native_source_materials"][0]["id"]
            output = [{"type": "function_call", "call_id": "final", "name": "finish_analysis",
                       "arguments": json.dumps({"report": final_report,
                                                "material_ids": [valid_id, "unavailable-original-report"],
                                                "uncertainties": ["原始财报未取得，摘要未获原文核实。"]})}]
        return httpx.Response(200, json={"id": f"response-{turn}", "status": "completed", "output": output})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(agents, "ModelClient", lambda cfg, before_request=None: ModelClient(cfg, before_request, transport))

    async def emit(event):
        events.append(event)

    result = await agents.run_research(context, config, tools, emit)

    assert budgets == [3, 2, 1]
    assert result["model_calls"] == len(requests) == 6
    assert result["model_calls"] <= config["max_model_calls"]
    assert result["report"] == final_report
    assert result["uncertainties"] == ["原始财报未取得，摘要未获原文核实。"]
    assert result["reference_summary"] == {"resolved": 1, "source_link": 0, "unresolved": 1}
    valid, missing = result["references"]
    assert valid["access"] == "search_excerpt" and valid["representation_kind"] == "native_search_source"
    assert store.material(valid["material_id"])["content"] == "A visible source excerpt."
    assert missing == {"material_id": "unavailable-original-report", "status": "unresolved", "reason": "unknown_or_inaccessible"}
    assert len(result["materials"]) == 5
    assert any(event["type"] == "agent_completed" for event in events)
    assert not any(event["type"] in {"agent_failed", "model_error"} for event in events)


@pytest.mark.asyncio
async def test_followup_restores_native_search_after_child_final_turn(monkeypatch):
    monkeypatch.setenv("FOURTH_BUDGET_KEY", "controlled-test-key")
    config = {"model_base_url": "https://model.example/v1", "model_name": "followup-budget-regression",
              "model_key_env": "FOURTH_BUDGET_KEY", "model_protocol": "responses",
              "model_native_search": True, "model_retries": 0,
              "max_agent_turns": 6, "max_model_calls": 20}
    requests = []

    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        turn = len(requests)
        if turn <= 5:
            assert {"type": "web_search"} in payload["tools"]
            output = [{"type": "web_search_call", "id": f"search-{turn}", "status": "completed",
                       "action": {"type": "search", "query": "initial disclosure"}}]
        else:
            if turn == 6:
                assert [(tool["type"], tool.get("name")) for tool in payload["tools"]] == [("function", "submit_report")]
            else:
                assert turn == 7
                assert {"type": "web_search"} in payload["tools"]
                assert "web_search_call.action.sources" in payload["include"]
                assert "Check the later correction" in json.dumps(payload["input"])
                assert "Initial bounded report" in json.dumps(payload["input"])
            output = [{"type": "function_call", "call_id": f"submit-{turn}", "name": "submit_report",
                       "arguments": json.dumps({"summary": "Initial bounded report" if turn == 6 else "Follow-up report"})}]
        return httpx.Response(200, json={"id": f"response-{turn}", "status": "completed", "output": output})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(agents, "ModelClient", lambda cfg, before_request=None: ModelClient(cfg, before_request, transport))

    async def emit(event):
        pass

    runtime = agents.ResearchRuntime({"task_id": "followup-budget", "account": {"id": "a", "version": 1}}, config, object(), emit)
    try:
        created = await runtime._spawn({"task": "Read initial disclosure"})
        child = runtime.children[created["agent_id"]]
        original_session = child.session
        await child.task_handle
        assert child.status == "completed" and child.session.native_search is False
        assert len(child.reports) == 1

        await runtime._dispatch("manager", "follow_up", {"agent_id": child.id, "task": "Check the later correction"})
        await child.task_handle

        assert child.session is original_session
        assert child.status == "completed" and child.round == 2
        assert [report["summary"] for report in child.reports] == ["Initial bounded report", "Follow-up report"]
        assert runtime.calls == len(requests) == 7
        assert child.session.native_search is True
    finally:
        await runtime.close()


def budget_config(monkeypatch, **overrides):
    monkeypatch.setenv("FOURTH_BUDGET_KEY", "controlled-test-key")
    return {"model_base_url": "https://model.example/v1", "model_name": "shared-budget-regression",
            "model_key_env": "FOURTH_BUDGET_KEY", "model_protocol": "responses",
            "model_native_search": True, "model_retries": 0,
            "max_agent_turns": 18, "max_model_calls": 30, "max_concurrent_agents": 2,
            "analysis_timeout_seconds": 10, "agent_timeout_seconds": 5,
            # Scaled whole-task budgets also need a scaled first-event policy:
            # retry feasibility must not compare this 5s child with a 90s wait.
            "model_first_event_timeout_seconds": .25,
            "model_stream_idle_timeout_seconds": .25,
            "model_request_hard_timeout_seconds": 1,
            "synthesis_request_timeout_seconds": 1, **overrides}


def model_response(name, arguments, ident="call"):
    return httpx.Response(200, json={"id": ident, "status": "completed", "output": [
        {"type": "function_call", "call_id": ident, "name": name, "arguments": json.dumps(arguments)}]})


def use_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(agents, "ModelClient", lambda cfg, before_request=None: ModelClient(cfg, before_request, transport))


@pytest.mark.asyncio
async def test_shared_30_calls_close_before_any_agent_reaches_18_turns(monkeypatch):
    config = budget_config(monkeypatch)
    events, requests = [], []
    child_counts = {}

    async def emit(event):
        events.append(event)

    async def handler(request):
        payload = json.loads(request.content)
        seed = json.loads(payload["input"][0]["content"])
        state = seed["runtime_status"]
        ident = state["agent_id"]
        requests.append((ident, payload))
        names = {tool.get("name") for tool in payload["tools"]}
        await asyncio.sleep(.002)
        if ident == "manager":
            if len(requests) == 1:
                return httpx.Response(200, json={"status": "completed", "output": [
                    {"type": "function_call", "call_id": f"spawn-{i}", "name": "spawn_research",
                     "arguments": json.dumps({"task": f"Research issuer {i}", "title": f"Issuer {i}"})} for i in range(2)]})
            if names == {"finish_analysis"}:
                assert state["closing"] is True
                assert len(state["children"]) == 2
                assert all(child["status"] == "completed" for child in state["children"])
                assert all(child["latest_report"]["open_questions"] for child in state["children"])
                assert {"type": "web_search"} not in payload["tools"]
                return model_response("finish_analysis", {"report": "Summary of available evidence. Issuer valuation remains unknown; missing statements prevent a conclusion."}, "manager-final")
            return model_response("wait_agents", {"timeout_seconds": .01}, f"wait-{len(requests)}")
        child_counts[ident] = child_counts.get(ident, 0) + 1
        if names == {"submit_report"}:
            assert state["closing"] is True
            return model_response("submit_report", {"summary": "Only a public source excerpt was obtained; no verified valuation.",
                "material_ids": [f"visible-{ident}"], "open_questions": ["Statement figures and valuation remain unknown."]}, f"submit-{ident}")
        return httpx.Response(200, json={"status": "completed", "output": [
            {"type": "web_search_call", "id": f"search-{ident}-{child_counts[ident]}", "status": "completed", "action": {"type": "search", "query": "issuer statement"}}]})

    use_transport(monkeypatch, handler)
    result = await agents.run_research({"task_id": "shared-30", "scope": "global"}, config, object(), emit)
    assert result["report"].startswith("Summary of available evidence")
    assert result["model_calls"] == len(requests) <= 30
    assert len(requests) >= 27  # Exercises the shared tail, not an early scripted finish.
    assert sum(ident == "manager" for ident, _ in requests) < 18
    assert max(child_counts.values()) < 18
    assert len(result["agents"]) == 2 and all(child["status"] == "completed" for child in result["agents"])
    assert any(event["type"] == "budget_closeout" for event in events)
    assert not any(event["type"] == "agent_failed" for event in events)


@pytest.mark.asyncio
async def test_retry_cannot_spend_the_manager_final_request(monkeypatch):
    config = budget_config(monkeypatch, model_retries=1)
    requests, events = [], []

    async def emit(event):
        events.append(event)

    async def handler(request):
        payload = json.loads(request.content)
        state = json.loads(payload["input"][0]["content"])["runtime_status"]
        requests.append(state["agent_id"])
        if state["agent_id"] != "manager":
            assert {tool.get("name") for tool in payload["tools"]} == {"submit_report"}
            return httpx.Response(503, json={"error": "fixture unavailable"})
        assert state["remaining_shared_model_calls"] == 1
        assert state["children"][0]["status"] == "failed"
        assert state["children"][0]["report_count"] == 0
        return model_response("finish_analysis", {"report": "Research failed before a child report. Available information is insufficient and all requested figures remain unknown."})

    use_transport(monkeypatch, handler)
    runtime = agents.ResearchRuntime({"task_id": "retry-reservation"}, config, object(), emit)
    try:
        created = await runtime._spawn({"task": "Supplemental figures"})
        runtime.calls = 28  # Earlier requests already used the ordinary budget.
        child = runtime.children[created["agent_id"]]
        result = await runtime.run()
        assert requests == [child.id, "manager"]  # Retry was refused before HTTP.
        assert result["model_calls"] == 30
        assert child.status == "failed" and child.reports == []
        assert "insufficient" in result["report"]
        assert any("最终汇总保留" in event["payload"].get("error", "") for event in events)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_compaction_keeps_live_child_state_and_unfinished_followup(monkeypatch):
    config = budget_config(monkeypatch, max_context_chars=10000, max_tool_result_chars=2000)
    seen = []

    async def emit(event):
        pass

    async def handler(request):
        payload = json.loads(request.content)
        seed = json.loads(payload["input"][0]["content"])
        child = seed["runtime_status"]["children"][0]
        seen.append(child)
        assert child["agent_id"] == "research-existing" and child["title"] == "Existing issuer research"
        assert child["status"] == "failed" and child["round"] == 2
        assert child["latest_report"]["summary"] == "Completed original visible report"
        assert child["latest_report"]["material_ids"] == ["material-original"]
        assert child["pending_messages"] == ["Check the subsequent correction"]
        assert any(item.get("role") == "user" and "archive_count" in item.get("content", "") for item in payload["input"])
        return model_response("finish_analysis", {"report": "Original source preserved; subsequent correction was not verified."})

    use_transport(monkeypatch, handler)
    runtime = agents.ResearchRuntime({"task_id": "compaction-state"}, config, object(), emit)
    session = runtime.client.session("researcher", {"task": "Existing issuer research"})
    runtime.children["research-existing"] = agents.AgentState("research-existing", "Existing issuer research", "Check issuer", session,
        status="failed", round=2, error="Follow-up request timed out", pending_messages=["Check the subsequent correction"],
        reports=[{"summary": "Completed original visible report", "material_ids": ["material-original"], "research_round": 1}])
    runtime.manager_session.history.append({"role": "user", "content": "Older visible data " * 1000})
    result = await runtime.run()
    assert seen and result["context_compactions"] >= 1
    assert result["report"].endswith("was not verified.")


@pytest.mark.asyncio
async def test_large_runtime_state_is_bounded_and_retrievable(monkeypatch):
    config = budget_config(monkeypatch, max_context_chars=10000)
    async def emit(event):
        pass
    runtime = agents.ResearchRuntime({"task_id": "large-state"}, config, object(), emit)
    long_question = "Unresolved financial uncertainty. " * 500
    try:
        for i in range(16):
            ident = f"research-{i}"
            session = runtime.client.session("researcher", {"task": ident})
            runtime.children[ident] = agents.AgentState(ident, f"Issuer {i}", "Check issuer", session,
                status="failed", pending_messages=[long_question], reports=[{"summary": "Earlier report", "research_round": 1,
                    "material_ids": [f"material-{j}" for j in range(100)], "open_questions": [long_question]}])
        runtime._pin_runtime_status(runtime.manager_session, "manager", 10, False)
        state = json.loads(runtime.manager_session.history[0]["content"])["runtime_status"]
        assert len(json.dumps(state, ensure_ascii=False)) <= 10000 // 3
        assert state["details_truncated"] is True
        assert len(state["children"]) == 16
        assert all(child["status"] == "failed" and child["unresolved"] for child in state["children"])
        full = json.loads(next(item for item in runtime.manager_session.archive if item["record_id"] == state["record_id"])["payload"])
        assert full["children"][0]["pending_messages"] == [long_question]
        assert full["children"][0]["latest_report"]["open_questions"] == [long_question]
        assert recall(runtime.manager_session, record_id=state["record_id"])["total_chars"] > 10000
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_synthesis_budget_also_forces_submission_before_exhaustion(monkeypatch):
    config = budget_config(monkeypatch, synthesis_max_calls=4)
    requests = []
    class Tools:
        async def get_material(self, material_id):
            return {"id": material_id, "access": "excerpt", "text": "Available source only"}
    async def emit(event):
        pass
    async def handler(request):
        payload = json.loads(request.content)
        state = json.loads(payload["input"][0]["content"])["runtime_status"]
        requests.append(payload)
        if len(requests) < 3:
            return model_response("get_material", {"material_id": "existing"}, f"read-{len(requests)}")
        assert {tool.get("name") for tool in payload["tools"]} == {"finish_analysis"}
        assert state["remaining_shared_model_calls"] == 2
        return model_response("finish_analysis", {"report": "Visible checkpoint summarized; missing figures remain unknown."})
    use_transport(monkeypatch, handler)
    result = await agents.run_research({"task_id": "synthesis-tail", "synthesis_checkpoint": {"reports": []}}, config, Tools(), emit)
    assert result["model_calls"] == 3 < 4
    assert result["report"].startswith("Visible checkpoint")


@pytest.mark.asyncio
async def test_retry_crossing_shared_tail_rebuilds_child_payload_before_http(monkeypatch):
    config = budget_config(monkeypatch, model_retries=1)
    requests = []
    async def emit(event):
        pass
    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            assert {"type": "web_search"} in payload["tools"]
            return httpx.Response(503, json={"error": "fixture unavailable"})
        assert len(requests) == 2
        assert {tool.get("name") for tool in payload["tools"]} == {"submit_report"}
        state = json.loads(payload["input"][0]["content"])["runtime_status"]
        assert state["closing"] is True
        return model_response("submit_report", {"summary": "Earlier research retained; supplemental request failed.",
            "open_questions": ["Supplemental figures remain unknown."]})
    use_transport(monkeypatch, handler)
    runtime = agents.ResearchRuntime({"task_id": "retry-transition"}, config, object(), emit)
    try:
        created = await runtime._spawn({"task": "Supplemental figures"})
        runtime.calls = 26
        child = runtime.children[created["agent_id"]]
        await child.task_handle
        assert child.status == "completed" and child.reports[0]["open_questions"]
        assert runtime.calls == 28 and len(requests) == 2
        with pytest.raises(ValueError, match="收尾"):
            await runtime._dispatch("manager", "follow_up", {"agent_id": child.id, "task": "Expand research again"})
    finally:
        await runtime.close()


def test_calculation_schema_only_advertises_implemented_operations():
    tool = next(tool for tool in agents.RESEARCH_TOOLS if tool["name"] == "calculate")
    assert tool["parameters"]["properties"]["operation"]["enum"] == [
        "sum", "multiply", "product", "divide", "ratio", "percent", "subtract", "difference"]
    assert "percent 为 a/b*100，不是增长率" in tool["description"]
