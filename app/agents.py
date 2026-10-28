"""Local manager / independent subagent runtime with event-driven coordination.

Every agent owns a ModelSession and real request/tool/result loop. No mock model
or demo response lives in production code. Account/version CAS is the caller's
responsibility; cancellation of run_research cancels all its child work.
"""
from __future__ import annotations

import asyncio
import copy
import json
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from .model import ModelBudgetExceeded, ModelClient, ModelConfigurationError, ModelError, ModelSession, ToolCall
from .context import archive, recall

PROMPTS = Path(__file__).with_name("prompts")
SHANGHAI = ZoneInfo("Asia/Shanghai")


class ResearchTimeout(ModelError):
    pass


class _CloseoutRequired(ModelBudgetExceeded):
    """No HTTP was sent: rebuild this request with the final-report tools."""


def _schema(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": required or [], "additionalProperties": False}}


STRING = {"type": "string"}
STRINGS = {"type": "array", "items": STRING}
INTEGER = {"type": "integer", "minimum": 0}
PAGE = {"type": "integer", "minimum": 1}
SUGGESTION = {"type": "object", "properties": {
    "id": {"type": "string", "description": "更新当前账户已有建议时填写其 ID；省略则新建并替代同证券旧建议"},
    "security_id": STRING, "action": {"type": "string", "enum": ["watch", "buy_candidate", "add_candidate", "hold", "reduce_candidate", "exit_candidate"]},
    "basis": STRING, "material_ids": STRINGS, "valid_until": {"type": ["string", "null"]},
    "price_conditions": {"type": "array", "items": {"type": "object", "properties": {
        "field": {"type": "string", "enum": ["price"]}, "operator": {"type": "string", "enum": ["gte", "lte", "between"]},
        "value": {"type": ["string", "number"]}, "lower": {"type": ["string", "number"]}, "upper": {"type": ["string", "number"]}}, "required": ["operator"]}},
    "agent_conditions": STRINGS}, "required": ["security_id", "action", "basis"]}
RESEARCH_RECORD = {"type": "object", "properties": {
    "kind": {"type": "string", "enum": ["market", "sector", "candidate"]}, "security_id": STRING, "sector_id": STRING,
    "title": STRING, "reason": STRING, "findings": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
    "material_ids": STRINGS, "status": STRING, "key_changes": STRING, "account_fit": STRING}, "required": ["kind", "title", "reason"]}
RESEARCH_TOOLS = [
    _schema("read", "读取公开网页正文与附件；可按 PDF 物理页码（从 1 开始）或文本字符范围读取。访问范围及截断状态以返回值为准。",
            {"url": STRING, "start_page": PAGE, "end_page": PAGE, "page": PAGE, "offset": INTEGER, "limit": {"type": "integer", "minimum": 1, "maximum": 20000}, "refresh": {"type": "boolean"}}, ["url"]),
    _schema("pdf_metadata", "获取 PDF 标题、元数据及总页数；长财报先定位再读取。", {"url": STRING, "refresh": {"type": "boolean"}}, ["url"]),
    _schema("search_pdf", "检索长 PDF，返回匹配片段与实际页码，再用 read 指定页范围读取。",
            {"url": STRING, "query": STRING, "start_page": PAGE, "end_page": PAGE, "max_results": {"type": "integer", "minimum": 1, "maximum": 50}}, ["url", "query"]),
    _schema("save_material", "保存逐条分类的 Agent 提取结果；允许未知元数据，不设可信度准入门槛。用 material_ids 关联 read/search 返回的原始材料 ID，不能编造；没有原文ID则保留缺口。材料可含 claim, category, security_ids, title, url, publisher, published_at, event_period, retrieved_at, excerpt, unit, access_scope, uncertainties。",
            {"material": {"type": "object", "additionalProperties": True}}, ["material"]),
    _schema("list_materials", "分页列出轻量材料索引（不包含全文）；用 ID 按需读取。", {"query": STRING, "limit": {"type": "integer", "minimum": 1, "maximum": 50}, "offset": INTEGER}),
    _schema("search_materials", "检索已有公开材料并分页返回索引、片段及位置，不代表已读完整原文。", {"query": STRING, "limit": {"type": "integer", "minimum": 1, "maximum": 50}, "offset": INTEGER}, ["query"]),
    _schema("get_material", "按材料 ID 读取元数据与有限首段；正文较长时按 next_start 继续。", {"material_id": STRING}, ["material_id"]),
    _schema("read_material_range", "读取指定材料字符范围，保留来源、页码、访问限制及范围信息。", {"material_id": STRING, "start": INTEGER, "length": {"type": "integer", "minimum": 1, "maximum": 12000}}, ["material_id"]),
    _schema("list_recommendations", "分页取得当前账户建议，先按业务状态筛选。active 包含所有仍需关注的建议；按 next_offset 继续读取，不能将价格满足视为判断有效。公共研究不能读取账户建议。",
            {"view": {"type": "string", "enum": ["active", "triggered", "needs_review", "history", "all"]},
             "limit": {"type": "integer", "minimum": 1, "maximum": 100}, "offset": INTEGER}),
    _schema("get_recommendation", "按已有建议 ID 读取当前账户的完整建议、价格状态与复核需求，禁止跨账户访问。", {"recommendation_id": STRING}, ["recommendation_id"]),
    _schema("recall_context", "检索当前 Agent 已整理的可见上下文；只读自己的历史。提供 record_id 时按字符范围取回原记录，否则按 query 分页查摘要与引用。",
            {"record_id": STRING, "query": STRING, "offset": INTEGER, "limit": {"type": "integer", "minimum": 1, "maximum": 20}, "start": INTEGER, "length": {"type": "integer", "minimum": 1, "maximum": 12000}}),
    _schema("history", "获取历史 OHLCV 与成交额，保留复权方式、时间和数据源限制。", {"symbol": STRING, "start": STRING, "end": STRING, "interval": STRING, "adjust": STRING, "limit": {"type": "integer", "minimum": 1, "maximum": 1000}}, ["symbol"]),
    _schema("sectors", "分页查询行业或概念板块基础信息；offset 为 limit 的非负整数倍；数据权限不足时如实返回。", {"kind": STRING, "limit": {"type": "integer", "minimum": 1, "maximum": 100}, "offset": INTEGER}),
    _schema("sector_members", "分页获取板块成分证券，offset 为 limit 的非负整数倍。", {"sector_id": STRING, "limit": {"type": "integer", "minimum": 1, "maximum": 100}, "offset": INTEGER}, ["sector_id"]),
    _schema("sector_history", "获取板块历史表现，可与基准 history 数据比较。", {"sector_id": STRING, "start": STRING, "end": STRING, "limit": {"type": "integer", "minimum": 1, "maximum": 1000}}, ["sector_id"]),
    _schema("benchmark_history", "获取市场基准历史表现，支持 CSI300、SSE50、CSI500，保留数据覆盖与限制。", {"benchmark": STRING, "start": STRING, "end": STRING, "limit": {"type": "integer", "minimum": 1, "maximum": 1000}}),
    _schema("fundamentals", "获取可用财务指标、报告期和披露时间；不可用字段保持未知。", {"symbol": STRING, "periods": {"type": "integer", "minimum": 1, "maximum": 40}}, ["symbol"]),
    _schema("calculate", "用代码做精确数值计算；percent 为 a/b*100，不是增长率。增长率先 subtract 求差，再 percent；只传已知输入，不以缺失代替零。",
            {"operation": {"type": "string", "enum": ["sum", "multiply", "product", "divide", "ratio", "percent", "subtract", "difference"]}, "values": {"type": "array", "items": {"type": ["string", "number"]}}}, ["operation", "values"]),
]
MANAGER_TOOLS = [
    _schema("prepare_synthesis", "资料研究完成后保存可见成果并进入汇总。之后仅使用已有材料、计算与报告工具；失败时可只恢复汇总。", {"summary": STRING, "material_ids": STRINGS, "open_questions": STRINGS}, ["summary"]),
    _schema("spawn_research", "创建有独立模型上下文和工具循环的子 Agent；按需要创建，不要求固定数量。",
            {"task": STRING, "title": STRING, "context": {"type": "object", "additionalProperties": True}}, ["task"]),
    _schema("send_message", "给子 Agent 发送补充消息。运行中在下个安全边界接收；已完成则继续同一会话。",
            {"agent_id": STRING, "message": STRING}, ["agent_id", "message"]),
    _schema("follow_up", "在原子 Agent 的独立会话中继续研究，保留此前报告和材料。",
            {"agent_id": STRING, "task": STRING}, ["agent_id", "task"]),
    _schema("wait_agents", "以事件等待子任务进展/完成，无需反复查询。空 agent_ids 表示所有子任务。返回状态与新增可见消息。",
            {"agent_ids": STRINGS, "timeout_seconds": {"type": "number", "minimum": 0, "maximum": 60}}),
    _schema("agent_status", "查看指定或全部子任务的状态、阶段发现和报告；需要等待时使用 wait_agents。", {"agent_id": STRING}),
    _schema("cancel_agent", "取消已无需要的子任务；已经保存的公开材料与阶段报告仍保留。", {"agent_id": STRING, "reason": STRING}, ["agent_id"]),
    _schema("finish_analysis", "提交账户最终报告；重要结论附来源与条件，数值取自代码计算。",
            {"report": STRING, "findings": {"type": "array", "items": {"type": "object", "additionalProperties": True}}, "material_ids": STRINGS,
             "valid_until": {"type": ["string", "null"]}, "uncertainties": STRINGS,
             "suggestions": {"type": "array", "items": SUGGESTION, "maxItems": 100},
             "maintained_suggestion_ids": STRINGS,
             "review_suggestion_ids": STRINGS,
             "revoked_suggestion_ids": STRINGS,
             "information_impacts": {"type": "array", "items": {"type": "object", "properties": {
                 "material_id": STRING, "status": {"type": "string", "enum": ["relevant", "irrelevant", "uncertain"]},
                 "symbols": STRINGS, "sectors": STRINGS, "market_wide": {"type": "boolean"}, "explanation": STRING,
                 "event_key": STRING}, "required": ["material_id", "status", "explanation"]}},
             "research_records": {"type": "array", "items": RESEARCH_RECORD}}, ["report"]),
]
CHILD_TOOLS = [
    _schema("report_progress", "把阶段发现交给主 Agent，不结束任务。仅提交可见摘要，不提交隐藏思维链。", {"summary": STRING, "material_ids": STRINGS}, ["summary"]),
    _schema("submit_report", "结束本次子任务并交付材料；主 Agent 后续可继续此会话。",
            {"summary": STRING, "findings": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
             "material_ids": STRINGS, "open_questions": STRINGS}, ["summary"]),
]


@dataclass
class AgentState:
    id: str
    title: str
    task: str
    session: ModelSession
    status: str = "queued"
    inbox: list[str] = field(default_factory=list)
    pending_messages: list[str] = field(default_factory=list)
    reports: list[dict] = field(default_factory=list)
    progress: list[dict] = field(default_factory=list)
    task_handle: asyncio.Task | None = None
    error: str | None = None
    round: int = 1

    def snapshot(self) -> dict:
        return {"agent_id": self.id, "title": self.title, "task": self.task,
                "status": self.status, "round": self.round, "reports": copy.deepcopy(self.reports),
                "progress": copy.deepcopy(self.progress), "error": self.error}


class ResearchRuntime:
    def __init__(self, context: dict, config: dict, research_tools: Any,
                 emit: Callable[[dict], Awaitable[None]]) -> None:
        self.context = copy.deepcopy(context)
        self.config = config = copy.deepcopy(config)
        from .config import model_config_summary
        self.model_config = model_config_summary(config)
        self.context['model_config'] = copy.deepcopy(self.model_config)
        self.tools = research_tools
        self.emit = emit
        self.account_id = (context.get("account") or {}).get("id")
        self.input_version = context.get("input_version", (context.get("account") or {}).get("version"))
        self.task_id = context.get("task_id") or context.get("job_id") or str(uuid.uuid4())
        self.calls = 0
        self.budget_lock = asyncio.Lock()
        self.request_owner = ContextVar("research_request_owner", default=("manager", False))
        self.budget_closing = False
        self.closeout_waited = False
        self.deadline: float | None = None
        self.client = ModelClient(config, before_request=self._use_budget)
        self.semaphore = asyncio.Semaphore(max(1, int(config.get("max_concurrent_agents", 3))))
        self.children: dict[str, AgentState] = {}
        self.handles: set[asyncio.Task] = set()
        self.events: list[dict] = []
        self.condition = asyncio.Condition()
        self.delivered_sequence = 0
        self.materials: list[dict] = []
        self.calculations: list[dict] = []
        self.synthesis_checkpoint = copy.deepcopy(context.get("synthesis_checkpoint"))
        self.synthesis_only = bool(self.synthesis_checkpoint)
        self.synthesis_started_calls = 0
        self.checkpoint_signature = ""
        self.closed = False
        self.reference_feedback_sent = False
        self.manager_session = self.client.session((PROMPTS / "manager.txt").read_text(), self.context)
        self.manager_session.task_id = self.task_id
        self.manager_session.agent_id = "manager"
        if self.synthesis_only:
            self.manager_session.phase = "synthesis"
            self.manager_session.native_search = False
            self.client.add_user(self.manager_session, {"recovery": "基于已保存的可见研究成果完成最终汇总；不重复联网或创建子任务。当前持仓与版本以本次 context 为准。"})

    async def _use_budget(self) -> None:
        async with self.budget_lock:
            if self.synthesis_only and self.calls - self.synthesis_started_calls >= max(1, min(int(self.config.get("synthesis_max_calls", 4)), 8)):
                raise ModelBudgetExceeded("汇总恢复达到有限调用预算；检查点保留，未发布半份最终报告")
            if self.calls >= int(self.config.get("max_model_calls", 40)):
                raise ModelBudgetExceeded("已达到本轮模型调用预算；已保存持仓及材料保留，可调整预算后重试")
            agent_id, final_request = self.request_owner.get()
            remaining = int(self.config.get("max_model_calls", 40)) - self.calls
            # This guard also runs before HTTP retries, under the shared lock.
            # A child must never consume the manager's final submission slot.
            if agent_id != "manager" and remaining <= 1:
                raise ModelBudgetExceeded("共享调用预算已为主 Agent 最终汇总保留；子任务未完成部分保持未知")
            if self._shared_closeout() and not final_request:
                raise _CloseoutRequired("共享预算进入收尾；本次尚未发送 HTTP")
            self.calls += 1

    def _shared_closeout(self) -> bool:
        remaining = self._remaining_calls()
        active = sum(child.status in {"queued", "running"} for child in self.children.values())
        # Leave one submission per active child and two manager calls (final
        # report plus, when available, citation correction / a failed request).
        if remaining <= active + 2:
            self.budget_closing = True
        return self.budget_closing

    def _remaining_calls(self) -> int:
        remaining = int(self.config.get("max_model_calls", 40)) - self.calls
        if self.synthesis_only:
            remaining = min(remaining, max(1, min(int(self.config.get("synthesis_max_calls", 4)), 8)) - (self.calls - self.synthesis_started_calls))
        return max(0, remaining)

    async def _wait_for_closeout(self) -> None:
        if self.closeout_waited:
            return
        self.closeout_waited = True
        active = [child.task_handle for child in self.children.values()
                  if child.task_handle and not child.task_handle.done()]
        wait_seconds = 30.0
        if self.deadline is not None:
            request_time = float(self.config.get("synthesis_request_timeout_seconds", self.config.get("model_request_timeout_seconds", 90)))
            remaining_time = max(0.0, self.deadline - time.monotonic())
            wait_seconds = min(wait_seconds, max(0.0, remaining_time - min(request_time, remaining_time / 2)))
        await self._publish("budget_closeout", "manager", {"remaining_model_calls": self._remaining_calls(),
            "active_children": len(active), "wait_seconds": wait_seconds,
            "note": "预算内收尾；等待已有子任务提交可见成果，未完成研究保留缺口。"})
        if active and wait_seconds > 0:
            # Timeout does not silently turn unfinished work into a report.
            await asyncio.wait(active, timeout=wait_seconds)

    def _pin_runtime_status(self, session: ModelSession, agent_id: str, remaining_turns: int, final_request: bool) -> None:
        """Refresh the retained seed, so compaction cannot erase collaboration."""
        states = list(self.children.values()) if agent_id == "manager" else [self.children[agent_id]]
        visible = []
        # Summaries are bounded; complete reports remain available via agent_status
        # during research. Explicit truncation is not represented as complete text.
        summary_limit = max(160, min(1200, int(self.config.get("max_context_chars", 240_000)) // max(16, len(states) * 8)))
        for child in states:
            item = {"agent_id": child.id, "title": child.title[:200], "status": child.status, "round": child.round,
                    "error": child.error, "report_count": len(child.reports),
                    "pending_messages": list(dict.fromkeys([*child.pending_messages, *child.inbox]))}
            if child.reports:
                report = child.reports[-1]
                summary = str(report.get("summary") or "")
                item["latest_report"] = {"research_round": report.get("research_round"), "summary": summary[:summary_limit],
                    "summary_truncated": len(summary) > summary_limit,
                    "material_ids": report.get("material_ids", []), "open_questions": report.get("open_questions", [])}
            if child.progress:
                item["latest_progress"] = {"summary": str(child.progress[-1].get("summary") or "")[:summary_limit],
                    "material_ids": child.progress[-1].get("material_ids", []), "is_final_report": False}
            visible.append(item)
        status = {"agent_id": agent_id, "model_calls_used": self.calls,
            "remaining_shared_model_calls": self._remaining_calls(),
            "remaining_agent_turns": remaining_turns, "closing": self.budget_closing,
            "children": visible,
            "instruction": "这是当前运行状态。已有子任务不要重复创建；旧报告不代表后续补查已完成，阶段进展不等于最终报告。"}
        if final_request:
            status["instruction"] += " 当前只提交已有证据支持的报告；明确列出未完成任务、资料缺口及其影响，未知数字或分类保持未知，不再扩展研究。"
        limit = max(1000, int(self.config.get("max_context_chars", 240_000)) // 3)
        if len(json.dumps(status, ensure_ascii=False)) > limit:
            record = archive(session, status, "runtime_status")
            status.update(details_truncated=True, record_id=record["record_id"],
                retrieve="recall_context(record_id,start,length) 取回完整可见状态；研究阶段也可用 agent_status。未取回的待办和缺口不能视为已解决。")
            for item in visible:
                item["pending_message_count"] = len(item["pending_messages"])
                item["unresolved"] = item["status"] != "completed" or bool(item["pending_messages"]) or bool((item.get("latest_report") or {}).get("open_questions"))
                item["pending_messages"] = [str(message)[:160] for message in item["pending_messages"][:2]]
                item["error"] = str(item["error"])[:160] if item["error"] else None
                item["title"] = item["title"][:80]
                item["details_truncated"] = True
                item.pop("latest_progress", None)
                if item.get("latest_report"):
                    report = item["latest_report"]
                    item["latest_report"] = {"research_round": report["research_round"], "summary": report["summary"][:160],
                        "summary_truncated": True, "material_ids": [str(mid)[:120] for mid in report["material_ids"][:2]],
                        "open_question_count": len(report["open_questions"])}
            # Preserve every child ID/status/round and unresolved flag even when
            # a large number of reports needs retrieval instead of inline text.
            for key in ("latest_report", "pending_messages", "error", "title"):
                if len(json.dumps(status, ensure_ascii=False)) <= limit:
                    break
                for item in visible:
                    item.pop(key, None)
            while visible and len(json.dumps(status, ensure_ascii=False)) > limit:
                omitted = visible.pop()
                status["children_omitted"] = status.get("children_omitted", 0) + 1
                status["unresolved_children_omitted"] = status.get("unresolved_children_omitted", 0) + int(omitted["unresolved"])
        seed = json.loads(session.history[0]["content"])
        seed["runtime_status"] = status
        session.history[0]["content"] = json.dumps(seed, ensure_ascii=False, default=str)

    async def _publish(self, kind: str, agent_id: str, payload: dict) -> dict:
        # No raw model request/response or opaque reasoning state is logged.
        event = {"sequence": len(self.events) + 1, "type": kind, "agent_id": agent_id,
                 "parent_id": None if agent_id == "manager" else "manager",
                 "task_id": self.task_id, "account_id": self.account_id, "input_version": self.input_version,
                 "timestamp": datetime.now(SHANGHAI).isoformat(), "payload": payload}
        event = json.loads(json.dumps(event, ensure_ascii=False, default=str, allow_nan=False))
        self.events.append(event)
        async with self.condition:
            self.condition.notify_all()
        await self.emit(copy.deepcopy(event))
        return event

    async def run(self) -> dict:
        self.deadline = time.monotonic() + float(self.config.get("analysis_timeout_seconds", 600))
        await self._publish("agent_started", "manager", {"title": "主 Agent", "reason": self.context.get("reason"), "model_config": self.model_config})
        if self.synthesis_only:
            await self._publish("synthesis_resumed", "manager", {"source_task_id": self.synthesis_checkpoint.get("task_id"), "max_calls": self.config.get("synthesis_max_calls", 4),
                "configuration_relation": self.synthesis_checkpoint.get("configuration_relation"), "model_config": self.model_config})
        try:
            async with asyncio.timeout(float(self.config.get("analysis_timeout_seconds", 600))):
                result = await self._loop("manager", self.manager_session, manager=True)
                await self._cancel_children()
                result.setdefault("report", result.pop("summary", ""))
                result.setdefault("findings", [])
                result.update({"task_id": self.task_id, "account_id": self.account_id,
                               "input_version": self.input_version, "data_as_of": self.context.get("data_as_of"),
                               "agents": [child.snapshot() for child in self.children.values()],
                               "model_calls": self.calls, "model_usage": self.client.usage_summary(self.client.requests),
                               "model_config": copy.deepcopy(self.model_config),
                               "synthesis_recovered": bool(self.context.get("synthesis_checkpoint")),
                               "native_search": self.client.native_status(),
                               "context_compactions": self.manager_session.compactions + sum(c.session.compactions for c in self.children.values()),
                               "materials": copy.deepcopy(self.materials)})
                await self._publish("agent_completed", "manager", {"report": result["report"], "model_calls": self.calls, "model_usage": result["model_usage"]})
                return json.loads(json.dumps(result, ensure_ascii=False, default=str, allow_nan=False))
        except TimeoutError as exc:
            await self._publish("agent_failed", "manager", {"error": "账户分析超过配置时限；阶段材料已保留"})
            raise ResearchTimeout("账户分析超过配置时限；可查看阶段材料并重试") from exc
        except asyncio.CancelledError:
            await self._publish("agent_cancelled", "manager", {"reason": "分析被取消或账户版本已更新"})
            raise
        except Exception as exc:
            await self._publish("agent_failed", "manager", {"error": self._safe_error(exc)})
            raise
        finally:
            await self.close()

    async def close(self) -> None:
        self.closed = True
        await self._cancel_children()
        await self.client.aclose()

    async def _cancel_children(self) -> None:
        active = list(self.handles)
        for task in active:
            if not task.done():
                task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)

    def _safe_error(self, exc: Exception) -> str:
        text = str(exc) or type(exc).__name__
        if self.client.key:
            text = text.replace(self.client.key, "[REDACTED]")
        return text[:1200]

    async def _loop(self, agent_id: str, session: ModelSession, *, manager: bool) -> dict:
        session.task_id = self.task_id
        max_turns = int(self.config.get("max_agent_turns", 12))
        if not manager:
            # A follow-up is a new bounded research round; the previous round
            # may have disabled native search solely for its final submission.
            session.native_search = True
        reference_candidate = None
        reference_report_counts = {}
        final_update_used = False
        for turn_index in range(max_turns):
            definitions = RESEARCH_TOOLS + (MANAGER_TOOLS if manager else CHILD_TOOLS)
            if manager and self.synthesis_only:
                permitted = {"get_material", "read_material_range", "list_materials", "search_materials", "recall_context", "calculate", "save_material", "list_recommendations", "get_recommendation", "finish_analysis"}
                definitions = [tool for tool in definitions if tool["name"] in permitted]
            remaining_turns = max_turns - turn_index
            final_request = final_update_used or self._shared_closeout() or (max_turns >= 6 and remaining_turns == 1)
            if max_turns >= 6 and remaining_turns <= 3:
                self.client.add_user(session, {"resource_budget": {"remaining_agent_turns": remaining_turns,
                    "remaining_shared_model_calls": max(0, int(self.config.get("max_model_calls", 40)) - self.calls)},
                    "instruction": "本轮预算即将用完。整理已经取得的可见证据并及时提交完整结论，未知保持未知，不继续扩展研究。最后一轮仅提交报告；未完成的子任务和资料缺口须明确说明。"})
                if remaining_turns == 1:
                    final_tool = "finish_analysis" if manager else "submit_report"
                    definitions = [tool for tool in definitions if tool["name"] == final_tool]
                    session.native_search = False
                    if manager:
                        session.phase = "synthesis"
            if final_request:
                if manager:
                    await self._wait_for_closeout()
                definitions = [tool for tool in definitions if tool["name"] == ("finish_analysis" if manager else "submit_report")]
                session.native_search = False
                if manager:
                    session.phase = "synthesis"
            allowed = {item["name"] for item in definitions}
            if manager:
                events = self._new_child_events()
                if events:
                    self.client.add_user(session, {"research_events": events})
                if not self.synthesis_only and self.children and all(child.status in {"completed", "failed", "cancelled"} for child in self.children.values()):
                    if any(child.reports for child in self.children.values()):
                        await self._save_checkpoint()
                        session.phase = "synthesis"
            else:
                self._deliver_inbox(self.children[agent_id])
            request_start, compactions = len(session.requests), session.compactions
            try:
                while True:
                    self._pin_runtime_status(session, agent_id, remaining_turns, final_request)
                    request_report_counts = {child.id: len(child.reports) for child in self.children.values()}
                    token = self.request_owner.set((agent_id, final_request))
                    try:
                        turn = await self.client.complete(session, definitions)
                        break
                    except _CloseoutRequired:
                        # Concurrent requests or a retry reached the shared tail
                        # after the payload was prepared. Rebuild before HTTP.
                        final_request = True
                        definitions = [tool for tool in definitions if tool["name"] == ("finish_analysis" if manager else "submit_report")]
                        allowed = {item["name"] for item in definitions}
                        session.native_search = False
                        if manager:
                            session.phase = "synthesis"
                            await self._wait_for_closeout()
                            events = self._new_child_events()
                            if events:
                                self.client.add_user(session, {"research_events": events})
                            await self._save_checkpoint()
                    finally:
                        self.request_owner.reset(token)
            except asyncio.CancelledError:
                await self._publish("model_error", agent_id, {"error": "模型请求随研究任务取消", "requests": copy.deepcopy(session.requests[request_start:])})
                raise
            except Exception as exc:
                partial = getattr(exc, "native_turn", None)
                if partial is not None:
                    await self._record_native(agent_id, partial)
                await self._publish("model_error", agent_id, {"error": self._safe_error(exc), "requests": copy.deepcopy(session.requests[request_start:]),
                    "native_search": self.client.native_status()})
                if isinstance(exc, ModelConfigurationError):
                    raise
                if final_update_used:
                    raise  # A failed final update does not make the earlier report current.
                if reference_candidate is not None:
                    reference_candidate['reference_resolution_note'] = '引用修正请求未完成；保留原报告的已解析部分和明确缺口。'
                    if manager:
                        await self._defer_late_final(reference_candidate, reference_report_counts, can_update=False)
                    return reference_candidate
                raise
            await self._publish("model_usage", agent_id, {"requests": copy.deepcopy(session.requests[request_start:]), "session": self.client.usage_summary(session.requests)})
            native_materials = await self._record_native(agent_id, turn)
            if session.compactions > compactions:
                await self._publish("context_compacted", agent_id, {"compactions": session.compactions, "archived_records": len(session.archive), "retrieval_tool": "recall_context"})
            if not turn.calls:
                if native_materials:
                    self.client.add_user(session, {"native_source_materials": native_materials})
                if turn.continuation_required:
                    if final_update_used:
                        raise ModelError("最终补充汇总未提交报告；可见研究检查点保留")
                    continue
                if not turn.text.strip():
                    raise ModelError("Agent 未提交可见报告")
                result = {"report" if manager else "summary": turn.text}
                if manager and await self._defer_late_final(result, request_report_counts,
                        can_update=not final_update_used and remaining_turns > 1 and self._remaining_calls() > 0):
                    final_update_used = True
                    continue
                if manager and final_update_used:
                    result["late_child_update_performed"] = True
                return result
            if turn.text.strip():
                await self._publish("agent_message", agent_id, {"summary": turn.text[:4000]})
            results: list[tuple[ToolCall, Any]] = []
            submitted = None
            # Resolve all calls before inserting messages, preserving protocol order.
            # Spawn calls return immediately, so subsequent child model loops run in parallel.
            for call in turn.calls:
                await self._publish("tool_started", agent_id, {"tool": call.name, "arguments": call.arguments})
                try:
                    if call.error:
                        raise ValueError(call.error)
                    if call.name not in allowed:
                        raise ValueError("工具不在该 Agent 的权限范围内")
                    output = await self._dispatch(agent_id, call.name, call.arguments)
                    if call.name == 'finish_analysis' and output.get('reference_feedback'):
                        reference_candidate = output['reference_feedback']
                        reference_report_counts = request_report_counts
                        if final_update_used or turn_index == max_turns - 1 or self.calls >= int(self.config.get('max_model_calls', 40)):
                            output = {'accepted': True, 'resolved_result': reference_candidate,
                                      'note': '没有剩余修正调用预算；保留引用缺口并交付有效部分。'}
                    if call.name in {"finish_analysis", "submit_report"} and output.get('accepted'):
                        submitted = copy.deepcopy(output.get('resolved_result', call.arguments))
                except (ModelBudgetExceeded, asyncio.CancelledError):
                    raise
                except Exception as exc:
                    output = {"error": self._safe_error(exc), "failed": True}
                results.append((call, output))
                await self._publish("tool_result", agent_id, {"tool": call.name, "result": self._preview(output)})
            self.client.add_results(session, results)
            if manager and getattr(self, "_fresh_synthesis_context", None) is not None:
                fresh = self._fresh_synthesis_context
                session.history = fresh.history
                session.archive = fresh.archive
                self._fresh_synthesis_context = None
                self.client.add_user(session, {"phase": "synthesis", "instruction": "研究已完成。使用检查点中的可见成果，必要时读取已有材料或重新计算，然后提交 finish_analysis；保持报告精炼。"})
            if native_materials:
                self.client.add_user(session, {"native_source_materials": native_materials})
            if submitted is not None:
                if manager and await self._defer_late_final(submitted, request_report_counts,
                        can_update=not final_update_used and remaining_turns > 1 and self._remaining_calls() > 0):
                    final_update_used = True
                    continue
                if manager and final_update_used:
                    submitted["late_child_update_performed"] = True
                return submitted
            if final_update_used:
                raise ModelError("最终补充汇总未提交报告；可见研究检查点保留")
        if reference_candidate is not None:
            if manager:
                await self._defer_late_final(reference_candidate, reference_report_counts, can_update=False)
            return reference_candidate
        raise ModelBudgetExceeded("Agent 达到本轮最大工具循环次数；阶段材料已保留")

    async def _defer_late_final(self, result: dict, report_counts: dict, *, can_update: bool) -> bool:
        """Use at most one existing turn to absorb reports arriving during final HTTP."""
        late = [{"agent_id": child.id, "title": child.title,
                 "reports": copy.deepcopy(child.reports[report_counts.get(child.id, 0):])}
                for child in self.children.values()
                if child.status == "completed" and len(child.reports) > report_counts.get(child.id, 0)]
        if not late:
            return False
        if can_update:
            self.client.add_user(self.manager_session, {"late_completed_child_reports": late,
                "previous_final_report": result,
                "instruction": "这些子任务在上一份最终报告请求期间完成。请只做一次最终更新，吸收其可见报告、材料引用和缺口；纠正已过时的未交付状态。只提交 finish_analysis，不再联网、扩展研究或创建子任务；未知仍保持未知。"})
            await self._publish("late_child_final_update", "manager", {"agent_ids": [item["agent_id"] for item in late],
                "remaining_model_calls": self._remaining_calls()})
            return True
        result["unincorporated_child_reports"] = [{"agent_id": item["agent_id"], "title": item["title"],
            "research_rounds": [report.get("research_round") for report in item["reports"]],
            "material_ids": sorted({mid for report in item["reports"] for mid in report.get("material_ids", []) if isinstance(mid, str)}),
            "reason": "本次最终请求期间交付；剩余调用/轮数不足或已执行一次最终更新"} for item in late]
        note = "补充交付状态：" + "、".join(item["title"] for item in late) + "在本次报告请求期间完成，正文尚未吸收这次结果；相关结论仍需复核。"
        result["uncertainties"] = [*(result.get("uncertainties") or []), note]
        result["report"] = str(result.get("report") or "") + "\n\n" + note
        return False

    async def _record_native(self, agent_id, turn):
        native_materials = []
        if turn.native_events or turn.sources or turn.annotations:
            if hasattr(self.tools, "record_native_search"):
                native_materials = await self.tools.record_native_search(agent_id, turn.native_events, turn.sources, turn.annotations)
                self.materials.extend(copy.deepcopy(native_materials or []))
            await self._publish("native_search", agent_id, {"events": turn.native_events, "sources": turn.sources,
                "annotations": turn.annotations, "materials": native_materials, "capability": turn.native_status})
        return native_materials

    async def _save_checkpoint(self, declaration: dict | None = None) -> dict:
        """Persist only visible, finished research; service enforces freshness/CAS."""
        if declaration is None:
            declaration = copy.deepcopy((self.synthesis_checkpoint or {}).get("manager_summary") or {})
        reports = []
        for child in self.children.values():
            for report in child.reports:
                visible = {key: copy.deepcopy(report[key]) for key in ("summary", "findings", "material_ids", "open_questions", "research_round") if key in report}
                if isinstance(visible.get("summary"), str) and len(visible["summary"]) > 12000:
                    visible["summary"] = visible["summary"][:12000]
                    visible["summary_truncated"] = True
                reports.append({"agent_id": child.id, "title": child.title, **visible})
        if not reports and not declaration:
            return {}
        unresolved_tasks = [{"agent_id": child.id, "title": child.title, "task": child.task,
            "status": child.status, "round": child.round, "error": child.error,
            "pending_messages": list(dict.fromkeys([*child.pending_messages, *child.inbox]))}
            for child in self.children.values()
            if child.status != "completed" or child.pending_messages or child.inbox]
        material_index = [{key: copy.deepcopy(material[key]) for key in ("id", "url", "source_title", "classification", "access", "fetched_at") if key in material} for material in self.materials]
        checkpoint = {"phase": "synthesis", "task_id": self.task_id, "account_id": self.account_id,
            "model_config": copy.deepcopy(self.model_config),
            "input_version": self.input_version, "scope": self.context.get("scope", "account"),
            "data_as_of": self.context.get("data_as_of"), "reports": reports,
            "materials": material_index, "material_ids": sorted({m["id"] for m in material_index if m.get("id")} |
                {mid for report in reports for mid in report.get("material_ids", []) if isinstance(mid, str)} |
                {mid for mid in (declaration or {}).get("material_ids", []) if isinstance(mid, str)}),
            "calculations": copy.deepcopy(self.calculations), "metrics": copy.deepcopy(self.context.get("metrics")),
            "open_questions": [*(declaration.get("open_questions") or []),
                *[question for report in reports for question in report.get("open_questions", [])]],
            "unresolved_tasks": unresolved_tasks,
            "unresolved_tasks_note": "旧子报告不代表后续补查已完成；最终汇总须保留这些未决任务及其影响。" if unresolved_tasks else None,
            "manager_summary": copy.deepcopy(declaration or {}), "model_calls_used": self.calls}
        signature = json.dumps(checkpoint, sort_keys=True, default=str)
        if signature != self.checkpoint_signature:
            self.checkpoint_signature = signature
            checkpoint["created_at"] = datetime.now(SHANGHAI).isoformat()
            self.synthesis_checkpoint = checkpoint
            if hasattr(self.tools, "save_checkpoint"):
                await self.tools.save_checkpoint(copy.deepcopy(checkpoint))
            await self._publish("synthesis_checkpoint", "manager", {"phase": "synthesis", "reports": len(reports),
                "material_ids": checkpoint["material_ids"], "calculations": len(self.calculations)})
        return checkpoint

    def _preview(self, value: Any) -> Any:
        serialized = json.dumps(value, ensure_ascii=False, default=str)
        if len(serialized) <= 16000:
            return value
        return {"truncated_for_event": True, "preview": serialized[:16000],
                "note": "完整材料通过来源 URL、材料 ID 或当前会话 recall_context 按需取回。"}

    def _deliver_inbox(self, child: AgentState) -> None:
        if child.inbox:
            self.client.add_user(child.session, {"manager_messages": list(child.inbox)})
            child.inbox.clear()

    def _new_child_events(self) -> list[dict]:
        events = [event for event in self.events[self.delivered_sequence:]
                  if event["agent_id"] != "manager" and event["type"] not in {"tool_started", "tool_result", "model_usage", "context_compacted", "native_search"}]
        self.delivered_sequence = len(self.events)
        events = copy.deepcopy(events)
        for event in events:
            if event["type"] == "material_saved":
                material = event["payload"].get("material", {})
                event["payload"]["material"] = {k: v for k, v in material.items() if k in {"id", "url", "source_title", "category", "classification", "access", "material_ids"}}
                event["payload"]["material"]["excerpt"] = str(material.get("content") or "")[:1000]
        return events

    @staticmethod
    def _text(args: dict, name: str, *, required: bool = True) -> str:
        value = args.get(name)
        if value is None and not required:
            return ""
        if not isinstance(value, str) or (required and not value.strip()):
            raise ValueError(f"{name} 需要非空文本")
        return value

    def _child(self, agent_id: str) -> AgentState:
        if agent_id not in self.children:
            raise ValueError("子 Agent 不属于当前账户分析任务")
        return self.children[agent_id]

    async def _dispatch(self, agent_id: str, name: str, args: dict) -> Any:
        if name == "read":
            return await self.tools.read(self._text(args, "url"), **{key: args[key] for key in ("start_page", "end_page", "page", "offset", "limit", "refresh") if key in args})
        if name in {"pdf_metadata", "search_pdf", "history", "sectors", "sector_members", "sector_history", "benchmark_history", "fundamentals"}:
            allowed = {tool["name"]: set(tool["parameters"]["properties"]) for tool in RESEARCH_TOOLS}
            return await getattr(self.tools, name)(**{key: value for key, value in args.items() if key in allowed[name]})
        if name == "recall_context":
            session = self.manager_session if agent_id == "manager" else self._child(agent_id).session
            return recall(session, **{key: args[key] for key in ("record_id", "query", "offset", "limit", "start", "length") if key in args})
        if name == "save_material":
            material = args.get("material")
            if not isinstance(material, dict):
                raise ValueError("material 需要对象；元数据可以为空")
            material = copy.deepcopy(material)
            for source, target in (("category", "classification"), ("claim", "content"),
                                   ("title", "source_title"), ("retrieved_at", "fetched_at"), ("access_scope", "access")):
                if source in material and target not in material:
                    material[target] = material[source]
            result = await self.tools.save_material(material)
            self.materials.append(copy.deepcopy(result))
            await self._publish("material_saved", agent_id, {"material": copy.deepcopy(result)})
            return result
        if name in {"list_materials", "search_materials"}:
            limit, offset = max(1, min(int(args.get("limit", 20)), 50)), max(0, int(args.get("offset", 0)))
            result = await getattr(self.tools, name)(self._text(args, "query", required=name == "search_materials"), limit=limit, offset=offset)
            if isinstance(result, list):
                return {"items": result, "offset": offset, "limit": limit, "next_offset": offset + limit if len(result) == limit else None,
                        "note": "轻量索引；使用 get_material 或 read_material_range 获取正文。"}
            return result
        if name == "get_material":
            return await self.tools.get_material(self._text(args, "material_id"))
        if name == "read_material_range":
            return await self.tools.read_material_range(self._text(args, "material_id"), start=max(0, int(args.get("start", 0))), length=max(1, min(int(args.get("length", 8000)), 12000)))
        if name in {"list_recommendations", "get_recommendation"}:
            if not self.account_id:
                raise ValueError("公共研究不能读取账户私有建议")
            if name == "get_recommendation":
                return await self.tools.get_recommendation(self._text(args, "recommendation_id"))
            return await self.tools.list_recommendations(view=args.get("view", "active"),
                limit=max(1, min(int(args.get("limit", 30)), 100)), offset=max(0, int(args.get("offset", 0))))
        if name == "calculate":
            values = args.get("values")
            if not isinstance(values, list) or any(isinstance(v, bool) or not isinstance(v, (str, int, float)) for v in values):
                raise ValueError("values 必须为已知数值数组；缺失值不能按零计算")
            output = await self.tools.calculate(self._text(args, "operation"), values)
            self.calculations.append({"agent_id": agent_id, "operation": args["operation"], "values": copy.deepcopy(values), "result": copy.deepcopy(output)})
            self.calculations = self.calculations[-32:]
            return output
        if name == "prepare_synthesis":
            if any(child.status in {"queued", "running"} for child in self.children.values()):
                raise ValueError("仍有运行中的子任务；请先等待或明确取消后再汇总")
            self._text(args, "summary")
            await self._save_checkpoint(args)
            self.synthesis_only = True
            self.synthesis_started_calls = self.calls
            fresh = self.client.session(self.manager_session.system, {**self.context, "synthesis_checkpoint": self.synthesis_checkpoint})
            # This function's result is resolved by the ordinary tool loop first;
            # compacting the old session here would orphan its pending call.
            self.manager_session.phase = "synthesis"
            self.manager_session.native_search = False
            self._fresh_synthesis_context = fresh
            return {"accepted": True, "phase": "synthesis", "material_ids": self.synthesis_checkpoint.get("material_ids", [])}
        if name == "spawn_research":
            return await self._spawn(args)
        if name in {"send_message", "follow_up"}:
            if self._shared_closeout():
                raise ValueError("共享预算已进入收尾；不再追加研究，未决问题写入最终报告")
            child = self._child(self._text(args, "agent_id"))
            message = self._text(args, "message" if name == "send_message" else "task")
            child.inbox.append(message)
            child.pending_messages.append(message)
            if child.status in {"completed", "failed", "cancelled"}:
                child.round += 1
                child.error = None
                child.status = "queued"
                self._launch(child)
            await self._publish("agent_message_sent", child.id, {"summary": message, "round": child.round})
            return {"accepted": True, "agent_id": child.id, "status": child.status, "round": child.round}
        if name == "agent_status":
            selected = [self._child(args["agent_id"])] if args.get("agent_id") else list(self.children.values())
            return [child.snapshot() for child in selected]
        if name == "wait_agents":
            return await self._wait(args)
        if name == "cancel_agent":
            child = self._child(self._text(args, "agent_id"))
            if child.task_handle and not child.task_handle.done():
                child.task_handle.cancel()
                await asyncio.gather(child.task_handle, return_exceptions=True)
            return child.snapshot()
        if name == "report_progress":
            self._text(args, "summary")
            child = self._child(agent_id)
            child.progress.append(copy.deepcopy(args))
            await self._publish("agent_progress", agent_id, copy.deepcopy(args))
            return {"delivered": True}
        if name in {"submit_report", "finish_analysis"}:
            self._text(args, "summary" if name == "submit_report" else "report")
            if name == "finish_analysis":
                for key in ("suggestions", "research_records"):
                    if key in args and (not isinstance(args[key], list) or not all(isinstance(item, dict) for item in args[key])):
                        raise ValueError(f"{key} 需要对象数组")
                if len(args.get("suggestions") or []) > 100:
                    raise ValueError("单次 suggestions 最多 100 条；已有建议可用 maintained_suggestion_ids 明确延续")
                for key in ("revoked_suggestion_ids", "maintained_suggestion_ids", "review_suggestion_ids"):
                    if key in args and (not isinstance(args[key], list) or not all(isinstance(item, str) and item.strip() for item in args[key])):
                        raise ValueError(f"{key} 需要已有建议 ID 的非空字符串数组")
                if not self.account_id and any(args.get(key) for key in ("suggestions", "revoked_suggestion_ids", "maintained_suggestion_ids", "review_suggestion_ids")):
                    raise ValueError("全局研究没有账户持仓版本；请提交公共 research_records，账户建议的处理由对应账户研究决定")
                if hasattr(self.tools, 'resolve_references'):
                    resolved = await self.tools.resolve_references(args)
                    if resolved['reference_summary']['unresolved'] and not self.reference_feedback_sent:
                        self.reference_feedback_sent = True
                        return {'accepted': False, 'reference_feedback': resolved,
                                'message': '部分引用不存在或不在当前范围。可查已有材料修正；不要编造或静默换来源。仅反馈一次，再次提交仍有缺口将明确保留并交付有效部分。'}
                    return {'accepted': True, 'resolved_result': resolved}
            return {"accepted": True}
        raise ValueError("未知工具")

    async def _spawn(self, args: dict) -> dict:
        if self.closed:
            raise ValueError("分析任务已经关闭")
        if self._shared_closeout():
            raise ValueError("共享预算已进入收尾；不再创建子任务，汇总已有证据并说明缺口")
        if len(self.children) >= int(self.config.get("max_total_agents", 16)):
            raise ValueError("已达到本轮子 Agent 数量上限；可继续已有子任务或直接分析")
        task = self._text(args, "task")
        child_id = "research-" + uuid.uuid4().hex[:12]
        context = {"task": task, "parent_task_id": self.task_id,
                   "account": copy.deepcopy(self.context.get("account")),
                   "input_version": self.input_version, "metrics": copy.deepcopy(self.context.get("metrics")),
                   "scope": self.context.get("scope", "account"),
                   "data_as_of": self.context.get("data_as_of"), "task_context": copy.deepcopy(args.get("context", {}))}
        session = self.client.session((PROMPTS / "researcher.txt").read_text(), context)
        session.agent_id = child_id
        child = AgentState(child_id, args.get("title") or task[:60], task, session)
        self.children[child_id] = child
        await self._publish("agent_created", child_id, {"title": child.title, "task": task, "status": "queued"})
        self._launch(child)
        return {"agent_id": child_id, "status": child.status}

    def _launch(self, child: AgentState) -> None:
        task = asyncio.create_task(self._run_child(child), name=f"{self.task_id}:{child.id}:{child.round}")
        child.task_handle = task
        self.handles.add(task)
        task.add_done_callback(self.handles.discard)

    async def _run_child(self, child: AgentState) -> None:
        try:
            async with self.semaphore:
                child.status = "running"
                await self._publish("agent_started", child.id, {"title": child.title, "round": child.round})
                async with asyncio.timeout(float(self.config.get("agent_timeout_seconds", 120))):
                    while True:
                        report = await self._loop(child.id, child.session, manager=False)
                        report["research_round"] = child.round
                        child.reports.append(report)
                        # Messages delivered before this completed report have
                        # been covered; messages received during the request are
                        # still in the inbox and remain visibly unresolved.
                        child.pending_messages = list(child.inbox)
                        if child.inbox:
                            # Message arrived while the last model response was pending.
                            # Do not lose it just because that response ended a report.
                            await self._save_checkpoint()
                            await self._publish("agent_progress", child.id, {"summary": report.get("summary", ""), "follow_up_pending": True})
                            continue
                        child.status = "completed"
                        # Persist before the parent HTTP returns or fails. A next
                        # manager turn is not guaranteed after this completion.
                        await self._save_checkpoint()
                        await self._publish("agent_completed", child.id, {"report": report, "round": child.round})
                        return
        except TimeoutError:
            child.status = "failed"
            child.error = "子任务超过配置时限；已保存材料和阶段报告保留"
            self.client.resolve_pending(child.session, child.error)
            await self._publish("agent_failed", child.id, {"error": child.error})
        except asyncio.CancelledError:
            child.status = "cancelled"
            self.client.resolve_pending(child.session, "子任务已取消；此工具调用未完成")
            await self._publish("agent_cancelled", child.id, {"reason": "父任务结束、取消或账户版本更新"})
            raise
        except Exception as exc:
            child.status = "failed"
            child.error = self._safe_error(exc)
            self.client.resolve_pending(child.session, child.error)
            await self._publish("agent_failed", child.id, {"error": child.error})

    async def _wait(self, args: dict) -> dict:
        ids = args.get("agent_ids") or list(self.children)
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise ValueError("agent_ids 必须为字符串数组")
        selected = [self._child(i) for i in ids]
        timeout = max(0.0, min(float(args.get("timeout_seconds", 30)), 60.0))

        def changed() -> bool:
            return any(event["agent_id"] in ids and event["type"] not in {"tool_started", "tool_result", "model_usage", "context_compacted"}
                       for event in self.events[self.delivered_sequence:]) or all(
                child.status in {"completed", "failed", "cancelled"} for child in selected)

        async with self.condition:
            if not changed():
                try:
                    await asyncio.wait_for(self.condition.wait_for(changed), timeout=timeout)
                except TimeoutError:
                    pass
        # Return all undelivered child events so unrelated useful findings are not lost.
        return {"events": self._new_child_events(), "agents": [child.snapshot() for child in selected]}


async def run_research(context: dict, config: dict, research_tools: Any,
                       emit: Callable[[dict], Awaitable[None]]) -> dict:
    """Run one version-bound account research request; raises on top-level failure."""
    runtime = ResearchRuntime(context, config, research_tools, emit)
    return await runtime.run()
