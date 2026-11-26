// Isolated browser verification for the 20th UI upgrade: wide detail dialogs,
// task detail hierarchy, demand cards, unified disclosure chevrons, timeline
// readability, and deduplicated footer actions.
// Serves real app/static/* assets against deterministic HTTP fixtures (0 model calls).
import { createRequire } from "node:module";
import { createServer } from "node:http";
import { readFile, mkdir, writeFile, stat } from "node:fs/promises";
import assert from "node:assert/strict";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const ACC_MAIN = "a".repeat(32);
const ACC_EMPTY = "b".repeat(32);
const JOB_FAILED = "f".repeat(32);
const JOB_DONE = "d".repeat(32);
const REP_CURRENT = "c".repeat(32);
const MAT_1 = "2".repeat(32);
const PROV_1 = "3".repeat(32);
const FORECAST_1 = "9".repeat(32);
const BATCH_1 = "8".repeat(32);
const CLOSE_EVENT_KEY = "close:96b91df38e854008b989206c63808c3a:2026-11-26";

const modelUsageSummary = {
  duration_ms: 5400,
  cache_summary: {
    requests: 9,
    complete_requests: 8,
    unknown_requests: 1,
    coverage: 8 / 9,
    reported_input_tokens: 64000,
    cache_read_tokens: 21000,
    uncached_input_tokens: 43000,
    weighted_read_ratio: 21000 / 64000,
    anomalous_requests: 0,
  },
  normalized_totals: {
    input_tokens: 64000,
    cache_read_tokens: 21000,
    uncached_input_tokens: 43000,
    output_tokens: 1800,
    reasoning_tokens: 600,
    visible_output_tokens: 1200,
    cache_write_tokens: 0,
    known_requests: {
      input_tokens: 9,
      cache_read_tokens: 8,
      uncached_input_tokens: 8,
      output_tokens: 8,
      reasoning_tokens: 5,
      visible_output_tokens: 8,
      cache_write_tokens: 0,
    },
    coverage: {
      input_tokens: 1,
      cache_read_tokens: 8 / 9,
      uncached_input_tokens: 8 / 9,
      output_tokens: 8 / 9,
      reasoning_tokens: 5 / 9,
      visible_output_tokens: 8 / 9,
      cache_write_tokens: 0,
    },
  },
  prefix_reason_counts: { first: 3, append_only: 4, tool_choice_changed: 1, history_rewritten: 1 },
  native_search_breakdown: {
    non_search_all: {
      count: 8,
      paired: 7,
      reported_input_tokens: 58000,
      cache_read_tokens: 21000,
      weighted_read_ratio: 21000 / 58000,
    },
    non_search_cross_turn: {
      count: 5,
      paired: 5,
      reported_input_tokens: 36000,
      cache_read_tokens: 19000,
      weighted_read_ratio: 19000 / 36000,
    },
    search_all: {
      count: 1,
      paired: 1,
      reported_input_tokens: 6000,
      cache_read_tokens: 2000,
      weighted_read_ratio: 2000 / 6000,
    },
  },
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
  job_id: JOB_DONE,
  status: "current",
  input_version: 5,
  created_at: "2026-11-25T15:32:00+08:00",
  data_as_of: "2026-11-25T15:00:00+08:00",
  report:
    "## 核心判断\n\n当前组合现金占比约 51.5%，具备较高回旋余地。**贵州茅台**估值回到中枢下方，但短期批价波动仍需观察。\n\n- 保持核心底仓，等待渠道去库确认\n- ETF 仓位维持逢低定投节奏\n\n## 风险提示\n\n若批价继续下探，需重新评估消费板块权重上限。",
  result: {
    report: "## 核心判断\n\n当前组合现金占比约 51.5%，具备较高回旋余地。",
    valid_until: "2026-12-30T15:00:00+08:00",
    universe_version: 4,
    delivery: { status: "full", decision_summary: "完成全范围持仓与公共候选复核。", scope: ["SH:600519", "SZ:159599"] },
    model_config: {
      role_id: "manager",
      model_name: "fixture/Model-A",
      provider_id: PROV_1,
      reasoning_effort: "high",
      model_network_route: "environment",
      model_network_revision: 3,
    },
    model_usage: modelUsageSummary,
    references: [
      {
        material_id: MAT_1,
        status: "resolved",
        scope: "public",
        access: "full_text",
        url: "https://example.com/annual.pdf",
        document_version: 1,
        range: { returned_range: { start_page: 12, end_page: 14 }, char_count: 4200 },
      },
    ],
  },
};

const failedJob = {
  id: JOB_FAILED,
  account_id: ACC_MAIN,
  task_kind: "account_analysis",
  reason: "close_summary",
  state: "failed",
  stage: "synthesis",
  failure_stage: "terminal_submission",
  error: "终态结构化提交协议未满足：缺少最终报告主体字段。",
  error_code: "final_submission_missing",
  input_version: 5,
  created_at: "2026-11-26T15:05:00+08:00",
  started_at: "2026-11-26T15:05:04+08:00",
  completed_at: "2026-11-26T15:12:41+08:00",
  trigger_source: "scheduler",
  trading_day: "2026-11-26",
  target: {
    account_name: "稳健核心观察账户",
    holdings: ["SH:600519", "SZ:159599"],
    candidates: ["SH:600036"],
    symbols: [],
  },
  model_config: {
    role_id: "manager",
    model_name: "fixture/Model-A",
    provider_id: PROV_1,
    reasoning_effort: "high",
    model_network_route: "environment",
    model_network_revision: 3,
  },
  delivery: {
    status: "incomplete",
    scope: ["SH:600519", "SZ:159599"],
    limitations: ["尚未收到完整终态提交"],
  },
  demands: [
    {
      id: "demand-close-20261126",
      kind: "close_summary",
      state: "failed",
      reason: "收盘总结",
      created_at: "2026-11-26T15:05:00+08:00",
      trading_day: "2026-11-26",
      event_key: CLOSE_EVENT_KEY,
    },
    {
      id: "demand-forecast-20261126",
      kind: "formal_forecast",
      state: "completed",
      reason: "正式预测批次",
      created_at: "2026-11-26T15:05:00+08:00",
      trading_day: "2026-11-26",
      event_key: "forecast:batch:2026-11-26:formal",
    },
    {
      id: "demand-material-20261127",
      kind: "material_review",
      state: "pending",
      reason: "补充复核三季度经营公告与渠道反馈",
      created_at: "2026-11-26T15:06:00+08:00",
      trading_day: "2026-11-27",
      event_key: "material:2f7a1c90d4b84e0f9a3c5e8b1d2f4a6c",
    },
  ],
  synthesis_checkpoint: {
    available: true,
    recovery_state: "synthesis_ready",
    completed_research_available: true,
    source_coverage: {
      [MAT_1]: { status: "covered" },
      "mat-gap-2": { status: "missing", reason: "delegated_report_not_delivered" },
    },
  },
  event_count: 12,
  last_event_seq: 12,
  result: {
    model_usage: modelUsageSummary,
    state: "failed",
    error_code: "final_submission_missing",
    synthesis_checkpoint: { available: true, recovery_state: "synthesis_ready" },
  },
};

const doneJob = {
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
    model_config: {
      role_id: "manager",
      model_name: "fixture/Model-A",
      provider_id: PROV_1,
      reasoning_effort: "high",
      model_network_route: "environment",
      model_network_revision: 2,
    },
    model_usage: modelUsageSummary,
  },
};

const jobsList = [failedJob, doneJob].map(({ result: _omitted, ...rest }) => ({
  ...rest,
  model_usage_summary: rest.id === JOB_FAILED ? modelUsageSummary : modelUsageSummary,
}));

// Minimal session summary that is enough for modelUsageView() to render its
// <details class="model-usage"> block inside the model_usage timeline event.
const eventUsageSession = {
  duration_ms: 1200,
  cache_summary: {
    requests: 1,
    complete_requests: 1,
    unknown_requests: 0,
    coverage: 1,
    reported_input_tokens: 6000,
    cache_read_tokens: 3000,
    uncached_input_tokens: 3000,
    weighted_read_ratio: 0.5,
    anomalous_requests: 0,
  },
  normalized_totals: {
    input_tokens: 6000,
    cache_read_tokens: 3000,
    uncached_input_tokens: 3000,
    output_tokens: 180,
    reasoning_tokens: 60,
    visible_output_tokens: 120,
    cache_write_tokens: 0,
    coverage: {
      input_tokens: 1,
      cache_read_tokens: 1,
      uncached_input_tokens: 1,
      output_tokens: 1,
      reasoning_tokens: 1,
      visible_output_tokens: 1,
      cache_write_tokens: 0,
    },
    known_requests: {
      input_tokens: 1,
      cache_read_tokens: 1,
      uncached_input_tokens: 1,
      output_tokens: 1,
      reasoning_tokens: 1,
      visible_output_tokens: 1,
      cache_write_tokens: 0,
    },
  },
  sequence: [
    {
      local_request_id: "req-event-usage-1",
      agent_id: "manager",
      session_id: "sess-manager-20261126",
      cache_epoch: 1,
      stage: "research",
      request_category: "first_session_request",
      native_search_requested: false,
      prefix_diagnostic: { change_reason: "first" },
      normalized_usage: {
        input_tokens: 6000,
        cache_read_tokens: 3000,
        uncached_input_tokens: 3000,
        output_tokens: 180,
        reasoning_tokens: 60,
        visible_output_tokens: 120,
        cache_write_tokens: 0,
      },
      duration_ms: 1200,
      cost_known: false,
    },
  ],
};

// 12 research events: nested JSON payloads, error & informational types,
// a mapped-but-bodyless event, an unmapped event, and a model_usage session.
const researchEvents = [
  {
    seq: 1,
    type: "model_configuration_bound",
    agent_id: "manager",
    at: "2026-11-26T15:05:04+08:00",
    payload: {
      message: "主 Agent 模型已绑定：fixture/Model-A（reasoning_effort=high）。",
      provider_id: PROV_1,
      model_name: "fixture/Model-A",
      reasoning_effort: "high",
      route: { mode: "environment", revision: 3 },
    },
  },
  {
    seq: 2,
    type: "agent_started",
    agent_id: "manager",
    at: "2026-11-26T15:05:05+08:00",
    payload: { agent_id: "manager", session_id: "sess-manager-20261126", research_round: 1 },
  },
  {
    seq: 3,
    type: "research_context_prepared",
    agent_id: "manager",
    at: "2026-11-26T15:05:19+08:00",
    payload: {
      research_round: 1,
      materials: [MAT_1],
      scope: { holdings: ["SH:600519", "SZ:159599"], candidates: ["SH:600036"] },
    },
  },
  {
    seq: 4,
    type: "tool_started",
    agent_id: "filing_researcher",
    at: "2026-11-26T15:05:31+08:00",
    payload: {
      tool_name: "fetch_material",
      arguments: { material_id: MAT_1, range: { start_page: 12, end_page: 14 }, options: { include_tables: true } },
    },
  },
  {
    seq: 5,
    type: "tool_result",
    agent_id: "filing_researcher",
    at: "2026-11-26T15:05:48+08:00",
    payload: {
      tool_name: "fetch_material",
      status: "success",
      result: {
        chars: 4200,
        pages: [12, 13, 14],
        excerpt: "第三季度营业总收入同比增长 8.2%，经营活动现金流量净额保持平稳。",
        citations: [{ page: 12, anchor: "revenue" }, { page: 14, anchor: "cash_flow" }],
      },
    },
  },
  {
    seq: 6,
    type: "model_request",
    agent_id: "manager",
    at: "2026-11-26T15:06:02+08:00",
    payload: {
      request_id: "req-close-1",
      model: "fixture/Model-A",
      reasoning_effort: "high",
      input_tokens: 9000,
      tools: ["fetch_material", "finish_analysis"],
    },
  },
  {
    seq: 7,
    type: "model_error",
    agent_id: "manager",
    at: "2026-11-26T15:12:10+08:00",
    payload: {
      error: "模型请求在 120s 内未返回；已保留检查点，可安全重试。",
      code: "model_timeout",
      attempt: 2,
      request_id: "req-close-2",
      diagnostics: { timeout_ms: 120000, upstream: "fixture.invalid", retryable: true },
    },
  },
  {
    seq: 8,
    type: "synthesis_checkpoint",
    agent_id: "manager",
    at: "2026-11-26T15:12:28+08:00",
    payload: {
      message: "已保存汇总检查点；后续重试可优先恢复已完成研究。",
      recovery_state: "synthesis_ready",
      source_coverage: { [MAT_1]: { status: "covered" }, "mat-gap-2": { status: "missing" } },
    },
  },
  {
    seq: 9,
    type: "terminal_submission_rejected",
    agent_id: "manager",
    at: "2026-11-26T15:12:41+08:00",
    payload: {
      error: "final_submission_missing",
      detail: {
        missing: ["report.body"],
        received_fields: ["report.title", "report.summary"],
        schema_version: 3,
        rejected_at: "2026-11-26T15:12:41+08:00",
      },
    },
  },
  {
    seq: 10,
    type: "model_usage",
    agent_id: "manager",
    at: "2026-11-26T15:12:43+08:00",
    payload: { session: eventUsageSession },
  },
  {
    seq: 11,
    type: "model_client_closed",
    agent_id: "manager",
    at: "2026-11-26T15:12:44+08:00",
    payload: { client_id: "c1", lifecycle_events: 2 },
  },
  {
    seq: 12,
    type: "custom_unmapped_ping",
    agent_id: "manager",
    at: "2026-11-26T15:12:45+08:00",
    payload: { source: "smoke" },
  },
];

const material = {
  id: MAT_1,
  title: "贵州茅台 2026 年三季度经营公告",
  classification: "disclosed_fact",
  access: "full_text",
  publisher: "上交所公告",
  published_at: "2026-10-28T18:00:00+08:00",
  fetched_at: "2026-11-26T15:10:00+08:00",
  summary: "营收与现金流保持稳健增长，渠道库存与批价波动需继续观察。",
  document_version: 1,
  document_identity_kind: "raw_bytes_sha256",
};

const materialText =
  "贵州茅台酒股份有限公司 2026 年第三季度报告（节选）\n\n" +
  "一、经营情况讨论与分析\n" +
  "报告期内，公司实现营业总收入同比增长 8.2%，归属于上市公司股东的净利润同比增长 9.1%。" +
  "经营活动产生的现金流量净额保持平稳，合同负债余额较年初有所回升。\n\n" +
  "二、渠道与批价\n" +
  "直销渠道占比继续提升，经销渠道库存处于合理区间；散瓶批价短期波动，公司维持稳价策略。\n\n" +
  "三、风险提示\n" +
  "宏观消费复苏节奏、渠道库存与批价波动可能影响后续季度表现。";

const forecastRow = {
  id: FORECAST_1,
  forecast_id: FORECAST_1,
  batch_id: BATCH_1,
  account_id: ACC_MAIN,
  symbol: "SH:600519",
  kind: "formal",
  horizon: 5,
  base_date: "2026-11-20",
  target_date: "2026-11-27",
  published_at: "2026-11-20T16:10:00+08:00",
  information_cutoff: "2026-11-20T15:00:00+08:00",
  direction: "up",
  actual_direction: "up",
  return_ratio: 0.0213,
  display_state: "evaluated",
  hit: true,
  manager_model: "fixture/Model-A",
  manager_effort: "high",
  assigned_attempt: 1,
  execution_attempt_id: "attempt-1",
  team_id: "team-20261120-close",
  rule_version: "rule-2026-11-a:0123456789ab",
  calendar_evidence: { target_date: "2026-11-27", trading_day: true },
};

const forecastBatchRow = {
  id: "row-attempt-2",
  batch_id: BATCH_1,
  account_id: ACC_MAIN,
  symbol: "SZ:159599",
  kind: "formal",
  horizon: 20,
  base_date: "2026-11-26",
  target_date: null,
  direction: "abstain",
  display_state: "waiting_data",
  hit: null,
  manager_model: "fixture/Model-A",
  manager_effort: "high",
  assigned_attempt: 2,
  execution_attempt_id: "attempt-2",
  rule_version: "rule-2026-11-a:0123456789ab",
};

const forecastBatch = {
  id: BATCH_1,
  account_id: ACC_MAIN,
  base_date: "2026-11-26",
  kind: "formal",
  expected_count: 2,
  state: "completed",
  research_state: "completed",
  preparation: { ready: 2 },
  rule_version: "rule-2026-11-a:0123456789ab",
  attempts: [
    { attempt: 1, state: "published", model: "fixture/Model-A", reasoning_effort: "high", requested: true },
    { attempt: 2, state: "open", model: "fixture/Model-A", reasoning_effort: "high", requested: false },
  ],
};

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
          worker: { running: true, active: 0 },
          calendar: {
            phase: "after_close",
            session: "after_close",
            trading_day: true,
            coverage: { start: "2026-01-01", end: "2026-12-31" },
          },
          runtime_version: { api_contract: 13, started_at: "2026-11-26T09:00:00+08:00", schema_version: 13, database_mode: "sqlite" },
          native_search: { status: "observed" },
        };
      } else if (p === "/api/settings") {
        body = { quote_provider: "manual", model_native_search: true, model_network_route: "environment", max_model_calls: null, max_agent_turns: null, synthesis_max_calls: null };
      } else if (p === "/api/providers") {
        body = [
          {
            id: PROV_1,
            display_name: "受控模型通道",
            base_url: "https://fixture.invalid/v1",
            protocol: "responses",
            revision: 1,
            key_configured: true,
            catalog: { status: "success", models: [{ id: "fixture/Model-A" }] },
          },
        ];
      } else if (p === "/api/environment-connection") {
        body = { configured: false };
      } else if (p === "/api/agent-roles" || p.endsWith("/agent-models")) {
        body = [
          {
            role_id: "manager",
            name: "主 Agent",
            responsibility: "统筹研究与交付",
            system_prompt: "受控提示词",
            enabled: true,
            version: 1,
            tools: ["finish_analysis"],
            default_binding: { provider_id: PROV_1, model_id: "fixture/Model-A", reasoning_effort: "high" },
            resolved: { provider_id: PROV_1, model_id: "fixture/Model-A", reasoning_effort: "high", binding_source: "global_default", effort_source: "global_default" },
          },
        ];
      } else if (p === "/api/accounts") {
        body = { accounts };
      } else if (p === `/api/accounts/${ACC_MAIN}`) {
        body = accounts[0];
      } else if (p === `/api/accounts/${ACC_EMPTY}`) {
        body = accounts[1];
      } else if (p === `/api/accounts/${ACC_MAIN}/analyses`) {
        body = { analyses: [{ id: REP_CURRENT, status: "current", input_version: 5 }] };
      } else if (p === `/api/accounts/${ACC_EMPTY}/analyses`) {
        body = { analyses: [] };
      } else if (p === `/api/analyses/${REP_CURRENT}`) {
        body = reportCurrent;
      } else if (p === `/api/accounts/${ACC_MAIN}/jobs`) {
        assert.ok(jobsList.every((item) => !("result" in item)), "List fixture must omit result");
        body = { jobs: jobsList };
      } else if (p === `/api/accounts/${ACC_EMPTY}/jobs`) {
        body = { jobs: [] };
      } else if (p === `/api/jobs/${JOB_FAILED}`) {
        body = failedJob;
      } else if (p === `/api/jobs/${JOB_DONE}`) {
        body = doneJob;
      } else if (p === `/api/jobs/${JOB_FAILED}/events`) {
        const after = Number(url.searchParams.get("after") || 0);
        body = { events: researchEvents.filter((event) => event.seq > after), next_after: 12, has_more: false };
      } else if (p === `/api/jobs/${JOB_DONE}/events`) {
        body = { events: [], next_after: 1, has_more: false };
      } else if (p === `/api/accounts/${ACC_MAIN}/recommendations`) {
        body = { items: [], total: 0, offset: 0, limit: 10, has_more: false, counts: { active: 0, history: 0 } };
      } else if (p === `/api/accounts/${ACC_EMPTY}/recommendations`) {
        body = { items: [], total: 0, offset: 0, limit: 10, has_more: false, counts: { active: 0, history: 0 } };
      } else if (p === "/api/materials") {
        body = url.searchParams.get("account_id") === ACC_MAIN ? { materials: [material] } : { materials: [] };
      } else if (p === `/api/materials/${MAT_1}/range`) {
        body = {
          id: MAT_1,
          title: material.title,
          access: "full_text",
          text: materialText,
          start: 0,
          end: materialText.length,
          total_chars: materialText.length,
          next_start: null,
        };
      } else if (p === "/api/universe") {
        body = {
          universe_version: 4,
          watchlist: ["SH:600519", "SZ:159599"],
          candidates: ["SH:600036"],
          monitored: ["SH:600519", "SZ:159599", "SH:600036"],
          quotes: [
            { symbol: "SH:600519", price: "1562.00", status: "fresh" },
            { symbol: "SZ:159599", price: "1.47", status: "fresh" },
          ],
        };
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
        body = {
          page: { items: [forecastRow, forecastBatchRow], total: 2, has_more: false, next_offset: null },
          statistics: {
            method: "按固定规则与期限分别统计；不会回填历史行情。",
            comparison_note: "不同证券或批次的准确率不能直接用于模型排名。",
            groups: [],
            attempt_statistics: { total: 2, states: [{ state: "published", count: 1, requested: 1, published: 1 }] },
          },
        };
      } else if (p === `/api/forecasts/${FORECAST_1}`) {
        body = forecastRow;
      } else if (p === "/api/forecast-batches") {
        body = [forecastBatch];
      } else if (p === `/api/forecast-batches/${BATCH_1}`) {
        body = forecastBatch;
      } else if (p === "/api/forecast-window-runs") {
        body = { runs: [] };
      } else if (p === `/api/accounts/${ACC_MAIN}/forecast-settings`) {
        body = {
          enabled: true,
          rule_version: "rule-2026-11-a:0123456789ab",
          horizons: [5, 20],
          labels: ["T+5", "T+20"],
          threshold: 0.005,
          start_time: "15:10",
          deadline_time: "16:30",
        };
      } else if (p === `/api/accounts/${ACC_EMPTY}/forecast-settings`) {
        body = { enabled: false, rule_version: "rule-empty", horizons: [5], labels: ["T+5"], threshold: 0.005, start_time: "15:10", deadline_time: "16:30" };
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

await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const base = `http://127.0.0.1:${server.address().port}`;

const evidence = {
  kind: "isolated_static_fixture_chromium",
  realModelCalls: 0,
  viewports: {},
  ac1: {},
  ac2: {},
  ac3: {},
  ac4: {},
  ac5: {},
  ac6: {},
  ac7: {},
  screenshots: {},
  errors: [],
};

let browser;
try {
  browser = await chromium.launch({
    headless: true,
    ...(process.env.BROWSER_EXECUTABLE ? { executablePath: process.env.BROWSER_EXECUTABLE } : {}),
  });
  evidence.browser = browser.version();
  await mkdir("test-results/twentieth", { recursive: true });

  const errors = [];
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  page.on("pageerror", (err) => errors.push(`pageerror: ${err.message}`));
  page.on("console", (msg) => {
    if (msg.type() === "error") errors.push(`console.error: ${msg.text()}`);
  });
  await page.addInitScript((id) => localStorage.setItem("guanlan.account", id), ACC_MAIN);

  const dialogState = () =>
    page.evaluate(() => {
      const dialog = document.querySelector("#dialog");
      if (!dialog) return { exists: false };
      const rect = dialog.getBoundingClientRect();
      const content = dialog.querySelector(".dialog-content");
      return {
        exists: true,
        open: dialog.open,
        size: dialog.dataset.size,
        width: rect.width,
        title: dialog.querySelector("#dialog-title")?.textContent || "",
        scrollWidth: dialog.scrollWidth,
        clientWidth: dialog.clientWidth,
        contentScrollWidth: content?.scrollWidth ?? null,
        contentClientWidth: content?.clientWidth ?? null,
        bodyOverflowX: getComputedStyle(dialog).overflowX,
      };
    });

  const closeDialog = async () => {
    const closed = page.evaluate(
      () =>
        new Promise((resolve) => {
          const dialog = document.querySelector("#dialog");
          if (!dialog.open) return resolve("already-closed");
          dialog.addEventListener("close", () => resolve("closed"), { once: true });
          setTimeout(() => resolve("timeout"), 5000);
        }),
    );
    await page.locator('#dialog [data-action="close-dialog"]').first().click();
    const outcome = await closed;
    assert.notEqual(outcome, "timeout", "Dialog close event did not fire");
    await page.waitForFunction(() => !document.querySelector("#dialog")?.open);
  };

  const openTaskDialog = async () => {
    await page.locator(`#app [data-task-id="${JOB_FAILED}"] [data-action="view-task"]`).first().click();
    await page.locator("#dialog[open]").waitFor();
    await page.waitForFunction(() => document.querySelector("#dialog-title")?.textContent === "研究任务");
  };

  const waitLoading = async () => {
    await page.locator(".loading-line").waitFor({ state: "hidden" });
  };

  // 0. Agent workbench main page (1440x900)
  await page.goto(base + "/#agents");
  await page.locator(`#app [data-task-id="${JOB_FAILED}"]`).waitFor();
  await page.locator(".workbench-cache-card").waitFor();
  await waitLoading();

  const mainViewTaskCount = await page.locator('#app .job-card .job-actions [data-action="view-task"]').count();
  assert.ok(mainViewTaskCount >= 1, `Workbench main page must keep view-task buttons, got ${mainViewTaskCount}`);
  evidence.ac5.mainViewTaskCount = mainViewTaskCount;

  await page.screenshot({ path: "test-results/twentieth/agents-workbench-desktop.png", fullPage: true });

  // 1. Open the research task dialog on the failed job (wide)
  await openTaskDialog();
  const wideOpen = await dialogState();
  assert.equal(wideOpen.title, "研究任务");
  assert.equal(wideOpen.size, "wide", `AC-1: research task dialog must be wide, got ${wideOpen.size}`);
  assert.ok(wideOpen.width >= 760 && wideOpen.width <= 830, `AC-1: wide dialog width out of range: ${wideOpen.width}`);
  evidence.ac1.taskDialogDesktop = wideOpen;
  evidence.viewports.desktop = { width: 1440, height: 900 };

  // 2. AC-2: no nested double border on the job card inside the dialog
  const ac2 = await page.evaluate(() => {
    const card = document.querySelector("#dialog .job-card");
    const delivery = document.querySelector("#dialog .job-delivery");
    const cardStyle = getComputedStyle(card);
    const deliveryStyle = getComputedStyle(delivery);
    return {
      borderLeft: cardStyle.borderLeftWidth,
      borderRight: cardStyle.borderRightWidth,
      borderTop: cardStyle.borderTopWidth,
      boxShadow: cardStyle.boxShadow,
      background: cardStyle.backgroundColor,
      deliveryPaddingLeft: deliveryStyle.paddingLeft,
      deliveryPaddingRight: deliveryStyle.paddingRight,
      deliveryPadding: deliveryStyle.padding,
    };
  });
  assert.equal(ac2.borderLeft, "0px", `AC-2: dialog job-card left border must be 0, got ${ac2.borderLeft}`);
  assert.equal(ac2.borderRight, "0px", `AC-2: dialog job-card right border must be 0, got ${ac2.borderRight}`);
  assert.equal(ac2.boxShadow, "none", `AC-2: dialog job-card box-shadow must be none, got ${ac2.boxShadow}`);
  assert.equal(ac2.deliveryPaddingLeft, "0px", `AC-2: job-delivery left padding must be 0, got ${ac2.deliveryPaddingLeft}`);
  assert.equal(ac2.deliveryPaddingRight, "0px", `AC-2: job-delivery right padding must be 0, got ${ac2.deliveryPaddingRight}`);
  evidence.ac2 = ac2;

  // 3. AC-3: independent demand card, inset items, long chip stays inside
  await page.locator("#dialog .task-demands > summary").click();
  await page.locator("#dialog .task-demand-item").first().waitFor();
  const ac3 = await page.evaluate(() => {
    const demands = document.querySelector("#dialog .task-demands");
    const summary = demands.querySelector(":scope > summary");
    const items = [...demands.querySelectorAll(".task-demand-item")];
    const first = items[0];
    const longItem = demands.querySelector('[data-read-key="demand:demand-close-20261126"]');
    const chip = longItem?.querySelector(".demand-key-chip");
    const ds = getComputedStyle(demands);
    const ss = getComputedStyle(summary);
    const cs = chip ? getComputedStyle(chip) : null;
    const rect = (node) => {
      const r = node.getBoundingClientRect();
      return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width };
    };
    return {
      borderTopWidth: ds.borderTopWidth,
      borderRightWidth: ds.borderRightWidth,
      borderBottomWidth: ds.borderBottomWidth,
      borderLeftWidth: ds.borderLeftWidth,
      borderRadius: ds.borderRadius,
      background: ds.backgroundColor,
      summaryPaddingLeft: parseFloat(ss.paddingLeft),
      summaryPaddingRight: parseFloat(ss.paddingRight),
      demandCount: items.length,
      demands: rect(demands),
      firstItem: rect(first),
      chip: chip ? { ...rect(chip), text: chip.textContent, overflowWrap: cs.overflowWrap, left: rect(chip).left, right: rect(chip).right } : null,
      itemInnerSpacingLeft: rect(first).left - rect(demands).left,
      itemInnerSpacingRight: rect(demands).right - rect(first).right,
    };
  });
  assert.notEqual(ac3.borderTopWidth, "0px", "AC-3: task-demands needs its own border");
  assert.notEqual(ac3.borderLeftWidth, "0px", "AC-3: task-demands needs its own left border");
  assert.notEqual(ac3.borderRadius, "0px", "AC-3: task-demands needs rounded corners");
  assert.ok(ac3.summaryPaddingLeft >= 12, `AC-3: summary left padding must be >= 12, got ${ac3.summaryPaddingLeft}`);
  assert.ok(ac3.summaryPaddingRight >= 12, `AC-3: summary right padding must be >= 12, got ${ac3.summaryPaddingRight}`);
  assert.ok(ac3.demandCount >= 3, `AC-3: expected >= 3 demands, got ${ac3.demandCount}`);
  assert.ok(ac3.firstItem.left > ac3.demands.left + 8, `AC-3: demand item too close to left edge (${ac3.firstItem.left} vs ${ac3.demands.left})`);
  assert.ok(ac3.firstItem.right < ac3.demands.right - 8, `AC-3: demand item too close to right edge (${ac3.firstItem.right} vs ${ac3.demands.right})`);
  assert.ok(ac3.chip, "AC-3: long demand chip must exist");
  assert.ok(ac3.chip.right <= ac3.firstItem.right + 1, `AC-3: long demand chip overflows its card (${ac3.chip.right} > ${ac3.firstItem.right})`);
  assert.ok(ac3.chip.text.includes(CLOSE_EVENT_KEY), "AC-3: chip must keep the long event_key readable");
  evidence.ac3 = ac3;

  // 4. AC-4: no native black triangles, CSS chevrons, readable timeline
  const chevrons = await page.evaluate(() => {
    const summaryOf = (selector) => {
      const node = document.querySelector(selector);
      if (!node) return null;
      const own = getComputedStyle(node);
      const after = getComputedStyle(node, "::after");
      return {
        listStyleType: own.listStyleType,
        afterContent: after.content,
        afterWidth: after.width,
        afterHeight: after.height,
        afterBorderRight: after.borderRightWidth,
      };
    };
    return {
      event: summaryOf("#dialog .job-event-details > summary"),
      checkpoint: summaryOf("#dialog .job-checkpoint-details > summary"),
      eventText: document.querySelector("#dialog .job-event-details > summary")?.textContent || "",
      checkpointText: document.querySelector("#dialog .job-checkpoint-details > summary")?.textContent || "",
      checkpointBorder: getComputedStyle(document.querySelector("#dialog .job-checkpoint-details")).borderTopWidth,
    };
  });
  assert.ok(chevrons.event, "AC-4: job-event-details summary must exist");
  assert.ok(chevrons.checkpoint, "AC-4: job-checkpoint-details summary must exist");
  assert.equal(chevrons.event.listStyleType, "none", `AC-4: event summary still shows native marker: ${chevrons.event.listStyleType}`);
  assert.equal(chevrons.checkpoint.listStyleType, "none", `AC-4: checkpoint summary still shows native marker: ${chevrons.checkpoint.listStyleType}`);
  assert.notEqual(chevrons.event.afterContent, "none", `AC-4: event summary ::after content missing: ${chevrons.event.afterContent}`);
  assert.notEqual(chevrons.event.afterContent, "normal", `AC-4: event summary ::after content invalid: ${chevrons.event.afterContent}`);
  assert.notEqual(chevrons.checkpoint.afterContent, "none", `AC-4: checkpoint summary ::after content missing: ${chevrons.checkpoint.afterContent}`);
  assert.ok(chevrons.eventText.includes("研究事件"), chevrons.eventText);
  evidence.ac4.chevrons = chevrons;

  // Expand the checkpoint to prove the unified chevron survives real content
  await page.locator("#dialog .job-checkpoint-details > summary").click();
  await page.locator("#dialog .job-checkpoint-details[open]").waitFor();
  const checkpointOpen = await page.evaluate(() => {
    const node = document.querySelector("#dialog .job-checkpoint-details");
    const summary = node.querySelector(":scope > summary");
    return {
      borderTopWidth: getComputedStyle(node).borderTopWidth,
      borderRadius: getComputedStyle(node).borderRadius,
      afterContent: getComputedStyle(summary, "::after").content,
      text: node.textContent.replace(/\s+/g, " ").trim().slice(0, 160),
    };
  });
  assert.notEqual(checkpointOpen.afterContent, "none", "AC-4: checkpoint chevron lost after opening");
  assert.equal(checkpointOpen.borderTopWidth, "1px", `AC-4: checkpoint box border missing: ${checkpointOpen.borderTopWidth}`);
  evidence.ac4.checkpointOpen = checkpointOpen;

  // Expand the timeline and wait for the real event payloads
  await page.locator("#dialog .job-event-details > summary").click();
  await page.waitForFunction(() => {
    const summary = document.querySelector("#dialog .job-event-details > summary");
    return summary && summary.textContent.includes("研究事件 · 已读取");
  });
  await page.waitForFunction(() => document.querySelectorAll("#dialog .job-events .event").length === 12);
  const openedSummary = await page.evaluate(() => {
    const summary = document.querySelector("#dialog .job-event-details > summary");
    return {
      text: summary.textContent,
      listStyleType: getComputedStyle(summary).listStyleType,
      afterContent: getComputedStyle(summary, "::after").content,
    };
  });
  assert.ok(openedSummary.text.includes("研究事件 · 已读取"), openedSummary.text);
  assert.ok(openedSummary.text.includes("12 条"), openedSummary.text);
  assert.equal(openedSummary.listStyleType, "none");
  assert.notEqual(openedSummary.afterContent, "none");
  evidence.ac4.openedSummary = openedSummary;

  const timeline = await page.evaluate(() => {
    const events = [...document.querySelectorAll("#dialog .job-events .event")];
    const eventText = (node) => node.textContent.replace(/\s+/g, " ").trim();
    const byType = (label) => events.find((node) => node.querySelector(".event-type")?.textContent.includes(label));
    const bodyFont = (node) => {
      const p = node.querySelector(":scope > p");
      return p ? { fontSize: parseFloat(getComputedStyle(p).fontSize), text: p.textContent } : null;
    };
    const errorEvent = byType("模型请求异常");
    const boundEvent = byType("模型配置已绑定");
    const startedEvent = byType("任务开始");
    const contextEvent = byType("研究上下文已就绪");
    const submissionEvent = byType("提交协议未满足");
    return {
      count: events.length,
      labels: events.map((node) => node.querySelector(".event-type")?.textContent || ""),
      text: events.map(eventText).join(" | "),
      errorClass: errorEvent?.classList.contains("is-error") ?? false,
      errorBody: errorEvent ? bodyFont(errorEvent) : null,
      boundBody: boundEvent ? bodyFont(boundEvent) : null,
      startedBodyPCount: startedEvent ? startedEvent.querySelectorAll(":scope > p").length : -1,
      startedText: startedEvent ? eventText(startedEvent) : "",
      contextBodyPCount: contextEvent ? contextEvent.querySelectorAll(":scope > p").length : -1,
      contextText: contextEvent ? eventText(contextEvent) : "",
      submissionErrorClass: submissionEvent?.classList.contains("is-error") ?? false,
      actorBadges: events.map((node) => node.querySelector(".event-actor")?.textContent || ""),
      timeAlignedRight: (() => {
        const time = events[0]?.querySelector("time");
        return time ? getComputedStyle(time).marginLeft : null;
      })(),
    };
  });
  assert.equal(timeline.count, 12, `AC-4: expected 12 rendered events, got ${timeline.count}`);
  assert.ok(timeline.labels.includes("模型配置已绑定"), `AC-4: missing Chinese label 模型配置已绑定: ${timeline.labels}`);
  assert.ok(timeline.labels.includes("任务开始"), "AC-4: missing Chinese label 任务开始");
  assert.ok(timeline.labels.includes("研究上下文已就绪"), "AC-4: missing Chinese label 研究上下文已就绪");
  assert.ok(timeline.labels.includes("模型请求异常"), "AC-4: missing Chinese label 模型请求异常");
  assert.equal(timeline.startedBodyPCount, 0, "AC-4: agent_started must not duplicate raw English body text");
  assert.ok(!timeline.startedText.includes("agent_started"), "AC-4: raw agent_started leaked into body");
  assert.equal(timeline.contextBodyPCount, 0, "AC-4: research_context_prepared must not duplicate raw English body text");
  assert.ok(!timeline.contextText.includes("research_context_prepared"), "AC-4: raw research_context_prepared leaked into body");
  assert.ok(timeline.boundBody && timeline.boundBody.fontSize >= 12, `AC-4: bound event body font too small: ${timeline.boundBody?.fontSize}`);
  assert.ok(timeline.errorBody && timeline.errorBody.fontSize >= 12, `AC-4: error event body font too small: ${timeline.errorBody?.fontSize}`);
  assert.ok(timeline.boundBody.text.includes("模型已绑定"), timeline.boundBody.text);
  assert.ok(timeline.errorBody.text.includes("模型请求在 120s 内未返回"), timeline.errorBody.text);
  assert.equal(timeline.errorClass, true, "AC-4: model_error must carry .event.is-error");
  assert.equal(timeline.submissionErrorClass, true, "AC-4: terminal_submission_rejected must carry .event.is-error");
  evidence.ac4.timeline = timeline;

  // F4: mapped-but-bodyless and unmapped events must not duplicate raw bodies.
  const extraEvents = await page.evaluate(() => {
    const events = [...document.querySelectorAll("#dialog .job-events .event")];
    const byExactType = (label) => events.find((node) => node.querySelector(".event-type")?.textContent === label);
    const bodyCount = (node) => (node ? node.querySelectorAll(":scope > .event-body").length : -1);
    const closed = byExactType("模型连接已关闭");
    const unmapped = byExactType("custom_unmapped_ping");
    const usage = byExactType("模型请求诊断");
    return {
      closedFound: Boolean(closed),
      closedBodyCount: bodyCount(closed),
      unmappedFound: Boolean(unmapped),
      unmappedLabel: unmapped?.querySelector(".event-type")?.textContent ?? "",
      unmappedBodyCount: bodyCount(unmapped),
      usageFound: Boolean(usage),
      usageBodyCount: bodyCount(usage),
    };
  });
  assert.equal(extraEvents.closedFound, true, "F4: model_client_closed event must render");
  assert.equal(extraEvents.closedBodyCount, 0, `F4: model_client_closed must not render .event-body, got ${extraEvents.closedBodyCount}`);
  assert.equal(extraEvents.unmappedFound, true, "F4: unmapped event must render");
  assert.equal(extraEvents.unmappedLabel, "custom_unmapped_ping", `F4: unmapped event must fall back to its raw type, got ${extraEvents.unmappedLabel}`);
  assert.equal(extraEvents.unmappedBodyCount, 0, `F4: unmapped event must not render .event-body, got ${extraEvents.unmappedBodyCount}`);
  assert.equal(extraEvents.usageFound, true, "F5: model_usage event must render");
  assert.equal(extraEvents.usageBodyCount, 0, `F5: model_usage event must not duplicate request text as .event-body, got ${extraEvents.usageBodyCount}`);
  evidence.ac4.extraEvents = extraEvents;

  // F5: the raw payload box is the bordered 7px surface; the nested model-usage
  // <details> must stay a plain surface so its summary draws no second divider.
  const usageEventLocator = page.locator("#dialog .job-events .event").filter({ hasText: "模型请求诊断" }).first();
  const usageRawDetails = usageEventLocator.locator(":scope > details.event-raw-details");
  assert.equal(await usageRawDetails.count(), 1, "F5: model_usage event must keep exactly one raw payload details box");
  assert.equal((await usageRawDetails.locator(":scope > summary").textContent()).trim(), "查看事件与来源详情");
  const usageRawBox = await usageRawDetails.evaluate((node) => {
    const style = getComputedStyle(node);
    return {
      hasRawClass: node.classList.contains("event-raw-details"),
      borderTopWidth: style.borderTopWidth,
      borderRightWidth: style.borderRightWidth,
      borderBottomWidth: style.borderBottomWidth,
      borderLeftWidth: style.borderLeftWidth,
      borderRadius: style.borderRadius,
    };
  });
  assert.equal(usageRawBox.hasRawClass, true, "F5: raw payload box must carry .event-raw-details");
  assert.equal(usageRawBox.borderTopWidth, "1px", `F5: raw payload box border missing: ${usageRawBox.borderTopWidth}`);
  assert.equal(usageRawBox.borderLeftWidth, "1px", `F5: raw payload box left border missing: ${usageRawBox.borderLeftWidth}`);
  assert.equal(usageRawBox.borderRadius, "7px", `F5: raw payload box radius must be 7px, got ${usageRawBox.borderRadius}`);

  const usageDetails = usageEventLocator.locator(":scope > details.model-usage");
  assert.equal(await usageDetails.count(), 1, "F5: model_usage event must render modelUsageView details");
  const usageHasRawClass = await usageDetails.evaluate((node) => node.classList.contains("event-raw-details"));
  assert.equal(usageHasRawClass, false, "F5: modelUsageView details must not reuse .event-raw-details");
  await usageDetails.locator(":scope > summary").click();
  await page.waitForFunction(() => {
    const node = [...document.querySelectorAll("#dialog .job-events .event")]
      .find((event) => event.querySelector(".event-type")?.textContent === "模型请求诊断");
    return Boolean(node?.querySelector(":scope > details.model-usage[open]"));
  });
  const usageSummary = await usageDetails.evaluate((node) => {
    const summary = node.querySelector(":scope > summary");
    const style = getComputedStyle(summary);
    return {
      open: node.open,
      text: summary.textContent,
      borderBottomWidth: style.borderBottomWidth,
      borderTopWidth: style.borderTopWidth,
    };
  });
  assert.equal(usageSummary.open, true, "F5: model usage details must open");
  assert.ok(usageSummary.text.includes("模型调用与缓存"), usageSummary.text);
  assert.equal(usageSummary.borderBottomWidth, "0px", `F5: model usage summary must not draw a second divider, got ${usageSummary.borderBottomWidth}`);
  evidence.ac4.usageDetails = { raw: usageRawBox, summary: usageSummary };

  // Event payload details box: bordered rounded box, pre visible, no horizontal overflow
  const errorEventLocator = page.locator("#dialog .job-events .event").filter({ hasText: "模型请求异常" }).first();
  const payloadDetails = errorEventLocator.locator(":scope > details");
  const payloadSummary = payloadDetails.locator(":scope > summary");
  assert.equal((await payloadSummary.textContent()).trim(), "查看事件与来源详情");
  const payloadClosed = await payloadDetails.evaluate((node) => {
    const style = getComputedStyle(node);
    return { borderTopWidth: style.borderTopWidth, borderRadius: style.borderRadius, background: style.backgroundColor };
  });
  assert.equal(payloadClosed.borderTopWidth, "1px", `AC-4: event details border missing: ${payloadClosed.borderTopWidth}`);
  assert.notEqual(payloadClosed.borderRadius, "0px", "AC-4: event details needs rounded corners");
  await payloadSummary.click();
  const payloadPre = payloadDetails.locator("pre");
  await payloadPre.waitFor({ state: "visible" });
  const payloadPreBox = await payloadPre.boundingBox();
  const payloadOpen = await payloadPre.evaluate((pre) => {
    const dialog = document.querySelector("#dialog");
    const content = dialog.querySelector(".dialog-content");
    const rect = pre.getBoundingClientRect();
    return {
      preTextStart: pre.textContent.slice(0, 120),
      preWidth: rect.width,
      preRight: rect.right,
      preScrollWidth: pre.scrollWidth,
      preClientWidth: pre.clientWidth,
      preOverflowX: getComputedStyle(pre).overflowX,
      dialogContentScrollWidth: content.scrollWidth,
      dialogContentClientWidth: content.clientWidth,
      dialogScrollWidth: dialog.scrollWidth,
      dialogClientWidth: dialog.clientWidth,
    };
  });
  payloadOpen.preBox = payloadPreBox;
  assert.ok(payloadOpen.preTextStart.includes("model_timeout"), `AC-4: opened payload JSON must show real fields: ${payloadOpen.preTextStart}`);
  assert.ok(
    payloadOpen.dialogContentScrollWidth <= payloadOpen.dialogContentClientWidth + 1,
    `AC-4: opened event details overflow dialog content (${payloadOpen.dialogContentScrollWidth} > ${payloadOpen.dialogContentClientWidth})`,
  );
  evidence.ac4.payload = { ...payloadClosed, ...payloadOpen };

  // 5. AC-5: dialog actions must not self-reference view-task
  const dialogActions = await page.evaluate(() => {
    const actions = document.querySelector("#dialog .job-actions");
    return {
      viewTask: actions.querySelectorAll('[data-action="view-task"]').length,
      retry: actions.querySelectorAll('[data-action="retry-job"]').length,
      result: actions.querySelectorAll('[data-action="job-result"]').length,
      labels: [...actions.querySelectorAll("button")].map((node) => node.textContent.trim()),
    };
  });
  assert.equal(dialogActions.viewTask, 0, "AC-5: dialog must not repeat 查看任务详情");
  assert.equal(dialogActions.retry, 1, "AC-5: retry must remain available in the dialog");
  assert.equal(dialogActions.result, 1, "AC-5: job result button must remain available in the dialog");
  evidence.ac5.dialogActions = dialogActions;

  // 6. Desktop screenshots of the task dialog
  // A taller desktop viewport captures the overview, expanded demands and the
  // timeline start in one frame; the second shot covers the deep timeline,
  // opened event JSON and the deduplicated action bar.
  await page.setViewportSize({ width: 1440, height: 1700 });
  await page.evaluate(() => {
    document.querySelector("#dialog .dialog-content").scrollTop = 0;
  });
  await page.waitForTimeout(120);
  await page.screenshot({ path: "test-results/twentieth/task-dialog-desktop.png" });
  await page.setViewportSize({ width: 1440, height: 900 });

  await page.evaluate(() => {
    const content = document.querySelector("#dialog .dialog-content");
    content.scrollTop = content.scrollHeight;
  });
  await page.waitForTimeout(120);
  await page.screenshot({ path: "test-results/twentieth/task-dialog-desktop-events.png" });

  // 6b. F3: switching an already-open wide dialog to a compact dialog must clear
  // data-size without closing the modal. A modal makes page-level controls inert,
  // so the compact trigger (删除当前虚拟账户) is dispatched through the page's own
  // click handler instead of a pointer click; a recorded close counter proves the
  // dialog was never closed in between.
  const wideBeforeSwitch = await dialogState();
  assert.equal(wideBeforeSwitch.open, true, "F3: precondition, task dialog must still be open");
  assert.equal(wideBeforeSwitch.size, "wide", `F3: precondition, task dialog must still be wide, got ${wideBeforeSwitch.size}`);
  await page.evaluate(() => {
    window.__dialogCloseEvents = 0;
    document.querySelector("#dialog").addEventListener("close", () => {
      window.__dialogCloseEvents += 1;
    });
    location.hash = "holdings";
  });
  await page.locator('[data-action="delete-account"]').waitFor();
  await page.evaluate(() => document.querySelector('[data-action="delete-account"]').click());
  await page.waitForFunction(() => document.querySelector("#dialog-title")?.textContent === "删除当前虚拟账户");
  const wideToCompact = await page.evaluate(() => {
    const dialog = document.querySelector("#dialog");
    const rect = dialog.getBoundingClientRect();
    return {
      open: dialog.open,
      size: dialog.dataset.size,
      hasSizeAttribute: dialog.hasAttribute("data-size"),
      width: rect.width,
      closes: window.__dialogCloseEvents,
      title: document.querySelector("#dialog-title")?.textContent || "",
    };
  });
  assert.equal(wideToCompact.open, true, "F3: dialog must stay open when switching wide to compact");
  assert.equal(wideToCompact.closes, 0, `F3: dialog must not be closed while switching, got ${wideToCompact.closes} close events`);
  assert.equal(wideToCompact.hasSizeAttribute, false, "F3: data-size must be removed on the compact dialog");
  assert.equal(wideToCompact.size, undefined, `F3: data-size must be cleared on the compact dialog, got ${wideToCompact.size}`);
  assert.ok(wideToCompact.width >= 460 && wideToCompact.width <= 560, `F3: compact width must stay near 510px, got ${wideToCompact.width}`);
  assert.equal(wideToCompact.title, "删除当前虚拟账户");
  evidence.ac1.wideToCompactWithoutClose = wideToCompact;
  await closeDialog();

  // 7. AC-1: wide size is cleared on close and never leaks into compact dialogs
  const afterClose = await dialogState();
  assert.equal(afterClose.size, undefined, `AC-1: dataset.size must be cleared after close, got ${afterClose.size}`);
  evidence.ac1.afterClose = afterClose;

  await page.locator('nav a[href="#holdings"]').click();
  await page.locator('[data-action="delete-account"]').waitFor();
  await waitLoading();
  await page.locator('[data-action="delete-account"]').click();
  await page.locator("#dialog[open]").waitFor();
  const compact = await dialogState();
  assert.equal(compact.size, undefined, `AC-1: compact dialog must not inherit wide size, got ${compact.size}`);
  assert.ok(compact.width <= 520, `AC-1: compact dialog width must be <= 520, got ${compact.width}`);
  assert.equal(compact.title, "删除当前虚拟账户");
  evidence.ac1.compactDeleteAccount = compact;
  await closeDialog();

  // Re-open the wide dialog after a compact one: wide must be re-applied cleanly
  await page.locator('nav a[href="#agents"]').click();
  await page.locator(`#app [data-task-id="${JOB_FAILED}"]`).waitFor();
  await waitLoading();
  await openTaskDialog();
  const reopened = await dialogState();
  assert.equal(reopened.size, "wide", `AC-1: wide must be re-applied after compact dialog, got ${reopened.size}`);
  assert.ok(reopened.width >= 760 && reopened.width <= 830, `AC-1: reopened width out of range: ${reopened.width}`);
  evidence.ac1.reopenedWide = reopened;
  await closeDialog();

  // 8. AC-6: every rich detail dialog is wide
  const richDialogs = [];
  const captureWide = async (label, expectedTitle) => {
    await page.locator("#dialog[open]").waitFor();
    await page.waitForFunction(
      (title) => document.querySelector("#dialog-title")?.textContent === title,
      expectedTitle,
    );
    const state = await dialogState();
    assert.equal(state.size, "wide", `AC-6: ${label} must be wide, got ${state.size}`);
    assert.ok(state.width >= 760, `AC-6: ${label} width must be >= 760, got ${state.width}`);
    assert.ok(state.scrollWidth <= state.clientWidth + 1, `AC-6: ${label} horizontal overflow ${state.scrollWidth} > ${state.clientWidth}`);
    richDialogs.push({ label, ...state });
    return state;
  };

  await page.locator(`#app [data-task-id="${JOB_FAILED}"] [data-action="job-result"]`).click();
  const jobResultDialog = await captureWide("任务结果与可观察统计", "任务结果与可观察统计");
  assert.ok((await page.locator("#dialog").textContent()).includes("失败诊断"));
  await closeDialog();

  await page.locator(`#app [data-task-id="${JOB_DONE}"] [data-action="open-report"]`).click();
  await captureWide("研究报告", "研究报告");
  await page.screenshot({ path: "test-results/twentieth/report-dialog-wide.png" });
  await closeDialog();

  await page.locator('nav a[href="#research"]').click();
  await page.locator("#material-search-form").waitFor();
  await page.locator('#app [data-action="open-material"]:visible').first().click();
  const materialDialog = await captureWide("研究证据 · 已保存材料", "研究证据 · 已保存材料");
  assert.ok((await page.locator("#dialog").textContent()).includes("贵州茅台"));
  await closeDialog();

  await page.locator('nav a[href="#forecasts"]').click();
  await page.locator("#forecast-records").waitFor();
  await page.locator('[data-sixth-action="forecast-detail"]').first().click();
  const forecastDialog = await captureWide("不可变预测 · 报告与评估修订", "不可变预测 · 报告与评估修订");
  await closeDialog();

  await page.locator('[data-sixth-action="forecast-batch-detail"]').first().click();
  const forecastBatchDialog = await captureWide("批次 · 配置归属与执行尝试", "批次 · 配置归属与执行尝试");
  await closeDialog();

  assert.equal(richDialogs.length, 5, `AC-6: expected 5 rich dialogs, got ${richDialogs.length}`);
  evidence.ac6 = {
    dialogs: richDialogs,
    jobResultDialog,
    materialDialog,
    forecastDialog,
    forecastBatchDialog,
  };

  // 9. AC-7: 390px mobile task dialog without horizontal overflow or clipped cards
  await page.setViewportSize({ width: 390, height: 844 });
  evidence.viewports.mobile = { width: 390, height: 844 };
  await page.locator('nav a[href="#agents"]').click();
  await page.locator(`#app [data-task-id="${JOB_FAILED}"]`).waitFor();
  await waitLoading();
  await openTaskDialog();
  await page.locator("#dialog .task-demands > summary").click();
  await page.locator("#dialog .task-demand-item").first().waitFor();
  await page.locator("#dialog .job-event-details > summary").click();
  await page.waitForFunction(() => {
    const summary = document.querySelector("#dialog .job-event-details > summary");
    return summary && summary.textContent.includes("研究事件 · 已读取");
  });
  await page.waitForFunction(() => document.querySelectorAll("#dialog .job-events .event").length === 12);

  const mobile = await page.evaluate(() => {
    const dialog = document.querySelector("#dialog");
    const content = dialog.querySelector(".dialog-content");
    const rect = (node) => {
      const r = node.getBoundingClientRect();
      return { left: r.left, right: r.right, width: r.width };
    };
    const items = [...dialog.querySelectorAll(".task-demand-item")];
    const events = [...dialog.querySelectorAll(".job-events .event")];
    return {
      innerWidth: window.innerWidth,
      dialogScrollWidth: dialog.scrollWidth,
      dialogClientWidth: dialog.clientWidth,
      contentScrollWidth: content.scrollWidth,
      contentClientWidth: content.clientWidth,
      dialogWidth: dialog.getBoundingClientRect().width,
      itemCount: items.length,
      eventCount: events.length,
      items: items.map(rect),
      events: events.map(rect),
    };
  });
  assert.ok(mobile.dialogScrollWidth <= mobile.dialogClientWidth + 1, `AC-7: dialog overflows (${mobile.dialogScrollWidth} > ${mobile.dialogClientWidth})`);
  assert.ok(mobile.contentScrollWidth <= mobile.contentClientWidth + 1, `AC-7: dialog content overflows (${mobile.contentScrollWidth} > ${mobile.contentClientWidth})`);
  assert.ok(mobile.itemCount >= 3, `AC-7: demand cards missing on mobile (${mobile.itemCount})`);
  assert.equal(mobile.eventCount, 12, `AC-7: events missing on mobile (${mobile.eventCount})`);
  for (const [index, item] of mobile.items.entries()) {
    assert.ok(item.left >= 0 && item.right <= mobile.innerWidth + 1, `AC-7: demand item ${index} clipped (${item.left}..${item.right} vs ${mobile.innerWidth})`);
  }
  for (const [index, event] of mobile.events.entries()) {
    assert.ok(event.left >= 0 && event.right <= mobile.innerWidth + 1, `AC-7: event ${index} clipped (${event.left}..${event.right} vs ${mobile.innerWidth})`);
  }
  evidence.ac7 = mobile;

  await page.evaluate(() => {
    const content = document.querySelector("#dialog .dialog-content");
    const summary = document.querySelector("#dialog .task-demands");
    content.scrollTop = Math.max(0, summary.offsetTop - 6);
  });
  await page.screenshot({ path: "test-results/twentieth/task-dialog-mobile-390.png" });
  await closeDialog();

  // 10. No JS errors, screenshots exist and are non-empty
  assert.deepEqual(errors, [], `Browser errors detected: ${errors.join("; ")}`);
  evidence.errors = errors;

  const screenshots = [
    "test-results/twentieth/task-dialog-desktop.png",
    "test-results/twentieth/task-dialog-desktop-events.png",
    "test-results/twentieth/agents-workbench-desktop.png",
    "test-results/twentieth/report-dialog-wide.png",
    "test-results/twentieth/task-dialog-mobile-390.png",
  ];
  for (const file of screenshots) {
    const info = await stat(file);
    assert.ok(info.size > 5000, `Screenshot ${file} unexpectedly small: ${info.size}`);
    evidence.screenshots[file] = info.size;
  }

  evidence.passed = true;
  await writeFile("test-results/twentieth/task-detail-evidence.json", JSON.stringify(evidence, null, 2));
  console.log(
    JSON.stringify({
      passed: true,
      viewports: evidence.viewports,
      taskDialogWidth: wideOpen.width,
      compactWidth: compact.width,
      richDialogs: richDialogs.map((item) => ({ label: item.label, width: item.width, size: item.size })),
      screenshots: Object.keys(evidence.screenshots).length,
      errors: errors.length,
    }),
  );
} finally {
  if (browser) await browser.close();
  server.close();
}
