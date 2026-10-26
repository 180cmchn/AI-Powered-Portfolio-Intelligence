"""Local manager / independent subagent runtime with event-driven coordination.

Every agent owns a ModelSession and real request/tool/result loop. No mock model
or demo response lives in production code. Account/version CAS is the caller's
responsibility; cancellation of run_research cancels all its child work.
"""
from __future__ import annotations

import asyncio
import copy
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from .model import ModelBudgetExceeded, ModelClient, ModelError, ModelSession, ToolCall
from .context import recall

PROMPTS = Path(__file__).with_name("prompts")
SHANGHAI = ZoneInfo("Asia/Shanghai")


class ResearchTimeout(ModelError):
    pass


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
    _schema("calculate", "用代码做精确数值计算（sum/multiply/divide/percent）；只传已知输入，不以缺失代替零。",
            {"operation": STRING, "values": {"type": "array", "items": {"type": ["string", "number"]}}}, ["operation", "values"]),
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
        self.config = config
        self.tools = research_tools
        self.emit = emit
        self.account_id = (context.get("account") or {}).get("id")
        self.input_version = context.get("input_version", (context.get("account") or {}).get("version"))
        self.task_id = context.get("task_id") or context.get("job_id") or str(uuid.uuid4())
        self.calls = 0
        self.budget_lock = asyncio.Lock()
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
            self.calls += 1

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
        await self._publish("agent_started", "manager", {"title": "主 Agent", "reason": self.context.get("reason")})
        if self.synthesis_only:
            await self._publish("synthesis_resumed", "manager", {"source_task_id": self.synthesis_checkpoint.get("task_id"), "max_calls": self.config.get("synthesis_max_calls", 4)})
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
        max_turns = int(self.config.get("max_agent_turns", 12))
        reference_candidate = None
        for turn_index in range(max_turns):
            definitions = RESEARCH_TOOLS + (MANAGER_TOOLS if manager else CHILD_TOOLS)
            if manager and self.synthesis_only:
                permitted = {"get_material", "read_material_range", "list_materials", "search_materials", "recall_context", "calculate", "save_material", "list_recommendations", "get_recommendation", "finish_analysis"}
                definitions = [tool for tool in definitions if tool["name"] in permitted]
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
                turn = await self.client.complete(session, definitions)
            except asyncio.CancelledError:
                await self._publish("model_error", agent_id, {"error": "模型请求随研究任务取消", "requests": copy.deepcopy(session.requests[request_start:])})
                raise
            except Exception as exc:
                partial = getattr(exc, "native_turn", None)
                if partial is not None:
                    await self._record_native(agent_id, partial)
                await self._publish("model_error", agent_id, {"error": self._safe_error(exc), "requests": copy.deepcopy(session.requests[request_start:]),
                    "native_search": self.client.native_status()})
                if reference_candidate is not None:
                    reference_candidate['reference_resolution_note'] = '引用修正请求未完成；保留原报告的已解析部分和明确缺口。'
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
                    continue
                if not turn.text.strip():
                    raise ModelError("Agent 未提交可见报告")
                return {"report" if manager else "summary": turn.text}
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
                        if turn_index == max_turns - 1 or self.calls >= int(self.config.get('max_model_calls', 40)):
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
                return submitted
        if reference_candidate is not None:
            return reference_candidate
        raise ModelBudgetExceeded("Agent 达到本轮最大工具循环次数；阶段材料已保留")

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
        reports = []
        for child in self.children.values():
            for report in child.reports:
                visible = {key: copy.deepcopy(report[key]) for key in ("summary", "findings", "material_ids", "open_questions") if key in report}
                if isinstance(visible.get("summary"), str) and len(visible["summary"]) > 12000:
                    visible["summary"] = visible["summary"][:12000]
                    visible["summary_truncated"] = True
                reports.append({"agent_id": child.id, "title": child.title, **visible})
        if not reports and not declaration:
            return {}
        material_index = [{key: copy.deepcopy(material[key]) for key in ("id", "url", "source_title", "classification", "access", "fetched_at") if key in material} for material in self.materials]
        checkpoint = {"phase": "synthesis", "task_id": self.task_id, "account_id": self.account_id,
            "input_version": self.input_version, "scope": self.context.get("scope", "account"),
            "data_as_of": self.context.get("data_as_of"), "reports": reports,
            "materials": material_index, "material_ids": sorted({m["id"] for m in material_index if m.get("id")} |
                {mid for report in reports for mid in report.get("material_ids", []) if isinstance(mid, str)} |
                {mid for mid in (declaration or {}).get("material_ids", []) if isinstance(mid, str)}),
            "calculations": copy.deepcopy(self.calculations), "metrics": copy.deepcopy(self.context.get("metrics")),
            "open_questions": [question for report in reports for question in report.get("open_questions", [])],
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
            child = self._child(self._text(args, "agent_id"))
            message = self._text(args, "message" if name == "send_message" else "task")
            child.inbox.append(message)
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
                        child.reports.append(report)
                        if child.inbox:
                            # Message arrived while the last model response was pending.
                            # Do not lose it just because that response ended a report.
                            await self._publish("agent_progress", child.id, {"summary": report.get("summary", ""), "follow_up_pending": True})
                            continue
                        child.status = "completed"
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
