#!/usr/bin/env python3
"""Real model evaluation; deterministic synthetic INPUTS are never model replies.

Ten labelled cases, including two isolated accounts for the same public evidence.
--live-url uses real public retrieval instead of the synthetic-input tools.
Exit 78=unconfigured/skipped, 1=execution failure, 2=structural review needed,
0=structural observations satisfied; research quality always requires review.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.agents import run_research
from app.config import defaults
from app.domain import metrics, symbol
from app.model import ModelConfigurationError, ModelError, probe_model
from app import providers
from evaluation_cases import cases

TZ = ZoneInfo("Asia/Shanghai")


def index(item):
    keys = ("id", "url", "source_title", "classification", "access", "published_at", "event_period", "version", "synthetic_input")
    return {k: item[k] for k in keys if k in item} | {"summary": str(item.get("content") or item.get("claim") or item.get("text") or "")[:250]}


class EvaluationTools:
    """Fixture documents, real model. Live mode never falls back to fixtures."""
    def __init__(self, config: dict, case: dict, live=False):
        self.config, self.case, self.live = config, case, live
        self.materials = []
        self.documents = copy.deepcopy(case.get("documents", []))
        self.read_observations = []
        if not live:
            for i in range(case.get("seed_material_count", 0)):
                self.materials.append({"id": f"seed-{i}", "source_title": f"历史研究索引 {i}", "url": self.documents[0]["url"],
                                       "text": (f"合成历史材料 {i}，一般背景内容，不含本次净利润数值。" * 300), "access": "excerpt", "synthetic_input": True})

    async def search(self, query):
        if self.live:
            return await providers.search(query, self.config)
        return {"mode": "synthetic_input_evaluation", "status": "ok", "query": query,
                "results": [index(item) | {"snippet": item["text"][:1500]} for item in self.documents]}

    def _document(self, url):
        found = next((item for item in self.documents if item["url"] == url), None)
        if found is None:
            raise ValueError("合成评估输入中没有此 URL；未提供虚构替代材料")
        return found

    async def read(self, url, **kwargs):
        if self.live:
            return await providers.read_url(url, self.config, **kwargs)
        doc = self._document(url)
        if doc.get("read_error"):
            self.read_observations.append({"url": url, "access": "failed"})
            return index(doc) | {"access": "failed", "text": None, "error": doc["read_error"]}
        if doc.get("pages"):
            start = int(kwargs.get("page") or kwargs.get("start_page") or 1)
            end = int(kwargs.get("page") or kwargs.get("end_page") or start)
            if not 1 <= start <= end <= len(doc["pages"]) or end - start > 9:
                raise ValueError("需要有效物理页码，每次最多 10 页")
            self.read_observations.append({"url": url, "start_page": start, "end_page": end})
            return index(doc) | {"access": "partial", "truncated": True, "page_count": len(doc["pages"]),
                                 "pages": [{"page": p, "text": doc["pages"][p - 1]} for p in range(start, end + 1)]}
        offset, limit = max(0, int(kwargs.get("offset", 0))), max(1, min(int(kwargs.get("limit", 8000)), 20000))
        end = min(offset + limit, len(doc["text"]))
        self.read_observations.append({"url": url, "start": offset, "end": end})
        return {**index(doc), "text": doc["text"][offset:end], "start": offset, "end": end,
                "total_chars": len(doc["text"]), "truncated": offset > 0 or end < len(doc["text"]),
                "next_start": end if end < len(doc["text"]) else None,
                **{k: doc[k] for k in ("previous_version", "original_url") if k in doc}}

    async def pdf_metadata(self, url, **kwargs):
        if self.live:
            return await providers.pdf_metadata(url, self.config, **kwargs)
        doc = self._document(url)
        return index(doc) | {"page_count": len(doc.get("pages", [])), "access": "metadata_only"}

    async def search_pdf(self, url, query, start_page=1, end_page=None, max_results=20):
        if self.live:
            return await providers.search_pdf(url, query, self.config, start_page=start_page, end_page=end_page, max_results=max_results)
        doc = self._document(url)
        pages = doc.get("pages", [])
        matches = [{"page": i + 1, "snippet": text[:600]} for i, text in enumerate(pages) if start_page <= i + 1 <= (end_page or len(pages)) and query in text]
        return {"url": url, "matches": matches[:max_results], "total_matches": len(matches), "synthetic_input": True, "access": "search_snippets"}

    async def save_material(self, material):
        saved = {**material, "id": f"eval-material-{len(self.materials) + 1}", "evaluation_only": True, "synthetic_input": not self.live}
        self.materials.append(saved)
        return saved

    async def list_materials(self, query="", limit=20, offset=0):
        items = [item for item in self.materials if not query or query.lower() in json.dumps(item, ensure_ascii=False).lower()]
        return [index(item) for item in items[offset:offset + limit]]

    async def search_materials(self, query, limit=20, offset=0):
        return await self.list_materials(query, limit, offset)

    async def get_material(self, material_id):
        return await self.read_material_range(material_id)

    async def read_material_range(self, material_id, start=0, length=8000):
        item = next((item for item in self.materials if item["id"] == material_id), None)
        if item is None:
            raise ValueError("材料 ID 不属于本评估案例")
        text = str(item.get("text") or item.get("content") or item.get("claim") or "")
        end = min(start + length, len(text))
        return index(item) | {"text": text[start:end], "start": start, "end": end, "total_chars": len(text),
                              "next_start": end if end < len(text) else None, "truncated": start > 0 or end < len(text)}

    async def calculate(self, operation, values):
        nums = [Decimal(str(v)) for v in values]
        if not nums or len(nums) > 1000 or any(not n.is_finite() for n in nums):
            raise ValueError("需要有限数值输入")
        if operation == "sum":
            result = sum(nums, Decimal(0))
        elif operation in {"multiply", "product"}:
            result = Decimal(1)
            for value in nums:
                result *= value
        elif operation in {"divide", "percent"} and len(nums) == 2:
            result = nums[0] / nums[1] * (100 if operation == "percent" else 1)
        elif operation == "subtract" and len(nums) == 2:
            result = nums[0] - nums[1]
        else:
            raise ValueError("不支持该运算或输入数量")
        return {"operation": operation, "inputs": [str(v) for v in nums], "result": str(result), "classification": "calculation"}

    def __getattr__(self, name):
        if name not in {"history", "sectors", "sector_members", "sector_history", "benchmark_history", "fundamentals"}:
            raise AttributeError(name)
        async def market_tool(**kwargs):
            if not self.live:
                return {"status": "unavailable", "error": "本合成案例未提供此市场数据；不得当作零或真实行情"}
            return await getattr(providers.MarketProvider(self.config), name)(**kwargs)
        return market_tool


def write_json(file, value):
    file.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    file.chmod(0o600)


async def evaluate_one(args, config, case, account_spec, output, live):
    security, stamp = symbol(args.symbol), datetime.now(TZ).isoformat()
    account = {"id": account_spec.get("id", f"eval-{case['id']}"), "name": "独立模型评估账户", "version": 1,
               "cash": account_spec.get("cash"), "preferences": {"risk": "balanced"},
               "holdings": [{"symbol": security, "quantity": account_spec.get("quantity", "100"), "cost_price": None}]}
    if live:
        market = providers.MarketProvider(config)
        quote, base = await asyncio.gather(market.quote(security), market.security(security))
    else:
        quote = {"symbol": security, "price": "10", "status": "demo", "as_of": stamp, "source": "合成评估输入，非真实行情"}
        base = {"symbol": security, "name": "虚构测试公司", "kind": "stock", "sector": "测试板块"}
    purpose = ("本轮是独立真实模型评估，不写入生产投资研究历史。请派发至少两个独立子任务：资料事实与分类研究、当前账户输入与敞口分析；"
               "收到报告后至少对一个原子 Agent 发出 follow_up 并等待补充结果。最终引用实际取得的证据，处理反向信息和旧结论变化，"
               "不虚构成本、现金、收益、材料页码或买卖依据。具体研究主题：" + case["title"])
    purpose += "。当前使用真实公开来源。" if live else "。所有来源均为明确标注的合成测试输入，模型必须真实调用；不要作为实际投资建议。"
    if case["id"] == "long_report":
        purpose += "请从给定长篇财报定位归母净利润与经营现金流，并按实际页码取回；可用 list_materials 复用已有历史索引。"
    context = {"task_id": f"eval-{case['id']}-{account['id']}", "account": account, "input_version": 1,
               "metrics": metrics(account, {security: quote}, {security: base}), "candidates": [], "data_as_of": quote.get("as_of"),
               "previous_analysis": case.get("previous_analysis"), "reason": purpose,
               "specified_source_urls": args.live_url or [doc["url"] for doc in case.get("documents", [])],
               "research_query": args.query or case["title"], "evaluation_mode": "real_model_live_sources" if live else "real_model_synthetic_inputs"}
    tools, events = EvaluationTools(config, case, live), []

    async def emit(event):
        events.append(event)
        if event["type"] in {"agent_created", "agent_completed", "agent_failed"}:
            print(f"[{case['id']}/{account['id']}] {event['type']}: {event['agent_id']}", flush=True)

    result, failure = None, None
    try:
        result = await run_research(context, config, tools, emit)
    except Exception as exc:
        failure = {"type": type(exc).__name__, "message": str(exc) if isinstance(exc, ModelError) else type(exc).__name__}
    created = {e["agent_id"] for e in events if e["type"] == "agent_created"}
    continued = [e for e in events if e["type"] == "agent_completed" and e["payload"].get("round", 0) >= 2]
    saved = [item for item in tools.materials if item.get("evaluation_only")]
    ids = {item["id"] for item in tools.materials}
    cited = (result or {}).get("material_ids") or []
    report = (result or {}).get("report", "")
    visible_citation = any(item["id"] in report or (item.get("url") and item["url"] in report) for item in saved)
    checks = {"two_independent_subtasks_observed": len(created) >= 2, "continued_original_subtask_completed": bool(continued),
              "materials_saved": bool(saved), "final_report_returned": bool(report),
              "known_material_references": bool(cited or visible_citation) and all(item in ids for item in cited),
              "account_identity_preserved": bool(result and result.get("account_id") == account["id"])}
    if case["id"] == "long_report":
        checks["page_28_actually_requested"] = any(o.get("start_page", 100) <= 28 <= o.get("end_page", 0) for o in tools.read_observations)
    # Citation presence is observable; whether it SUPPORTS a conclusion is human review.
    record = {"label": "真实模型评估；非投资历史", "case": case["id"], "title": case["title"], "created_at": stamp,
              "mode": context["evaluation_mode"], "structural_checks": checks, "failure": failure,
              "human_review": {"status": "pending", "rubric": case["rubric"], "additional_criteria": ["主 Agent 是否实际利用子 Agent 证据", "引用是否支持对应判断", "是否考虑反向证据", "信息不足是否诚实表达"]},
              "observed_classifications": sorted({str(item.get("classification", item.get("category", "unknown"))) for item in saved}),
              "context": context, "result": result, "materials": saved, "events": events, "read_observations": tools.read_observations}
    directory = output / f"{case['id']}--{account['id']}"
    directory.mkdir()
    write_json(directory / "evaluation.json", record)
    (directory / "report.md").write_text("# 实际模型输出（待人工审阅）\n\n" + (report or f"执行失败：{failure}") + "\n\n输入模式：" + context["evaluation_mode"], encoding="utf-8")
    (directory / "report.md").chmod(0o600)
    return {"case": case["id"], "account": account["id"], "structural_checks": checks, "failure": failure,
            "human_review": "pending", "record": str(directory / "evaluation.json"), "model_usage": (result or {}).get("model_usage")}


async def evaluate(args):
    config = defaults()
    config.update(max_model_calls=args.max_calls, max_agent_turns=24, max_total_agents=6,
                  model_allow_no_key=args.allow_local_no_key, chat_use_max_completion_tokens=args.chat_max_completion_tokens)
    available = cases()
    if args.list_cases:
        print(json.dumps([{k: case[k] for k in ("id", "title", "rubric")} for case in available], ensure_ascii=False, indent=2))
        return 0
    if args.check_config:
        print(json.dumps({"model_name_present": bool(config.get("model_name")), "model_key_present": bool(os.getenv(config["model_key_env"])),
                          "search_provider_configured": config.get("search_provider") not in {None, "disabled"},
                          "search_key_present": bool(os.getenv(config["search_key_env"]))}, ensure_ascii=False))
        return 0
    probe = await probe_model(config)  # No search/market calls when credentials absent.
    print("PROBE PASSED: 实际两轮函数协议；不是研究质量验收。", flush=True)
    if args.probe_only:
        return 0
    if args.live_url:
        selected = [{"id": "live_sources", "title": args.query or "指定真实公开材料研究", "rubric": ["引用与实际访问范围一致", "事实、预测、观点、冲突逐条区分", "结合当前账户且保留未知输入"]}]
    else:
        selected = [case for case in available if not args.case or case["id"] in args.case]
    output = Path(args.output) if args.output else ROOT / "data" / "evaluations" / datetime.now(TZ).strftime("eval-%Y%m%d-%H%M%S-%f")
    output.mkdir(parents=True, exist_ok=False)
    manifest = {"mode": "real_model_live_sources" if args.live_url else "real_model_synthetic_inputs", "model": config["model_name"],
                "protocol": config["model_protocol"], "provider_host": urlsplit(config["model_base_url"]).hostname,
                "probe": probe, "prompt_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT / "app" / "prompts").glob("*.txt")},
                "input_suite_sha256": hashlib.sha256(json.dumps(selected, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                "created_at": datetime.now(TZ).isoformat(), "runs": [], "human_review": "pending", "quality_passed": None}
    for case in selected:
        for account_spec in case.get("accounts", [{}]):
            try:
                record = await evaluate_one(args, config, case, account_spec, output, bool(args.live_url))
            except Exception as exc:
                record = {"case": case["id"], "failure": {"type": type(exc).__name__, "message": str(exc) if isinstance(exc, ModelError) else type(exc).__name__}, "human_review": "pending"}
            manifest["runs"].append(record)
            write_json(output / "manifest.json", manifest)  # Retain completed cases after interruption.
    failures = [r for r in manifest["runs"] if r.get("failure")]
    structural = bool(manifest["runs"]) and all(not r.get("failure") and bool(r.get("structural_checks")) and all(r["structural_checks"].values()) for r in manifest["runs"])
    print(json.dumps({"case_count": len(selected), "run_count": len(manifest["runs"]), "execution_failures": len(failures), "structural_checks_passed": structural,
                      "quality": "REQUIRES_HUMAN_REVIEW", "saved_to": str(output)}, ensure_ascii=False, indent=2))
    return 1 if failures else (0 if structural else 2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-only", action="store_true")
    parser.add_argument("--check-config", action="store_true", help="仅显示配置/凭据是否存在，绝不输出密钥")
    parser.add_argument("--list-cases", action="store_true", help="列出十类合成输入与人工审阅标准，不调用模型")
    parser.add_argument("--case", action="append", choices=[case["id"] for case in cases()], help="仅运行指定案例，可重复；默认十类全部执行")
    parser.add_argument("--live-url", action="append", help="指定真实公开来源；不使用合成输入")
    parser.add_argument("--query")
    parser.add_argument("--symbol", default="SH:600000")
    parser.add_argument("--max-calls", type=int, default=40, choices=range(8, 101), metavar="8..100", help="每个案例/账户最多实际请求数，默认 40")
    parser.add_argument("--allow-local-no-key", action="store_true")
    parser.add_argument("--chat-max-completion-tokens", action="store_true")
    parser.add_argument("--output", help="新建目录，不覆盖已有结果")
    args = parser.parse_args()
    try:
        code = asyncio.run(evaluate(args))
    except ModelConfigurationError as exc:
        print(f"SKIPPED (缺少真实模型配置；所有未运行案例仍未验证): {exc}", file=sys.stderr)
        code = 78
    except ModelError as exc:
        print(f"FAILED (实际模型协议检查失败): {exc}", file=sys.stderr)
        code = 1
    except (ValueError, OSError) as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
