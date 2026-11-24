"""Local manager / independent subagent runtime with event-driven coordination.

Every agent owns a ModelSession and real request/tool/result loop. No mock model
or demo response lives in production code. Account/version CAS is the caller's
responsibility; cancellation of run_research cancels all its child work.
"""
from __future__ import annotations

import asyncio
import copy
import json
import itertools
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from .clock import now_datetime
from pathlib import Path
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from .model import ModelBudgetExceeded, ModelClient, ModelConfigurationError, ModelError, ModelTimeoutError, ModelSession, ToolCall, cancellation_source
from .config import timeout_policy, timeout_policy_warnings
from .context import archive, recall, references, visible_message, compact_calculation_metadata, bounded_result
from .calculation_context import projection as calculation_projection, signature as calculation_signature
from .delivery import validate_final_result, DeliveryContractError, FinalSubmissionMissing, MAX_RESEARCH_RECORDS
from .recovery import manager_material_deliveries, restored_manager_material_evidence
from .report_metadata import prediction_context
from .market_materials import FIELDS as MARKET_ROW_FIELDS
from .research_progress import KnownLookupTracker, is_failed_tool_output, native_material_identity, tool_round_signature
from .source_projection import project_source_item, projected_result_matches, diverse_source_order

PROMPTS = Path(__file__).with_name("prompts")
SHANGHAI = ZoneInfo("Asia/Shanghai")


class ResearchTimeout(ModelError):
    code = 'analysis_budget_exhausted'


def _time_context(context):
    """Describe decision dates without changing the clock or elapsed deadlines."""
    execution_time = now_datetime(SHANGHAI).isoformat()
    historical = context.get('historical_evidence')
    replay = isinstance(historical, dict)
    decision_as_of = None if replay else execution_time
    basis = 'historical_checkpoint_unavailable' if replay else 'execution_time'
    checkpoint = historical.get('historical_checkpoint_at') if replay else None
    if isinstance(checkpoint, str):
        try:
            parsed = datetime.fromisoformat(checkpoint)
            if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                decision_as_of = parsed.isoformat()
                basis = 'historical_checkpoint_at'
        except ValueError:
            pass
    return {'execution_time': execution_time, 'data_as_of': copy.deepcopy(context.get('data_as_of')),
        'historical_replay': replay, 'decision_as_of': decision_as_of, 'decision_time_basis': basis,
        'instruction': ('execution_time 是本次执行时间，data_as_of 是资料/估值时点，decision_as_of 是本轮判断的时间基准。'
            '今天、下一交易日、尚未开盘等相对日期必须说明所依据的时点；历史行情与历史判断不能冒充当前状态。'
            + ('历史检查点缺失或没有合法时区，历史判断时点未知；使用明确绝对日期，不猜测相对日期。'
               if replay and decision_as_of is None else ''))}


def _team_context(team):
    """Model-visible attribution, not executable prompts or credential references."""
    if not isinstance(team, dict):
        return team
    result = {key: copy.deepcopy(team[key]) for key in
              ('team_id', 'catalog_version', 'override_revision', 'account_id', 'frozen_at') if key in team}
    result['roles'] = {role_id: {key: copy.deepcopy(role[key]) for key in
        ('role_id', 'version', 'prompt_version', 'tool_version', 'resolved') if key in role}
        for role_id, role in team.get('roles', {}).items()}
    result['connections'] = {provider_id: {key: provider[key] for key in
        ('id', 'revision', 'credential_revision', 'protocol') if key in provider}
        for provider_id, provider in team.get('connections', {}).items()}
    return result


def _source_index_context(materials, conclusions):
    """Keep cited source metadata; other fetched sources remain explicit ID indexes."""
    cited = set()

    def visit(value):
        if isinstance(value, dict):
            if isinstance(value.get('material_id'), str):
                cited.add(value['material_id'])
            material_ids = value.get('material_ids')
            if isinstance(material_ids, list):
                cited.update(mid for mid in material_ids if isinstance(mid, str))
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(conclusions)
    entries = [material for material in (materials if isinstance(materials, list) else []) if isinstance(material, dict)]
    return ([{key: material[key] for key in ('id', 'url', 'source_title', 'access', 'classification') if key in material}
             for material in entries if material.get('id') in cited],
            sorted({material['id'] for material in entries if isinstance(material.get('id'), str)}))


def _previous_analysis_context(value):
    if not isinstance(value, dict):
        return _research_context(value)
    raw_result = value.get('result')
    result = raw_result if isinstance(raw_result, dict) else {}
    projected = {key: _research_context(value[key]) for key in
        ('id', 'job_id', 'account_id', 'input_version', 'status', 'created_at', 'data_as_of', 'report') if key in value}
    # Last report and its conclusions remain account-scoped. Replaying its full
    # source bodies/HTTP diagnostics provides no new research evidence.
    projected['result'] = {key: (_team_context(result[key]) if key == 'team_snapshot' else _research_context(result[key])) for key in
        ('report', 'findings', 'suggestions', 'research_records', 'forecasts', 'uncertainties',
         'references', 'material_ids', 'unincorporated_child_reports', 'model_config', 'team_snapshot') if key in result}
    if result.get('report') == projected.get('report'):
        projected['result'].pop('report', None)
    projected['material_index'], projected['material_ids'] = _source_index_context(
        result.get('materials', []), projected['result'])
    projected['history_note'] = '上次研究结论和来源索引；本轮未重新取得的原文不因列入索引而视为已读。'
    if raw_result is not None and not isinstance(raw_result, dict):
        projected['legacy_result_note'] = '历史结果结构无法投影；原记录保留，未解析细节保持未知。'
    return projected


def _report_context(value, *, audit_limit=0):
    """Remove only internal receipt row inventories, never report evidence."""
    report = copy.deepcopy(value)
    if isinstance(report, dict):
        for key in ('material_delivery_ids', 'report_delivery_ids'):
            if (isinstance(report.get(key), list)
                    and len(json.dumps(report[key])) > audit_limit):
                report[key.removesuffix('_ids') + '_count'] = len(report.pop(key))
    return report


def _checkpoint_context(value, *, index_budget=16000):
    # Validation proofs stay in SQLite. They duplicate document versions and
    # excerpts, and are not additional model-visible research conclusions.
    keys = ('phase', 'task_id', 'account_id', 'input_version', 'scope', 'data_as_of', 'model_config',
            'team_snapshot', 'reports', 'material_ids', 'metrics', 'open_questions',
            'unresolved_tasks', 'unresolved_tasks_note', 'manager_summary', 'created_at',
            'checkpoint_job_id', 'resume_attempt', 'configuration_relation', 'recovery_state', 'unknown_material_ids',
            'partial_research', 'interruption', 'recovery_assessment', 'recovery_mode')
    result = {key: (_team_context(value[key]) if key == 'team_snapshot' else _research_context(value[key]))
              for key in keys if key in value}
    if isinstance(result.get('reports'), list):
        result['reports'] = [_report_context(report) for report in result['reports']]
    if isinstance(result.get('manager_summary'), dict):
        # Small legacy declarations remain byte-for-byte equivalent. Only an
        # inventory large enough to consume the metadata allowance is reduced.
        result['manager_summary'] = _report_context(result['manager_summary'],
            audit_limit=max(256, index_budget // 10))
    # Runtime restores the immutable records separately and pins their exact
    # summaries once, against the same request budget. Duplicating those views
    # here can crowd them out before that allocation even starts.
    result['calculation_count'] = len(value.get('calculations') or [])
    result['materials'], indexed_ids = _source_index_context(value.get('materials', []),
        {'reports': result.get('reports'), 'manager_summary': result.get('manager_summary')})
    material_ids = result.get('material_ids')
    all_ids = sorted({mid for mid in material_ids if isinstance(mid, str)} | set(indexed_ids)) if isinstance(material_ids, list) else indexed_ids
    coverage = value.get('source_coverage') or {}
    counts, unresolved = {}, []
    for stream, entry in coverage.items():
        if not isinstance(entry, dict):
            continue
        status = entry.get('status', 'unknown')
        counts[status] = counts.get(status, 0) + 1
        # Discovery-only links are not hundreds of unfinished research tasks.
        # This does not change stored coverage or convert any stream to covered.
        if status not in {'covered', 'index_only'}:
            unresolved.append({'source_stream': stream, **{key: copy.deepcopy(entry[key]) for key in
                ('status', 'reason', 'latest_document_version_id', 'latest_material_ids',
                 'historical_material_ids', 'unresolved_report_agents') if key in entry}})

    size = lambda item: len(json.dumps(item, ensure_ascii=False, default=str))
    def fit(items, budget):
        shown = []
        for item in items:
            if size([*shown, item]) <= budget:
                shown.append(copy.deepcopy(item))
        return shown
    result['unresolved_source_coverage'] = fit(unresolved, max(0, index_budget // 2))
    if counts.get('index_only') or len(unresolved) != len(result['unresolved_source_coverage']):
        result['source_coverage_summary'] = {'status_counts': counts,
            'unresolved_count': len(unresolved),
            'unlisted_unresolved_count': len(unresolved) - len(result['unresolved_source_coverage']),
            'note': 'index_only 不是研究义务或已读；未列未决项仍未解决，原身份保存在检查点。'}
    priority_ids = list(dict.fromkeys([mid for entry in unresolved for key in
        ('latest_material_ids', 'historical_material_ids') for mid in
        (entry.get(key) if isinstance(entry.get(key), list) else []) if isinstance(mid, str)] +
        [item['id'] for item in result['materials'] if isinstance(item.get('id'), str)]))
    priority_ids = [mid for mid in priority_ids if mid in set(all_ids)]
    ordered_ids = [*priority_ids, *[mid for mid in all_ids if mid not in set(priority_ids)]]
    materials_count = len(result['materials'])
    result['materials'] = fit(result['materials'], max(0, index_budget // 5))
    # Share unused source/detail space rather than reserving empty slices; small
    # inventories that already fit must not lose IDs to a fixed metadata quota.
    ids_budget = min(max(3000, index_budget // 5), max(0, index_budget - size(result['unresolved_source_coverage'])
                     - size(result['materials']) - index_budget // 10))
    result['material_ids'] = fit(ordered_ids, ids_budget)
    if len(set(all_ids) | set(priority_ids)) != len(result['material_ids']) or materials_count != len(result['materials']):
        result['material_index_summary'] = {'total_ids': len(set(all_ids) | set(priority_ids)),
            'listed_ids': len(result['material_ids']), 'unlisted_ids': len(set(all_ids) | set(priority_ids)) - len(result['material_ids']),
            'cited_metadata_count': materials_count, 'unlisted_cited_metadata_count': materials_count - len(result['materials']),
            'note': '省略来源索引不表示资料不存在，也不构成原文交付。'}
    access = []
    for item in value.get('report_deliveries', []):
        if not isinstance(item, dict) or not isinstance(item.get('body'), dict):
            continue
        entry = {key: item['body'].get(key) for key in
            ('report_id', 'source_agent_id', 'research_round', 'role_id', 'access', 'summary_truncated', 'note')}
        if entry not in access:
            access.append(entry)
    result['delegated_research_access'] = fit(access, max(0, index_budget // 10))
    if len(access) != len(result['delegated_research_access']):
        result['delegated_access_unlisted_count'] = len(access) - len(result['delegated_research_access'])
    result['checkpoint_note'] = '可见子研究成果保留原实例、轮次、模型和来源范围；主 Agent 收到报告不等于亲读原文。材料列表仅为索引。'
    return result


def _research_context(value, *, index_budget=16000):
    """Keep immutable audit snapshots in storage, project visible prompt context."""
    if isinstance(value, list):
        return [_research_context(item, index_budget=index_budget) for item in value]
    if not isinstance(value, dict):
        return copy.deepcopy(value)
    if value.get('phase') == 'synthesis' and ('reports' in value or 'manager_summary' in value):
        return _checkpoint_context(value, index_budget=index_budget)
    return {key: (_team_context(item) if key in
        {'team_snapshot', '_agent_team', 'previous_team_snapshot', 'current_team_snapshot'} else
        _previous_analysis_context(item) if key == 'previous_analysis' else _research_context(item, index_budget=index_budget))
        for key, item in value.items() if key not in {'_model_api_key', 'credential_reference', '_analysis_deadline', '_forecast_deadline'}}


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
MATERIAL_UPDATES = {"type": "array", "items": {"type": "object", "properties": {
    "latest_material_id": STRING, "prior_material_ids": STRINGS,
    "status": {"type": "string", "enum": ["handled", "needs_review"]}, "summary": STRING,
    "supersedes_agent_ids": STRINGS}, "required": ["latest_material_id", "prior_material_ids", "status", "summary"],
    "additionalProperties": False}, "description": "对实际读过的资料更正给出可见处理说明；历史对照可保留。主 Agent 可列出被本次说明更新的子 Agent ID，不能将尚未阅读或未处理的新版声明为完成。"}
RESEARCH_TOOLS = [
    _schema("read", "读取公开网页正文与附件；可按 PDF 物理页码（从 1 开始）或文本字符范围读取。访问范围及截断状态以返回值为准。",
            {"url": STRING, "start_page": PAGE, "end_page": PAGE, "page": PAGE, "offset": INTEGER, "limit": {"type": "integer", "minimum": 1, "maximum": 20000}, "refresh": {"type": "boolean"}}, ["url"]),
    _schema("pdf_metadata", "获取 PDF 标题、元数据及总页数；长财报先定位再读取。", {"url": STRING, "refresh": {"type": "boolean"}}, ["url"]),
    _schema("search_pdf", "检索长 PDF，返回匹配片段与实际页码，再用 read 指定页范围读取。",
            {"url": STRING, "query": STRING, "start_page": PAGE, "end_page": PAGE, "max_results": {"type": "integer", "minimum": 1, "maximum": 50}}, ["url", "query"]),
    _schema("save_material", "保存逐条分类的 Agent 提取结果；允许未知元数据，不设可信度准入门槛。用 material_ids 关联 read/search 返回的原始材料 ID，不能编造；没有原文ID则保留缺口。材料可含 claim, category, security_ids, title, url, publisher, published_at, event_period, retrieved_at, excerpt, unit, access_scope, uncertainties。关键财务数字可逐项填 financial_metrics: [{subject, report_period, metric, value, unit, comparison: {kind, baseline_period, value, unit}}]；同比与环比明确基准和正负号，未知填null，营收与利润分别保留；数值为正不等于同比增长。提取结果不自动成为原始事实。",
            {"material": {"type": "object", "additionalProperties": True}}, ["material"]),
    _schema("list_materials", "分页列出轻量材料索引（不包含全文）；用 ID 按需读取。", {"query": STRING, "limit": {"type": "integer", "minimum": 1, "maximum": 50}, "offset": INTEGER}),
    _schema("search_materials", "检索已有公开材料并分页返回索引、片段及位置，不代表已读完整原文。", {"query": STRING, "limit": {"type": "integer", "minimum": 1, "maximum": 50}, "offset": INTEGER}, ["query"]),
    _schema("query_market_rows", "按已保存结构化行情材料查询行；默认日期倒序最近30行，精确日期用相同start/end。日期字段为time，百分比换手为turnover_pct。需要核对K线形态时可选signed_body、real_body、upper_shadow、lower_shadow，返回附带candle_status/candle_reason；这些是同一行OHLC的精确几何量，不自动判断长短或趋势。按next_offset分页，保留复权、来源和实际访问范围。",
            {"material_id": STRING, "start": STRING, "end": STRING,
             "fields": {"type": "array", "items": {"type": "string", "enum": sorted(MARKET_ROW_FIELDS)}},
             "limit": {"type": "integer", "minimum": 1, "maximum": 100}, "offset": INTEGER,
             "order": {"type": "string", "enum": ["asc", "desc"]}, "symbol": STRING}, ["material_id"]),
    _schema("market_indicators", "对已保存同口径行情计算可复算的收益、均线、区间波动及可选基准比较；return 是 start/end 筛选后全部日期的首尾收益，window 只控制均线。tail_close_streak描述所选实际收盘序列尾部的连续变动间隔及边界，不是放量天数或已核实的连续交易日；close_to_ma_change_pct是相对均线涨跌百分比，单位为%，不是百分点。20个收盘观察点只有19个收益间隔；固定N个交易日收益需先query_market_rows选择实际N+1个可比收盘日期，再以start/end计算并核对return_period，数据不足须说明。按实际起止日期、valid_samples观察点、return_intervals收益间隔和均线window分别描述；不得将最近N条价格或window当作N日收益，不以缺失值补零。",
            {"material_id": STRING, "start": STRING, "end": STRING,
             "window": {"type": "integer", "minimum": 2, "maximum": 252, "description": "仅均线所用收盘样本数；收益区间由start/end及实际行情日期决定"}, "benchmark_material_id": STRING}, ["material_id"]),
    _schema("get_material", "按材料 ID 读取元数据与有限首段；正文较长时按 next_start 继续。", {"material_id": STRING}, ["material_id"]),
    _schema("read_material_range", "读取指定材料字符范围，保留来源、页码、访问限制及范围信息。", {"material_id": STRING, "start": INTEGER, "length": {"type": "integer", "minimum": 1, "maximum": 12000}}, ["material_id"]),
    _schema("list_recommendations", "分页取得当前账户建议，先按业务状态筛选。active 包含所有仍需关注的建议；按 next_offset 继续读取，不能将价格满足视为判断有效。公共研究不能读取账户建议。",
            {"view": {"type": "string", "enum": ["active", "triggered", "needs_review", "history", "all"]},
             "limit": {"type": "integer", "minimum": 1, "maximum": 100}, "offset": INTEGER}),
    _schema("get_recommendation", "按已有建议 ID 读取当前账户的完整建议、价格状态与复核需求，禁止跨账户访问。", {"recommendation_id": STRING}, ["recommendation_id"]),
    _schema("recall_context", "检索当前 Agent 已整理的可见上下文；只读自己的历史。提供 record_id 时按字符范围取回原记录，否则按 query 分页查摘要与引用。",
            {"record_id": STRING, "query": STRING, "offset": INTEGER, "limit": {"type": "integer", "minimum": 1, "maximum": 20}, "start": INTEGER, "length": {"type": "integer", "minimum": 1, "maximum": 12000}}),
    _schema("history", "获取历史 OHLCV 与成交额，保留复权方式、时间和数据源限制。", {"symbol": {"type": "string", "description": "市场前缀证券代码，例如 SZ:159278"}, "start": STRING, "end": STRING, "interval": {"type": "string", "enum": ["daily", "weekly", "monthly", "1m", "5m", "15m", "30m", "60m"], "description": "默认 daily；日线用 daily，不使用 1d"}, "adjust": {"type": "string", "enum": ["none", "qfq", "hfq"], "description": "默认 none；前复权 qfq，后复权 hfq"}, "limit": {"type": "integer", "minimum": 1, "maximum": 1000}}, ["symbol"]),
    _schema("sectors", "分页查询行业或概念板块基础信息；offset 为 limit 的非负整数倍；数据权限不足时如实返回。", {"kind": STRING, "limit": {"type": "integer", "minimum": 1, "maximum": 100}, "offset": INTEGER}),
    _schema("sector_members", "分页获取板块成分证券，offset 为 limit 的非负整数倍。", {"sector_id": STRING, "limit": {"type": "integer", "minimum": 1, "maximum": 100}, "offset": INTEGER}, ["sector_id"]),
    _schema("sector_history", "获取板块历史表现，可与基准 history 数据比较。", {"sector_id": STRING, "start": STRING, "end": STRING, "limit": {"type": "integer", "minimum": 1, "maximum": 1000}}, ["sector_id"]),
    _schema("benchmark_history", "获取市场基准历史表现，支持 CSI300、SSE50、CSI500，保留数据覆盖与限制。", {"benchmark": STRING, "start": STRING, "end": STRING, "limit": {"type": "integer", "minimum": 1, "maximum": 1000}}),
    _schema("fundamentals", "获取可用财务指标、报告期和披露时间；不可用字段保持未知。", {"symbol": STRING, "periods": {"type": "integer", "minimum": 1, "maximum": 40}}, ["symbol"]),
    _schema("calculate", "用代码做精确数值计算；percent 为 a/b*100，不是增长率。增长率先 subtract 求差，再 percent；只传已知输入，不以缺失代替零。",
            {"operation": {"type": "string", "enum": ["sum", "multiply", "product", "divide", "ratio", "percent", "subtract", "difference"]}, "values": {"type": "array", "items": {"type": ["string", "number"]}}}, ["operation", "values"]),
]
MANAGER_TOOLS = [
    _schema("list_agent_roles", "列出本轮冻结的全部系统角色、职责及账户绑定；配置错误不会删除角色。", {}),
    _schema("list_business_demands", "按当前任务分页读取需求；只在最终报告 covered_demand_ids 中声明实际处理的需求。", {"limit": {"type": "integer", "minimum": 1, "maximum": 50}, "offset": INTEGER}),
    _schema("list_forecast_targets", "分页读取本轮服务端锁定的kind、基准日、截止和各证券期限/目标交易日；exploratory是探索记录，不称为正式；每项明确预测或弃权，未覆盖会保留。", {"limit": {"type": "integer", "minimum": 1, "maximum": 100}, "offset": INTEGER}),
    _schema("prepare_synthesis", "资料研究完成后保存可见成果并进入汇总。之后仅使用已有材料、计算与报告工具；失败时可只恢复汇总。", {"summary": STRING, "material_ids": STRINGS, "open_questions": STRINGS, "material_updates": MATERIAL_UPDATES}, ["summary"]),
    _schema("spawn_research", "创建有独立模型上下文和工具循环的子 Agent；按需要创建，不要求固定数量。",
            {"role_id": STRING, "task": STRING, "title": STRING, "context": {"type": "object", "additionalProperties": True}}, ["role_id", "task"]),
    _schema("send_message", "给子 Agent 发送补充消息。运行中在下个安全边界接收；已完成则继续同一会话。",
            {"agent_id": STRING, "message": STRING}, ["agent_id", "message"]),
    _schema("follow_up", "在原子 Agent 的独立会话中继续研究，保留此前报告和材料。",
            {"agent_id": STRING, "task": STRING}, ["agent_id", "task"]),
    _schema("wait_agents", "以事件等待子任务进展/完成，无需反复查询。空 agent_ids 表示所有子任务。返回状态与新增可见消息。",
            {"agent_ids": STRINGS, "timeout_seconds": {"type": "number", "minimum": 0, "maximum": 60}}),
    _schema("agent_status", "查看指定或全部子任务的状态、阶段发现和报告；需要等待时使用 wait_agents。", {"agent_id": STRING}),
    _schema("cancel_agent", "取消已无需要的子任务；已经保存的公开材料与阶段报告仍保留。", {"agent_id": STRING, "reason": STRING}, ["agent_id"]),
    _schema("finish_analysis", "仅在准备交付时调用一次，且必须是本轮唯一工具调用；不能用于测试或占位。提交账户最终报告；重要结论附来源与条件，数值取自代码计算。",
            {"report": STRING, "completion": {"type": "string", "enum": ["full", "limited"]},
             "scope": {"type": "array", "items": {"type": "string"}, "description": "实际回答的目标：公共整轮需求用public，账户整轮用account或cash_only，即使仅一只证券；仅服务端明确target_security的单证券任务只填该证券；局部证券scope不等于整轮范围"},
             "decision_summary": {"type": "string", "description": "本轮已有依据支持的判断及适用条件；可持有、观察或有理由弃权"},
             "limitations": {"type": "array", "items": {"type": "string"}, "description": "具体未完成内容与影响；limited 必须说明，完整范围可为空"},
             "findings": {"type": "array", "items": {"type": "object", "additionalProperties": True}}, "material_ids": STRINGS,
             "valid_until": {"type": ["string", "null"]}, "uncertainties": STRINGS,
             "covered_demand_ids": {"type": "array", "items": {"type": "integer", "minimum": 1},
                 "description": "有冻结需求时必须显式填写；只列已读、实际回答且提交scope足以覆盖的需求ID。局部证券scope不能声明完成公共或账户整轮需求；未覆盖填[]，不要求全部完成。"},
             "forecasts": {"type": "array", "items": {"type": "object", "properties": {
                 "security_id": STRING, "horizon": {"type": "integer", "minimum": 1},
                 "direction": {"type": "string", "enum": ["up", "sideways", "down", "abstain"]},
                 "reason": STRING, "material_ids": STRINGS}, "required": ["security_id", "horizon", "direction", "reason"], "additionalProperties": False}},
             "suggestions": {"type": "array", "items": SUGGESTION, "maxItems": 100},
             "maintained_suggestion_ids": STRINGS,
             "review_suggestion_ids": STRINGS,
             "revoked_suggestion_ids": STRINGS,
             "information_impacts": {"type": "array", "items": {"type": "object", "properties": {
                 "material_id": STRING, "status": {"type": "string", "enum": ["relevant", "irrelevant", "uncertain"]},
                 "symbols": STRINGS, "sectors": STRINGS, "market_wide": {"type": "boolean"}, "explanation": STRING,
                 "event_key": STRING}, "required": ["material_id", "status", "explanation"]}},
             "research_records": {"type": "array", "items": RESEARCH_RECORD, "maxItems": MAX_RESEARCH_RECORDS}}, ["report", "completion", "scope", "decision_summary", "limitations"]),
]
CHILD_TOOLS = [
    _schema("report_progress", "把阶段发现交给主 Agent，不结束任务。仅提交可见摘要，不提交隐藏思维链。", {"summary": STRING, "material_ids": STRINGS}, ["summary"]),
    _schema("submit_report", "仅在准备交付时调用一次，且必须是本轮唯一工具调用；不能用于测试或占位。结束本次子任务并交付材料；主 Agent 后续可继续此会话。",
            {"summary": STRING, "findings": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
             "material_ids": STRINGS, "open_questions": STRINGS, "material_updates": MATERIAL_UPDATES}, ["summary"]),
]


@dataclass
class AgentState:
    id: str
    title: str
    task: str
    session: ModelSession
    role_id: str = "researcher"
    model_config: dict = field(default_factory=dict)
    client: Any = None
    status: str = "queued"
    inbox: list[str] = field(default_factory=list)
    pending_messages: list[str] = field(default_factory=list)
    reports: list[dict] = field(default_factory=list)
    progress: list[dict] = field(default_factory=list)
    task_handle: asyncio.Task | None = None
    error: str | None = None
    round: int = 1
    round_time_budget: float | None = None
    submission_reserve_seconds: float = 0.0
    deadline_closing: bool = False

    def snapshot(self) -> dict:
        return {"agent_id": self.id, "role_id": self.role_id, "model_config": copy.deepcopy(self.model_config), "title": self.title, "task": self.task,
                "status": self.status, "round": self.round, "reports": copy.deepcopy(self.reports),
                "progress": copy.deepcopy(self.progress), "error": self.error}


class ResearchRuntime:
    def __init__(self, context: dict, config: dict, research_tools: Any,
                 emit: Callable[[dict], Awaitable[None]]) -> None:
        self.context = _research_context(context,
            index_budget=max(500, int(config.get('max_context_chars', 240_000)) // 12))
        # Freeze the declaration contract from server-owned input, before any
        # context projection can shorten its visible demand index. No model
        # prose or claimed ID can turn this requirement on or off.
        demand_total = (context.get('business_demand_coverage') or {}).get('total')
        self.require_demand_coverage = (type(demand_total) is int and demand_total > 0) or bool(context.get('business_demands'))
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
        self.closeout_reasons: set[str] = set()
        self.closeout_waited = False
        self.deadline: float | None = None
        self.inherited_deadline = context.get('_analysis_deadline')
        self.forecast_deadline = context.get('_forecast_deadline')
        self.deadline_code = 'analysis_budget_exhausted'
        self.team = copy.deepcopy(config.get('_agent_team'))
        manager_config = self._role_config('manager') if self.team else config
        self.model_config = model_config_summary(manager_config)
        self.context['model_config'] = copy.deepcopy(self.model_config)
        if self.team:
            self.context['agent_roles'] = self._role_index()
            self.context['team_id'] = self.team['team_id']
        self.client = ModelClient(manager_config, before_request=self._use_budget)
        self.semaphore = asyncio.Semaphore(max(1, int(config.get("max_concurrent_agents", 3))))
        self.children: dict[str, AgentState] = {}
        self.handles: set[asyncio.Task] = set()
        self.events: list[dict] = []
        self.condition = asyncio.Condition()
        self.delivered_sequence = 0
        self.materials: list[dict] = []
        self.material_delivery_ids: dict[str, list[int]] = {}
        self.report_delivery_seen: set[str] = set()
        self.request_report_visibility: list[dict] = []
        self.submitted_report_counts: dict[str, int] = {}
        self.report_tool_messages: dict[str, str] = {}
        self.calculations: list[dict] = []
        self.restored_material_evidence: list[dict] = []
        self.synthesis_checkpoint = copy.deepcopy(context.get("synthesis_checkpoint"))
        self.restored_checkpoint = copy.deepcopy(self.synthesis_checkpoint)
        self.synthesis_only = bool(self.synthesis_checkpoint and self.synthesis_checkpoint.get('recovery_mode') != 'continue_research')
        self.synthesis_started_calls = 0
        self.checkpoint_signature = ""
        self.closed = False
        self.cancelled_by = 'parent_finished'
        self.reference_feedback_sent = False
        self.manager_session = self.client.session(self.team["roles"]["manager"]["system_prompt"] if self.team else (PROMPTS / "manager.txt").read_text(), self.context)
        self.manager_session.task_id = self.task_id
        self.manager_session.agent_id = "manager"
        self.manager_session.on_request_accepted = lambda observation: self._accepted_context(
            self.manager_session, 'manager', observation)
        if self.synthesis_checkpoint:
            self.manager_session.begin_cache_epoch('recovery_rebuild')
            self.manager_session.session_origin = 'new_session_from_visible_checkpoint'
        self.calculation_coverage: list[dict] = []
        self._restore_calculations()
        self._restore_material_evidence()
        if self.synthesis_only:
            self.manager_session.phase = "synthesis"
            self.manager_session.native_search = False
            self.client.add_user(self.manager_session, {"recovery": "基于已保存的可见研究成果完成最终汇总；不重复联网或创建子任务。当前持仓与版本以本次 context 为准。"})
            if self.synthesis_checkpoint.get('partial_research'):
                self.client.add_user(self.manager_session, {'recovery_scope': '原研究中途失败，仅复用已完成计算与可见成果，未完成范围仍未知。本次交付须明确标为有限分析；不能声称所有专题完成。'})
        elif self.restored_checkpoint:
            self.client.add_user(self.manager_session, {'recovery': '基于已保存成果继续研究。已完成子报告现在真实交付，保留原任务、实例及材料范围；不等于主 Agent 亲读原文。先利用已完成计算和材料索引，按未完成范围决定补查或有限分析。不得重放已中断请求或把旧未完成任务视为完成。'})

    def _remember_calculation(self, agent_id, tool, arguments, output, *, restored=None):
        summary = calculation_projection(tool, output, arguments)
        if summary is None:
            return
        session = self.manager_session if agent_id == 'manager' or restored else self._child(agent_id).session
        original = {'tool': tool, 'arguments': copy.deepcopy(arguments), 'output': copy.deepcopy(output)}
        record = archive(session, original, 'calculation_inputs')
        identity = (restored or {}).get('calculation_id') or 'calculation-' + uuid.uuid4().hex
        provenance = {key: copy.deepcopy((restored or {}).get(key, value)) for key, value in {
            'agent_id': agent_id, 'source_task_id': self.task_id, 'input_version': self.input_version,
            'research_round': self.children[agent_id].round if agent_id in self.children else None,
            'model_config': self.children[agent_id].model_config if agent_id in self.children else self.model_config}.items()}
        summary.update(calculation_id=identity, **provenance,
            full_inputs={'record_id': record['record_id'], 'tool': 'recall_context', 'start': 0,
                         'total_chars': record['total_chars'], 'session_agent_id': session.agent_id},
            visibility='deterministic projection of completed tool output; inclusion does not prove model adoption')
        self.calculations.append({**original, **provenance, 'calculation_id': identity, 'summary': summary,
            'signature': calculation_signature(tool, arguments, output), 'restored': bool(restored)})

    def _restore_calculations(self):
        for record in (self.synthesis_checkpoint or {}).get('calculations') or []:
            if not isinstance(record, dict):
                continue
            if isinstance(record.get('summary'), dict) and (record.get('summary_only') or not isinstance(record.get('output'), dict)):
                # Older checkpoints can contain only the already-visible view.
                # Preserve that view, but never invent original operands or
                # present the summary archive as a complete tool output.
                summary = copy.deepcopy(record['summary'])
                identity = record.get('calculation_id') or summary.get('calculation_id') or 'calculation-' + uuid.uuid4().hex
                item = archive(self.manager_session, {'saved_summary': summary}, 'calculation_summary')
                item['calculation_id'] = identity
                tool = record.get('tool') or summary.get('tool')
                summary.update(calculation_id=identity, summary_only=True,
                    full_inputs={'record_id': item['record_id'], 'tool': 'recall_context', 'start': 0,
                        'total_chars': item['total_chars'], 'session_agent_id': 'manager',
                        'scope': 'saved_summary_only; original full tool output unavailable'})
                summary.setdefault('inputs_visibility',
                    {'status': 'complete', 'source': 'saved_summary.inputs'} if isinstance(summary.get('inputs'), list)
                    else {'status': 'unavailable', 'source': None, 'reason': 'legacy checkpoint saved only a summary'})
                for key in ('agent_id', 'source_task_id', 'input_version', 'research_round', 'model_config'):
                    value = record.get(key)
                    if key == 'source_task_id' and value is None:
                        value = (self.synthesis_checkpoint or {}).get('task_id')
                    summary.setdefault(key, copy.deepcopy(value))
                self.calculations.append({**{key: summary.get(key) for key in
                    ('agent_id', 'source_task_id', 'input_version', 'research_round', 'model_config')},
                    'tool': tool, 'arguments': {}, 'output': {}, 'summary': summary,
                    'calculation_id': identity, 'summary_only': True, 'restored': True,
                    'signature': calculation_signature(tool, {}, summary)})
            elif record.get('tool') in {'market_indicators', 'calculate', 'query_market_rows'} and isinstance(record.get('output'), dict):
                self._remember_calculation(record.get('agent_id', 'manager'), record['tool'],
                    record.get('arguments') or {}, record['output'], restored=record)
            elif 'operation' in record and isinstance(record.get('result'), dict):
                self._remember_calculation(record.get('agent_id', 'unknown'), 'calculate',
                    {'operation': record['operation'], 'values': record.get('values', [])}, record['result'],
                    restored={**record, 'source_task_id': (self.synthesis_checkpoint or {}).get('task_id'),
                              'input_version': (self.synthesis_checkpoint or {}).get('input_version'),
                              'research_round': None, 'model_config': None})

    def _restore_material_evidence(self):
        # A previous session's record_id is not a capability in this session.
        # Rebuild records from exact saved visible objects only; raw tool-result
        # archives and legacy receipts without bodies are never expanded.
        indexes = []
        for item in restored_manager_material_evidence(self.synthesis_checkpoint or {}):
            record = archive(self.manager_session, {'tool': item['tool'], 'result': item['result']},
                             'restored_material')
            visible_item = {key: copy.deepcopy(item[key]) for key in ('tool', 'result', 'material_id',
                'source_task_id', 'source_agent_id', 'visibility', 'previously_delivered', 'delivered_range') if key in item}
            self.restored_material_evidence.append({**visible_item, 'record_id': record['record_id'],
                'visibility': item.get('visibility', 'previously_delivered_manager_tool_output')})
            if item.get('previously_delivered') is False:
                self.client.add_user(self.manager_session, {'recovered_completed_tool_outputs': [visible_item],
                    'note': '这些是旧任务已完成但未确认发给模型的本地工具结果；现在首次提供，不属于旧模型已读证据。'})
                message = copy.deepcopy(self.manager_session.history[-1])
                proof = next((value for value in (self.synthesis_checkpoint or {}).get('pending_tool_outputs', [])
                    if value.get('tool') == item['tool'] and value.get('result') == item['result']), None)
                pending = getattr(self.manager_session, '_pending_material_deliveries', [])
                pending.append({'call': ToolCall('recovered-' + uuid.uuid4().hex, item['tool'], {}),
                    'message': message, 'result': copy.deepcopy(item['result']), 'proof': copy.deepcopy(proof)})
                self.manager_session._pending_material_deliveries = pending
            indexes.append({key: item[key] for key in
                ('material_id', 'source_task_id', 'source_agent_id', 'tool', 'delivered_range')} |
                {'retrieve': {'tool': 'recall_context', 'record_id': record['record_id'],
                              'start': 0, 'total_chars': record['total_chars']}})
        if indexes:
            limit = max(500, min(int(self.config.get('max_tool_result_chars', 16000)),
                                int(self.config.get('max_context_chars', 240_000)) // 12))
            visible_index = bounded_result(self.manager_session, indexes, limit)
            self.context['restored_material_index'] = visible_index
            seed = json.loads(self.manager_session.history[0]['content'])
            seed['restored_material_index'] = visible_index
            self.manager_session.history[0]['content'] = json.dumps(seed, ensure_ascii=False, default=str)

    def _manager_material_receipts(self):
        if not hasattr(self.tools, 'store'):
            return []
        return manager_material_deliveries(self.tools.store.checkpoint_deliveries(self.task_id))

    def _available_calculations(self, agent_id):
        if agent_id != 'manager':
            return [record for record in self.calculations if record['agent_id'] == agent_id]
        reports = [report for child in self.children.values() for report in child.reports]
        reported = {identity for report in reports for identity in report.get('calculation_ids', [])}
        return [record for record in self.calculations if record['agent_id'] == 'manager'
                or record['calculation_id'] in reported or record.get('restored')]

    @staticmethod
    def _visible_calculations(session):
        """Inspect only application-owned visible projections in current input."""
        summaries, indexes = {}, {}
        for message in session.history:
            if message.get('role') != 'user' or message.get('_context_checkpoint'):
                continue
            try:
                value = json.loads(message.get('content', '{}'))
            except (ValueError, TypeError):
                continue
            if not isinstance(value, dict) or value.get('truncated_for_context'):
                continue
            for item in value.get('calculation_summaries', []):
                summaries[item.get('calculation_id')] = item
            for item in (value.get('calculation_indexes') or {}).get('items', []):
                indexes[item.get('calculation_id')] = item
        return summaries, indexes

    def _pin_calculations(self, seed, session, agent_id, final_request, *, incremental=False):
        """Reserve bounded value-first evidence before generic prose/row excerpts.

        A whole summary either fits or becomes an explicit index; individual
        values are never silently removed from a JSON prefix.
        """
        for key in ('calculation_summaries', 'calculation_indexes', 'calculation_access', 'calculation_contexts'):
            seed.pop(key, None)
        records = self._available_calculations(agent_id)
        if incremental:
            visible, indexed = self._visible_calculations(session)
            records = [record for record in records if record['calculation_id'] not in visible
                       and record['calculation_id'] not in indexed]
        if not records:
            return
        if isinstance(seed.get('synthesis_checkpoint'), dict):
            seed['synthesis_checkpoint'].pop('calculations', None)
        size = lambda value: len(json.dumps(value, ensure_ascii=False, default=str))
        max_chars = int(self.config.get('max_context_chars', 240_000))
        access = ('当前仅可提交最终报告，不能调用 recall_context；仅索引或未列出的数值不在本次可见上下文。'
            '已成功计算与最终报告采用是不同事实，不得把未采用描述为工具未取得。' if final_request else
            '摘要来自实际工具输出。完整输入和逐日数据可用 full_inputs 指向的 recall_context 分段取回；原始行未因摘要而视为亲读。')
        access += (' context_ref 引用本请求 calculation_contexts 的共享字段；摘要未单列字段从该项继承，单列字段优先，引用缺失则该字段未知。'
                   ' model_config_ref=seed.model_config 仅引用本请求配置。')
        # Charge the actual metadata to the same envelope as reports/status.
        # A fixed reserve disproportionately discards exact results in small
        # contexts even when the complete package would fit. Index rows use the
        # separate, bounded allowance below; reserve their counters here.
        envelope = {**seed, 'calculation_summaries': [], 'calculation_contexts': {},
            'calculation_indexes': {'items': [], 'omitted_summary_count': len(records),
                                    'unlisted_index_count': len(records)},
            'calculation_access': access}
        budget = max(0, int(max_chars * .66) - size(envelope) + size([]) + size({}))
        prepared = []
        for record in records:
            summary = copy.deepcopy(record['summary'])
            if agent_id == 'manager' and summary.get('agent_id') != 'manager':
                # Child-local archive IDs cannot be used in the manager session.
                local = next((item for item in session.archive if item.get('calculation_id') == record['calculation_id']), None)
                if local is None:
                    local = archive(session, {key: record[key] for key in ('tool', 'arguments', 'output')}, 'calculation_inputs')
                    local['calculation_id'] = record['calculation_id']
                summary['full_inputs'] = {'record_id': local['record_id'], 'tool': 'recall_context',
                    'start': 0, 'total_chars': local['total_chars'], 'session_agent_id': agent_id}
                if record.get('summary_only'):
                    summary['full_inputs']['scope'] = 'saved_summary_only; original full tool output unavailable'
            prepared.append(summary)
        prepared, contexts = compact_calculation_metadata(prepared)
        summaries, indexes, included_contexts = [], [], {}
        for summary in prepared:
            proposed_contexts = dict(included_contexts)
            if summary.get('context_ref') in contexts:
                proposed_contexts[summary['context_ref']] = contexts[summary['context_ref']]
            if (isinstance(seed.get('model_config'), dict)
                    and summary.get('model_config') == seed['model_config']):
                # Factor this exact repeated identity through an explicit
                # same-request reference; stored originals stay whole.
                summary = {key:value for key,value in summary.items() if key != 'model_config'}
                summary['model_config_ref'] = 'seed.model_config'
            if size([*summaries, summary]) + size(proposed_contexts) <= budget:
                summaries.append(summary)
                included_contexts = proposed_contexts
            else:
                indexes.append({key: summary.get(key) for key in
                    ('calculation_id', 'tool', 'agent_id', 'source_task_id', 'status', 'full_inputs')})
        if included_contexts:
            seed['calculation_contexts'] = included_contexts
        seed['calculation_summaries'] = summaries
        # Index rows are bounded independently. Full accounting stays in the
        # coverage event; no unbounded list can force the seed over its limit.
        index_limit = max(0, int(max_chars * .70) - size(seed) - 1000)
        shown = []
        for item in indexes:
            if size([*shown, item]) > index_limit:
                break
            shown.append(item)
        seed['calculation_indexes'] = {'items': shown, 'omitted_summary_count': len(indexes),
                                       'unlisted_index_count': len(indexes) - len(shown)}
        seed['calculation_access'] = access

    async def _record_calculation_coverage(self, session, agent_id, final_request):
        records = self._available_calculations(agent_id)
        if not records and not final_request:
            return
        summaries, indexes = self._visible_calculations(session)
        included, indexed = set(summaries), set(indexes)
        visible_records = {item.get('calculation_id'): (item.get('full_inputs') or {}).get('record_id')
            for item in [*summaries.values(), *indexes.values()]}
        coverage = {'request_number': self.calls, 'agent_id': agent_id, 'final_request': bool(final_request),
            'phase': session.phase,
            'calculations': [{'calculation_id': item['calculation_id'], 'tool': item['tool'],
                'source_agent_id': item['agent_id'], 'source_task_id': item['source_task_id'],
                'status': item['summary'].get('status'),
                'visibility': 'summary' if item['calculation_id'] in included else 'index' if item['calculation_id'] in indexed else 'omitted',
                'record_id': visible_records.get(item['calculation_id'])} for item in records],
            'note': 'Actual prepared request coverage; inclusion does not establish use in the report.'}
        self.calculation_coverage.append(coverage)
        await self._publish('final_calculation_coverage' if final_request else 'calculation_context_coverage', agent_id, coverage)

    def _role_index(self):
        return [{key: copy.deepcopy(role.get(key)) for key in
                 ('role_id', 'name', 'responsibility', 'enabled', 'version', 'tools', 'resolved', 'config_error')}
                for role in (self.team or {}).get('roles', {}).values()]

    def _role_config(self, role_id):
        if not hasattr(self.tools, 'store'):
            raise ModelConfigurationError('具名角色运行需要持久配置解析器')
        try:
            cfg = self.tools.store.runtime_role_config(self.team, role_id, self.config)
        except ValueError as exc:
            raise ModelConfigurationError(str(exc)) from exc
        cfg['account_id'] = self.account_id
        return cfg

    def _client_for(self, agent_id):
        return self.client if agent_id == 'manager' else (self.children[agent_id].client or self.client)

    async def _use_budget(self) -> None:
        async with self.budget_lock:
            agent_id, final_request = self.request_owner.get()
            session = self.manager_session if agent_id == 'manager' else self.children[agent_id].session
            expired = [(key, value) for key, value in session.deadlines.items() if value <= time.monotonic()]
            if expired:
                raise ModelTimeoutError(min(expired, key=lambda item: item[1])[0])
            if self.team:
                role_id = 'manager' if agent_id == 'manager' else self.children[agent_id].role_id
                try:
                    self.tools.store.check_team_credentials(self.team, role_id)
                except (ValueError, KeyError) as exc:
                    raise ModelConfigurationError(str(exc)) from exc
            synthesis_limit = self.config.get('synthesis_max_calls', 4)
            call_limit = self.config.get('max_model_calls', 40)
            if self.synthesis_only and synthesis_limit is not None and self.calls - self.synthesis_started_calls >= max(1, int(synthesis_limit)):
                raise ModelBudgetExceeded("汇总恢复达到有限调用预算；检查点保留，未发布半份最终报告")
            if call_limit is not None and self.calls >= int(call_limit):
                raise ModelBudgetExceeded("已达到本轮模型调用预算；已保存持仓及材料保留，可调整预算后重试")
            remaining = self._remaining_calls()
            # This guard also runs before HTTP retries, under the shared lock.
            # Reserve first submission AND one correction inside the same lock
            # used for every actual HTTP attempt, including transport retries.
            if agent_id != "manager" and remaining is not None and remaining <= self._final_call_reserve():
                raise ModelBudgetExceeded("共享调用预算已为主 Agent 最终提交及一次协议修正保留；子任务未完成部分保持未知")
            if (self._shared_closeout() or self._child_closeout(agent_id)) and not final_request:
                raise _CloseoutRequired("本任务进入预算收尾；本次尚未发送 HTTP")
            self.calls += 1
            await self._record_calculation_coverage(self.manager_session if agent_id == 'manager'
                else self.children[agent_id].session, agent_id, final_request)
            if agent_id == 'manager':
                # ModelClient invokes this after final context compaction and
                # credential/budget checks, immediately before the HTTP attempt.
                # An archived or merely queued child report is not a delivery.
                self._record_visible_report_deliveries(record=False)
                await self._publish('research_context_prepared', 'manager', {
                    'request_number': self.calls, 'phase': session.phase,
                    'final_request': bool(final_request),
                    'reports': copy.deepcopy(self.request_report_visibility),
                    'note': '最终压缩后准备发送的范围；尚未作为端点接受或模型阅读回执。'})

    async def _accepted_context(self, session, agent_id, observation):
        """Commit visibility only after the actual endpoint accepts the input."""
        if hasattr(self.tools, 'store'):
            visible = {self._report_message_key(message) for message in session.history}
            projected_outputs = []
            for message in session.history:
                if message.get('role') != 'user':
                    continue
                try:
                    value = json.loads(message.get('content', '{}'))
                except (ValueError, TypeError):
                    continue
                if isinstance(value, dict) and not value.get('truncated_for_context'):
                    projected_outputs.extend(item for item in value.get('delivered_research_evidence', [])
                        if isinstance(item, dict) and 'result' in item and not item.get('truncated_for_synthesis'))
            retained_pending = []
            for pending in getattr(session, '_pending_material_deliveries', []):
                call, message = pending['call'], pending['message']
                raw_value = json.loads(message.get('output', message.get('content', '{}')))
                value = pending.get('result', raw_value)
                direct = self._report_message_key(message) in visible and not (
                    isinstance(raw_value, dict) and raw_value.get('truncated_for_context'))
                projected = any(item.get('tool') == call.name and projected_result_matches(item, value)
                    for item in projected_outputs)
                if not direct and not projected:
                    if pending.get('proof'):
                        retained_pending.append(pending)
                    continue  # An archive pointer is not a delivered original.
                receipts = self.tools.store.record_material_delivery(self.task_id, self.account_id,
                    agent_id, call.name, value,
                    authored=call.arguments.get('material') if call.name == 'save_material' else None)
                self.material_delivery_ids.setdefault(agent_id, []).extend(receipts)
            session._pending_material_deliveries = retained_pending
        if agent_id == 'manager':
            delivered = self._record_visible_report_deliveries()
            await self._publish('research_context_delivery', 'manager', {
                'request_number': self.calls, 'local_request_id': observation.get('local_request_id'),
                'phase': session.phase, 'reports': copy.deepcopy(self.request_report_visibility),
                'accepted_by_endpoint': True,
                'note': '实际输入已由端点接受；不证明模型采用，更不等于主 Agent 亲读原文。'})
            if delivered and self.children and not self.context.get('synthesis_checkpoint'):
                await self._save_checkpoint()

    def _shared_closeout(self) -> bool:
        remaining = self._remaining_calls()
        active = sum(child.status in {"queued", "running"} for child in self.children.values())
        # Leave one submission per active child and two manager calls (final
        # report plus, when available, citation correction / a failed request).
        if remaining is not None and remaining <= active + self._final_call_reserve():
            self.budget_closing = True
            self.closeout_reasons.add('shared_calls_reserved')
        # Do not launch another full ordinary request if its allowed duration
        # could consume the reserved final window. This also tells children to
        # submit their visible work early enough for the manager to use it.
        if self.deadline is not None and self.deadline - time.monotonic() <= (
                self._synthesis_reserve() + timeout_policy(self.config)['request_hard_seconds']):
            self.budget_closing = True
            self.closeout_reasons.add('analysis_time_reserved')
        return self.budget_closing

    def _child_closeout(self, agent_id: str) -> bool:
        """Reserve submission inside this child round, without closing siblings."""
        child = self.children.get(agent_id)
        if child is None:
            return False
        deadline = child.session.deadlines.get('child_budget_exhausted')
        if deadline is not None and deadline - time.monotonic() <= child.submission_reserve_seconds:
            child.deadline_closing = True
        return child.deadline_closing

    def _final_call_reserve(self):
        reserve = max(2, min(4, int(self.config.get('final_submission_reserved_calls', 2))))
        limit = self.config.get('max_model_calls', 40)
        return reserve if limit is None else min(int(limit), reserve)

    def _synthesis_reserve(self):
        # Reserve is part of the existing total, not extra time after expiry.
        return min(timeout_policy(self.config, 'synthesis')['request_hard_seconds'] * 2,
                   float(self.config.get('analysis_timeout_seconds', 1200)) * .40)

    def _remaining_calls(self) -> int | None:
        limit = self.config.get('max_model_calls', 40)
        limits = [] if limit is None else [int(limit) - self.calls]
        synthesis_limit = self.config.get('synthesis_max_calls', 4)
        if self.synthesis_only and synthesis_limit is not None:
            limits.append(max(1, int(synthesis_limit)) - (self.calls - self.synthesis_started_calls))
        return max(0, min(limits)) if limits else None

    def _calls_available(self):
        remaining = self._remaining_calls()
        return remaining is None or remaining > 0

    async def _wait_for_closeout(self) -> None:
        if self.closeout_waited:
            return
        self.closeout_waited = True
        self.budget_closing = True
        if not self.closeout_reasons:
            self.closeout_reasons.add('manager_final_turn')
        pending = [child for child in self.children.values()
                   if child.task_handle and not child.task_handle.done()]
        active = [child.task_handle for child in pending]
        wait_seconds = 30.0
        remaining_time = None
        if self.deadline is not None:
            request_time = timeout_policy(self.config, 'synthesis')['request_hard_seconds']
            remaining_time = max(0.0, self.deadline - time.monotonic())
            child_time = max((max(0.0, min(child.session.deadlines.values()) - time.monotonic())
                if child.session.deadlines else float(self.config.get('agent_timeout_seconds', 600))
                for child in pending), default=0.0)
            # Existing paid research gets its remaining bounded opportunity.
            # The final request retains time within the same whole-round limit.
            wait_seconds = min(child_time, max(0.0, remaining_time - min(request_time * 2, self._synthesis_reserve())))
        await self._publish("budget_closeout", "manager", {"remaining_model_calls": self._remaining_calls(),
            "reasons": sorted(self.closeout_reasons), "remaining_analysis_seconds": remaining_time,
            "completed_report_ids": [r.get('report_id') for c in self.children.values() for r in c.reports],
            "active_child_ids": [child.id for child in pending],
            "active_children": len(active), "wait_seconds": wait_seconds,
            "reserved_manager_http_attempts": self._final_call_reserve(),
            "note": "预算内收尾；等待已有子任务提交可见成果，未完成研究保留缺口。"})
        if active and wait_seconds > 0:
            # Timeout does not silently turn unfinished work into a report.
            await asyncio.wait(active, timeout=wait_seconds)
        await self._publish('budget_closeout_finished', 'manager', {
            'remaining_model_calls': self._remaining_calls(),
            'completed_report_ids': [r.get('report_id') for c in self.children.values() for r in c.reports],
            'unfinished_child_ids': [c.id for c in self.children.values() if c.status in {'queued', 'running'}],
            'note': '下次请求在此之后构造；是否实际纳入以报告交付回执为准，晚到成果不自动视为已使用。'})

    def _runtime_context(self, session, agent_id, remaining_turns, final_request, *, seed=None, incremental=False, retained_budget=None):
        """Build a visible state update without changing any existing message."""
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
                item["latest_report"] = {"report_id": report.get("report_id"), "research_round": report.get("research_round"), "summary": summary[:summary_limit],
                    "summary_truncated": len(summary) > summary_limit,
                    "material_ids": report.get("material_ids", []), "open_questions": report.get("open_questions", [])}
            if child.progress:
                item["latest_progress"] = {"summary": str(child.progress[-1].get("summary") or "")[:summary_limit],
                    "material_ids": child.progress[-1].get("material_ids", []), "is_final_report": False}
            visible.append(item)
        status = {"agent_id": agent_id, "model_calls_used": self.calls,
            "remaining_shared_model_calls": self._remaining_calls(),
            "remaining_agent_turns": remaining_turns,
            "closing": self.budget_closing or (agent_id != 'manager' and self.children[agent_id].deadline_closing),
            "time_context": _time_context(self.context),
            "children": visible,
            "instruction": "这是当前运行状态。已有子任务不要重复创建；旧报告不代表后续补查已完成，阶段进展不等于最终报告。"}
        if getattr(session, 'submission_feedback', None):
            status['submission_feedback'] = session.submission_feedback
        if getattr(session, 'submission_candidate', None):
            status['candidate_draft'] = session.submission_candidate
        if final_request:
            status["instruction"] += " 当前只提交已有证据支持的报告；明确列出未完成任务、资料缺口及其影响，未知数字或分类保持未知，不再扩展研究。"
            if agent_id == "manager":
                status["instruction"] += (" 直接且仅调用一次 finish_analysis，将最终结果写入工具参数，不先输出普通消息或单独 JSON。"
                    "report 简明地一次写清账户判断、范围内每只证券的动作、依据、重要反向因素和改变看法的条件；"
                    "完整填写必填字段，可选结构仅在必要时填写，避免重复正文。"
                    "正文关键来源就近使用已有 URL、材料ID或子报告身份，计算保留输入口径；不复制整份来源索引。")
                if self.require_demand_coverage:
                    status['instruction'] += ' 本任务 covered_demand_ids 是必填数组；只声明已读、实际回答且scope足以覆盖的需求，未覆盖可填[]。公共整轮用public，账户整轮用account/cash_only，即使仅一只证券；仅明确target_security任务只填该证券。不必新增查阅或完成全部需求。'
        limit = max(1000, int(self.config.get("max_context_chars", 240_000)) // 3)
        def status_over_budget():
            if len(json.dumps(status, ensure_ascii=False)) > limit:
                return True
            if retained_budget is None:
                return False
            package = {**(seed or {}), 'prediction_context': prediction_context(self.context), 'runtime_status': status}
            message = {'role': 'user', 'content': json.dumps(package, ensure_ascii=False, default=str)}
            return len(json.dumps(message, ensure_ascii=False)) > retained_budget

        if status_over_budget():
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
                    item["latest_report"] = {"report_id": report.get("report_id"), "research_round": report["research_round"], "summary": report["summary"][:160],
                        "summary_truncated": True, "material_ids": [str(mid)[:120] for mid in report["material_ids"][:2]],
                        "open_question_count": len(report["open_questions"])}
            # Preserve every child ID/status/round and unresolved flag even when
            # a large number of reports needs retrieval instead of inline text.
            for key in ("latest_report", "pending_messages", "error", "title"):
                if not status_over_budget():
                    break
                for item in visible:
                    item.pop(key, None)
            while visible and status_over_budget():
                omitted = visible.pop()
                status["children_omitted"] = status.get("children_omitted", 0) + 1
                status["unresolved_children_omitted"] = status.get("unresolved_children_omitted", 0) + int(omitted["unresolved"])
        seed = {} if seed is None else seed
        # Identity is retained context, not child-status budget. Adding it to
        # status would evict existing collaboration rows at small budgets.
        seed["prediction_context"] = prediction_context(self.context)
        seed["runtime_status"] = status
        self._pin_calculations(seed, session, agent_id, final_request, incremental=incremental)
        return seed

    def _bound_previous_analysis(self, seed, session):
        """Keep the previous account conclusion once, with bounded repeated detail.

        Archive the existing visible-context projection, not raw source bodies
        or a different account's report. Nothing is retrieved from an archive.
        """
        previous = seed.get('previous_analysis')
        if not isinstance(previous, dict) or previous.get('bounded_history_projection'):
            return
        size = lambda item: len(json.dumps(item, ensure_ascii=False, default=str))
        limit = max(1000, int(self.config.get('max_context_chars', 240_000)) // 10)
        if size(previous) <= limit:
            return
        record = archive(session, previous, 'previous_analysis_context')
        projected = {key: copy.deepcopy(previous[key]) for key in
            ('id', 'job_id', 'account_id', 'input_version', 'status', 'created_at', 'data_as_of', 'report') if key in previous}
        projected.update(bounded_history_projection=True,
            full_context={'record_id': record['record_id'], 'total_chars': record['total_chars'],
                          'tool': 'recall_context', 'session_agent_id': 'manager'},
            history_note='上次报告的原结论与身份；结构化重复内容及来源库存有界保留。未纳入本次不等于此前未取得，未重新读取的来源不因旧报告而视为本轮已读。仅提交时不能再取归档。')
        # Reports themselves may exceed a small configured context. Preserve a
        # visibly marked excerpt and the exact account-scoped archive pointer.
        text = projected.get('report')
        if isinstance(text, str) and size(projected) > limit:
            projected['report'] = text[:max(0, limit // 2)]
            projected['report_truncated_for_context'] = True
        structured = previous.get('result') if isinstance(previous.get('result'), dict) else {}
        projected['result'] = {}
        omitted = {}
        for key in ('findings', 'suggestions', 'forecasts', 'uncertainties', 'research_records', 'references'):
            items = structured.get(key)
            if not isinstance(items, list):
                continue
            kept = []
            projected['result'][key] = kept
            for item in items:
                kept.append(copy.deepcopy(item))
                if size(projected) > limit - 600:
                    kept.pop()
            omitted[key] = len(items) - len(kept)
        projected['omitted_structure_counts'] = omitted
        projected['material_index_count'] = len(previous.get('material_ids') or [])
        seed['previous_analysis'] = projected

    def _bound_checkpoint_reports(self, seed, session):
        checkpoint = seed.get('synthesis_checkpoint')
        if not isinstance(checkpoint, dict) or checkpoint.get('reports_bounded_for_context'):
            return
        reports = checkpoint.get('reports')
        if not isinstance(reports, list):
            return
        size = lambda value: len(json.dumps(value, ensure_ascii=False, default=str))
        budget = max(500, int(self.config.get('max_context_chars', 240_000)) // 5)
        if size(reports) <= budget:
            return
        record = archive(session, reports, 'checkpoint_report_context')
        kept, deferred = [], []
        for report in reports:
            if size([*kept, report]) <= budget:
                kept.append(copy.deepcopy(report))
            else:
                deferred.append(report)
        omitted = 0
        for report in deferred:
            excerpt = {key: copy.deepcopy(report[key]) for key in
                ('report_id', 'agent_id', 'role_id', 'research_round', 'checkpoint_source_job_id') if key in report}
            excerpt.update(summary=str(report.get('summary') or '')[:160], summary_truncated=True,
                findings_omitted=True, open_question_count=len(report.get('open_questions') or []),
                complete_report_ref=record['record_id'])
            if size([*kept, excerpt]) <= budget:
                kept.append(excerpt)
            else:
                omitted += 1
        checkpoint.update(reports=kept, reports_bounded_for_context=True,
            unlisted_report_count=omitted,
            report_context={'record_id': record['record_id'], 'total_reports': len(reports),
                'total_chars': record['total_chars'],
                'note': '摘要/省略项不是全文交付，未决问题不因未列而解决；只提交时不能再读取归档。'})

    @staticmethod
    def _checkpoint_has_full_report(seed, report):
        if not report.get('report_id'):
            return False
        for saved in (seed.get('synthesis_checkpoint') or {}).get('reports', []):
            if (saved.get('report_id') == report['report_id'] and not saved.get('summary_truncated')
                    and all(saved.get(key) == report.get(key) for key in
                        ('summary', 'findings', 'material_ids', 'open_questions', 'material_updates') if key in report)):
                return True
        return False

    def _retained_runtime_context(self, session, agent_id, remaining_turns, final_request, *, for_compaction=False):
        """Freeze one fresh evidence package only at an explicit compaction."""
        seed = json.loads(session.history[0]['content'])
        if for_compaction and getattr(session, '_final_native_source_contexts', None):
            # Ordinary final requests keep the original source user messages.
            # Only a necessary compression moves that already-selected package
            # into the retained seed; never rescan or expand source archives.
            seed['retained_visible_source_contexts'] = copy.deepcopy(session._final_native_source_contexts)
        if ((agent_id != 'manager' and getattr(session, '_child_final_projected', False))
                or (agent_id == 'manager' and getattr(session, '_manager_final_projected', False))):
            # A large rejected submission may require the ordinary compactor.
            # Keep the same selected final evidence and exact calculation views;
            # do not rescan archives or allocate them against one another again.
            calculation_keys = ('calculation_summaries', 'calculation_indexes',
                                'calculation_access', 'calculation_contexts')
            frozen = {key: copy.deepcopy(seed[key]) for key in calculation_keys if key in seed}
            seed = self._runtime_context(session, agent_id, remaining_turns, True,
                seed=seed, incremental=True,
                retained_budget=int(self.config.get('max_context_chars', 240_000) * .67))
            for key in calculation_keys:
                seed.pop(key, None)
            seed.update(frozen)
            session._last_runtime_children = copy.deepcopy(seed['runtime_status']['children'])
            return seed
        for key in ('retained_child_reports', 'runtime_status', 'calculation_summaries',
                    'calculation_indexes', 'calculation_access', 'calculation_contexts'):
            seed.pop(key, None)
        # Full completed reports remain attributable and accessible after generic
        # history retirement. Oversized reports have explicit archive pointers;
        # their unseen tails are never represented as read.
        if agent_id == 'manager':
            self._bound_previous_analysis(seed, session)
            self._bound_checkpoint_reports(seed, session)
            reports = [_report_context(report) for child in self.children.values() for report in child.reports
                       if not self._checkpoint_has_full_report(seed, report)]
            if reports:
                budget = max(500, int(self.config.get('max_context_chars', 240_000)) // 5)
                seed['retained_child_reports'] = bounded_result(session, reports, budget)
        candidates = self._retain_research_sources(seed, session, agent_id) if for_compaction else None
        # Allocate calculations after reports and visible sources, from the same bounded envelope.
        # Otherwise two independently full allocations can immediately trigger
        # another compaction of an unchanged package on the next request.
        seed = self._runtime_context(session, agent_id, remaining_turns, final_request, seed=seed,
            retained_budget=int(self.config.get('max_context_chars', 240_000) * .67))
        if candidates:
            # Calculations/status/reports are now fixed. Spend only actual
            # serialized space still free inside the SAME retained envelope.
            self._retain_research_sources(seed, session, agent_id,
                candidates=candidates, fill_remaining=True)
        session._last_runtime_children = copy.deepcopy(seed['runtime_status']['children'])
        return seed

    def _pin_runtime_status(self, session: ModelSession, agent_id: str, remaining_turns: int | None, final_request: bool) -> None:
        """Freeze initial context; deliver subsequent state and evidence at tail."""
        initial = not session.requests or getattr(session, '_building_context_epoch', False)
        seed = json.loads(session.history[0]['content']) if initial else None
        if initial and ((agent_id != 'manager' and getattr(session, '_child_final_projected', False))
                or (agent_id == 'manager' and getattr(session, '_manager_final_projected', False))):
            update = self._retained_runtime_context(session, agent_id, remaining_turns, final_request)
        else:
            update = self._runtime_context(session, agent_id, remaining_turns, final_request,
                                           seed=seed, incremental=not initial)
        children = copy.deepcopy(update['runtime_status']['children'])
        if not initial:
            previous = {item['agent_id']: item for item in getattr(session, '_last_runtime_children', [])}
            changed = []
            for item in children:
                old = previous.get(item['agent_id'])
                if item == old:
                    continue
                delta = {key: value for key, value in item.items()
                         if key == 'agent_id' or old is None or old.get(key) != value}
                removed = sorted(set(old or {}) - set(item))
                if removed:
                    delta['removed_fields'] = removed
                changed.append(delta)
            update['runtime_status']['children'] = changed
            update['runtime_status']['children_update'] = 'merge_by_agent_id; removed_fields clears prior fields; unchanged fields retain prior values; omitted children are not completed'
            if update.get('prediction_context') == getattr(session, '_last_prediction_context', None):
                update.pop('prediction_context', None)
            update['context_update'] = {'task_id': self.task_id, 'agent_id': agent_id,
                'sequence': getattr(session, '_runtime_update_sequence', 0) + 1,
                'instruction': '本消息按出现顺序更新当前状态；新事实和明确更正优先于此前观点，未改变的事实仍保留。null预算表示按次数不限，时间、取消和完成条件仍有效。'}
            session._runtime_update_sequence = update['context_update']['sequence']
            session.history.append({'role': 'user', 'content': json.dumps(update, ensure_ascii=False, default=str, allow_nan=False)})
        else:
            session.history[0]['content'] = json.dumps(update, ensure_ascii=False, default=str, allow_nan=False)
        session._last_runtime_children = children
        session._last_prediction_context = prediction_context(self.context)
        session.retained_context_factory = lambda: self._retained_runtime_context(
            session, agent_id, remaining_turns, final_request, for_compaction=True)

    async def _publish(self, kind: str, agent_id: str, payload: dict) -> dict:
        # No raw model request/response or opaque reasoning state is logged.
        event = {"sequence": len(self.events) + 1, "type": kind, "agent_id": agent_id,
                 "parent_id": None if agent_id == "manager" else "manager",
                 "task_id": self.task_id, "account_id": self.account_id, "input_version": self.input_version,
                 "timestamp": now_datetime(SHANGHAI).isoformat(), "payload": payload}
        event = json.loads(json.dumps(event, ensure_ascii=False, default=str, allow_nan=False))
        self.events.append(event)
        async with self.condition:
            self.condition.notify_all()
        await self.emit(copy.deepcopy(event))
        return event

    async def run(self) -> dict:
        self.deadline = min(time.monotonic() + float(self.config.get("analysis_timeout_seconds", 1200)),
                            self.inherited_deadline if self.inherited_deadline is not None else float('inf'),
                            self.forecast_deadline if self.forecast_deadline is not None else float('inf'))
        self.deadline_code = ('forecast_deadline_exceeded' if self.forecast_deadline is not None
                             and self.forecast_deadline <= self.deadline else 'analysis_budget_exhausted')
        self.manager_session.deadlines = {self.deadline_code: self.deadline}
        await self._publish("agent_started", "manager", {"title": "主 Agent", "reason": self.context.get("reason"), "model_config": self.model_config,
            'resource_limits': {'request_timeout_seconds': timeout_policy(self.config)['request_hard_seconds'],
                'timeout_policy': timeout_policy(self.config), 'synthesis_timeout_policy': timeout_policy(self.config, 'synthesis'),
                'policy_warnings': timeout_policy_warnings(self.config),
                'remaining_analysis_seconds': max(0, self.deadline - time.monotonic()),
                'synthesis_reserve_seconds': self._synthesis_reserve(),
                'child_timeout_seconds': self.config.get('agent_timeout_seconds', 600),
                'analysis_timeout_seconds': self.config.get('analysis_timeout_seconds', 1200),
                'max_model_calls': self.config.get('max_model_calls', 40),
                'synthesis_max_calls': self.config.get('synthesis_max_calls', 4)}})
        if self.synthesis_only:
            await self._publish("synthesis_resumed", "manager", {"source_task_id": self.synthesis_checkpoint.get("task_id"), "max_calls": self.config.get("synthesis_max_calls", 4),
                "configuration_relation": self.synthesis_checkpoint.get("configuration_relation"), "model_config": self.model_config})
        elif self.restored_checkpoint:
            await self._publish('research_resumed', 'manager', {
                'source_task_id': self.restored_checkpoint.get('checkpoint_job_id'),
                'completed_reports': len(self.restored_checkpoint.get('reports', [])),
                'completed_calculations': len(self.calculations),
                'recovery_mode': 'continue_research',
                'note': '复用有效已完成成果；未完成专题保留，按当前共享预算继续研究。'})
        try:
            async with asyncio.timeout_at(self.deadline):
                result = await self._loop("manager", self.manager_session, manager=True)
                await self._cancel_children()
                if self.children:
                    # Persist completed late work and the actual cancellation
                    # state before publishing the accepted main submission.
                    await self._save_checkpoint()
                await self._record_unincorporated_child_reports(result, self.submitted_report_counts)
                result.setdefault("report", result.pop("summary", ""))
                result.setdefault("findings", [])
                result.update({"task_id": self.task_id, "account_id": self.account_id,
                               "input_version": self.input_version, "data_as_of": self.context.get("data_as_of"),
                               "agents": [child.snapshot() for child in self.children.values()],
                               "model_calls": self.calls, "model_usage": self.client.usage_summary([r for client in {self.client, *[c.client or self.client for c in self.children.values()]} for r in client.requests]),
                               "model_config": copy.deepcopy(self.model_config),
                               "team_snapshot": copy.deepcopy(self.team),
                               "synthesis_recovered": bool(self.restored_checkpoint and self.restored_checkpoint.get('recovery_mode') != 'continue_research'),
                               "research_recovered": bool(self.restored_checkpoint),
                               "recovery_mode": (self.restored_checkpoint or {}).get('recovery_mode'),
                               "native_search": self.client.native_status(),
                               "context_compactions": self.manager_session.compactions + sum(c.session.compactions for c in self.children.values()),
                               "calculation_coverage": copy.deepcopy(self.calculation_coverage),
                               "materials": copy.deepcopy(self.materials)})
                result['research_delivery'] = {'final_report_submitted': result.get('submission', {}).get('action') == 'finish_analysis',
                    'completion': result.get('completion'), 'content_assessment': 'not_evaluated',
                    'child_total': len(self.children), 'child_completed': sum(c.status == 'completed' for c in self.children.values()),
                    'child_incomplete': [{'agent_id': c.id, 'title': c.title, 'status': c.status, 'error': c.error,
                        'saved_report_count': len(c.reports)} for c in self.children.values() if c.status != 'completed'],
                    'recovered_unresolved_tasks': copy.deepcopy((self.context.get('synthesis_checkpoint') or {}).get('unresolved_tasks', [])),
                    'recovered_partial_research': bool((self.context.get('synthesis_checkpoint') or {}).get('partial_research')),
                    'note': '执行与专题提交统计，不代表资料覆盖、建议质量或方向准确率。'}
                if hasattr(self.tools, 'store'):
                    result['report_access'] = [{key: item['body'].get(key) for key in
                        ('report_id', 'source_agent_id', 'research_round', 'role_id', 'access', 'summary_truncated', 'note')}
                        for item in self.tools.store.checkpoint_report_deliveries(self.task_id)]
                await self._publish("agent_completed", "manager", {"report": result["report"], "model_calls": self.calls, "model_usage": result["model_usage"]})
                return json.loads(json.dumps(result, ensure_ascii=False, default=str, allow_nan=False))
        except TimeoutError as exc:
            self.cancelled_by = self.deadline_code
            self.client.resolve_pending(self.manager_session, '整轮总预算耗尽；此调用未完成续接，未完成部分保持未知')
            stage = 'forecast_deadline' if self.deadline_code == 'forecast_deadline_exceeded' else 'analysis_deadline'
            await self._save_interrupted_checkpoint(stage)
            message = ModelTimeoutError.MESSAGES[self.deadline_code]
            await self._publish("agent_failed", "manager", {"error": message, 'failure_stage': stage,
                'error_code': self.deadline_code, 'cancelled_by': self.deadline_code,
                'timeout_seconds': self.config.get('analysis_timeout_seconds', 1200), 'model_calls': self.calls})
            raise ResearchTimeout(message, code=self.deadline_code) from exc
        except asyncio.CancelledError as exc:
            # Service's outer total deadline also arrives as cancellation.
            # Preserve completed work without guessing whether the cause was
            # that deadline, an explicit cancel or service interruption.
            self.client.resolve_pending(self.manager_session, '整轮已取消；此调用未完成续接，未完成部分保持未知')
            await self._save_interrupted_checkpoint('cancelled_or_interrupted')
            expired = self.deadline is not None and time.monotonic() >= self.deadline
            self.cancelled_by = self.deadline_code if expired else cancellation_source(exc)
            await self._publish("agent_cancelled", "manager", {"reason": ModelTimeoutError.MESSAGES[self.deadline_code] if expired else "父任务取消；实际用户/版本/停机原因见任务事件",
                'error_code': self.deadline_code if expired else 'cancelled',
                'cancelled_by': self.cancelled_by})
            raise
        except Exception as exc:
            self.cancelled_by = 'parent_failed'
            if isinstance(exc, (ModelError, DeliveryContractError, FinalSubmissionMissing)):
                self.client.resolve_pending(self.manager_session, '模型或共享预算失败；此调用未完成续接，未完成部分保持未知')
                await self._save_interrupted_checkpoint('model_request_or_budget')
            await self._publish("agent_failed", "manager", {"error": self._safe_error(exc), 'error_code': getattr(exc, 'code', type(exc).__name__)})
            raise
        finally:
            await self.close()

    async def close(self) -> None:
        self.closed = True
        await self._cancel_children()
        clients = {self.client, *[c.client or self.client for c in self.children.values()]}
        await asyncio.gather(*[client.aclose(source='runtime_close') for client in clients])
        if not getattr(self, '_client_close_events_published', False):
            self._client_close_events_published = True
            for client in clients:
                await self._publish('model_client_closed', 'manager', {
                    'client_id': client.network_diagnostics.client_id,
                    'lifecycle_events': copy.deepcopy(client.network_diagnostics.lifecycle_events)})

    async def _cancel_children(self) -> None:
        active = list(self.handles)
        for task in active:
            if not task.done():
                task.cancel(self.cancelled_by)
        if active:
            await asyncio.gather(*active, return_exceptions=True)

    def _safe_error(self, exc: Exception) -> str:
        text = str(exc) or type(exc).__name__
        for client in [self.client, *[c.client or self.client for c in self.children.values()]]:
            if client.key:
                text = text.replace(client.key, "[REDACTED]")
        return text[:1200]

    async def _loop(self, agent_id: str, session: ModelSession, *, manager: bool) -> dict:
        client = self._client_for(agent_id)
        session.task_id = self.task_id
        max_turns = self.config.get("max_agent_turns", 12)
        max_turns = int(max_turns) if max_turns is not None else None
        if not manager:
            # A follow-up is a new bounded research round; the previous round
            # may have disabled native search solely for its final submission.
            session.native_search = True
            if getattr(session, '_child_final_projected', False):
                client.add_user(session, {'child_research_resumed': {
                    'round': self.children[agent_id].round,
                    'instruction': '这是后续研究；上一提交阶段的仅提交限制已经结束。使用当前实际提供的工具处理新增问题，旧提取仍保留原身份和范围。'}})
            # One projection per submission sequence; a later follow-up may
            # start a new sequence, while correction turns keep their prefix.
            session._child_final_projected = False
        reference_candidate = None
        reference_report_counts = {}
        submission_required = False
        submission_error = None
        submission_failures = 0
        previous_progress = None
        repeated_progress = 0
        known_lookups = KnownLookupTracker(max_entries=128)
        known_lookup_rounds = 0
        seen_native_materials = set()
        for turn_index in (range(max_turns) if max_turns is not None else itertools.count()):
            definitions = RESEARCH_TOOLS + (MANAGER_TOOLS if manager else CHILD_TOOLS)
            if manager and self.require_demand_coverage:
                definitions = [copy.deepcopy(tool) if tool['name'] == 'finish_analysis' else tool
                               for tool in definitions]
                for tool in definitions:
                    if tool['name'] == 'finish_analysis':
                        tool['parameters']['required'].append('covered_demand_ids')
            if self.team:
                role_id = 'manager' if manager else self.children[agent_id].role_id
                permitted_tools = set(self.team['roles'][role_id]['tools'])
                definitions = [tool for tool in definitions if tool['name'] in permitted_tools]
            if manager and self.synthesis_only:
                permitted = {"get_material", "read_material_range", "list_materials", "search_materials", "query_market_rows", "market_indicators", "recall_context", "calculate", "save_material", "list_recommendations", "get_recommendation", "list_business_demands", "list_forecast_targets", "finish_analysis"}
                definitions = [tool for tool in definitions if tool["name"] in permitted]
            remaining_turns = max_turns - turn_index if max_turns is not None else None
            final_request = (submission_required or self._shared_closeout() or self._child_closeout(agent_id)
                             or (remaining_turns is not None and remaining_turns <= (2 if manager else 1)))
            if max_turns is not None and max_turns >= 6 and remaining_turns <= 3:
                client.add_user(session, {"resource_budget": {"remaining_agent_turns": remaining_turns,
                    "remaining_shared_model_calls": self._remaining_calls()},
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
                received_child_updates = bool(events)
                if events:
                    client.add_user(session, {"research_events": events})
                if not self.synthesis_only and self.children and all(child.status in {"completed", "failed", "cancelled"} for child in self.children.values()):
                    if any(child.reports for child in self.children.values()):
                        await self._save_checkpoint()
                        session.phase = "synthesis"
            else:
                received_child_updates = self._deliver_inbox(self.children[agent_id])
            if final_request:
                if manager:
                    await self._enter_synthesis_context(session, reason='bounded_final_request')
                else:
                    await self._enter_child_final_context(session, agent_id, reason='bounded_final_request')
            request_start, compactions = len(session.requests), session.compactions
            try:
                while True:
                    self._pin_runtime_status(session, agent_id, remaining_turns, final_request)
                    request_report_counts = {child.id: len(child.reports) for child in self.children.values()}
                    token = self.request_owner.set((agent_id, final_request))
                    try:
                        turn = await client.complete(session, definitions,
                            tool_choice=("finish_analysis" if manager else "submit_report") if final_request else None)
                        break
                    except _CloseoutRequired:
                        # A child deadline, concurrent request or retry reached
                        # its reserve after preparation. Rebuild before HTTP.
                        final_request = True
                        definitions = [tool for tool in definitions if tool["name"] == ("finish_analysis" if manager else "submit_report")]
                        allowed = {item["name"] for item in definitions}
                        session.native_search = False
                        if manager:
                            session.phase = "synthesis"
                            await self._wait_for_closeout()
                            events = self._new_child_events()
                            if events:
                                received_child_updates = True
                                client.add_user(session, {"research_events": events})
                            await self._save_checkpoint()
                            await self._enter_synthesis_context(session, reason='concurrent_budget_closeout')
                        else:
                            await self._enter_child_final_context(session, agent_id, reason='concurrent_budget_closeout')
                    finally:
                        self.request_owner.reset(token)
            except asyncio.CancelledError:
                await self._publish("model_error", agent_id, {"error": "模型请求随研究任务取消", "requests": copy.deepcopy(session.requests[request_start:])})
                raise
            except Exception as exc:
                partial = getattr(exc, "native_turn", None)
                if partial is not None:
                    await self._record_native(agent_id, partial)
                await self._publish("model_error", agent_id, {"error": self._safe_error(exc), 'error_code': getattr(exc, 'code', type(exc).__name__), "requests": copy.deepcopy(session.requests[request_start:]),
                    "native_search": client.native_status()})
                if isinstance(exc, ModelConfigurationError):
                    raise
                if final_request and getattr(exc, 'submission_retry_safe', False):
                    submission_failures += 1
                    submission_error = DeliveryContractError(str(exc))
                    await self._publish('terminal_submission_rejected', agent_id, {
                        'error_code': submission_error.code, 'cause': getattr(exc, 'code', type(exc).__name__),
                        'completed_but_incomplete_submission': True})
                    if submission_failures < 2 and (remaining_turns is None or remaining_turns > 1) and self._calls_available():
                        submission_required = True
                        session.submission_feedback = exc.submission_feedback
                        client.add_user(session, {'submission_feedback': exc.submission_feedback})
                        continue
                    raise submission_error from exc
                if reference_candidate is not None:
                    reference_candidate['reference_resolution_note'] = '引用修正请求未完成；保留原报告的已解析部分和明确缺口。'
                    if manager:
                        self.submitted_report_counts = reference_report_counts
                    return reference_candidate
                raise
            await self._publish("model_usage", agent_id, {"requests": copy.deepcopy(session.requests[request_start:]), "session": client.usage_summary(session.requests)})
            native_materials = await self._record_native(agent_id, turn)
            new_native_materials = False
            for item in native_materials:
                key = native_material_identity(item)
                if key not in seen_native_materials:
                    seen_native_materials.add(key)
                    new_native_materials = True
            if session.compactions > compactions:
                await self._publish("context_compacted", agent_id, {"compactions": session.compactions, "archived_records": len(session.archive), "retrieval_tool": "recall_context"})
            if not turn.calls:
                known_lookup_rounds = 0
                if native_materials:
                    client.add_user(session, {"native_source_materials": native_materials})
                if turn.continuation_required and not final_request:
                    continue
                # HTTP completion and visible prose are progress, never a business
                # commit. Preserve the full visible candidate for explicit recovery.
                reference_candidate = None
                if turn.text.strip():
                    limit = max(500, int(self.config.get('max_context_chars', 240_000)) // 8)
                    draft = archive(session, turn.text, 'candidate_draft')
                    session.submission_candidate = {'text': turn.text[:limit],
                        'truncated': len(turn.text) > limit, 'record_id': draft['record_id'],
                        'is_final_report': False}
                    await self._publish("final_submission_candidate", agent_id, {
                        "text": turn.text, "is_final_report": False,
                        "error_code": "final_submission_missing"})
                submission_error = FinalSubmissionMissing("尚未调用终止工具；可见文字已保留为候选，未发布账户报告")
                submission_failures += 1
                await self._publish("terminal_submission_rejected", agent_id, {
                    "error_code": submission_error.code, "requested_tool_choice":
                    ("finish_analysis" if manager else "submit_report") if final_request else None,
                    "remaining_model_calls": self._remaining_calls()})
                if submission_failures >= 2 or (remaining_turns is not None and remaining_turns <= 1) or not self._calls_available():
                    raise submission_error
                submission_required = True
                session.submission_feedback = '普通文字尚未提交。仅调用一次终止工具，基于已有草稿与事实填写判断、scope、completion 和具体 limitations；不重新搜索。'
                client.add_user(session, {"submission_feedback": "普通文本未构成正式交付。保留刚才已有判断，仅调用一次 "
                    + ("finish_analysis，填写 report、completion、scope、decision_summary、limitations。" if manager else "submit_report，填写 summary 与具体缺口。")
                    + "不得重新搜索；不补造未知事实或预测。"})
                continue
            terminal_calls = [call for call in turn.calls if call.name in {'finish_analysis', 'submit_report'}]
            if final_request and not terminal_calls:
                submission_failures += 1
                submission_required = True
                submission_error = FinalSubmissionMissing('最终阶段未调用指定终止工具；研究工具未执行')
                session.submission_feedback = str(submission_error)
                error = {'accepted': False, 'failed': True, 'error_code': submission_error.code,
                         'error': str(submission_error)}
                client.add_results(session, [(call, error) for call in turn.calls])
                self._retain_submission_feedback(session, turn.calls)
                await self._publish('terminal_submission_rejected', agent_id, error)
                if submission_failures >= 2 or (remaining_turns is not None and remaining_turns <= 1) or not self._calls_available():
                    raise submission_error
                continue
            if terminal_calls and (len(terminal_calls) != 1 or len(turn.calls) != 1):
                # A terminal submission is a lifecycle transition, never a batch
                # of competing candidates or a tool probe. Reject the entire
                # local-call turn atomically; nothing in it is executed.
                reference_candidate = None
                submission_required = True
                submission_failures += 1
                submission_error = DeliveryContractError('终止提交必须唯一且不能与研究工具混批')
                session.submission_feedback = str(submission_error)
                error = {'accepted': False, 'failed': True, 'error_code': 'invalid_terminal_submission',
                    'error': '终止提交必须是本轮唯一工具调用；请只调用一次 finish_analysis/submit_report，且不能与研究工具混用。'}
                client.add_results(session, [(call, error) for call in turn.calls])
                self._retain_submission_feedback(session, turn.calls)
                for call in turn.calls:
                    await self._publish('tool_result', agent_id, {'tool': call.name, 'result': error})
                await self._publish('terminal_submission_rejected', agent_id, {'call_count': len(turn.calls), 'error_code': error['error_code']})
                if submission_failures >= 2 or (remaining_turns is not None and remaining_turns <= 1) or not self._calls_available():
                    raise submission_error
                continue
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
                    arguments = copy.deepcopy(call.arguments)
                    if call.name == 'finish_analysis':
                        # Submission identity is runtime-owned, never model input.
                        arguments['submission'] = {'action': call.name, 'contract_version': 1, 'call_id': call.id}
                        arguments = validate_final_result(arguments,
                            allowed_scope=self.context.get('allowed_delivery_scope'),
                            required_scope=self.context.get('required_delivery_scope'),
                            require_demand_coverage=self.require_demand_coverage)
                    output = await self._dispatch(agent_id, call.name, arguments)
                    if call.name == 'finish_analysis' and output.get('reference_feedback'):
                        reference_candidate = output['reference_feedback']
                        reference_report_counts = request_report_counts
                        if (remaining_turns is not None and remaining_turns <= 1) or not self._calls_available():
                            output = {'accepted': True, 'resolved_result': reference_candidate,
                                      'note': '没有剩余修正调用预算；保留引用缺口并交付有效部分。'}
                    if call.name in {"finish_analysis", "submit_report"} and output.get('accepted'):
                        submitted = copy.deepcopy(output.get('resolved_result', arguments))
                        submitted['submission'] = {'action': call.name, 'contract_version': 1, 'call_id': call.id}
                        submitted['submission_outcome'] = 'recovered' if submission_failures else 'first_attempt'
                        submitted['submission_corrections'] = submission_failures
                        submitted['material_delivery_ids'] = list(self.material_delivery_ids.get(agent_id, []))
                except (ModelBudgetExceeded, asyncio.CancelledError):
                    raise
                except Exception as exc:
                    output = {"error": self._safe_error(exc), "failed": True,
                              "error_code": getattr(exc, 'code', 'invalid_tool_arguments')}
                    if call.name in {'finish_analysis', 'submit_report'}:
                        reference_candidate = None
                        submission_required = True
                        submission_failures += 1
                        submission_error = DeliveryContractError(self._safe_error(exc))
                        session.submission_feedback = str(submission_error)
                        output['error_code'] = submission_error.code
                        await self._publish('terminal_submission_rejected', agent_id, output)
                    if call.name in {'market_indicators', 'calculate', 'query_market_rows'}:
                        self._remember_calculation(agent_id, call.name, call.arguments, output)
                results.append((call, output))
                await self._publish("tool_result", agent_id, {"tool": call.name, "result": self._preview(output)})
            client.add_results(session, results)
            result_messages = session.history[-len(results):] if results else []
            progress = tool_round_signature(results)
            repeated_progress = repeated_progress + 1 if progress == previous_progress else 0
            previous_progress = progress
            # Waiting on a confirmed live child is useful even when its next
            # visible finding has not arrived. It is not repeated failed work.
            waiting_on_live_target = bool(results) and all(self._waiting_on_live_target(call, output) for call, output in results)
            # Keep observing failed lookups to detect alternating failures,
            # but do not treat an unrelated live child as progress on failed work.
            all_known = known_lookups.observe(results)
            failed_round = any(is_failed_tool_output(output) for _, output in results)
            delivered_progress = bool(new_native_materials or waiting_on_live_target or received_child_updates)
            has_live_progress = bool(delivered_progress
                or (manager and not failed_round
                    and any(child.status in {'queued', 'running'} for child in self.children.values())))
            exempt_round = all_known and has_live_progress
            if exempt_round or delivered_progress:
                repeated_progress = 0
            if exempt_round:
                known_lookup_rounds = 0
            if repeated_progress >= 2 and not terminal_calls:
                submission_required = True
                client.add_user(session, {'progress_boundary': '连续相同工具输入和结果没有新增进展；基于已有证据提交，并列出未解决问题。'})
                await self._publish('research_no_progress', agent_id, {'identical_tool_rounds': repeated_progress + 1, 'action': 'submit_existing_evidence'})
            if not all_known or terminal_calls or has_live_progress:
                known_lookup_rounds = 0
            else:
                known_lookup_rounds += 1
                # The legacy identical-round guard takes precedence if both match.
                if repeated_progress < 2:
                    if known_lookup_rounds == 1:
                        client.add_user(session, {'research_lookup_repeat_warning': '本轮只读查阅的参数和业务结果均已获取且未变化。'
                            '可在下一轮读取新原文、运行新计算或验证新线索，也可直接基于已有证据提交。'})
                        await self._publish('research_lookup_repeat_warning', agent_id, {
                            'known_lookup_rounds': known_lookup_rounds, 'lookup_count': len(results),
                            'action': 'warn_before_closeout'})
                    else:
                        submission_required = True
                        client.add_user(session, {'progress_boundary': '提醒后仍在查阅参数和业务结果均未变化的页面；'
                            '基于已有证据提交，并列出未解决问题。'})
                        await self._publish('research_no_progress', agent_id, {'known_lookup_rounds': known_lookup_rounds,
                            'reason': 'repeated_known_lookups', 'action': 'submit_existing_evidence'})
            if terminal_calls and submitted is None:
                self._retain_submission_feedback(session, turn.calls)
            if manager:
                self._remember_report_results(results, result_messages)
            # Inspect the bounded, visible tool results, never the unabridged
            # provider/body object that may have been put into the context archive.
            if hasattr(self.tools, 'store'):
                pending = getattr(session, '_pending_material_deliveries', [])
                for (call, _), message in zip(results, result_messages):
                    value = json.loads(message.get('output', message.get('content', '{}')))
                    proof = self.tools.store.prepare_pending_tool_output(self.task_id, self.account_id,
                        agent_id, call.name, value)
                    pending.append({'call': copy.deepcopy(call), 'message': copy.deepcopy(message), 'proof': proof})
                session._pending_material_deliveries = pending
            if manager and getattr(self, "_fresh_synthesis_context", None) is not None:
                self._fresh_synthesis_context = None
                await self._enter_synthesis_context(session, reason='prepare_synthesis')
            if native_materials:
                client.add_user(session, {"native_source_materials": native_materials})
            if submitted is not None:
                # Reserved calls are correction capacity, not a requirement to
                # generate another report after an accepted explicit submission.
                if manager:
                    self.submitted_report_counts = request_report_counts
                return submitted
            if submission_error is not None and (submission_failures >= 2 or (remaining_turns is not None and remaining_turns <= 1) or not self._calls_available()):
                raise submission_error
        if reference_candidate is not None:
            if manager:
                self.submitted_report_counts = reference_report_counts
            return reference_candidate
        if submission_error is not None:
            raise submission_error
        raise FinalSubmissionMissing("Agent 达到本轮最大工具循环次数但未正式提交；阶段材料已保留")

    @staticmethod
    def _retain_submission_feedback(session, calls):
        """Retain one complete rejected protocol round across synthesis projection."""
        ids = {call.id for call in calls}
        session.submission_feedback_messages = [copy.deepcopy(message) for message in session.history
            if message.get('call_id', message.get('tool_call_id')) in ids
            or any(call.get('id') in ids for call in message.get('tool_calls', []))]

    def _visible_source_candidates(self, session, agent_id, *, archive_current=True):
        """Collect only this session's visible conversation results and arguments.

        Shared by ordinary compaction and child closeout. A tool_result archive
        may hold an unread tail and is never opened. During ordinary compaction
        the protocol groups have already been archived by context.compact.
        """
        messages = []
        for record in session.archive:
            if record.get('kind') != 'conversation':
                continue
            try:
                value = json.loads(record['payload'])
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict) and (visible := visible_message(value)):
                messages.append((record['record_id'], visible))
        for message in session.history[1:] if archive_current else []:
            if message.get('_context_checkpoint'):
                continue
            if visible := visible_message(message):
                record = archive(session, visible, 'conversation')
                messages.append((record['record_id'], visible))
        calls, arguments, candidates, seen = {}, {}, [], set()
        control_tools = {'report_progress', 'submit_report', 'agent_status', 'wait_agents',
                         'prepare_synthesis', 'finish_analysis'}
        index_tools = {'recall_context', 'list_materials', 'search_materials'}
        for record_id, message in messages:
            if message.get('type') == 'function_call':
                calls[message.get('call_id')] = message.get('name')
                try:
                    arguments[message.get('call_id')] = json.loads(message.get('arguments') or '{}')
                except (ValueError, TypeError):
                    pass
            for call in message.get('tool_calls') or []:
                calls[call.get('id')] = (call.get('function') or {}).get('name')
                try:
                    arguments[call.get('id')] = json.loads((call.get('function') or {}).get('arguments') or '{}')
                except (ValueError, TypeError):
                    pass
            is_output = message.get('type') == 'function_call_output' or message.get('role') == 'tool'
            text = message.get('output') if message.get('type') == 'function_call_output' else message.get('content')
            try:
                value = json.loads(text) if isinstance(text, str) else text
            except (ValueError, TypeError):
                value = text
            if (message.get('role') == 'user' and isinstance(value, dict)
                    and isinstance(value.get('recovered_completed_tool_outputs'), list)):
                # Recovery provides exact completed outputs as visible user
                # data. Preserve only that delivered wrapper, never expand the
                # separate restored_material archive or invent current owners.
                for recovered in value['recovered_completed_tool_outputs']:
                    if not isinstance(recovered, dict) or not isinstance(recovered.get('tool'), str) or 'result' not in recovered:
                        continue
                    item = copy.deepcopy(recovered)
                    item['record_id'] = record_id
                    item['recipient_agent_id'] = agent_id
                    signature = json.dumps(item | {'record_id': None},
                        ensure_ascii=False, sort_keys=True, default=str)
                    if signature not in seen:
                        seen.add(signature)
                        candidates.append((item['tool'] in index_tools, len(candidates), item))
                continue
            if is_output:
                call_id = message.get('call_id', message.get('tool_call_id'))
                tool = calls.get(call_id)
                if tool is None or tool in control_tools or tool in {'calculate', 'market_indicators'}:
                    continue
                if tool == 'query_market_rows' and calculation_projection(tool, value) is not None:
                    continue  # Exact geometric values are pinned with calculations.
            elif message.get('role') == 'user' and isinstance(value, dict) and 'native_source_materials' in value:
                tool = 'native_source_materials'
            else:
                continue
            item = {'tool': tool, 'result': value, 'record_id': record_id,
                    'visibility': 'previously_delivered_tool_output',
                    'source_task_id': session.task_id or self.task_id, 'source_agent_id': agent_id}
            if tool == 'save_material':
                args = arguments.get(call_id)
                authored = args.get('material') if isinstance(args, dict) else None
                if isinstance(authored, dict):
                    # Production save_material returns an index, not the whole
                    # extraction. Its own visible arguments are still authored
                    # evidence; no stored material or unread tail is expanded.
                    item['authored_material'] = copy.deepcopy(authored)
                    item['authored_material_classification'] = 'agent_extraction'
                    item['authored_material_note'] = '本子会话可见工具参数中的模型提取，不是独立核验的原始事实；保存结果以result为准。'
                    item['save_result_status'] = ('failed' if isinstance(value, dict) and
                        (value.get('failed') or value.get('error')) else
                        'saved' if isinstance(value, dict) and value.get('id') else 'unconfirmed')
            signature = json.dumps({key: item[key] for key in
                ('tool', 'result', 'authored_material') if key in item},
                ensure_ascii=False, sort_keys=True, default=str)
            if signature in seen:
                continue
            seen.add(signature)
            if any(self._report_message_key(pending.get('message', {})) == self._report_message_key(message)
                   for pending in getattr(session, '_pending_material_deliveries', [])):
                item['visibility'] = 'completed_tool_output_not_previously_delivered'
            candidates.append((tool in index_tools, len(candidates), item))
        return candidates

    def _retain_research_sources(self, seed, session, agent_id, *, candidates=None, fill_remaining=False):
        """Keep whole visible outputs inside the existing research envelope.

        First reserve at most one fifth for whole sources, then calculations
        and status are allocated. A second pass may use only space actually left
        in that same retained limit, keeping those allocated values unchanged.
        """
        if candidates is None:
            candidates = self._visible_source_candidates(session, agent_id, archive_current=False)
        for key in ('delivered_research_evidence', 'omitted_evidence_indexes', 'omitted_output_count',
                    'unlisted_omission_count', 'research_evidence_retention', 'retained_visible_source_contexts'):
            seed.pop(key, None)
        if not candidates:
            return
        size = lambda value: len(json.dumps(value, ensure_ascii=False, default=str))
        wire_size = lambda: size({'role': 'user', 'content': json.dumps(seed, ensure_ascii=False, default=str)})
        max_chars = int(self.config.get('max_context_chars', 240_000))
        before = wire_size()
        # Scale with the actual configured context. Sources never raise the
        # retained envelope or displace the reserved room for subsequent tools.
        limit = int(max_chars * .67) if fill_remaining else min(before + max_chars // 5, int(max_chars * .50))
        kept, native_sources, omitted = [], [], []
        seed.update(delivered_research_evidence=kept, retained_visible_source_contexts=native_sources, omitted_evidence_indexes=[],
            omitted_output_count=len(candidates), unlisted_omission_count=len(candidates),
            research_evidence_retention={
                'scope': 'this session visible tool outputs only',
                'instruction': '本 epoch 保留已交付原文/提取的完整可见结果；模型提取不是独立原文。省略项不等于从未读取或原文不存在，研究时可按本会话 record_id 取回。未读的归档尾部仍未知。'})
        discovery_tools = {'recall_context', 'list_materials', 'search_materials',
            'list_agent_roles', 'list_business_demands', 'list_forecast_targets', 'list_recommendations'}
        ordered = diverse_source_order([('tool', project_source_item(item)) for _, _, item in candidates], discovery_tools)
        for _, (_, item) in ordered:
            # Native excerpts keep the existing user-context wrapper; ordinary
            # compression relocates it without reclassifying it as tool output.
            target = native_sources if item['tool'] == 'native_source_materials' else kept
            target.append(item['result'] if target is native_sources else item)
            if wire_size() > limit:
                target.pop()
                omitted.append({'record_id': item['record_id'], 'tool': item['tool'],
                    'reason': 'whole_visible_output_exceeds_research_context_budget'})
        seed['omitted_output_count'] = len(omitted)
        for item in omitted:
            seed['omitted_evidence_indexes'].append(item)
            if wire_size() > limit:
                seed['omitted_evidence_indexes'].pop()
                break
        seed['unlisted_omission_count'] = len(omitted) - len(seed['omitted_evidence_indexes'])
        return candidates

    async def _enter_child_final_context(self, session, agent_id, *, reason):
        """Reproject this child's visible evidence without extending its budget.

        Conversation archives contain the bounded messages actually offered to
        the model. Hidden tool-result archives and other agents' evidence are
        deliberately not candidates. No checkpoint or parent context is read.
        """
        if getattr(session, '_child_final_projected', False):
            return
        if session.pending_calls:
            raise ValueError('子报告上下文必须在所有工具调用已返回后建立')
        size = lambda value: len(json.dumps(value, ensure_ascii=False, default=str))
        before = size(session.history)
        candidates = self._visible_source_candidates(session, agent_id)
        # Start from the child's already-visible seed, never the manager seed.
        seed = json.loads(session.history[0]['content'])
        # The same owned sources will be selected below. Do not charge the old
        # research package against calculations and then duplicate or replace it.
        for key in ('delivered_research_evidence', 'retained_visible_source_contexts',
                    'research_evidence_retention', 'omitted_evidence_indexes',
                    'omitted_output_count', 'unlisted_omission_count'):
            seed.pop(key, None)
        for key in ('runtime_status', 'calculation_summaries', 'calculation_indexes',
                    'calculation_access', 'calculation_contexts'):
            seed.pop(key, None)
        seed['child_final_transition'] = {'reason': reason,
            'instruction': '仅使用本次可见证据提交子报告。此前交付与本次保留不同；省略项不可在本次调用recall_context补取，保持明确缺口。模型提取仍是提取，不替代原文。'}
        session.history = [{'role': 'user', 'content': json.dumps(seed, ensure_ascii=False, default=str)}]
        session.begin_cache_epoch('synthesis')
        session._building_context_epoch = True
        try:
            self._pin_runtime_status(session, agent_id, None, True)
        finally:
            session._building_context_epoch = False
        fixed = copy.deepcopy(session.history)
        fixed.extend(visible for message in getattr(session, 'submission_feedback_messages', [])
                     if (visible := visible_message(message)))
        max_chars = int(self.config.get('max_context_chars', 240_000))
        # Keep whole returned values. Prefer actual source/extraction outputs to
        # discovery indexes, and recent results within each class. This chooses
        # neither a financial sign nor a preferred factual conclusion.
        kept, native_sources, omitted = [], [], []
        envelope = {'delivered_research_evidence': kept, 'retained_visible_source_contexts': native_sources,
            'omitted_evidence_indexes': [],
            'omitted_output_count': 0,
            'evidence_note': '仅保留本子会话实际可见工具输出；省略内容不等于原文不存在，未读取的归档尾部未展开。'}
        def projected():
            messages = copy.deepcopy(fixed)
            first = json.loads(messages[0]['content'])
            first.update(envelope)
            messages[0]['content'] = json.dumps(first, ensure_ascii=False, default=str)
            return messages
        limit = int(max_chars * .70) - 1200  # Leave room for the next status delta.
        for _, _, item in sorted(candidates, key=lambda entry: (entry[0], -entry[1])):
            target = native_sources if item['tool'] == 'native_source_materials' else kept
            target.append(item['result'] if target is native_sources else item)
            if size(projected()) > limit:
                target.pop()
                omitted.append({'record_id': item['record_id'], 'tool': item['tool'],
                                'reason': 'complete_visible_result_exceeds_remaining_context_budget'})
        envelope['omitted_output_count'] = len(omitted)
        for item in omitted:
            envelope['omitted_evidence_indexes'].append(item)
            if size(projected()) > limit:
                envelope['omitted_evidence_indexes'].pop()
                break
        envelope['unlisted_omission_count'] = len(omitted) - len(envelope['omitted_evidence_indexes'])
        session.history = projected()
        session.native_search = False
        session._child_final_projected = True
        await self._publish('child_final_context_projected', agent_id, {'reason': reason,
            'before_chars': before, 'after_chars': size(session.history),
            'visible_tool_outputs': len(candidates), 'included_outputs': len(kept) + len(native_sources),
            'omitted_outputs': len(omitted), 'archive_count': len(session.archive)})

    async def _enter_synthesis_context(self, session, *, reason):
        """Project delivered evidence at a resolved boundary; never replay audit data.

        Every old visible record remains in this session's archive with its
        original ID. Only delivered conversation/tool output is projected; raw
        oversized tool archives and opaque provider reasoning are not expanded.
        """
        if getattr(session, '_synthesis_projected', False):
            return
        if session.pending_calls:
            raise ValueError('汇总上下文必须在所有工具调用已返回后建立')
        size = lambda value: len(json.dumps(value, ensure_ascii=False, default=str))
        before = size(session.history)
        messages = []
        for record in session.archive:
            if record.get('kind') == 'conversation':
                try:
                    message = json.loads(record['payload'])
                except (ValueError, TypeError):
                    continue
                messages.append((record['record_id'], message))
        for message in session.history[1:]:
            if message.get('_context_checkpoint'):
                continue
            visible = visible_message(message)
            if visible:
                item = archive(session, visible, 'conversation')
                messages.append((item['record_id'], visible))
        calls, arguments, bindings = {}, {}, {}
        for record_id, message in messages:
            if message.get('type') == 'function_call':
                calls[message.get('call_id')] = message.get('name')
                try:
                    arguments[message.get('call_id')] = json.loads(message.get('arguments') or '{}')
                except (ValueError, TypeError):
                    pass
            if message.get('type') == 'function_call_output' or message.get('role') == 'tool':
                call_id = message.get('call_id', message.get('tool_call_id'))
                bindings[record_id] = (calls.get(call_id), copy.deepcopy(arguments.get(call_id, {})))
            for call in message.get('tool_calls') or []:
                calls[call.get('id')] = (call.get('function') or {}).get('name')
                try:
                    arguments[call.get('id')] = json.loads((call.get('function') or {}).get('arguments') or '{}')
                except (ValueError, TypeError):
                    pass
        # Equal completed results are one visible fact. Keep the newest record
        # and position, so rereading an original does not leave it behind every
        # later lookup. Stored archives/receipts and already-sent history stay intact.
        evidence, source_messages, extra_messages, seen = {}, [], [], set()
        for item in self.restored_material_evidence:
            signature = json.dumps({'tool': item['tool'], 'result': item['result']},
                                   sort_keys=True, ensure_ascii=False, default=str)
            evidence.pop(signature, None)
            evidence[signature] = copy.deepcopy(item); seen.add(signature)
        latest_budget = None
        for record_id, message in messages:
            text = message.get('output') if message.get('type') == 'function_call_output' else message.get('content')
            try:
                value = json.loads(text) if isinstance(text, str) else text
            except (ValueError, TypeError):
                value = text
            if message.get('type') == 'function_call_output' or message.get('role') == 'tool':
                call_id = message.get('call_id', message.get('tool_call_id'))
                tool, args = bindings.get(record_id, (None, {}))
                if tool in ('agent_status', 'wait_agents', 'report_progress', 'prepare_synthesis', 'finish_analysis', 'submit_report'):
                    continue  # Latest full child reports/status are pinned separately.
                if tool in {'market_indicators', 'calculate', 'query_market_rows'} and isinstance(value, dict) and not value.get('truncated_for_context'):
                    # Compatibility for already-delivered sessions/checkpoints
                    # from before explicit calculation retention was installed.
                    signature = calculation_signature(tool, args, value)
                    if not any(record['signature'] == signature for record in self.calculations):
                        self._remember_calculation('manager', tool, args, value)
                item = {'tool': tool, 'result': value, 'record_id': record_id,
                        'visibility': 'previously_delivered_tool_output'}
                if tool == 'save_material' and isinstance(args.get('material'), dict):
                    # The saved result is only an index. Its actual arguments
                    # are this manager's own authored extraction, not a newly
                    # fetched source or an expansion of hidden archived text.
                    item['authored_material'] = copy.deepcopy(args['material'])
                    item['authored_material_classification'] = 'agent_extraction'
                    item['authored_material_note'] = '本 Agent 自己提交的提取内容，不是独立原文；保存是否成功以 result 为准。'
                    item['save_result_status'] = ('failed' if isinstance(value, dict) and (value.get('error') or value.get('failed'))
                        else 'saved' if isinstance(value, dict) and value.get('id') else 'unconfirmed')
                if any(pending.get('proof', {}).get('tool') == tool
                       and pending.get('proof', {}).get('result') == value
                       for pending in getattr(session, '_pending_material_deliveries', []) if pending.get('proof')):
                    item['visibility'] = 'completed_tool_output_not_previously_delivered'
                signature = json.dumps({key: item[key] for key in
                    ('tool', 'result', 'authored_material') if key in item}, sort_keys=True, ensure_ascii=False, default=str)
                evidence.pop(signature, None)
                evidence[signature] = item; seen.add(signature)
            elif message.get('role') == 'user' and isinstance(value, dict):
                if 'native_source_materials' in value:
                    signature = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
                    if signature not in seen:
                        source_messages.append(value); seen.add(signature)
                elif 'late_completed_child_reports' in value:
                    extra_messages.append(value)
                elif 'resource_budget' in value:
                    latest_budget = value
                elif value.get('truncated_for_context') and value.get('record_id'):
                    # Only this wrapper was delivered. Never inject its hidden
                    # unabridged tool_result archive as if it had been read.
                    item = {'tool': 'visible_user_context', 'result': value, 'record_id': record_id,
                            'visibility': 'previously_delivered_user_context'}
                    signature = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
                    evidence.pop(signature, None)
                    evidence[signature] = item; seen.add(signature)
        if not self.synthesis_only:
            await self._save_checkpoint()
        seed = copy.deepcopy(self.context)
        max_chars = int(self.config.get('max_context_chars', 240_000))
        batch = seed.get('forecast_batch')
        if isinstance(batch, dict) and size(batch) > max_chars // 3:
            # Initial ModelClient.session already bounds oversized target pages.
            # Rebuilding synthesis must not silently re-expand that unread page
            # and evict completed arithmetic. The original remains retrievable;
            # this is a visible context projection, never a scope/sample change.
            record = archive(session, batch, 'forecast_target_page')
            seed['forecast_batch'] = {key: copy.deepcopy(batch[key]) for key in
                ('id', 'kind', 'base_date', 'deadline', 'input_version', 'rule_version',
                 'expected_count', 'has_more', 'offset', 'next_offset') if key in batch}
            seed['forecast_batch'].update(items=[], items_omitted_for_context=len(batch.get('items', [])),
                record_id=record['record_id'], total_chars=record['total_chars'], truncated_for_context=True,
                note='目标页已归档，预期范围未改，不代表已读。研究时按 record_id 取回或分页；仅能 finish_analysis 时，未读取或未研究目标保持未覆盖。')
        seed.pop('runtime_status', None)
        self._bound_previous_analysis(seed, session)
        if self.synthesis_checkpoint:
            seed['synthesis_checkpoint'] = _checkpoint_context(self.synthesis_checkpoint,
                index_budget=max(500, max_chars // 12))
            checkpoint = seed['synthesis_checkpoint']
            # Keep current metrics once when the saved copy is exactly the same
            # JSON value. Different historical values and the stored checkpoint
            # remain intact; reports and calculation evidence are not reduced.
            if ('metrics' in seed and 'metrics' in checkpoint and
                    json.dumps(seed['metrics'], sort_keys=True, default=str) ==
                    json.dumps(checkpoint['metrics'], sort_keys=True, default=str)):
                checkpoint.pop('metrics')
            if (size(seed) > max_chars // 3 and isinstance(seed.get('model_config'), dict)
                    and checkpoint.get('model_config') == seed['model_config']):
                checkpoint.pop('model_config')
                checkpoint['model_config_ref'] = 'seed.model_config'
        self._bound_checkpoint_reports(seed, session)
        seed['synthesis_transition'] = {'reason': reason, 'archive_count': len(session.archive),
            'instruction': '仅基于本次可见研究结果汇总。completed_tool_output_not_previously_delivered 表示本地工具已完成、现在首次交付，不是旧模型已读。保留未决问题和资料范围；索引及截断部分不等于新阅读。只提交一次最终报告，不再扩展研究。'}
        # Build the retained seed without ModelClient.session's one-third clip.
        # Include current status before allocating output space, so refreshing it
        # immediately before HTTP cannot consume an unaccounted-for third.
        session.history = [{'role': 'user', 'content': json.dumps(seed, ensure_ascii=False, default=str)}]
        remaining_turns = (latest_budget or {}).get('resource_budget', {}).get('remaining_agent_turns',
            self.config.get('max_agent_turns', 12))
        session.begin_cache_epoch('synthesis')
        session._building_context_epoch = True
        try:
            self._pin_runtime_status(session, 'manager', remaining_turns, reason != 'prepare_synthesis')
        finally:
            session._building_context_epoch = False
        seed_message = copy.deepcopy(session.history[0])
        fixed = [seed_message]
        fixed.extend(copy.deepcopy(getattr(session, 'submission_feedback_messages', [])))
        if latest_budget:
            fixed.append({'role': 'user', 'content': json.dumps(latest_budget, ensure_ascii=False, default=str)})
        # Source messages are actual delivered excerpts too. Fit them using the
        # same budget instead of letting many native sources bypass the bound.
        candidates = [('tool', item) for item in evidence.values()
            if item.get('tool') not in {'market_indicators', 'calculate'}
            and not (item.get('tool') == 'query_market_rows'
                and calculation_projection('query_market_rows', item.get('result')) is not None)]
        candidates += [('source', value) for value in source_messages]
        candidates += [('extra', value) for value in extra_messages]
        available = max(0, int(max_chars * .70) - size(fixed) - 400)
        compact_evidence, kept_sources, truncated = [], [], 0
        deferred = []
        discovery_tools = {'list_materials', 'search_materials', 'recall_context', 'visible_user_context',
                           'list_agent_roles', 'list_business_demands', 'list_forecast_targets', 'list_recommendations'}
        # Keep whole actual source/extraction results before discovery indexes;
        # prefer recent visible results within each group, not a chosen fact or
        # financial conclusion. Equal slicing of hundreds of indexes otherwise
        # discards every numerical table even when several whole results fit.
        candidates = [(kind, project_source_item(item) if kind == 'tool' else item)
            for kind, item in candidates]
        # One recent whole result per tool gets a chance before repeated results
        # from that tool. Exact shared row metadata saves space without choosing
        # a financial fact or opening any previously unread archive.
        ordered = diverse_source_order(candidates, discovery_tools, balance_repeated=True)
        for _, (kind, item) in ordered:
            cost = size(item)
            if cost <= available:
                if kind == 'tool':
                    compact_evidence.append(item)
                else:
                    kept_sources.append(item)
                available -= cost
                continue
            deferred.append((kind, item))
        each = available // max(1, len(deferred))
        for kind, item in deferred:
            payload = json.dumps(item.get('result') if kind == 'tool' else item, ensure_ascii=False, default=str)
            refs = references(item)[:6]
            while refs and size(refs) > each // 4:
                refs.pop()
            if each < 300:
                # An extreme number of results cannot all fit; the original
                # records and the explicit omission count remain available.
                truncated += 1
                continue
            compact_evidence.append({'tool': item.get('tool', 'visible_source_context'),
                'record_id': item.get('record_id'),
                'visible_excerpt': payload[:max(0, each - size(refs) - 300)], 'references': refs,
                'truncated_for_synthesis': True, 'total_visible_chars': len(payload),
                'note': '此前交付全文仍在本会话档案；该片段之外的内容不据此补造结论。'})
            truncated += 1
        def projected_messages():
            messages = copy.deepcopy(fixed)
            first = json.loads(messages[0]['content'])
            if compact_evidence or truncated:
                first.update({
                    'delivered_research_evidence': compact_evidence,
                    'bounded_or_omitted_outputs': truncated,
                    'evidence_note': '截断或省略内容保留在原档案；未列出的细节保持未知，不以索引充当原文。'})
                if truncated:
                    first['omitted_evidence'] = {'truncated_for_synthesis': True, 'count': truncated}
            if kept_sources:
                # Preserve the established native-source user-message shape.
                # These exact selected values are moved into the seed only if
                # a later rejected candidate requires generic compaction.
                messages.extend({'role': 'user', 'content': json.dumps(value, ensure_ascii=False, default=str)}
                    for value in kept_sources if 'native_source_materials' in value)
                late = [item for value in kept_sources for item in value.get('late_completed_child_reports', [])]
                if late:
                    first['late_completed_child_reports'] = late
            messages[0]['content'] = json.dumps(first, ensure_ascii=False, default=str)
            return messages
        session.history = projected_messages()
        # JSON escaping adds overhead. Enforce the final serialized threshold,
        # including native sources, before the generic compactor can retire it.
        while size(session.history) > int(max_chars * .74) and (compact_evidence or kept_sources):
            if compact_evidence:
                compact_evidence.pop()
            else:
                kept_sources.pop()
            truncated += 1
            session.history = projected_messages()
        session._final_native_source_contexts = copy.deepcopy([
            value for value in kept_sources if 'native_source_materials' in value])
        session.phase = 'synthesis'
        session.native_search = False
        session._synthesis_projected = True
        session._manager_final_projected = reason != 'prepare_synthesis'
        await self._publish('synthesis_context_projected', 'manager', {'reason': reason,
            'before_chars': before, 'after_chars': size(session.history), 'visible_tool_outputs': len(evidence),
            'truncated_outputs': truncated,
            'archive_count': len(session.archive)})

    async def _record_unincorporated_child_reports(self, result: dict, report_counts: dict) -> None:
        """Expose saved late evidence without buying another accepted report."""
        late = [{"agent_id": child.id, "title": child.title,
                 "reports": copy.deepcopy(child.reports[report_counts.get(child.id, 0):])}
                for child in self.children.values()
                if len(child.reports) > report_counts.get(child.id, 0)]
        if not late:
            return
        result["unincorporated_child_reports"] = [{"agent_id": item["agent_id"], "title": item["title"],
            "research_rounds": [report.get("research_round") for report in item["reports"]],
            "material_ids": sorted({mid for report in item["reports"] for mid in report.get("material_ids", []) if isinstance(mid, str)}),
            "reason": "本次最终请求期间交付；首次有效提交后结束，未为迟到成果追加模型请求"} for item in late]
        note = "补充交付状态：" + "、".join(item["title"] for item in late) + "在本次报告请求期间完成，正文尚未吸收这次结果；相关结论仍需复核。"
        result["uncertainties"] = [*(result.get("uncertainties") or []), note]
        result["report"] = str(result.get("report") or "") + "\n\n" + note
        await self._publish('late_child_reports_retained', 'manager', {
            'reports': copy.deepcopy(result['unincorporated_child_reports']),
            'additional_model_calls': 0, 'remaining_model_calls': self._remaining_calls()})

    async def _record_native(self, agent_id, turn):
        native_materials = []
        if turn.native_events or turn.sources or turn.annotations:
            if hasattr(self.tools, "record_native_search"):
                kwargs = {'model_config': self._client_for(agent_id).config} if hasattr(self.tools, 'store') else {}
                native_materials = await self.tools.record_native_search(agent_id, turn.native_events, turn.sources, turn.annotations, **kwargs)
                self.materials.extend(copy.deepcopy(native_materials or []))
                if hasattr(self.tools, 'store'):
                    for material in native_materials or []:
                        saved = self.tools.store.material(material['id'])
                        receipts = self.tools.store.record_material_delivery(self.task_id, self.account_id,
                            agent_id, 'native_search', {**material, 'visible_excerpt': saved.get('content', '')})
                        self.material_delivery_ids.setdefault(agent_id, []).extend(receipts)
            await self._publish("native_search", agent_id, {"events": turn.native_events, "sources": turn.sources,
                "annotations": turn.annotations, "materials": native_materials, "capability": turn.native_status})
        return native_materials

    @staticmethod
    def _report_message_key(message):
        return json.dumps({key: message.get(key) for key in
            ('role', 'type', 'call_id', 'tool_call_id', 'output', 'content')}, sort_keys=True, default=str)

    def _remember_report_results(self, results, messages):
        # Remember provenance of the actual bounded function outputs; a model
        # echo via save_material is not an agent_status/wait_agents delivery.
        for (call, _), message in zip(results, messages):
            if call.name in {'agent_status', 'wait_agents', 'cancel_agent'}:
                self.report_tool_messages[self._report_message_key(message)] = call.name

    def _record_visible_report_deliveries(self, *, record=True):
        """Record report content only through Runtime-owned delivery channels.

        Tool archives, model arguments, authored material echoes and child
        completion alone are not receipts. Truncated JSON is not parsed as a
        full report. The hook runs after final request-context compaction.
        """
        self.request_report_visibility = []
        if not hasattr(self.tools, 'store'):
            return []
        reports = {report['report_id']: (report, self.task_id)
            for child in self.children.values() for report in child.reports if report.get('report_id')}
        # Model context omits internal receipt inventories. Receipt lineage and
        # identity must use the immutable raw checkpoint, never that projection.
        checkpoint = self.restored_checkpoint or {}
        reports.update({report['report_id']: (report, report.get('checkpoint_source_job_id') or checkpoint.get('checkpoint_job_id') or checkpoint.get('task_id'))
            for report in checkpoint.get('reports', []) if report.get('report_id')})
        receipts = []
        channel = ('restored_research' if checkpoint.get('recovery_mode') == 'continue_research' else 'restored_synthesis') if checkpoint else 'manager_context'
        def report(value):
            if not isinstance(value, dict) or value.get('truncated_for_context') or value.get('truncated_for_synthesis'):
                return
            reference = reports.get(value.get('report_id'))
            if not reference or not isinstance(value.get('summary'), str):
                return
            original, source_job_id = reference
            full = not value.get('summary_truncated') and not original.get('summary_truncated') and all(
                value.get(key) == original.get(key) for key in
                ('summary', 'findings', 'material_ids', 'open_questions', 'material_updates') if key in original)
            visibility = {'report_id': original['report_id'], 'source_task_id': source_job_id,
                          'access': 'full_report' if full else 'report_summary'}
            if visibility not in self.request_report_visibility:
                self.request_report_visibility.append(visibility)
            if not record:
                return
            signature = json.dumps([source_job_id, original['report_id'], value], sort_keys=True, default=str)
            if signature in self.report_delivery_seen:
                return
            receipt = self.tools.store.record_report_delivery(self.task_id, self.account_id,
                original, value, channel=channel, source_job_id=source_job_id)
            if receipt:
                self.report_delivery_seen.add(signature)
                receipts.append(receipt)
        def snapshots(items):
            for item in items if isinstance(items, list) else []:
                if isinstance(item, dict):
                    for value in item.get('reports') or []:
                        report(value)
        def events(items):
            for item in items if isinstance(items, list) else []:
                if isinstance(item, dict) and item.get('type') == 'agent_completed':
                    report((item.get('payload') or {}).get('report'))
        def tool_result(tool, value):
            if isinstance(value, dict) and (value.get('truncated_for_context') or value.get('truncated_for_synthesis')):
                return
            if tool == 'agent_status':
                snapshots(value)
            elif tool == 'cancel_agent':
                snapshots([value])
            elif tool == 'wait_agents' and isinstance(value, dict):
                snapshots(value.get('agents'))
                events(value.get('events'))
        for message in self.manager_session.history:
            if message.get('_context_checkpoint'):
                continue
            try:
                value = json.loads(message.get('output', message.get('content', '{}')))
            except (TypeError, ValueError):
                continue
            if message.get('role') == 'tool' or message.get('type') == 'function_call_output':
                tool_result(self.report_tool_messages.get(self._report_message_key(message)), value)
                continue
            if message.get('role') != 'user' or not isinstance(value, dict) or value.get('truncated_for_context'):
                continue
            # These wrappers are created by Runtime, not by model-authored tool
            # data. Do not recursively interpret arbitrary findings as reports.
            for item in value.get('retained_child_reports', []) if isinstance(value.get('retained_child_reports'), list) else []:
                report(item)
            saved = value.get('synthesis_checkpoint') or {}
            for item in saved.get('reports') or []:
                report(item)
            for child in (value.get('runtime_status') or {}).get('children') or []:
                report(child.get('latest_report'))
            events(value.get('research_events'))
            snapshots(value.get('late_completed_child_reports'))
        return sorted(set(receipts))

    async def _save_checkpoint(self, declaration: dict | None = None, *, interruption=None) -> dict:
        """Persist only visible, finished research; service enforces freshness/CAS."""
        if declaration is None:
            declaration = copy.deepcopy((self.synthesis_checkpoint or {}).get("manager_summary") or {})
        else:
            declaration = copy.deepcopy(declaration)
            declaration.pop('material_delivery_ids', None)
            declaration.pop('report_delivery_ids', None)
            if self.material_delivery_ids.get('manager'):
                declaration['material_delivery_ids'] = list(self.material_delivery_ids['manager'])
        restored = self.restored_checkpoint or {}
        reports = [{**copy.deepcopy(report), 'checkpoint_source_job_id': report.get('checkpoint_source_job_id') or
            restored.get('checkpoint_job_id') or restored.get('task_id')} for report in restored.get('reports', [])]
        for child in self.children.values():
            for report in child.reports:
                visible = {key: copy.deepcopy(report[key]) for key in ("report_id", "summary", "findings", "material_ids", "open_questions", "research_round", "material_delivery_ids", "material_updates", "calculation_ids") if key in report}
                if isinstance(visible.get("summary"), str) and len(visible["summary"]) > 12000:
                    visible["summary"] = visible["summary"][:12000]
                    visible["summary_truncated"] = True
                reports.append({"agent_id": child.id, "role_id": child.role_id, "model_config": copy.deepcopy(child.model_config), "title": child.title, **visible})
        available_calculations = self._available_calculations('manager')
        material_receipts = self._manager_material_receipts()
        pending_outputs = [copy.deepcopy(item['proof']) for item in
            getattr(self.manager_session, '_pending_material_deliveries', []) if item.get('proof')]
        if not reports and not declaration and not available_calculations and not material_receipts and not pending_outputs:
            return {}
        unresolved_tasks = [*copy.deepcopy(restored.get('unresolved_tasks', [])), *[{"agent_id": child.id, "title": child.title, "task": child.task,
            "status": child.status, "round": child.round, "error": child.error,
            "pending_messages": list(dict.fromkeys([*child.pending_messages, *child.inbox]))}
            for child in self.children.values()
            if child.status != "completed" or child.pending_messages or child.inbox]]
        material_index = [*copy.deepcopy(restored.get('materials', [])), *[{key: copy.deepcopy(material[key]) for key in ("id", "url", "source_title", "classification", "access", "fetched_at") if key in material} for material in self.materials]]
        calculation_material_ids = {entry.get('material_id') for record in available_calculations
            for entry in (record['summary'], record['summary'].get('benchmark') or {})
            if isinstance(entry.get('material_id'), str)}
        checkpoint = {"phase": "synthesis", "task_id": self.task_id, "account_id": self.account_id,
            "model_config": copy.deepcopy(self.model_config), "team_snapshot": copy.deepcopy(self.team),
            "input_version": self.input_version, "scope": self.context.get("scope", "account"),
            "data_as_of": self.context.get("data_as_of"), "reports": reports,
            "materials": material_index, "material_ids": sorted({m["id"] for m in material_index if m.get("id")} |
                set(restored.get('material_ids', [])) |
                calculation_material_ids |
                {mid for report in reports for mid in report.get("material_ids", []) if isinstance(mid, str)} |
                {mid for mid in (declaration or {}).get("material_ids", []) if isinstance(mid, str)}),
            "calculations": copy.deepcopy(available_calculations), "metrics": copy.deepcopy(self.context.get("metrics")),
            "open_questions": [*(declaration.get("open_questions") or []),
                *[question for report in reports for question in report.get("open_questions", [])]],
            "unresolved_tasks": unresolved_tasks,
            "unresolved_tasks_note": "旧子报告不代表后续补查已完成；最终汇总须保留这些未决任务及其影响。" if unresolved_tasks else None,
            "manager_summary": copy.deepcopy(declaration or {}), "model_calls_used": self.calls}
        checkpoint['pending_tool_outputs'] = pending_outputs
        if restored:
            checkpoint['restored_material_deliveries'] = copy.deepcopy(restored.get('material_deliveries', []))
        if hasattr(self.tools, 'store'):
            deliveries = [*checkpoint.get('restored_material_deliveries', []), *material_receipts]
            checkpoint = self.tools.store.include_manager_materials(checkpoint, deliveries, self.account_id)
            checkpoint['material_deliveries'] = copy.deepcopy(deliveries)
            checkpoint['report_deliveries'] = self.tools.store.checkpoint_report_deliveries(self.task_id)
        if interruption:
            checkpoint.update(partial_research=True, interruption=copy.deepcopy(interruption))
            checkpoint['open_questions'].append('原研究中途失败，未完成专题和未核实范围仍未知；本次恢复应交付有限分析。')
        signature = json.dumps(checkpoint, sort_keys=True, default=str)
        if signature != self.checkpoint_signature:
            self.checkpoint_signature = signature
            checkpoint["created_at"] = now_datetime(SHANGHAI).isoformat()
            self.synthesis_checkpoint = checkpoint
            if hasattr(self.tools, "save_checkpoint"):
                await self.tools.save_checkpoint(copy.deepcopy(checkpoint))
            await self._publish("synthesis_checkpoint", "manager", {"phase": "synthesis", "reports": len(reports),
                "material_ids": checkpoint["material_ids"], "calculations": len(self.calculations)})
        return checkpoint

    async def _save_interrupted_checkpoint(self, stage):
        """Save only completed tool/child work, never an unfinished SSE response."""
        if self.synthesis_only or self.manager_session.pending_calls:
            return
        calculations = self._available_calculations('manager')
        reports = [*(self.restored_checkpoint or {}).get('reports', []),
                   *[report for child in self.children.values() for report in child.reports]]
        material_receipts = self._manager_material_receipts()
        pending_outputs = [item['proof'] for item in getattr(self.manager_session, '_pending_material_deliveries', [])
                           if item.get('proof')]
        if not reports and not any(c.get('summary', {}).get('status') == 'ok' for c in calculations) and not self.restored_checkpoint and not material_receipts and not pending_outputs:
            return
        previous = self.synthesis_checkpoint or {}
        old_ids = {c.get('calculation_id') for c in previous.get('calculations', [])}
        new_ids = {c.get('calculation_id') for c in calculations}
        old_reports = {r.get('report_id') for r in previous.get('reports', [])}
        new_reports = {r.get('report_id') for r in reports}
        old_materials = {item['id'] for item in manager_material_deliveries(previous.get('material_deliveries', []))}
        new_materials = {item['id'] for item in [*material_receipts,
            *manager_material_deliveries((self.restored_checkpoint or {}).get('material_deliveries', []))]}
        old_pending = {item.get('visible_result_hash') for item in previous.get('pending_tool_outputs', [])}
        new_pending = {item.get('visible_result_hash') for item in pending_outputs}
        if previous.get('task_id') == self.task_id and (not old_ids <= new_ids or not old_reports <= new_reports or
                not old_materials <= new_materials or
                (old_ids == new_ids and old_reports == new_reports and old_materials == new_materials and old_pending == new_pending)):
            return  # Preserve a more complete or already-ready final checkpoint.
        interruption = {'stage': stage, 'completed_calculations': len(calculations), 'completed_child_reports': len(reports),
            'note': '研究在完整交付前中断；仅完成的工具结果/子报告可复用。未终止SSE、未完成专题、未读取材料不计作成果。'}
        try:
            saved = await self._save_checkpoint(interruption=interruption)
            if saved:
                await self._publish('partial_research_saved', 'manager', interruption)
        except Exception as exc:
            # Do not replace the original model/timeout error with a secondary
            # persistence error or incorrectly claim that a checkpoint exists.
            await self._publish('checkpoint_save_failed', 'manager', {'error_type': type(exc).__name__})

    def _preview(self, value: Any) -> Any:
        serialized = json.dumps(value, ensure_ascii=False, default=str)
        if len(serialized) <= 16000:
            return value
        return {"truncated_for_event": True, "preview": serialized[:16000],
                "note": "完整材料通过来源 URL、材料 ID 或当前会话 recall_context 按需取回。"}

    def _deliver_inbox(self, child: AgentState) -> bool:
        if not child.inbox:
            return False
        (child.client or self.client).add_user(child.session, {"manager_messages": list(child.inbox)})
        child.inbox.clear()
        return True

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
        if name in {tool['name'] for tool in MANAGER_TOOLS} and agent_id != 'manager':
            raise ValueError('只有主 Agent 可使用调度和正式发布工具')
        if name == 'list_agent_roles':
            return {'team_id': (self.team or {}).get('team_id'), 'roles': self._role_index()}
        if name in {'list_business_demands', 'list_forecast_targets'}:
            return await getattr(self.tools, name)(limit=min(100, max(1, int(args.get('limit', 30)))), offset=max(0, int(args.get('offset', 0))))
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
                        "note": "轻量索引；使用 get_material/read_material_range 获取正文，行情材料可用 query_market_rows 按日期取行。"}
            return result
        if name in {"query_market_rows", "market_indicators"}:
            output = await getattr(self.tools, name)(**args)
            # Ordinary row reads stay material evidence. Only requested candle
            # arithmetic has a calculation projection to retain across epochs.
            self._remember_calculation(agent_id, name, args, output)
            return output
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
            self._remember_calculation(agent_id, name, args, output)
            return output
        if name == "prepare_synthesis":
            if any(child.status in {"queued", "running"} for child in self.children.values()):
                raise ValueError("仍有运行中的子任务；请先等待或明确取消后再汇总")
            self._text(args, "summary")
            await self._save_checkpoint(args)
            self.synthesis_only = True
            self.synthesis_started_calls = self.calls
            # Build the prompt only after this tool call has been resolved.
            # This function's result is resolved by the ordinary tool loop first;
            # compacting the old session here would orphan its pending call.
            self.manager_session.phase = "synthesis"
            self.manager_session.native_search = False
            self._fresh_synthesis_context = True
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
            return [{**child.snapshot(), 'delivery_note': '这是子实例的专题成果，不是主 Agent 亲读原文。可在预算内 follow_up 缩小问题或追问，也可明确不采用；已完成仅表示提交结束。'} for child in selected]
        if name == "wait_agents":
            return await self._wait(args)
        if name == "cancel_agent":
            child = self._child(self._text(args, "agent_id"))
            if child.task_handle and not child.task_handle.done():
                child.task_handle.cancel('manager_cancelled_child')
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
                if hasattr(self.tools, 'preflight_delivery'):
                    args = await self.tools.preflight_delivery(args)
                if 'covered_demand_ids' in args and (not isinstance(args['covered_demand_ids'], list) or
                        any(type(value) is not int or value < 1 for value in args['covered_demand_ids'])):
                    raise ValueError('covered_demand_ids 需要分页需求中已实际处理的正整数 ID 数组')
                for key in ("suggestions", "research_records", "forecasts"):
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
            return {"accepted": True, "resolved_result": args}
        raise ValueError("未知工具")

    async def _spawn(self, args: dict) -> dict:
        if self.closed:
            raise ValueError("分析任务已经关闭")
        if self._shared_closeout():
            raise ValueError("共享预算已进入收尾；不再创建子任务，汇总已有证据并说明缺口")
        if len(self.children) >= int(self.config.get("max_total_agents", 16)):
            raise ValueError("已达到本轮子 Agent 数量上限；可继续已有子任务或直接分析")
        if any(key not in {'role_id', 'task', 'title', 'context'} for key in args):
            raise ValueError('主 Agent 只能选择 role_id 和任务，不能改写模型、供应商或思考强度')
        role_id = self._text(args, 'role_id') if self.team else args.get('role_id', 'researcher')
        role = (self.team or {}).get('roles', {}).get(role_id)
        if self.team and (not role or not role.get('enabled') or role_id == 'manager'):
            raise ValueError('只能派发已注册且全局启用的子角色，不能递归派发 manager')
        task = self._text(args, "task")
        child_id = "research-" + uuid.uuid4().hex[:12]
        context = {"task": task, "parent_task_id": self.task_id,
                   "account": copy.deepcopy(self.context.get("account")),
                   "input_version": self.input_version, "metrics": copy.deepcopy(self.context.get("metrics")),
                   "scope": self.context.get("scope", "account"),
                   "data_as_of": self.context.get("data_as_of"), "task_context": copy.deepcopy(args.get("context", {}))}
        cfg = self._role_config(role_id) if self.team else self.config
        client = ModelClient(cfg, before_request=self._use_budget) if self.team else self.client
        from .config import model_config_summary
        context['role_id'] = role_id
        context['model_config'] = model_config_summary(cfg)
        session = client.session(role['system_prompt'] if role else (PROMPTS / 'researcher.txt').read_text(), context)
        session.agent_id = child_id
        session.on_request_accepted = lambda observation: self._accepted_context(session, child_id, observation)
        child = AgentState(child_id, args.get("title") or task[:60], task, session,
                           role_id=role_id, model_config=model_config_summary(cfg), client=client)
        self.children[child_id] = child
        await self._publish("agent_created", child_id, {"title": child.title, "task": task, "role_id": role_id, "model_config": child.model_config, "status": "queued"})
        self._launch(child)
        return {"agent_id": child_id, "role_id": role_id, "status": child.status}

    def _launch(self, child: AgentState) -> None:
        task = asyncio.create_task(self._run_child(child), name=f"{self.task_id}:{child.id}:{child.round}")
        child.task_handle = task
        self.handles.add(task)
        task.add_done_callback(self.handles.discard)

    async def _run_child(self, child: AgentState) -> None:
        queued_at = time.monotonic()
        child_deadline = queued_at + float(self.config.get('agent_timeout_seconds', 600))
        if self.deadline is not None:
            child_deadline = min(child_deadline, self.deadline - self._synthesis_reserve())
        # A follow-up starts a new round; queue time consumes this same budget.
        # Parent clipping reduces the actual round total before computing its
        # reserve. Neither this reserve nor final submission extends a deadline.
        child.deadline_closing = False
        child.session.queue_wait_ms = 0.0
        child.round_time_budget = max(0.0, child_deadline - queued_at)
        child_policy = timeout_policy((child.client or self.client).config)
        child.submission_reserve_seconds = min(child_policy['request_hard_seconds'] * 2,
                                               child.round_time_budget * .40)
        child.session.deadlines = {'child_budget_exhausted': child_deadline}
        if self.deadline is not None:
            child.session.deadlines[self.deadline_code] = self.deadline
        try:
            async with asyncio.timeout_at(child_deadline), self.semaphore:
                child.session.queue_wait_ms = round((time.monotonic() - queued_at) * 1000, 2)
                child.status = "running"
                await self._publish("agent_started", child.id, {"title": child.title, "round": child.round,
                    'model_config': child.model_config, 'resource_limits': {
                        'request_timeout_seconds': child_policy['request_hard_seconds'],
                        'timeout_policy': child_policy,
                        'queue_wait_ms': child.session.queue_wait_ms,
                        'remaining_child_seconds': max(0, child_deadline - time.monotonic()),
                        'round_time_budget_seconds': child.round_time_budget,
                        'submission_reserve_seconds': child.submission_reserve_seconds,
                        'child_timeout_seconds': self.config.get('agent_timeout_seconds', 600)}})
                while True:
                    report = await self._loop(child.id, child.session, manager=False)
                    # Runtime identity cannot be supplied by the model.
                    report.update(report_id=uuid.uuid4().hex, agent_id=child.id,
                        role_id=child.role_id, model_config=copy.deepcopy(child.model_config),
                        research_round=child.round,
                        calculation_ids=[record['calculation_id'] for record in self.calculations
                            if record['agent_id'] == child.id and record['research_round'] == child.round])
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
            (child.client or self.client).resolve_pending(child.session, child.error)
            await self._publish("agent_failed", child.id, {"error": child.error, 'failure_stage': 'child_deadline',
                'error_code': 'child_budget_exhausted', 'cancelled_by': 'child_budget_exhausted',
                'queue_wait_ms': child.session.queue_wait_ms or round((time.monotonic() - queued_at) * 1000, 2),
                'timeout_seconds': self.config.get('agent_timeout_seconds', 600), 'saved_report_count': len(child.reports)})
        except asyncio.CancelledError as exc:
            child.status = "cancelled"
            (child.client or self.client).resolve_pending(child.session, "子任务已取消；此工具调用未完成")
            expired = [(key, value) for key, value in child.session.deadlines.items() if value <= time.monotonic()]
            source = min(expired, key=lambda item: item[1])[0] if expired else cancellation_source(exc)
            await self._publish("agent_cancelled", child.id, {"reason": "父任务结束、取消或账户版本更新",
                'error_code': source if expired else 'cancelled', 'cancelled_by': source})
            raise
        except Exception as exc:
            child.status = "failed"
            child.error = self._safe_error(exc)
            (child.client or self.client).resolve_pending(child.session, child.error)
            await self._publish("agent_failed", child.id, {"error": child.error, 'error_code': getattr(exc, 'code', None)})

    def _waiting_on_live_target(self, call, output) -> bool:
        if call.name not in {'wait_agents', 'agent_status'}:
            return False
        if isinstance(output, dict) and (output.get('failed') or output.get('error')):
            return False
        if call.name == 'wait_agents':
            ids = call.arguments.get('agent_ids') or list(self.children)
        else:
            target = call.arguments.get('agent_id')
            ids = [target] if target else list(self.children)
        return any(child is not None and child.task_handle and not child.task_handle.done()
                   for child in (self.children.get(identity) for identity in ids))

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
