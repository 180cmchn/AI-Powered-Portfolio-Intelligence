"""Deterministic evaluation evidence checks; no actual model/news/quotes here."""
from copy import deepcopy

import pytest

from app.domain import AccountInput
from app.store import Store
from scripts import verify_fifth_iteration as evaluation


def passing_evidence():
    """Synthetic summary only: this does not certify any external endpoint."""
    requests = [{"agent_id": role, "stage": stage, "reasoning_effort_sent": "medium",
        "request_accepted": True, "reported_reasoning_effort": None, "usage": None,
        "attempt": 1, "duration_ms": 10}
        for role, stage in (("manager", "research"), ("child-1", "research"), ("manager", "synthesis"))]
    rounds = [{"label": label, "state": "completed", "result": {"report": "Controlled fixture report"},
        "events": [], "observations": {"metrics_recomputable": True}}
        for label in ("public", "A", "B", "edited-A")]
    rounds[0]["events"] = [
        {"type": "model_usage", "payload": {"requests": requests}},
        {"type": "native_search", "payload": {"events": [{"type": "web_search_call", "status": "completed"}]}},
        {"type": "agent_completed", "agent_id": "child-1", "payload": {"report": "Controlled child result"}},
    ]
    return {"same_application_code": True, "rounds": rounds,
        "quote_samples": [{"intraday_fresh": True, "quote":{"symbol":evaluation.SYMBOL,"name":"机器人 ETF 鹏华","price":"0.965"}} for _ in range(3)],
        "steps": {"b_input_unchanged_by_a_edit": True, "holding_edit": {"prior_success_stale": True},
            "scheduler_news_demand": [{"kind": "news_observation"}],
            "accounts": {"a": {"cash": "10000", "holdings": [{"symbol": evaluation.SYMBOL, "quantity": "20000"}]}}},
        "browser": {"ok": True}}


def test_actual_evaluation_account_payloads_fit_production_schema():
    a = AccountInput.model_validate(evaluation.account_payload("A", "10000", "10000", "conservative"))
    b = AccountInput.model_validate(evaluation.account_payload("B", "0", "20000", "balanced"))
    assert a.holdings[0].symbol == evaluation.SYMBOL and a.holdings[0].quantity == "10000"
    assert a.cash == "10000" and b.cash == "20000" and b.holdings == []
    assert len(a.preferences.horizon) <= 200


@pytest.mark.parametrize("failure", ["exception", "browser", "native", "effort", "no_acceptance", "metrics", "lunch", "partial", "wrong_security", "precision"])
def test_incomplete_or_failed_evidence_never_passes(failure):
    summary = passing_evidence()
    if failure == "exception":
        summary["execution_failure"] = {"type": "TimeoutError"}
    elif failure == "browser":
        summary["browser"]["ok"] = False
    elif failure == "native":
        summary["rounds"][0]["events"][1]["payload"]["events"][0]["status"] = "failed"
    elif failure == "effort":
        summary["rounds"][0]["events"][0]["payload"]["requests"][1]["reasoning_effort_sent"] = "low"
    elif failure == "no_acceptance":
        for request in summary["rounds"][0]["events"][0]["payload"]["requests"]:
            request["request_accepted"] = None
    elif failure == "metrics":
        summary["rounds"][1]["observations"]["metrics_recomputable"] = False
    elif failure == "wrong_security":
        summary["quote_samples"][0]["quote"]["symbol"] = "SZ:159770"
    elif failure == "precision":
        summary["quote_samples"][0]["quote"]["price"] = "0.96"
    elif failure == "lunch":
        summary["quote_samples"][0]["intraday_fresh"] = False
    else:
        summary["rounds"][1]["state"] = "failed"
        summary["rounds"][1]["result"] = {}
    evaluation.summarize_acceptance(summary, {"reasoning_effort": "medium"})
    assert summary["mechanical_acceptance"] is False
    assert not all(summary["checks"].values())


def test_http_success_does_not_invent_reported_effort_usage_or_research_quality():
    summary = passing_evidence()
    evaluation.summarize_acceptance(summary, {"reasoning_effort": "medium"})
    assert summary["mechanical_acceptance"] is True  # Synthetic evidence shape only.
    assert summary["research_quality"] == "pending_evidence_review"
    assert summary["request_accounting"]["billing_amount"] is None
    assert summary["request_accounting"]["provider_reported_usage"] == [None, None, None]
    assert all(r["reported_reasoning_effort"] is None for r in summary["request_diagnostics"])


def test_failed_requests_retries_and_failed_searches_are_counted_without_usage_fabrication():
    summary = passing_evidence()
    request = deepcopy(summary["rounds"][0]["events"][0]["payload"]["requests"][0])
    request.update(attempt=2, request_accepted=False, error="HTTP 400", duration_ms=25)
    summary["rounds"][0]["events"].extend([
        {"type": "model_error", "payload": {"requests": [request]}},
        {"type": "native_search", "payload": {"events": [{"type": "web_search_call", "status": "failed"}]}},
    ])
    summary["rounds"][0].update(state="failed", result={})
    evaluation.summarize_acceptance(summary, {"reasoning_effort": "medium"})
    accounting = summary["request_accounting"]
    assert accounting["http_requests"] == 4 and accounting["retries"] == 1
    assert accounting["native_search_event_items"] == 2 and accounting["completed_native_search_items"] == 1
    assert accounting["duration_ms"] == 55
    assert summary["request_diagnostics"][-1]["error"] == "HTTP 400"
    assert summary["mechanical_acceptance"] is False


def test_lunch_sample_cannot_be_intraday_proof_or_rewrite_source_time(monkeypatch):
    monkeypatch.setattr(evaluation, "market_state", lambda: {"phase": "closed", "trading_day": True})
    quote = {"as_of": "2026-10-28T11:29:00+08:00", "fresh_for_triggers": True, "cache_hit": False}
    before = deepcopy(quote)
    sample = evaluation.sample_record(quote)
    assert sample["intraday_fresh"] is False
    assert sample["quote"] == before and quote == before


def test_unselected_worker_executions_are_inventoried_and_included_in_request_budget(tmp_path):
    store = Store(tmp_path / "evaluation-inventory.sqlite3")
    store.set_meta("settings", {"debounce_seconds": 0, "quote_provider": "manual"})
    account = store.save_account(AccountInput.model_validate(evaluation.account_payload("A", "10000", "10000", "balanced")))
    job = store.claim_jobs()[0]
    store.event(job["id"], {"type": "model_error", "agent_id": "manager", "payload": {"requests": [{
        "agent_id": "manager", "reasoning_effort_sent": "medium", "request_accepted": False, "attempt": 1, "duration_ms": 2}]}})
    store.set_job_state(job["id"], "failed", "Controlled upstream failure")
    store.request_analysis(None, "news_observation", "controlled-additional-public")
    run = store.claim_global()
    store.event(run["id"], {"type": "model_error", "agent_id": "manager", "payload": {"requests": [{
        "agent_id": "manager", "reasoning_effort_sent": "medium", "request_accepted": False, "attempt": 1, "duration_ms": 3}]}})
    store.finish_global(run["id"], "failed", error="Controlled additional failure")
    executions = evaluation.collect_executions(store, [{"job_id": account["latest_job_id"], "label": "A"}])
    assert len(executions) == 2
    assert {row["label"] for row in executions} == {"A", "additional-public"}
    summary = passing_evidence()
    summary["all_executions"] = executions
    evaluation.summarize_acceptance(summary, {"reasoning_effort": "medium"})
    assert summary["request_accounting"]["http_requests"] == 2
    assert summary["request_accounting"]["duration_ms"] == 5
    assert summary["execution_inventory"]["started_executions"] == 2
    assert summary["execution_inventory"]["public_executions"] == 1
    assert summary["execution_inventory"]["account_executions"] == 1
    assert summary["execution_inventory"]["additional_executions"][0]["job_id"] == run["id"]
    assert summary["mechanical_acceptance"] is False
