// Isolated browser visual & interaction verification for the 18th UI upgrade.
// Serves real app/static/* assets against deterministic HTTP fixtures (0 model calls).
import { createRequire } from "node:module";
import { createServer } from "node:http";
import { readFile, mkdir, writeFile, stat } from "node:fs/promises";
import assert from "node:assert/strict";
import { selectChoice } from "./choice_helper.mjs";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const ACC_MAIN = "a".repeat(32);
const ACC_EMPTY = "b".repeat(32);
const REP_CURRENT = "c".repeat(32);
const REP_EXPIRED = "d".repeat(32);
const REP_LEGACY_HYDRATED = "5".repeat(32);
const JOB_DONE = "e".repeat(32);
const JOB_FAILED = "f".repeat(32);
const JOB_RUNNING = "1".repeat(32);
const JOB_LEGACY = "4".repeat(32);
const MAT_1 = "2".repeat(32);
const PROV_1 = "3".repeat(32);

const usageRequests = [
  {
    local_request_id: "req-mgr-1",
    session_id: "sess-mgr",
    agent_id: "manager",
    cache_epoch: 0,
    stage: "research",
    request_category: "first_session_request",
    native_search_requested: false,
    duration_ms: 1420,
    model: "fixture/Model-A",
    reasoning_effort_sent: "high",
    reported_reasoning_effort: "high",
    prefix_diagnostic: { change_reason: "first" },
    normalized_usage: {
      input_tokens: 8000,
      cache_read_tokens: 0,
      uncached_input_tokens: 8000,
      output_tokens: 220,
      reasoning_tokens: 120,
      visible_output_tokens: 100,
      cache_write_tokens: 0,
    },
    cost_known: false,
  },
  {
    local_request_id: "req-mgr-2",
    session_id: "sess-mgr",
    agent_id: "manager",
    cache_epoch: 0,
    stage: "research",
    request_category: "continuation",
    native_search_requested: false,
    duration_ms: 980,
    model: "fixture/Model-A",
    reasoning_effort_sent: "high",
    reported_reasoning_effort: "high",
    prefix_diagnostic: { change_reason: "append_only" },
    normalized_usage: {
      input_tokens: 10000,
      cache_read_tokens: 9000,
      uncached_input_tokens: 1000,
      output_tokens: 180,
      reasoning_tokens: 80,
      visible_output_tokens: 100,
      cache_write_tokens: 0,
    },
    cost_known: false,
  },
  {
    local_request_id: "req-mgr-3",
    session_id: "sess-mgr",
    agent_id: "manager",
    cache_epoch: 1,
    stage: "research",
    request_category: "first_epoch_request",
    native_search_requested: false,
    duration_ms: 1120,
    model: "fixture/Model-A",
    reasoning_effort_sent: "high",
    reported_reasoning_effort: "high",
    prefix_diagnostic: {
      change_reason: "tool_choice_changed",
      first_changed_path: "tool_choice",
    },
    normalized_usage: {
      input_tokens: 11000,
      cache_read_tokens: 2200,
      uncached_input_tokens: 8800,
      output_tokens: 210,
      reasoning_tokens: 110,
      visible_output_tokens: 100,
      cache_write_tokens: 0,
    },
    cost_known: false,
  },
  {
    local_request_id: "req-child-1",
    session_id: "sess-child",
    agent_id: "filing_researcher",
    cache_epoch: 0,
    stage: "research",
    request_category: "first_session_request",
    native_search_requested: true,
    duration_ms: 1650,
    model: "fixture/Model-A",
    reasoning_effort_sent: "medium",
    reported_reasoning_effort: "medium",
    prefix_diagnostic: { change_reason: "first" },
    normalized_usage: {
      input_tokens: 6000,
      cache_read_tokens: 1500,
      uncached_input_tokens: 4500,
      output_tokens: 260,
      reasoning_tokens: null,
      visible_output_tokens: 260,
      cache_write_tokens: null,
    },
    cost_known: false,
  },
  {
    local_request_id: "req-child-2",
    session_id: "sess-child",
    agent_id: "filing_researcher",
    cache_epoch: 1,
    stage: "research",
    request_category: "continuation",
    native_search_requested: false,
    duration_ms: 1340,
    model: "fixture/Model-A",
    reasoning_effort_sent: "medium",
    reported_reasoning_effort: "medium",
    prefix_diagnostic: {
      change_reason: "history_rewritten",
      first_changed_path: "input[2].content",
    },
    normalized_usage: {
      input_tokens: 7000,
      cache_read_tokens: 1400,
      uncached_input_tokens: 5600,
      output_tokens: 190,
      reasoning_tokens: null,
      visible_output_tokens: 190,
      cache_write_tokens: null,
    },
    cost_known: false,
  },
  {
    local_request_id: "req-mgr-4",
    session_id: "sess-mgr",
    agent_id: "manager",
    cache_epoch: 2,
    stage: "synthesis",
    request_category: "failed",
    native_search_requested: false,
    duration_ms: 410,
    model: "fixture/Model-A",
    reasoning_effort_sent: "high",
    reported_reasoning_effort: null,
    error: "upstream_usage_omitted",
    prefix_diagnostic: {
      change_reason: "compression",
      first_changed_path: "input[1]",
    },
    normalized_usage: {
      input_tokens: 9000,
      cache_read_tokens: null,
      uncached_input_tokens: null,
      output_tokens: null,
      reasoning_tokens: null,
      visible_output_tokens: null,
      cache_write_tokens: null,
    },
    cost_known: false,
  },
  {
    local_request_id: "req-mgr-5",
    session_id: "sess-mgr",
    agent_id: "manager",
    cache_epoch: 3,
    cache_epoch_reason: "first",
    stage: "research",
    request_category: "first_epoch_request",
    native_search_requested: false,
    duration_ms: 880,
    model: "fixture/Model-A",
    reasoning_effort_sent: "high",
    reported_reasoning_effort: "high",
    prefix_diagnostic: { change_reason: "append_only" },
    normalized_usage: {
      input_tokens: 5000,
      cache_read_tokens: 0,
      uncached_input_tokens: 5000,
      output_tokens: 100,
      reasoning_tokens: 50,
      visible_output_tokens: 50,
      cache_write_tokens: 0,
    },
    cost_known: false,
  },
];

// Same shape produced by app/model_usage.py usage_summary (complete paired = 6 of 7).
const modelUsageSummary = {
  duration_ms: 7800,
  cache_summary: {
    requests: 7,
    complete_requests: 6,
    unknown_requests: 1,
    coverage: 6 / 7,
    reported_input_tokens: 47000,
    cache_read_tokens: 14100,
    uncached_input_tokens: 32900,
    weighted_read_ratio: 14100 / 47000,
    anomalous_requests: 0,
  },
  normalized_totals: {
    input_tokens: 56000,
    cache_read_tokens: 14100,
    uncached_input_tokens: 32900,
    output_tokens: 1160,
    reasoning_tokens: 360,
    visible_output_tokens: 800,
    cache_write_tokens: 0,
    known_requests: {
      input_tokens: 7,
      cache_read_tokens: 6,
      uncached_input_tokens: 6,
      output_tokens: 6,
      reasoning_tokens: 4,
      visible_output_tokens: 6,
      cache_write_tokens: 4,
    },
    coverage: {
      input_tokens: 1,
      cache_read_tokens: 6 / 7,
      uncached_input_tokens: 6 / 7,
      output_tokens: 6 / 7,
      reasoning_tokens: 4 / 7,
      visible_output_tokens: 6 / 7,
      cache_write_tokens: 4 / 7,
    },
  },
  sequence: usageRequests,
};

// Lightweight list-level summary produced by real GET /api/accounts/:id/jobs?events=false
// (NO result field, NO sequence, carries prefix_reason_counts and native_search_breakdown).
// Notice req-mgr-5 has change_reason="append_only" + cache_epoch_reason="first":
// backend compact_usage_summary excludes it from non_search_cross_turn while keeping it in non_search_all.
const lightweightModelUsageSummary = {
  requests: 7,
  duration_ms: 7800,
  failed_requests: 1,
  cache_summary: modelUsageSummary.cache_summary,
  normalized_totals: modelUsageSummary.normalized_totals,
  prefix_reason_counts: {
    first: 2,
    append_only: 2,
    tool_choice_changed: 1,
    history_rewritten: 1,
    compression: 1,
  },
  native_search_breakdown: {
    non_search_all: {
      count: 6,
      paired: 5,
      reported_input_tokens: 41000,
      cache_read_tokens: 12600,
      weighted_read_ratio: 12600 / 41000,
    },
    non_search_cross_turn: {
      count: 4,
      paired: 3,
      reported_input_tokens: 28000,
      cache_read_tokens: 12600,
      weighted_read_ratio: 12600 / 28000,
    },
    search_all: {
      count: 1,
      paired: 1,
      reported_input_tokens: 6000,
      cache_read_tokens: 1500,
      weighted_read_ratio: 1500 / 6000,
    },
  },
};

const legacyUsageRequestsWithoutSearchFlag = usageRequests.map(
  ({ native_search_requested: _omitted, ...rest }) => rest,
);

const legacyModelUsageSummaryWithoutSearchFlag = {
  ...modelUsageSummary,
  sequence: legacyUsageRequestsWithoutSearchFlag,
};

const accounts = [
  {
    id: ACC_MAIN,
    name: "稳健核心观察账户",
    version: 5,
    cash: "184500.50",
    latest_job_id: JOB_FAILED,
    analysis_state: "current",
    holdings: [
      { symbol: "SH:600519", name: "贵州茅台", quantity: "100", cost_price: "1520.00", available_quantity: "100" },
      { symbol: "SZ:159599", name: "沪深300ETF", quantity: "12000", cost_price: "1.42", available_quantity: "12000" },
    ],
    preferences: { risk: "balanced", horizon: "6-18个月", max_position_pct: 35, max_sector_pct: 50 },
    metrics: {
      securities_value: "173840.00",
      cash: "184500.50",
      total_assets: "358340.50",
      unrealized_pnl: "4760.00",
      missing_prices: [],
      positions: [
        {
          symbol: "SH:600519",
          name: "贵州茅台",
          kind: "stock",
          quantity: "100",
          price: "1562.00",
          market_value: "156200.00",
          weight_pct: "89.85",
          cost_price: "1520.00",
          unrealized_pnl: "4200.00",
          quote: { as_of: "2026-11-25T15:00:00+08:00", source: "eastmoney", status: "fresh" },
        },
        {
          symbol: "SZ:159599",
          name: "沪深300ETF",
          kind: "etf",
          unit: "份",
          quantity: "12000",
          price: "1.47",
          market_value: "17640.00",
          weight_pct: "10.15",
          cost_price: "1.42",
          unrealized_pnl: "560.00",
          quote: { as_of: "2026-11-25T15:00:00+08:00", source: "eastmoney", status: "fresh" },
        },
      ],
      sector_weights: [
        { sector: "食品饮料", weight_pct: 89.85 },
        { sector: "宽基指数 ETF", weight_pct: 10.15 },
      ],
      notes: ["所有证券已完成最近交易日收盘对价；现金未参与内部证券权重分母。"],
    },
  },
  {
    id: ACC_EMPTY,
    name: "空置观察账户",
    version: 1,
    cash: null,
    latest_job_id: null,
    analysis_state: "idle",
    holdings: [],
    preferences: { risk: "conservative", horizon: "长期" },
    metrics: { securities_value: null, cash: null, total_assets: null, unrealized_pnl: null, missing_prices: [], positions: [], sector_weights: [] },
  },
];

const reportCurrent = {
  id: REP_CURRENT,
  account_id: ACC_MAIN,
  status: "current",
  input_version: 5,
  created_at: "2026-11-25T15:32:00+08:00",
  data_as_of: "2026-11-25T15:00:00+08:00",
  report: "## 核心判断\n\n当前组合现金占比约 51.5%，具备较高回旋余地。**贵州茅台**估值回到中枢下方，但短期批价波动仍需观察。\n\n- 保持核心底仓，等待渠道去库确认\n- ETF 仓位维持逢低定投节奏",
  result: {
    report: "## 核心判断\n\n当前组合现金占比约 51.5%，具备较高回旋余地。",
    valid_until: "2026-12-30T15:00:00+08:00",
    universe_version: 4,
    delivery: { status: "full", decision_summary: "完成全范围持仓与公共候选复核。", scope: ["SH:600519", "SZ:159599"] },
    model_config: { role_id: "manager", model_name: "fixture/Model-A", provider_id: PROV_1, reasoning_effort: "high", model_network_route: "environment", model_network_revision: 1 },
    model_usage: modelUsageSummary,
    references: [{ material_id: MAT_1, status: "resolved", scope: "public", access: "full_text", url: "https://example.com/annual.pdf", document_version: 1, range: { returned_range: { start_page: 12, end_page: 14 }, char_count: 4200 } }],
  },
};

const reportExpired = {
  id: REP_EXPIRED,
  account_id: ACC_MAIN,
  status: "expired",
  input_version: 4,
  created_at: "2026-09-15T15:00:00+08:00",
  data_as_of: "2026-09-14T15:00:00+08:00",
  report: "真正无标志的历史报告正文：无关联任务事件。",
  result: {
    report: "真正无标志的历史报告正文：无关联任务事件。",
    valid_until: "2026-09-30T15:00:00+08:00",
    universe_version: 2,
    delivery: { status: "legacy", scope: ["SH:600519"], limitations: ["早期归档无联网标识事件"] },
    model_usage: legacyModelUsageSummaryWithoutSearchFlag,
  },
};

const reportLegacyHydrated = {
  id: REP_LEGACY_HYDRATED,
  account_id: ACC_MAIN,
  job_id: JOB_LEGACY,
  status: "expired",
  input_version: 4,
  created_at: "2026-10-01T15:00:00+08:00",
  data_as_of: "2026-09-30T15:00:00+08:00",
  report: "历史报告正文：上一季度现金与消费仓位复盘（事件位于第 2 页）。",
  result: {
    report: "历史报告正文：上一季度现金与消费仓位复盘（事件位于第 2 页）。",
    valid_until: "2026-10-15T15:00:00+08:00",
    universe_version: 3,
    delivery: { status: "limited", scope: ["SH:600519"], limitations: ["当时部分候选停牌"] },
    model_usage: legacyModelUsageSummaryWithoutSearchFlag,
  },
};

const jobsMain = [
  {
    id: JOB_FAILED,
    account_id: ACC_MAIN,
    task_kind: "account_analysis",
    reason: "manual",
    state: "failed",
    stage: "synthesis",
    failure_stage: "terminal_submission",
    error: "终态结构化提交协议未满足：缺少最终报告主体字段。",
    error_code: "final_submission_missing",
    input_version: 5,
    created_at: "2026-11-25T16:10:00+08:00",
    started_at: "2026-11-25T16:10:04+08:00",
    completed_at: "2026-11-25T16:12:41+08:00",
    trigger_source: "user_click",
    delivery: { status: "incomplete", limitations: ["尚未收到完整终态提交"] },
    synthesis_checkpoint: {
      available: true,
      recovery_state: "synthesis_ready",
      completed_research_available: true,
      source_coverage: { [MAT_1]: { status: "covered" } },
    },
    event_count: 2,
    last_event_seq: 2,
    result: { model_usage: modelUsageSummary },
  },
  {
    id: JOB_DONE,
    account_id: ACC_MAIN,
    task_kind: "account_analysis",
    reason: "manual",
    state: "completed",
    stage: "published",
    input_version: 5,
    report_id: REP_CURRENT,
    report_status: "current",
    created_at: "2026-11-25T15:25:00+08:00",
    started_at: "2026-11-25T15:25:02+08:00",
    completed_at: "2026-11-25T15:32:00+08:00",
    trigger_source: "user_click",
    delivery: { status: "full", scope: ["SH:600519", "SZ:159599"] },
    event_count: 1,
    last_event_seq: 1,
    result: {
      model_config: { role_id: "manager", model_name: "fixture/Model-A", provider_id: PROV_1, reasoning_effort: "high" },
      model_usage: modelUsageSummary,
    },
  },
  {
    id: JOB_RUNNING,
    account_id: ACC_MAIN,
    task_kind: "security_research",
    reason: "manual_security:SH:600519",
    state: "running",
    stage: "researching",
    input_version: 5,
    created_at: "2026-11-25T16:20:00+08:00",
    started_at: "2026-11-25T16:20:03+08:00",
    trigger_source: "user_click",
    event_count: 0,
    last_event_seq: 0,
  },
  {
    id: JOB_LEGACY,
    account_id: ACC_MAIN,
    task_kind: "account_analysis",
    reason: "scheduled",
    state: "completed",
    stage: "published",
    input_version: 4,
    report_id: REP_LEGACY_HYDRATED,
    report_status: "expired",
    created_at: "2026-10-01T14:50:00+08:00",
    started_at: "2026-10-01T14:50:02+08:00",
    completed_at: "2026-10-01T15:00:00+08:00",
    trigger_source: "scheduler",
    delivery: { status: "limited", scope: ["SH:600519"], limitations: ["当时部分候选停牌"] },
    event_count: 101,
    last_event_seq: 101,
    result: {
      model_usage: legacyModelUsageSummaryWithoutSearchFlag,
    },
  },
];

// Real /api/accounts/:id/jobs?events=false omits `result` and provides top-level `model_usage_summary`.
const jobsMainList = jobsMain.map(({ result: _omittedResult, ...rest }) => ({
  ...rest,
  model_usage_summary:
    rest.id === JOB_FAILED || rest.id === JOB_DONE
      ? lightweightModelUsageSummary
      : null,
}));

const recsActive = [
  {
    id: "rec-active-1",
    symbol: "SH:600519",
    action: "buy_candidate",
    review_status: "needs_review",
    price_state: "met",
    observed_price: "1562.00",
    price_observed_at: "2026-11-25T15:00:00+08:00",
    input_version: 4,
    revision: 2,
    generated_at: "2026-11-20T15:00:00+08:00",
    valid_until: "2026-12-30T15:00:00+08:00",
    replaces_ids: ["rec-old-1"],
    basis: "价格回落至目标区间下沿，现金余额充足，可按分批规则复核增持窗口。",
    price_conditions: [{ field: "price", op: "<=", target: "1580.00" }],
    agent_conditions: { summary: "需核对近两周批价与动销反馈是否企稳" },
    references: [{ material_id: MAT_1, status: "resolved", scope: "public", access: "full_text" }],
  },
];

const recsHistory = [
  {
    id: "rec-old-1",
    symbol: "SH:600519",
    action: "hold",
    review_status: "expired",
    price_state: "not_met",
    observed_price: "1620.00",
    input_version: 3,
    revision: 1,
    generated_at: "2026-10-05T15:00:00+08:00",
    valid_until: "2026-10-20T15:00:00+08:00",
    replaced_by_id: "rec-active-1",
    basis: "旧版持有建议：观察三季报现金流表现后已被新版替代。",
  },
];

const eventRequests = [];

const server = createServer(async (req, res) => {
  try {
    const url = new URL(req.url, "http://127.0.0.1");
    const p = url.pathname;
    if (p.startsWith("/api/")) {
      let body = null;
      if (p === "/api/status") {
        body = {
          mode: "live",
          model_configured: true,
          configuration_errors: [],
          worker: { running: true, active: 1 },
          calendar: { phase: "after_close", session: "after_close", trading_day: true, coverage: { start: "2026-01-01", end: "2026-12-31" } },
          runtime_version: { api_contract: 13, started_at: "2026-11-25T09:00:00+08:00", schema_version: 13, database_mode: "sqlite" },
          native_search: { status: "observed" },
        };
      } else if (p === "/api/settings") {
        body = { quote_provider: "manual", model_native_search: true, model_network_route: "environment", max_model_calls: null, max_agent_turns: null, synthesis_max_calls: null };
      } else if (p === "/api/providers") {
        body = [{ id: PROV_1, display_name: "受控模型通道", base_url: "https://fixture.invalid/v1", protocol: "responses", revision: 1, key_configured: true, catalog: { status: "success", models: [{ id: "fixture/Model-A" }] } }];
      } else if (p === "/api/environment-connection") {
        body = { configured: false };
      } else if (p === "/api/agent-roles" || p.endsWith("/agent-models")) {
        body = [{ role_id: "manager", name: "主 Agent", responsibility: "统筹研究与交付", system_prompt: "受控提示词", enabled: true, version: 1, tools: ["finish_analysis"], default_binding: { provider_id: PROV_1, model_id: "fixture/Model-A", reasoning_effort: "high" }, resolved: { provider_id: PROV_1, model_id: "fixture/Model-A", reasoning_effort: "high", binding_source: "global_default", effort_source: "global_default" } }];
      } else if (p === "/api/accounts") {
        body = { accounts };
      } else if (p === `/api/accounts/${ACC_MAIN}`) {
        body = accounts[0];
      } else if (p === `/api/accounts/${ACC_EMPTY}`) {
        body = accounts[1];
      } else if (p === `/api/accounts/${ACC_MAIN}/analyses`) {
        body = { analyses: [{ id: REP_CURRENT, status: "current", input_version: 5 }, { id: REP_LEGACY_HYDRATED, status: "expired", input_version: 4 }, { id: REP_EXPIRED, status: "expired", input_version: 4 }] };
      } else if (p === `/api/accounts/${ACC_EMPTY}/analyses`) {
        body = { analyses: [] };
      } else if (p === `/api/analyses/${REP_CURRENT}`) {
        body = reportCurrent;
      } else if (p === `/api/analyses/${REP_LEGACY_HYDRATED}`) {
        body = reportLegacyHydrated;
      } else if (p === `/api/analyses/${REP_EXPIRED}`) {
        body = reportExpired;
      } else if (p === `/api/accounts/${ACC_MAIN}/jobs`) {
        assert.ok(jobsMainList.every(item => !("result" in item)), "List fixture must omit result");
        body = { jobs: jobsMainList };
      } else if (p === `/api/accounts/${ACC_EMPTY}/jobs`) {
        body = { jobs: [] };
      } else if (p === `/api/jobs/${JOB_FAILED}`) {
        body = jobsMain[0];
      } else if (p === `/api/jobs/${JOB_DONE}`) {
        body = jobsMain[1];
      } else if (p === `/api/jobs/${JOB_RUNNING}`) {
        body = jobsMain[2];
      } else if (p === `/api/jobs/${JOB_LEGACY}`) {
        body = jobsMain[3];
      } else if (p.endsWith("/events")) {
        const after = url.searchParams.get("after") || "0";
        eventRequests.push({ path: p, after });
        if (p.includes(JOB_LEGACY)) {
          if (after === "0") {
            body = {
              events: [
                { seq: 1, type: "agent_progress", agent_id: "manager", at: "2026-10-01T14:52:00+08:00", payload: { message: "第 1 页研究进展事件（未含模型请求明细）" } },
                { seq: 100, type: "tool_result", agent_id: "manager", at: "2026-10-01T14:56:00+08:00", payload: { tool_name: "fetch_material" } },
              ],
              next_after: 100,
              has_more: true,
            };
          } else if (after === "100") {
            body = {
              events: [
                { seq: 101, type: "model_usage", agent_id: "manager", at: "2026-10-01T14:59:55+08:00", payload: { requests: usageRequests, session: modelUsageSummary } },
              ],
              next_after: 101,
              has_more: false,
            };
          } else {
            body = { events: [], next_after: Number(after), has_more: false };
          }
        } else {
          body = {
            events: p.includes(JOB_FAILED)
              ? [
                  { seq: 1, type: "model_usage", agent_id: "manager", at: "2026-11-25T16:12:30+08:00", payload: { requests: usageRequests, session: modelUsageSummary } },
                  { seq: 2, type: "terminal_submission_rejected", agent_id: "manager", at: "2026-11-25T16:12:41+08:00", payload: { error: "final_submission_missing" } },
                ]
              : p.includes(JOB_DONE)
                ? [{ seq: 1, type: "model_usage", agent_id: "manager", at: "2026-11-25T15:31:55+08:00", payload: { requests: usageRequests, session: modelUsageSummary } }]
                : [],
            next_after: 2,
            has_more: false,
          };
        }
      } else if (p === `/api/accounts/${ACC_MAIN}/recommendations`) {
        const isHist = url.searchParams.get("view") === "history";
        const items = isHist ? recsHistory : recsActive;
        body = { items, total: items.length, offset: 0, limit: 10, has_more: false, counts: { active: recsActive.length, history: recsHistory.length } };
      } else if (p === `/api/accounts/${ACC_EMPTY}/recommendations`) {
        body = { items: [], total: 0, offset: 0, limit: 10, has_more: false, counts: { active: 0, history: 0 } };
      } else if (p === "/api/materials") {
        body = url.searchParams.get("account_id") === ACC_MAIN
          ? { materials: [{ id: MAT_1, title: "贵州茅台 2026 年三季度经营公告", classification: "disclosed_fact", access: "full_text", publisher: "上交所公告", published_at: "2026-10-28T18:00:00+08:00", fetched_at: "2026-11-25T15:10:00+08:00", summary: "营收与现金流保持稳健增长。", document_version: 1, document_identity_kind: "raw_bytes_sha256" }] }
          : { materials: [] };
      } else if (p === `/api/materials/${MAT_1}/range`) {
        body = { id: MAT_1, title: "贵州茅台 2026 年三季度经营公告", access: "full_text", text: "第三季度营业总收入稳步增长，经营活动现金流量净额表现平稳。", start: 0, end: 31, total_chars: 31, next_start: null };
      } else if (p === "/api/universe") {
        body = { universe_version: 4, watchlist: ["SH:600519", "SZ:159599"], candidates: ["SH:600036"], monitored: ["SH:600519", "SZ:159599", "SH:600036"], quotes: [{ symbol: "SH:600519", price: "1562.00", status: "fresh" }, { symbol: "SZ:159599", price: "1.47", status: "fresh" }] };
      } else if (p === "/api/research") {
        body = { records: [] };
      } else if (p === "/api/global-research") {
        body = { runs: [] };
      } else if (p === "/api/demands") {
        body = { demands: [] };
      } else if (p.endsWith("/ledger")) {
        const isMain = p.includes(ACC_MAIN);
        body = {
          account: isMain ? accounts[0] : accounts[1],
          fee_plan: { version: 1, stock_commission_rate: "0.00025", etf_commission_rate: "0.0001" },
          trades: [],
          cash_events: [],
          corrections: [],
        };
      } else if (p === "/api/forecasts") {
        body = { items: [], total: 0, offset: 0, limit: 30, has_more: false };
      } else if (p === "/api/forecast-batches") {
        body = { batches: [] };
      } else if (p === "/api/forecast-window-runs") {
        body = { runs: [] };
      } else if (p.endsWith("/forecast-settings")) {
        body = { enabled: true, rule_version: 1, evaluation_horizon_days: [1, 5, 20] };
      }
      res.writeHead(body === null ? 404 : 200, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify(body));
      return;
    }
    if (p === "/favicon.ico") {
      res.writeHead(204);
      res.end();
      return;
    }
    const clean = p === "/" ? "index.html" : p.replace(/^\/static\//, "").replace(/^\/+/, "");
    const fileUrl = new URL(`../app/static/${clean}`, import.meta.url);
    const content = await readFile(fileUrl);
    const ctype = clean.endsWith(".js")
      ? "application/javascript; charset=utf-8"
      : clean.endsWith(".css")
        ? "text/css; charset=utf-8"
        : "text/html; charset=utf-8";
    res.writeHead(200, { "Content-Type": ctype });
    res.end(content);
  } catch (err) {
    res.writeHead(500);
    res.end(String(err));
  }
});

await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
const base = `http://127.0.0.1:${server.address().port}`;

let browser;
try {
  browser = await chromium.launch({
    headless: true,
    ...(process.env.BROWSER_EXECUTABLE ? { executablePath: process.env.BROWSER_EXECUTABLE } : {}),
  });
  await mkdir("test-results/eighteenth-ui", { recursive: true });

  const errors = [];
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  page.on("pageerror", err => errors.push(`pageerror: ${err.message}`));
  page.on("console", msg => {
    if (msg.type() === "error") errors.push(`console.error: ${msg.text()}`);
  });
  await page.addInitScript(id => localStorage.setItem("guanlan.account", id), ACC_MAIN);

  const checkOverflow = async (label) => {
    const dims = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      innerWidth: window.innerWidth,
    }));
    assert.ok(
      dims.scrollWidth <= dims.innerWidth,
      `${label}: horizontal overflow scrollWidth=${dims.scrollWidth} > innerWidth=${dims.innerWidth}`,
    );
    return dims;
  };

  // 1. Desktop 1440x900: Overview + Loading state + Focus-visible + Disabled
  let delayRead = true;
  await page.route("**/api/status*", async route => {
    if (delayRead) {
      delayRead = false;
      await new Promise(r => setTimeout(r, 150));
    }
    await route.continue();
  });
  await page.goto(base + "/#overview");
  await page.locator(".stat-grid").waitFor();
  const desktopOverviewDims = await checkOverflow("desktop overview");
  await page.screenshot({ path: "test-results/eighteenth-ui/desktop-overview.png", fullPage: true });

  // Focus-visible keyboard ring check
  await page.keyboard.press("Tab");
  const focusCheck = await page.evaluate(() => {
    const el = document.activeElement;
    const style = el ? getComputedStyle(el) : null;
    return { tag: el?.tagName, outlineStyle: style?.outlineStyle, outlineWidth: style?.outlineWidth };
  });
  assert.ok(focusCheck.tag && focusCheck.tag !== "BODY", "Keyboard Tab moves focus onto an interactive element");
  await page.evaluate(() => document.activeElement && document.activeElement.blur());

  // 2. Desktop Research page: Report readability, expired badge, recommendations lineage, expanded model-usage KPI & prefix diagnostics
  await page.locator('nav a[href="#research"]').click();
  await page.locator(`[data-report-id="${REP_CURRENT}"]`).waitFor();
  await page.locator(`[data-report-id="${REP_LEGACY_HYDRATED}"]`).waitFor();
  await page.locator(`[data-report-id="${REP_EXPIRED}"]`).waitFor();

  const reportTypography = await page.locator(`[data-report-id="${REP_CURRENT}"] .report-body-surface .report-text`).evaluate(el => {
    const cs = getComputedStyle(el);
    return { fontSize: parseFloat(cs.fontSize), color: cs.color };
  });
  assert.ok(reportTypography.fontSize >= 13, `Expected readable report font >= 13px, got ${reportTypography.fontSize}`);

  const norm = s => String(s || "").replace(/\s+/g, " ").trim();

  // Expand model usage on current report (without loading any events for JOB_DONE/JOB_FAILED) and verify Prompt Cache KPIs
  // Notice req-mgr-5 has change_reason="append_only" + cache_epoch_reason="first", so it stays in non-search all (30.7%)
  // and is excluded from non-search cross-turn (45%), matching backend app/model_usage.py.
  const reportUsage = page.locator(`[data-report-id="${REP_CURRENT}"] .model-usage`);
  await reportUsage.locator(":scope > summary").click();
  await reportUsage.locator(".usage-kpi-grid").waitFor();
  assert.equal(await reportUsage.locator(".usage-kpi").count(), 5);
  const usageText = norm(await reportUsage.textContent());
  assert.ok(usageText.includes("非联网跨轮缓存率 45%"), usageText);
  assert.ok(usageText.includes("非联网全部 30.7%"), usageText);
  assert.ok(usageText.includes("联网请求缓存率 25%"), usageText);
  assert.ok(usageText.includes("前缀稳定性分布"), usageText);
  assert.ok(usageText.includes("按 Agent / 实例缓存读取分布"), usageText);
  assert.ok(usageText.includes("工具选择变化"), usageText);
  assert.ok(usageText.includes("历史重写"), usageText);
  assert.ok(usageText.includes("上下文压缩"), usageText);
  assert.ok(usageText.includes("未知"), "Missing fields remain explicit 未知");
  assert.equal(eventRequests.filter(r => !r.path.includes(JOB_LEGACY)).length, 0, "Current report usage must render without fetching events");

  // Legacy report with job_id (REP_LEGACY_HYDRATED) auto-hydrates across 2 pages of events on research view and stops after page 2
  const hydratedLegacyUsage = page.locator(`[data-report-id="${REP_LEGACY_HYDRATED}"] .model-usage`);
  await hydratedLegacyUsage.locator(":scope > summary").click();
  await page.waitForFunction(
    (id) => {
      const el = document.querySelector(`[data-report-id="${id}"] .model-usage`);
      return el && el.textContent.replace(/\s+/g, " ").includes("非联网跨轮缓存率 45%");
    },
    REP_LEGACY_HYDRATED,
  );
  const hydratedLegacyText = norm(await hydratedLegacyUsage.textContent());
  assert.ok(hydratedLegacyText.includes("非联网跨轮缓存率 45%"), hydratedLegacyText);
  assert.ok(hydratedLegacyText.includes("非联网全部 30.7%"), hydratedLegacyText);
  assert.ok(hydratedLegacyText.includes("联网请求缓存率 25%"), hydratedLegacyText);
  assert.deepEqual(
    eventRequests.filter(r => r.path.includes(JOB_LEGACY)).map(r => r.after),
    ["0", "100"],
    "Research view legacy hydration must fetch exactly 2 pages and stop",
  );
  await hydratedLegacyUsage.locator(":scope > summary").click();

  // Truly unflagged legacy report (no job_id / no events) must honestly keep showing 未知
  const legacyReportUsage = page.locator(`[data-report-id="${REP_EXPIRED}"] .model-usage`);
  await legacyReportUsage.locator(":scope > summary").click();
  await legacyReportUsage.locator(".usage-kpi-grid").waitFor();
  const legacyUsageText = norm(await legacyReportUsage.textContent());
  assert.ok(legacyUsageText.includes("非联网跨轮缓存率 未知"), legacyUsageText);
  assert.ok(legacyUsageText.includes("非联网全部 未知"), legacyUsageText);
  assert.ok(legacyUsageText.includes("联网请求缓存率 未知"), legacyUsageText);
  await legacyReportUsage.locator(":scope > summary").click();

  // Expand first session detail table and verify first_changed_path & search tags
  await reportUsage.locator("details").first().locator(":scope > summary").click();
  const sessionText = await reportUsage.locator("details").first().textContent();
  assert.ok(sessionText.includes("tool_choice"), sessionText);
  assert.ok(sessionText.includes("非联网"), sessionText);

  // Check active & history recommendations + lineage + separated condition cards
  const activeRec = page.locator('#active-recommendations [data-recommendation-id="rec-active-1"]');
  await activeRec.waitFor();
  assert.equal(await activeRec.getByText("价格条件已满足", { exact: true }).count(), 1);
  assert.equal(await activeRec.getByText("判断待复核", { exact: true }).count(), 1);
  const activeRecText = await activeRec.textContent();
  assert.ok(activeRecText.includes("替代先前建议"));
  assert.ok(activeRecText.includes("rec-old-1"));
  assert.ok(activeRecText.includes("可计算价格条件"));
  assert.ok(activeRecText.includes("仍需 Agent 判断"));

  await page.locator('[data-action="recommendation-history"]').click();
  const histRec = page.locator('#recommendation-history [data-recommendation-id="rec-old-1"]');
  await histRec.waitFor();
  assert.ok((await histRec.textContent()).includes("已被新建议替代"));
  assert.ok((await histRec.textContent()).includes("rec-active-1"));

  // Disabled button state check on pagination
  const prevBtn = page.locator('[data-pagination="recommendations"] button[data-page="-1"]');
  assert.equal(await prevBtn.isDisabled(), true);

  const desktopResearchDims = await checkOverflow("desktop research");
  await page.screenshot({ path: "test-results/eighteenth-ui/desktop-research.png", fullPage: true });

  // Verify route navigation away from research (clears state.events) and back re-hydrates legacy report
  await page.locator('nav a[href="#overview"]').click();
  await page.locator(".stat-grid").waitFor();
  const eventsBeforeReturnResearch = eventRequests.filter(r => r.path.includes(JOB_LEGACY)).length;
  await page.locator('nav a[href="#research"]').click();
  await page.locator(`[data-report-id="${REP_LEGACY_HYDRATED}"]`).waitFor();
  const rehydratedLegacyUsage = page.locator(`[data-report-id="${REP_LEGACY_HYDRATED}"] .model-usage`);
  await rehydratedLegacyUsage.locator(":scope > summary").click();
  await page.waitForFunction(
    (id) => {
      const el = document.querySelector(`[data-report-id="${id}"] .model-usage`);
      return el && el.textContent.replace(/\s+/g, " ").includes("非联网跨轮缓存率 45%");
    },
    REP_LEGACY_HYDRATED,
  );
  const rehydratedText = norm(await rehydratedLegacyUsage.textContent());
  assert.ok(rehydratedText.includes("非联网跨轮缓存率 45%"), rehydratedText);
  assert.ok(rehydratedText.includes("非联网全部 30.7%"), rehydratedText);
  assert.ok(rehydratedText.includes("联网请求缓存率 25%"), rehydratedText);
  assert.deepEqual(
    eventRequests.filter(r => r.path.includes(JOB_LEGACY)).slice(eventsBeforeReturnResearch).map(r => r.after),
    ["0", "100"],
    "Returning to research view after state.events clear must re-hydrate legacy report across 2 pages",
  );
  await rehydratedLegacyUsage.locator(":scope > summary").click();

  // 3. Desktop Agents Workbench: top workbench cache card (from lightweight model_usage_summary without result/events), failed diagnosis panel, task-card usage strip
  const beforeAgentsEventCount = eventRequests.length;
  await page.locator('nav a[href="#agents"]').click();
  await page.locator(".workbench-cache-card").waitFor();
  assert.equal(eventRequests.length, beforeAgentsEventCount, "Workbench must aggregate lightweight model_usage_summary without fetching events");
  const wbText = norm(await page.locator(".workbench-cache-card").textContent());
  assert.ok(wbText.includes("当前页 Prompt Cache 与前缀稳定性"), wbText);
  assert.ok(wbText.includes("覆盖 2 个任务 · 14 次请求"), wbText);
  assert.ok(wbText.includes("总体加权缓存读取率30%"), wbText);
  assert.ok(wbText.includes("非联网跨轮续接缓存率45%"), wbText);
  assert.ok(wbText.includes("非联网全部 30.7%"), wbText);
  assert.ok(wbText.includes("联网请求缓存率25%"), wbText);
  assert.ok(wbText.includes("前缀稳定性分布"), wbText);
  assert.ok(wbText.includes("首次请求 ×4"), wbText);
  assert.ok(wbText.includes("尾部追加 ×4"), wbText);
  assert.ok(wbText.includes("工具选择变化 ×2"), wbText);
  assert.ok(wbText.includes("历史重写 ×2"), wbText);
  assert.ok(wbText.includes("上下文压缩 ×2"), wbText);

  const failedCard = page.locator(`[data-task-id="${JOB_FAILED}"]`);
  await failedCard.locator(".failure-diagnosis-panel").waitFor();
  await failedCard.locator(".job-usage-strip").waitFor();
  const failText = await failedCard.locator(".failure-diagnosis-panel").textContent();
  assert.ok(failText.includes("失败诊断"));
  assert.ok(failText.includes("失败阶段：终态报告提交"));
  assert.ok(failText.includes("final_submission_missing"));
  assert.ok(failText.includes("已有可恢复汇总检查点"));
  assert.equal(await failedCard.locator('[data-action="retry-job"]').count(), 1);

  const doneCard = page.locator(`[data-task-id="${JOB_DONE}"]`);
  await doneCard.locator(".job-usage-strip").waitFor();
  const doneStripText = norm(await doneCard.locator(".job-usage-strip").textContent());
  assert.ok(doneStripText.includes("加权缓存读取率"), doneStripText);
  assert.ok(doneStripText.includes("7 次请求 · 完整 6/7"), doneStripText);

  const desktopAgentsDims = await checkOverflow("desktop agents");
  await page.screenshot({ path: "test-results/eighteenth-ui/desktop-agents.png", fullPage: true });

  // Open job-result modal on failed job (has native_search_requested in sequence -> no event fetch needed)
  await failedCard.locator('[data-action="job-result"]').click();
  await page.locator("#dialog[open] .failure-diagnosis-panel").waitFor();
  await page.locator("#dialog .model-usage > summary").click();
  await page.locator("#dialog .usage-kpi-grid").waitFor();
  const dialogUsageText = norm(await page.locator("#dialog .model-usage").textContent());
  assert.ok(dialogUsageText.includes("非联网跨轮缓存率 45%"), dialogUsageText);
  assert.ok(dialogUsageText.includes("非联网全部 30.7%"), dialogUsageText);
  assert.ok(dialogUsageText.includes("联网请求缓存率 25%"), dialogUsageText);
  assert.equal(eventRequests.filter(r => r.path.includes(JOB_FAILED)).length, 0);
  await page.screenshot({ path: "test-results/eighteenth-ui/desktop-dialog-usage.png", fullPage: false });
  await page.locator('#dialog [data-action="close-dialog"]').click();

  // Open job-result modal on legacy job (already cached from research view -> no extra fetch)
  const legacyEventsBeforeModal = eventRequests.filter(r => r.path.includes(JOB_LEGACY)).length;
  const legacyCard = page.locator(`[data-task-id="${JOB_LEGACY}"]`);
  await legacyCard.locator('[data-action="job-result"]').click();
  await page.locator("#dialog[open] .model-usage > summary").click();
  await page.locator("#dialog .usage-kpi-grid").waitFor();
  const legacyDialogText = norm(await page.locator("#dialog .model-usage").textContent());
  assert.ok(legacyDialogText.includes("非联网跨轮缓存率 45%"), legacyDialogText);
  assert.ok(legacyDialogText.includes("非联网全部 30.7%"), legacyDialogText);
  assert.ok(legacyDialogText.includes("联网请求缓存率 25%"), legacyDialogText);
  assert.equal(eventRequests.filter(r => r.path.includes(JOB_LEGACY)).length, legacyEventsBeforeModal);
  await page.locator('#dialog [data-action="close-dialog"]').click();

  // Check remaining desktop routes for overflow
  for (const r of ["holdings", "market", "providers", "roles", "forecasts", "settings"]) {
    await page.locator(`nav a[href="#${r}"]`).click();
    await page.locator(`nav a[href="#${r}"][aria-current="page"]`).waitFor();
    await page.locator(".loading-line").waitFor({ state: "hidden" });
    await checkOverflow(`desktop ${r}`);
  }

  // Empty state check via empty account
  await selectChoice(page, "#account-switch", ACC_EMPTY);
  await page.locator(".loading-line").waitFor({ state: "hidden" });
  await page.locator('nav a[href="#research"]').click();
  await page.getByText("当前页暂无研究报告").waitFor();
  await selectChoice(page, "#account-switch", ACC_MAIN);
  await page.locator(".loading-line").waitFor({ state: "hidden" });

  // 4. Mobile 390x844 verification & screenshots across key routes
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator('nav a[href="#overview"]').click();
  await page.locator(".stat-grid").waitFor();
  const mobileOverviewDims = await checkOverflow("mobile overview");
  await page.screenshot({ path: "test-results/eighteenth-ui/mobile-overview.png", fullPage: true });

  await page.locator('nav a[href="#research"]').click();
  await page.locator(`[data-report-id="${REP_CURRENT}"]`).waitFor();
  const mobileResearchDims = await checkOverflow("mobile research");
  await page.screenshot({ path: "test-results/eighteenth-ui/mobile-research.png", fullPage: true });

  await page.locator('nav a[href="#agents"]').click();
  await page.locator(".workbench-cache-card").waitFor();
  const mobileAgentsDims = await checkOverflow("mobile agents");
  await page.screenshot({ path: "test-results/eighteenth-ui/mobile-agents.png", fullPage: true });

  for (const r of ["holdings", "market", "settings"]) {
    await page.locator(`nav a[href="#${r}"]`).click();
    await page.locator(`nav a[href="#${r}"][aria-current="page"]`).waitFor();
    await page.locator(".loading-line").waitFor({ state: "hidden" });
    await checkOverflow(`mobile ${r}`);
  }

  // 5. Isolated fresh-page checks for bounded 2-page legacy event hydration on job-result & open-report
  const runIsolatedAgentsCheck = async (fn) => {
    const p2 = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    p2.on("pageerror", err => errors.push(`pageerror: ${err.message}`));
    p2.on("console", msg => {
      if (msg.type() === "error") errors.push(`console.error: ${msg.text()}`);
    });
    await p2.addInitScript(id => localStorage.setItem("guanlan.account", id), ACC_MAIN);
    try {
      await p2.goto(base + "/#agents");
      await p2.locator(`[data-task-id="${JOB_LEGACY}"]`).waitFor();
      await fn(p2);
    } finally {
      await p2.close();
    }
  };

  // 5a. Fresh page -> #agents -> click job-result on JOB_LEGACY (must paginate 2 pages and stop)
  eventRequests.length = 0;
  await runIsolatedAgentsCheck(async (p2) => {
    assert.equal(eventRequests.filter(r => r.path.includes(JOB_LEGACY)).length, 0, "Agents list itself must not prefetch legacy job events");
    await p2.locator(`[data-task-id="${JOB_LEGACY}"] [data-action="job-result"]`).click();
    await p2.locator("#dialog[open] .model-usage > summary").click();
    await p2.locator("#dialog .usage-kpi-grid").waitFor();
    const isolatedJobDialogText = norm(await p2.locator("#dialog .model-usage").textContent());
    assert.ok(isolatedJobDialogText.includes("非联网跨轮缓存率 45%"), isolatedJobDialogText);
    assert.ok(isolatedJobDialogText.includes("非联网全部 30.7%"), isolatedJobDialogText);
    assert.ok(isolatedJobDialogText.includes("联网请求缓存率 25%"), isolatedJobDialogText);
    assert.deepEqual(
      eventRequests.filter(r => r.path.includes(JOB_LEGACY)).map(r => r.after),
      ["0", "100"],
      "job-result modal must fetch exactly 2 pages for legacy job and stop",
    );
    await p2.locator('#dialog [data-action="close-dialog"]').click();
    // Re-opening job-result reuses cached dialog events without another fetch
    await p2.locator(`[data-task-id="${JOB_LEGACY}"] [data-action="job-result"]`).click();
    await p2.locator("#dialog[open] .model-usage").waitFor();
    assert.equal(eventRequests.filter(r => r.path.includes(JOB_LEGACY)).length, 2);
    await p2.locator('#dialog [data-action="close-dialog"]').click();
  });

  // 5b. Fresh page -> #agents -> click open-report on JOB_LEGACY (must paginate 2 pages and stop)
  eventRequests.length = 0;
  await runIsolatedAgentsCheck(async (p2) => {
    await p2.locator(`[data-task-id="${JOB_LEGACY}"] [data-action="open-report"]`).waitFor();
    assert.equal(eventRequests.filter(r => r.path.includes(JOB_LEGACY)).length, 0);
    await p2.locator(`[data-task-id="${JOB_LEGACY}"] [data-action="open-report"]`).click();
    await p2.locator("#dialog[open] .model-usage > summary").click();
    await p2.locator("#dialog .usage-kpi-grid").waitFor();
    const isolatedReportDialogText = norm(await p2.locator("#dialog .model-usage").textContent());
    assert.ok(isolatedReportDialogText.includes("非联网跨轮缓存率 45%"), isolatedReportDialogText);
    assert.ok(isolatedReportDialogText.includes("非联网全部 30.7%"), isolatedReportDialogText);
    assert.ok(isolatedReportDialogText.includes("联网请求缓存率 25%"), isolatedReportDialogText);
    assert.deepEqual(
      eventRequests.filter(r => r.path.includes(JOB_LEGACY)).map(r => r.after),
      ["0", "100"],
      "open-report modal must fetch exactly 2 pages for legacy report and stop",
    );
    await p2.locator('#dialog [data-action="close-dialog"]').click();
  });

  // 5c. Fresh page -> #research with transient event API 500 -> releases dedupe key -> switchAccount & back retries & succeeds
  eventRequests.length = 0;
  {
    const p3 = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const p3PageErrors = [];
    p3.on("pageerror", err => p3PageErrors.push(err.message));
    await p3.addInitScript(id => localStorage.setItem("guanlan.account", id), ACC_MAIN);
    let failFirstLegacyFetch = true;
    await p3.route(`**/api/jobs/${JOB_LEGACY}/events*`, async route => {
      if (failFirstLegacyFetch) {
        failFirstLegacyFetch = false;
        await route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ error: "transient" }) });
        return;
      }
      await route.continue();
    });
    try {
      await p3.goto(base + "/#research");
      await p3.locator(`[data-report-id="${REP_LEGACY_HYDRATED}"]`).waitFor();
      await p3.locator(".loading-line").waitFor({ state: "hidden" });
      const u3 = p3.locator(`[data-report-id="${REP_LEGACY_HYDRATED}"] .model-usage`);
      await u3.locator(":scope > summary").click();
      await u3.locator(".usage-kpi-grid").waitFor();
      const failedText = norm(await u3.textContent());
      assert.ok(failedText.includes("非联网跨轮缓存率 未知"), failedText);

      // Switch account away and back (clears state.events & legacyReportUsageHydrated) and verify retry succeeds cleanly
      await selectChoice(p3, "#account-switch", ACC_EMPTY);
      await p3.getByText("当前页暂无研究报告").waitFor();
      await selectChoice(p3, "#account-switch", ACC_MAIN);
      await p3.locator(`[data-report-id="${REP_LEGACY_HYDRATED}"]`).waitFor();
      await p3.locator(`[data-report-id="${REP_LEGACY_HYDRATED}"] .model-usage > summary`).click();
      await p3.waitForFunction(
        (id) => {
          const el = document.querySelector(`[data-report-id="${id}"] .model-usage`);
          return el && el.textContent.replace(/\s+/g, " ").includes("非联网跨轮缓存率 45%");
        },
        REP_LEGACY_HYDRATED,
      );
      const recoveredText = norm(await p3.locator(`[data-report-id="${REP_LEGACY_HYDRATED}"] .model-usage`).textContent());
      assert.ok(recoveredText.includes("非联网跨轮缓存率 45%"), recoveredText);
      assert.ok(recoveredText.includes("非联网全部 30.7%"), recoveredText);
      assert.ok(recoveredText.includes("联网请求缓存率 25%"), recoveredText);
      assert.deepEqual(
        eventRequests.filter(r => r.path.includes(JOB_LEGACY)).map(r => r.after),
        ["0", "100"],
        "After transient 500 failure and account switch back, hydration must retry and finish in 2 pages",
      );
      assert.deepEqual(p3PageErrors, []);
    } finally {
      await p3.close();
    }
  }

  // Verify every screenshot file exists and is non-empty
  const screenshots = [
    "test-results/eighteenth-ui/desktop-overview.png",
    "test-results/eighteenth-ui/desktop-research.png",
    "test-results/eighteenth-ui/desktop-agents.png",
    "test-results/eighteenth-ui/desktop-dialog-usage.png",
    "test-results/eighteenth-ui/mobile-overview.png",
    "test-results/eighteenth-ui/mobile-research.png",
    "test-results/eighteenth-ui/mobile-agents.png",
  ];
  const sizes = {};
  for (const file of screenshots) {
    const info = await stat(file);
    assert.ok(info.size > 5000, `Screenshot ${file} unexpectedly small: ${info.size}`);
    sizes[file] = info.size;
  }

  assert.deepEqual(errors, [], `Browser errors detected: ${errors.join("; ")}`);

  const evidence = {
    passed: true,
    browser: browser.version(),
    realModelCalls: 0,
    viewports: {
      desktop: { width: 1440, height: 900, overview: desktopOverviewDims, research: desktopResearchDims, agents: desktopAgentsDims },
      mobile: { width: 390, height: 844, overview: mobileOverviewDims, research: mobileResearchDims, agents: mobileAgentsDims },
    },
    reportTypography,
    focusCheck,
    screenshots: sizes,
  };
  await writeFile("test-results/eighteenth-ui/evidence.json", JSON.stringify(evidence, null, 2));
  console.log(JSON.stringify({ passed: true, screenshots: Object.keys(sizes).length, errors: 0 }));
} finally {
  if (browser) await browser.close();
  server.close();
}
