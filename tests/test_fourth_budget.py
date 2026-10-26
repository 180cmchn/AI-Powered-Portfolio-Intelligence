"""A protocol substitute exercises the real runtime and durable evidence resolver."""
import json

import httpx
import pytest

from app import agents
from app.domain import AccountInput
from app.model import ModelClient
from app.service import ResearchTools
from app.store import Store


@pytest.mark.asyncio
async def test_searching_model_finishes_inside_original_budget_with_evidence_gap(tmp_path, monkeypatch):
    monkeypatch.setenv("FOURTH_BUDGET_KEY", "controlled-test-key")
    config = {"model_base_url": "https://model.example/v1", "model_name": "budget-regression",
              "model_key_env": "FOURTH_BUDGET_KEY", "model_protocol": "responses",
              "model_native_search": True, "model_retries": 0,
              "max_agent_turns": 6, "max_model_calls": 6, "analysis_timeout_seconds": 5}
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
            assert budget == {"remaining_agent_turns": 7 - turn, "remaining_shared_model_calls": 7 - turn}
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
    assert result["model_calls"] == len(requests) == config["max_model_calls"]
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
              "max_agent_turns": 6, "max_model_calls": 7}
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
