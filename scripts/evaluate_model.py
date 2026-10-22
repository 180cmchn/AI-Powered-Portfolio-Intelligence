#!/usr/bin/env python3
"""Run a real configured model against a labelled research evaluation.

Default: synthetic *input material*, real model and real local multi-agent loops.
--live-url: actual public retrieval, actual configured quotes; no fixture content.
There are no canned model replies. Outputs never enter production analysis DBs.
Exit 78 = configuration absent (SKIPPED), 1 = run failed, 2 = review needed,
0 = requested structural checks passed; quality always needs human review.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agents import run_research  # noqa: E402
from app.config import defaults  # noqa: E402
from app.domain import metrics, symbol  # noqa: E402
from app.model import ModelConfigurationError, ModelError, probe_model  # noqa: E402
from app import providers  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")
FIXTURE_URL = "https://example.invalid/model-evaluation/mixed-report"
FIXTURE_TEXT = """【合成评估材料，不对应任何真实公司的真实业绩，不是投资依据】
以下是一段测试报道，发布时间未知；当前只可见转载摘要，原始报告暂时打不开。
摘要称：评估公司在其半年报披露营业收入为 10 亿元，归母净利润为 1 亿元。
管理层表示，预计下半年销量增长 20%，但这一预测尚未实现。
评论员据此推荐现在买入，并声称股价下个月一定上涨。
另一段未经核实的转述称收入数字可能需要更正；没有附更正公告。
上述数值、预测、评论和传闻仅用于检验模型分类与不确定性表达，不应被当作真实证券信息。
"""


class EvaluationTools:
    def __init__(self, config: dict, live: bool):
        self.config, self.live = config, live
        self.materials: list[dict] = []

    async def search(self, query):
        if self.live:
            return await providers.search(query, self.config)
        return {"mode": "synthetic_input_evaluation", "status": "ok", "query": query,
                "results": [{"title": "合成混合报道：仅评估输入", "url": FIXTURE_URL,
                             "snippet": FIXTURE_TEXT, "published_at": None, "access": "excerpt",
                             "source": "本地合成评估材料"}]}

    async def read(self, url):
        if self.live:
            return await providers.read_url(url, self.config)
        return {"mode": "synthetic_input_evaluation", "url": url, "access": "failed",
                "text": None, "published_at": None,
                "error": "评估情景：原文访问失败；实际可见范围只有 search 返回的合成摘要。"}

    async def save_material(self, material):
        saved = {**material, "id": f"eval-material-{len(self.materials) + 1}",
                 "evaluation_only": True, "synthetic_input": not self.live}
        self.materials.append(saved)
        return saved

    async def list_materials(self, query=""):
        return [item for item in self.materials if not query or query.lower() in json.dumps(item, ensure_ascii=False).lower()]

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


async def evaluate(args) -> int:
    config = defaults()
    config.update(max_model_calls=args.max_calls, max_agent_turns=20,
                  max_total_agents=6, model_allow_no_key=args.allow_local_no_key,
                  chat_use_max_completion_tokens=args.chat_max_completion_tokens)
    # Probe first: missing credentials exits before any market/search call.
    probe = await probe_model(config)
    print("PROBE PASSED: 函数调用和返回值续接已实测；研究质量仍待检查。", flush=True)
    if args.probe_only:
        return 0

    live = bool(args.live_url)
    stamp = datetime.now(TZ).isoformat()
    security = symbol(args.symbol)
    account = {"id": "model-evaluation-only", "name": "真实模型评估用虚拟账户",
               "version": 1, "cash": None, "preferences": {"risk": "balanced"},
               "holdings": [{"symbol": security, "quantity": "100", "cost_price": None}]}
    if live:
        market = providers.MarketProvider(config)
        quote = await market.quote(security)
        base = await market.security(security)
    else:
        quote = {"symbol": security, "price": "10", "status": "demo", "as_of": stamp,
                 "source": "明确合成的评估输入价格，非真实行情"}
        base = {"symbol": security, "name": "合成评估持仓", "kind": "stock", "sector": "评估用板块"}
    values = metrics(account, {security: quote}, {security: base})
    purpose = (
        "本轮是独立的真实模型验收，不写入投资研究历史。请真实派发至少两个独立子任务："
        "一个研究并逐条分类资料，另一个研究该虚拟账户的不完整输入与组合风险。"
        "收到首轮报告后，至少向一个原子 Agent 发出 follow_up 补充任务并等待补充结果。"
        "最终报告引用你实际拿到的来源，解释事实、主体预测、外部观点和不确定内容，"
        "明确成本、现金及未知发布时间；不得虚构收益，也不得将荐股观点改写为事实。"
    )
    if live:
        purpose += " 这是实际公开资料评估，请读取指定 URL；搜索不可用时仍可直接读指定来源。"
    else:
        purpose += " 本轮 search 仅返回明确标识的合成输入材料，read 故意模拟原文无法打开。所有数值都是测试输入，不作真实投资建议。"
    context = {"task_id": "eval-" + datetime.now(TZ).strftime("%Y%m%d-%H%M%S-%f"),
               "account": account, "input_version": 1, "metrics": values,
               "candidates": [], "data_as_of": quote.get("as_of"), "previous_analysis": None,
               "reason": purpose, "specified_source_urls": args.live_url or [FIXTURE_URL],
               "research_query": args.query or "阅读给定材料并分别提取披露事实、预测、观点及冲突",
               "evaluation_mode": "real_model_live_sources" if live else "real_model_synthetic_inputs"}
    events = []

    async def emit(event):
        events.append(event)
        if event["type"] in {"agent_created", "agent_started", "agent_completed", "agent_failed", "agent_message_sent"}:
            print(f"{event['type']}: {event['agent_id']}", flush=True)

    tools = EvaluationTools(config, live)
    output = Path(args.output) if args.output else ROOT / "data" / "evaluations" / context["task_id"]
    output.mkdir(parents=True, exist_ok=False)
    result, failure = None, None
    try:
        result = await run_research(context, config, tools, emit)
    except Exception as exc:
        failure = {"type": type(exc).__name__, "message": str(exc) if isinstance(exc, ModelError) else type(exc).__name__}
    created = {event["agent_id"] for event in events if event["type"] == "agent_created"}
    continued = [event for event in events if event["type"] == "agent_completed" and event["payload"].get("round", 0) >= 2]
    # Structural observations only. Classification correctness is manually judged.
    observed = {str(item.get("classification", item.get("category", "unknown"))) for item in tools.materials}
    checks = {"two_independent_subtasks_observed": len(created) >= 2,
              "continued_original_subtask_completed": bool(continued),
              "materials_saved": bool(tools.materials),
              "final_report_returned": bool(result and result.get("report"))}
    record = {"label": "真实模型评估；非真实投资分析历史", "mode": context["evaluation_mode"],
              "created_at": stamp, "model": config.get("model_name"), "protocol": config.get("model_protocol"),
              "probe": probe, "structural_checks": checks, "observed_classifications": sorted(observed),
              "failure": failure, "context": context, "result": result,
              "materials": tools.materials, "events": events,
              "human_review_required": ["混合材料是否逐条正确分类", "最终结论是否引用实际可见事实并保留访问限制",
                                        "外部荐股和未来预测是否被错误当作已发生事实", "缺现金/成本是否仍杜撰完整资产或收益",
                                        "反向信息、触发和失效条件是否与证据匹配"]}
    file = output / "evaluation.json"
    file.write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    file.chmod(0o600)
    report_file = output / "report.md"
    report_file.write_text("# 真实模型评估结果（非投资建议）\n\n" +
                          ("输入含明确合成样例，不是联网财报或行情。\n\n" if not live else "输入来自实际公开读取；详见 evaluation.json 访问范围。\n\n") +
                          ((result or {}).get("report") or f"本次未完成：{failure}"), encoding="utf-8")
    report_file.chmod(0o600)
    print(json.dumps({"structural_checks": checks, "observed_classifications": sorted(observed),
                      "quality": "REQUIRES_HUMAN_REVIEW", "saved_to": str(output)}, ensure_ascii=False, indent=2))
    if failure:
        return 1
    return 0 if all(checks.values()) else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-only", action="store_true", help="只进行两轮付费模型协议检查")
    parser.add_argument("--live-url", action="append", help="实际公开材料 URL，可重复；未提供时使用明确合成输入")
    parser.add_argument("--query", help="实际研究问题或检索词")
    parser.add_argument("--symbol", default="SH:600000", help="虚拟评估持仓证券，默认 SH:600000，100 股")
    parser.add_argument("--max-calls", type=int, default=40, choices=range(8, 101), metavar="8..100")
    parser.add_argument("--allow-local-no-key", action="store_true", help="仅适用于本机无需认证的模型服务")
    parser.add_argument("--chat-max-completion-tokens", action="store_true")
    parser.add_argument("--output", help="新建评估输出目录（不会覆盖现有目录）")
    args = parser.parse_args()
    try:
        code = asyncio.run(evaluate(args))
    except ModelConfigurationError as exc:
        print(f"SKIPPED (缺少真实模型配置): {exc}", file=sys.stderr)
        code = 78
    except ModelError as exc:
        print(f"FAILED (真实模型协议检查未通过): {exc}", file=sys.stderr)
        code = 1
    except (ValueError, OSError) as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
