"""Recovery guards and successful-checkpoint consumption through production service."""
from copy import deepcopy

import pytest

from app.domain import AccountInput
from app.service import Service
from app.store import Store


def test_manual_security_outside_holdings_or_candidates_has_quote_guard(tmp_path):
    store = Store(tmp_path / "target-quote.sqlite3")
    account = store.save_account(AccountInput(name="A", holdings=[]))
    context = {"account": account, "metrics": {"positions": []}, "candidate_quotes": {},
               "target_security": {"symbol": "SH:600519", "quote": {
                   "price": "10", "as_of": "2026-10-26", "source": "controlled", "status": "closed"}}}
    changed = deepcopy(context)
    changed["target_security"]["quote"]["price"] = "11"
    assert store.research_guard(changed) != store.research_guard(context)


@pytest.mark.asyncio
async def test_successful_recovery_is_not_reused_for_next_manual_research(tmp_path):
    store = Store(tmp_path / "consume.sqlite3")
    store.set_meta("settings", {"quote_provider": "manual", "debounce_seconds": 0})
    account = store.save_account(AccountInput(name="A", holdings=[]))
    original = store.claim_jobs(account_id=account["id"])[0]
    context = {"account": account, "metrics": {"positions": []},
               "universe_version": store.universe_snapshot()["universe_version"]}
    store.save_synthesis_checkpoint(original, {"phase": "synthesis", "task_id": original["id"],
        "material_ids": [], "reports": [{"summary": "Finished visible research"}]}, context)
    store.set_job_state(original["id"], "failed", "controlled synthesis failure")
    resumed = []

    async def runner(context, config, tools, emit):
        resumed.append(bool(context.get("synthesis_checkpoint")))
        return {"report": "Complete report", "synthesis_checkpoint_job_id": "untrusted-model-value"}

    service = Service(store, runner)
    for _ in range(2):
        store.enqueue(account["id"], "manual")
        job = store.claim_jobs(account_id=account["id"])[0]
        await service.execute(job)
        assert store.job(job["id"])["state"] == "completed"
    assert resumed == [True, False]
    with store.connect() as db:
        assert db.execute("SELECT resume_count FROM synthesis_checkpoints WHERE job_id=?", (original["id"],)).fetchone()[0] == 2
