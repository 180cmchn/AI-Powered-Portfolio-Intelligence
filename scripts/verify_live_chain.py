#!/usr/bin/env python3
"""Small, opt-in real Service/Store/model chain in an independent evaluation DB.

Exit 78: model not configured; 1: execution/service failure; 2: observations
need review; 0: mechanical observations passed (NOT an investment-quality grade).
Only this project's .env and the selected live database's settings are read.
The live database is opened in SQLite read-only mode and never passed to Store.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import json
import os
from pathlib import Path
import sqlite3
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import db_path, defaults
from app.domain import AccountInput, TZ, symbol
from app.model import ModelClient, ModelConfigurationError, ModelError, probe_model
from app.service import ResearchTools, Service
from app.store import Store

PDF_URL = "https://static.cninfo.com.cn/finalpage/2026-08-15/1225475868.PDF"


def configuration(path):
    config = defaults()
    origin = "project_environment_and_defaults"
    if path.exists():
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            mode = db.execute("SELECT value FROM meta WHERE key='mode'").fetchone()
            if mode and json.loads(mode[0]) != "live":
                raise ValueError("settings database must be a live database")
            row = db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()
            if row:
                config.update(json.loads(row[0]))
                origin = "project_environment_plus_read_only_live_settings"
    return config, origin


def public_config(config, origin):
    host = urlsplit(str(config.get("model_base_url") or "")).hostname
    no_key = host in ("localhost", "127.0.0.1", "::1") and config.get("model_allow_no_key", False)
    return {"configured": bool(config.get("model_name") and (os.getenv(config.get("model_key_env", "MODEL_API_KEY")) or no_key)),
            "protocol": config.get("model_protocol"), "model_name": config.get("model_name"), "model_host": host,
            "search_provider": config.get("search_provider"), "quote_provider": config.get("quote_provider"),
            "research_provider": config.get("research_provider"), "settings_origin": origin}


def scrubbed(value, config):
    secrets = [os.getenv(config.get(key, ""), "") for key in ("model_key_env", "search_key_env", "tushare_token_env")]
    def clean(item):
        if isinstance(item, dict):
            return {key: clean(val) for key, val in item.items()}
        if isinstance(item, list):
            return [clean(val) for val in item]
        if isinstance(item, str):
            for secret in secrets:
                if secret:
                    item = item.replace(secret, "[redacted]")
        return item
    return json.dumps(clean(value), ensure_ascii=False, indent=2, default=str)


def write_json(path, value, config):
    path.write_text(scrubbed(value, config), encoding="utf-8")
    path.chmod(0o600)


def all_events(store, job_id):
    events, cursor = [], 0
    while True:
        page = store.events_since(job_id, cursor, 500)
        events.extend(page["events"])
        if not page["has_more"]:
            return events
        cursor = page["next_after"]


def classify_failure(error):
    text = str(error or "").lower()
    if any(word in text for word in ("401", "403", "unauthorized", "authentication")):
        return "authentication_failed"
    if any(word in text for word in ("timeout", "connect", "network", "网络", "超时")):
        return "network_or_timeout"
    return "research_execution_failed"


def observation(store, run, global_run=False):
    events = all_events(store, run["id"])
    result = run.get("result") or {}
    tools = [e for e in events if e["type"] == "tool_started"]
    counts = Counter(e["type"] for e in events)
    metric = result.get("metrics") or {}
    values = [Decimal(p["price"]) * Decimal(p["quantity"]) for p in metric.get("positions", []) if p.get("price") is not None]
    recomputed = str(sum(values, Decimal(0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    checks = {"report_returned": bool(result.get("report")),
              "subagent_created": counts["agent_created"] > 0,
              "subagent_tool_call": any(e.get("agent_id") not in (None, "manager") for e in tools),
              "stage_result_observed": any(e["type"] in ("agent_progress", "agent_completed") and e.get("agent_id") != "manager" for e in events),
              "follow_up_observed": any(e.get("payload", {}).get("tool") == "follow_up" for e in tools),
              "follow_up_required": "review_actual_research_need; absence is not automatic failure",
              "shared_research_used": result.get("shared_research_used", []),
              "reference_resolution": result.get("reference_resolution", result.get("references", [])),
              "reference_summary": result.get("reference_summary", {}),
              "metrics_recomputable": (recomputed == metric.get("known_priced_value")) if metric else None,
              "known_priced_value_recomputed": recomputed if metric else None}
    return {"job_id": run["id"], "scope": "public" if global_run else "account", "state": run["state"],
            "error": run.get("error"), "failure_category": classify_failure(run.get("error")) if run["state"] in ("failed", "waiting_config") else None,
            "input_version": run.get("input_version"), "universe_version": run.get("universe_version", result.get("universe_version")),
            "event_counts": dict(counts), "observations": checks, "model_calls": result.get("model_calls"),
            "result": result, "events": events}


async def verify(args, config, origin):
    summary = {"test_kind": "real_model_real_network_production_service_chain", "started_at": datetime.now(TZ).isoformat(),
               "configuration": public_config(config, origin), "protocol_probe": {"state": "not_requested"},
               "research_quality": "pending_human_review", "rounds": [], "steps": {}}
    if not summary["configuration"]["configured"]:
        print(scrubbed(summary["configuration"], config))
        return 78
    client = ModelClient(config)  # validates local config without a network request
    await client.aclose()
    if args.check_config:
        print(scrubbed(summary["configuration"], config))
        return 0
    output = args.output or ROOT / "data" / "evaluations" / ("third-" + datetime.now(TZ).strftime("%Y%m%d-%H%M%S-%f"))
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    output.chmod(0o700)
    config = {**config, "debounce_seconds": 0, "max_model_calls": min(40, args.max_calls),
              "max_concurrent_agents": 2, "max_total_agents": 3, "max_agent_turns": 20,
              "analysis_timeout_seconds": args.timeout, "agent_timeout_seconds": min(args.timeout, 300),
              "max_output_tokens": 4000, "read_max_bytes": 15000000, "read_max_chars": 12000,
              "search_max_results": 3, "news_queries": [], "max_job_attempts": 1}
    store = Store(output / "evaluation.sqlite3", mode="live")
    store.set_meta("settings", config)
    service = Service(store)
    summary["output_directory"] = str(output)
    summary["database"] = str(store.path)
    summary["budget"] = {"research_rounds": 4, "max_model_calls_per_round": config["max_model_calls"],
                         "optional_protocol_probe_separate": bool(args.probe)}
    def checkpoint():
        write_json(output / "summary.json", summary, config)
    checkpoint()
    print(scrubbed({"stage": "evaluation_created", "output_directory": str(output)}, config), flush=True)
    try:
        if args.probe:
            try:
                probe = await probe_model(config)
                summary["protocol_probe"] = {"state": "passed", "result": probe, "meaning": "protocol only; not research success"}
            except ModelError as exc:
                summary["protocol_probe"] = {"state": "failed", "category": classify_failure(exc), "error": str(exc)}
                checkpoint()
                return 1
        symbols = [symbol(value) for value in args.symbols]
        store.save_universe({"watchlist": symbols, "candidates": symbols[:1]})
        purpose = ("独立真实链路验收。请基于实际取得的材料做小规模研究，按需要派发至少一个独立事实/资料子任务并取得其报告；"
                   "本轮研究边界仅为：从所给财报核对营业收入与归母净利润两个数值（含单位/报告期/同比口径），"
                   "结合给定metrics说明当前账户敞口、差异与证据缺口；不要求扩展为整份公司深度研究。"
                   "已有公共研究可复用，不必各账户重复提取整份财报。证据足够后及时形成条件性结论。"
                   "必要时追问或补查，不为测试制造无必要研究。区分客观事实、主体预测、外部意见、系统推论；"
                   "复用公共研究但独立分析当前账户。来源与证券的对应关系必须根据正文判断，不因同时提供就假定相关。"
                   "不要声称历史财报为今日更正，不将生成总结当外部事实。预算有限，优先检查已有材料与实际引用。公开来源：" + args.source_url)
        a = store.save_account(AccountInput(name="真实验收 A（独立临时账户）", cash="10000", preferences={"risk": "conservative", "horizon": "长期"},
            holdings=[{"symbol": symbols[0], "quantity": "100"}]), reason=purpose)
        b = store.save_account(AccountInput(name="真实验收 B（独立临时账户）", cash="20000", preferences={"risk": "balanced", "horizon": "中期"},
            holdings=[{"symbol": symbols[1], "quantity": "500"}]), reason=purpose)
        summary["account_ids"] = {"a": a["id"], "b": b["id"]}
        summary["steps"]["quotes"] = await service.refresh_quotes(symbols, force=True)
        summary["steps"]["quote_records"] = store.cache("quotes")
        source_tools = ResearchTools(store, {"id": "live-source-acquisition", "account_id": None}, config)
        reads = []
        for page in args.pages:
            result = await source_tools.read(args.source_url, page=page, limit=10000)
            reads.append({k: result.get(k) for k in ("url", "material_id", "material_identity", "access", "error", "fetched_at", "document_id", "returned_range", "returned_chars", "truncated")})
        summary["steps"]["public_material_reads"] = reads
        if config.get("search_provider") not in (None, "disabled"):
            summary["steps"]["search"] = await source_tools.search(args.query)
        else:
            summary["steps"]["search"] = {"status": "unconfigured", "note": "Public URL retrieval remains available."}
        checkpoint()
        store.request_analysis(None, "evaluation", "third:global", reason=purpose)
        run = store.claim_global()
        print(json.dumps({"stage": "public_research_started", "job_id": run["id"]}), flush=True)
        await service.execute_global(run)
        observed = observation(store, store.global_run(run["id"]), True)
        summary["rounds"].append(observed)
        write_json(output / "round-1-public.json", observed, config)
        checkpoint()
        if observed["state"] != "completed":
            summary["steps"]["remaining_model_rounds"] = "not_run_after_public_research_failure"
            return 1
        jobs = store.claim_jobs(limit=2)
        for index, job in enumerate(jobs, 2):
            print(json.dumps({"stage": "account_research_started", "round": index, "job_id": job["id"]}), flush=True)
            await service.execute(job)
            observed = observation(store, store.job(job["id"]))
            summary["rounds"].append(observed)
            write_json(output / f"round-{index}-account.json", observed, config)
            checkpoint()
        b_before = {"account": store.account(b["id"]), "analyses": store.analyses(b["id"]), "jobs": store.jobs(b["id"])}
        old_a_analyses = store.analyses(a["id"])
        current_a = store.account(a["id"])
        edited = store.save_account({**current_a, "holdings": [{"symbol": symbols[0], "quantity": "200"}],
                                    "expected_version": current_a["version"]}, a["id"], reason=purpose + "；本次 A 持仓由100股改为200股，复核最新敞口。")
        summary["steps"]["holding_edit"] = {"previous_version": current_a["version"], "new_version": edited["version"],
            "prior_analyses_stale": all(store.analysis(row["id"])["status"] == "stale" for row in old_a_analyses),
            "had_prior_analysis": bool(old_a_analyses), "new_job_id": edited["latest_job_id"]}
        # Only the explicitly edited account executes; unrelated deferred work stays inspectable.
        claimed = store.claim_jobs(limit=1, account_id=a["id"])
        for job in claimed:
            if job["id"] == edited["latest_job_id"]:
                print(json.dumps({"stage": "edited_account_research_started", "job_id": job["id"]}), flush=True)
                await service.execute(job)
                observed = observation(store, store.job(job["id"]))
                summary["rounds"].append(observed)
                write_json(output / "round-4-edited-account.json", observed, config)
        b_after = {"account": store.account(b["id"]), "analyses": store.analyses(b["id"]), "jobs": store.jobs(b["id"])}
        summary["steps"]["account_b_unchanged_after_a_edit"] = b_before == b_after
        summary["steps"]["current_shared_research"] = store.research_records(limit=100)
        summary["steps"]["account_views"] = {key: {"account": service.account(account_id), "analyses": store.analyses(account_id),
            "active_recommendations": store.recommendations_page(account_id, view="active"),
            "history_recommendations": store.recommendations_page(account_id, view="history")} for key, account_id in summary["account_ids"].items()}
        if any(run["state"] != "completed" for run in summary["rounds"]):
            return 1
        checks = [len(summary["rounds"]) == 4, bool(summary["steps"]["account_b_unchanged_after_a_edit"]),
                  bool(old_a_analyses), summary["steps"]["holding_edit"]["prior_analyses_stale"],
                  any(read.get("returned_chars", 0) for read in reads),
                  any(q.get("price") is not None for q in summary["steps"]["quote_records"].values()),
                  all(run["observations"]["report_returned"] for run in summary["rounds"]),
                  any(run["observations"]["subagent_tool_call"] for run in summary["rounds"])]
        summary["mechanical_observations_passed"] = all(checks)
        return 0 if all(checks) else 2
    except Exception as exc:
        summary["execution_failure"] = {"type": type(exc).__name__, "category": classify_failure(exc),
            "message": str(exc) if isinstance(exc, ModelError) else type(exc).__name__}
        return 1
    finally:
        summary["finished_at"] = datetime.now(TZ).isoformat()
        summary["materials"] = store.material_index(limit=100, public_only=True)
        write_json(output / "materials.json", store.materials(), config)
        checkpoint()
        print(scrubbed({"output_directory": str(output), "database": str(store.path), "round_states": [r["state"] for r in summary["rounds"]],
                       "research_quality": summary["research_quality"], "execution_failure": summary.get("execution_failure"),
                       "mechanical_observations_passed": summary.get("mechanical_observations_passed")}, config), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-config", action="store_true")
    parser.add_argument("--settings-db", type=Path, default=db_path("live"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--source-url", default=PDF_URL)
    parser.add_argument("--pages", type=int, nargs="+", default=[28, 29])
    parser.add_argument("--symbols", nargs=2, default=["SH:600519", "SZ:000001"])
    parser.add_argument("--query", default="贵州茅台 2026 半年度报告 归属于母公司 净利润")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--max-calls", type=int, choices=range(1, 41), default=40)
    parser.add_argument("--probe", action="store_true", help="Optional separate two-call model protocol check")
    args = parser.parse_args()
    try:
        config, origin = configuration(args.settings_db)
        return asyncio.run(verify(args, config, origin))
    except (ModelConfigurationError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"status": "invalid_configuration", "error_type": type(exc).__name__}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
