"""Controlled replay of the live publication defect; never invokes a real model.

Uses the production Store and Service.execute publication/review path in a fresh
temporary database. Numeric conditions mirror the two suggestions that exposed
the defect; this is not a rerun of the isolated live acceptance experiment.
"""
from datetime import datetime, timedelta

import pytest

from app.domain import AccountInput, TZ
from app.service import Service
from app.store import Store


@pytest.mark.asyncio
async def test_service_publication_with_closed_quote_baseline_stays_current(tmp_path, monkeypatch):
    store = Store(tmp_path / "closed-quote-publication-replay.sqlite3")
    store.set_meta("settings", {"debounce_seconds": 0, "quote_provider": "manual",
                              "scheduled_research_enabled": False,
                              "market_refresh_enabled": False, "forecast_evaluation_enabled": False})
    account = store.save_account(AccountInput(name="受控重放：休市行情发布",
        holdings=[{"symbol": "SZ:002354", "quantity": "1000"},
                  {"symbol": "SZ:159599", "quantity": "1000"}]))
    quote_at = (datetime.now(TZ) - timedelta(days=1)).replace(hour=15, minute=35, second=0, microsecond=0)
    for security, price in [("SZ:002354", "7.520"), ("SZ:159599", "1.948")]:
        store.put_cache("quotes", security, {"symbol": security, "price": price,
            "as_of": quote_at.isoformat(), "status": "closed", "fresh_for_triggers": False,
            "source": "controlled-replay"})
    runs = []

    async def controlled_replay(context, config, tools, emit):
        runs.append(context["job_id"])
        return {"report": "受控重放报告：休市旧行情，价格触发条件未知；未调用真实模型。",
                "suggestions": [
                    {"security_id": security, "action": "watch", "basis": "受控重放条件，非实时建议",
                     "price_conditions": [{"operator": "gte", "field": "price", "value": threshold}]}
                    for security, threshold in [("SZ:002354", "8.36"), ("SZ:159599", "2.287")]]}

    service = Service(store, runner=controlled_replay)

    async def cached_quotes(symbols=None):
        return list(store.cache("quotes").values())

    monkeypatch.setattr(service, "refresh_quotes", cached_quotes)
    previous_recommendation_ids = set()
    # New recommendation IDs on replacement used to evade the per-ID stale
    # event key and buy a follow-up report after every publication.
    for iteration in range(2):
        if iteration:
            store.enqueue(account["id"], "manual")
        job = store.claim_jobs(account_id=account["id"])[0]
        await service.execute(job)
        actual = store.job(job["id"], events=True)
        assert actual["state"] == "completed"
        assert actual["report_status"] == "current"
        assert any(event["type"] == "published" for event in actual["events"])
        assert store.account(account["id"])["latest_job_id"] == job["id"]
        suggestions = store.recommendations_page(account["id"], view="active")["items"]
        ids = {item["id"] for item in suggestions}
        assert len(ids) == 2 and not ids & previous_recommendation_ids
        previous_recommendation_ids = ids
        assert all(item["price_state"] == "stale" and item["review_status"] == "needs_review"
                   and item["review_followup"] is None for item in suggestions)
        for _ in range(3):
            store.review_recommendations(service.quotes())
            store.dispatch_demands()
        assert not store.demands(account["id"]) and not store.claim_jobs()
    assert len(runs) == 2
    reopened = Store(store.path)
    reopened.review_recommendations(Service(reopened).quotes())
    assert not reopened.demands(account["id"]) and not reopened.claim_jobs()
    assert reopened.analyses(account["id"])[0]["status"] == "current"
    # Baseline suppression must not remove the user's explicit review action.
    reopened.retry_recommendation_review(account["id"], next(iter(previous_recommendation_ids)))
    assert len(reopened.claim_jobs()) == 1
