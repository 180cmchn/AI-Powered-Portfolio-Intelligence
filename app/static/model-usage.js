// Rendering consumes server-normalized usage. It never infers a cache miss
// from an absent field or averages request percentages in the browser.
const escape = value => String(value ?? "").replace(/[&<>"']/g, c =>
  ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"})[c]);
const finite = value => typeof value === "number" && Number.isFinite(value);
const number = value => finite(value)
  ? value.toLocaleString("zh-CN", {maximumFractionDigits: 6}) : "未知";
const percent = value => finite(value)
  ? `${(value * 100).toLocaleString("zh-CN", {maximumFractionDigits: 1})}%` : "未知";
const kpiPercent = value => finite(value)
  ? `${(value * 100).toLocaleString("zh-CN", {maximumFractionDigits: 1})}%` : "未知";
const stage = value => ({
  research: "研究",
  synthesis: "汇总",
  final_submission: "最终提交",
  cold: "首次请求",
  continuation: "续接",
  probe: "连接探测",
})[value] || value || "未记录";
const reasonMap = {
  first: { label: "首次请求", tone: "neutral" },
  first_request: { label: "首次请求", tone: "neutral" },
  initial: { label: "首次请求", tone: "neutral" },
  append_only: { label: "尾部追加", tone: "good" },
  unchanged: { label: "输入未变", tone: "good" },
  compaction: { label: "上下文压缩", tone: "warn" },
  compression: { label: "上下文压缩", tone: "warn" },
  synthesis: { label: "进入汇总", tone: "neutral" },
  system_changed: { label: "系统提示变更", tone: "warn" },
  tools_changed: { label: "工具权限变化", tone: "warn" },
  tool_choice_changed: { label: "工具选择变化", tone: "warn" },
  model_changed: { label: "模型变更", tone: "warn" },
  effort_changed: { label: "思考强度变更", tone: "warn" },
  history_rewritten: { label: "历史重写", tone: "bad" },
  context_epoch_changed: { label: "上下文 epoch 变化", tone: "warn" },
  recovery_rebuild: { label: "恢复重建", tone: "warn" },
};
const reason = value => reasonMap[value]?.label || value || "未记录";
const reasonTone = value => reasonMap[value]?.tone || "neutral";
const category = value => ({
  failed: "失败请求",
  first_session_request: "会话首轮",
  first_epoch_request: "新 epoch 首轮",
  continuation: "正常续接",
})[value] || value || "未记录";
const agentTitle = value => ({
  manager: "主 Agent (manager)",
  probe: "连接探测 (probe)",
})[value] || value || "未记录";
const usage = request => request.normalized_usage || request;

function weightedRatioForRows(rows) {
  let inputSum = 0;
  let cacheReadSum = 0;
  let pairedCount = 0;
  for (const r of rows) {
    const u = usage(r);
    if (finite(u.input_tokens) && finite(u.cache_read_tokens)) {
      inputSum += u.input_tokens;
      cacheReadSum += u.cache_read_tokens;
      pairedCount += 1;
    }
  }
  return {
    totalCount: rows.length,
    pairedCount,
    inputTokens: pairedCount ? inputSum : null,
    cacheReadTokens: pairedCount ? cacheReadSum : null,
    ratio: pairedCount && inputSum > 0 ? cacheReadSum / inputSum : null,
  };
}

function createBucketAccumulator() {
  return { count: 0, paired: 0, reported_input_tokens: 0, cache_read_tokens: 0, hasTokens: false };
}

function addBucket(acc, bucket) {
  if (!bucket || typeof bucket !== "object") return;
  if (finite(bucket.count)) acc.count += bucket.count;
  if (finite(bucket.paired)) acc.paired += bucket.paired;
  if (finite(bucket.reported_input_tokens) && finite(bucket.cache_read_tokens)) {
    acc.reported_input_tokens += bucket.reported_input_tokens;
    acc.cache_read_tokens += bucket.cache_read_tokens;
    acc.hasTokens = true;
  }
}

function finalizeBucket(acc) {
  const hasPairedTokens = acc.paired > 0 && acc.hasTokens;
  return {
    count: acc.count,
    paired: acc.paired,
    reported_input_tokens: hasPairedTokens ? acc.reported_input_tokens : null,
    cache_read_tokens: hasPairedTokens ? acc.cache_read_tokens : null,
    weighted_read_ratio: hasPairedTokens && acc.reported_input_tokens > 0
      ? acc.cache_read_tokens / acc.reported_input_tokens
      : null,
  };
}

function breakdownStatsFromRows(rows) {
  const nonSearchAllRows = rows.filter(r => r.native_search_requested === false);
  const nonSearchCrossRows = nonSearchAllRows.filter(r =>
    r.prefix_diagnostic?.change_reason !== "first" &&
    r.cache_epoch_reason !== "first" &&
    r.request_category !== "first_session_request");
  const searchAllRows = rows.filter(r => r.native_search_requested === true);
  const toBucket = (list) => {
    const w = weightedRatioForRows(list);
    return {
      count: w.totalCount,
      paired: w.pairedCount,
      reported_input_tokens: w.inputTokens,
      cache_read_tokens: w.cacheReadTokens,
      weighted_read_ratio: w.ratio,
    };
  };
  return {
    non_search_all: toBucket(nonSearchAllRows),
    non_search_cross_turn: toBucket(nonSearchCrossRows),
    search_all: toBucket(searchAllRows),
    known_search_requests: nonSearchAllRows.length + searchAllRows.length,
    total_requests: rows.length,
  };
}

function breakdownStatsFromSummary(summary) {
  const b = summary?.native_search_breakdown;
  const totalRequests = summary?.cache_summary?.requests || 0;
  const empty = () => ({ count: 0, paired: 0, reported_input_tokens: null, cache_read_tokens: null, weighted_read_ratio: null });
  if (!b || typeof b !== "object") {
    return {
      non_search_all: empty(),
      non_search_cross_turn: empty(),
      search_all: empty(),
      known_search_requests: 0,
      total_requests: totalRequests,
    };
  }
  const norm = (bucket) => {
    const count = finite(bucket?.count) ? bucket.count : 0;
    const paired = finite(bucket?.paired) ? bucket.paired : 0;
    const inp = finite(bucket?.reported_input_tokens) ? bucket.reported_input_tokens : null;
    const read = finite(bucket?.cache_read_tokens) ? bucket.cache_read_tokens : null;
    const ratio = finite(bucket?.weighted_read_ratio)
      ? bucket.weighted_read_ratio
      : (paired > 0 && finite(inp) && inp > 0 && finite(read) ? read / inp : null);
    return { count, paired, reported_input_tokens: inp, cache_read_tokens: read, weighted_read_ratio: ratio };
  };
  const non_search_all = norm(b.non_search_all);
  const non_search_cross_turn = norm(b.non_search_cross_turn);
  const search_all = norm(b.search_all);
  return {
    non_search_all,
    non_search_cross_turn,
    search_all,
    known_search_requests: non_search_all.count + search_all.count,
    total_requests: totalRequests,
  };
}

export function formatSearchBreakdown(statsOrSummary) {
  const stats = statsOrSummary?.non_search_all ? statsOrSummary : breakdownStatsFromSummary(statsOrSummary);
  const allKnown = stats.total_requests > 0 && stats.known_search_requests >= stats.total_requests;
  const noneKnown = stats.known_search_requests === 0;
  const nonSearchAll = stats.non_search_all;
  const nonSearchCross = stats.non_search_cross_turn;
  const searchAll = stats.search_all;

  let nonSearchAllText = "未知";
  if (nonSearchAll.paired > 0 && finite(nonSearchAll.weighted_read_ratio)) {
    nonSearchAllText = kpiPercent(nonSearchAll.weighted_read_ratio);
  } else if (nonSearchAll.count > 0 || noneKnown || !allKnown) {
    nonSearchAllText = "未知";
  } else {
    nonSearchAllText = "暂无样本";
  }
  const nonSearchAllMeta = nonSearchAll.count > 0 ? `${nonSearchAll.paired}/${nonSearchAll.count} 次成对` : "";

  let nonSearchCrossValue = "未知";
  let nonSearchCrossMeta = "请求未附带联网标识";
  if (nonSearchCross.paired > 0 && finite(nonSearchCross.weighted_read_ratio)) {
    nonSearchCrossValue = kpiPercent(nonSearchCross.weighted_read_ratio);
    nonSearchCrossMeta = `${nonSearchCross.paired}/${nonSearchCross.count} 次续接成对 (non-search)`;
  } else if (nonSearchCross.count > 0) {
    nonSearchCrossValue = "未知";
    nonSearchCrossMeta = `0/${nonSearchCross.count} 次续接成对 · 计量缺失`;
  } else if (nonSearchAll.count > 0) {
    nonSearchCrossValue = "暂无续接";
    nonSearchCrossMeta = `仅首轮非联网 ${nonSearchAll.count} 次`;
  } else if (noneKnown || !allKnown) {
    nonSearchCrossValue = "未知";
    nonSearchCrossMeta = "请求未附带联网标识";
  } else {
    nonSearchCrossValue = "暂无样本";
    nonSearchCrossMeta = "当前样本均为原生联网";
  }

  let searchValue = "未知";
  let searchMeta = "请求未附带联网标识";
  if (searchAll.paired > 0 && finite(searchAll.weighted_read_ratio)) {
    searchValue = kpiPercent(searchAll.weighted_read_ratio);
    searchMeta = `${searchAll.paired}/${searchAll.count} 次联网成对 (web_search)`;
  } else if (searchAll.count > 0) {
    searchValue = "未知";
    searchMeta = `${searchAll.paired}/${searchAll.count} 次联网成对 · 计量缺失`;
  } else if (noneKnown || !allKnown) {
    searchValue = "未知";
    searchMeta = "请求未附带联网标识";
  } else {
    searchValue = "暂无样本";
    searchMeta = "未触发原生联网";
  }

  return {
    nonSearchAllText,
    nonSearchAllMeta,
    nonSearchCrossValue,
    nonSearchCrossMeta,
    searchValue,
    searchMeta,
  };
}

export function prefixDistHTML(prefixReasonCounts, emptyText = "") {
  const entries = prefixReasonCounts instanceof Map
    ? [...prefixReasonCounts.entries()]
    : Object.entries(prefixReasonCounts || {});
  const valid = entries.filter(([, count]) => finite(count) && count > 0);
  if (!valid.length) {
    return emptyText
      ? `<div class="prefix-dist-card"><div class="prefix-dist-head"><strong>前缀稳定性分布</strong><span class="small muted">${escape(emptyText)}</span></div></div>`
      : "";
  }
  let stableCount = 0;
  let unstableCount = 0;
  for (const [code, count] of valid) {
    const tone = reasonTone(code);
    if (tone === "good") stableCount += count;
    else if (tone === "warn" || tone === "bad") unstableCount += count;
  }
  return `<div class="prefix-dist-card">
    <div class="prefix-dist-head">
      <strong>前缀稳定性分布</strong>
      <span class="small muted">稳定续接 ${escape(stableCount)} 次 · 前缀变更/重写 ${escape(unstableCount)} 次</span>
    </div>
    <div class="prefix-dist-badges">${valid.map(([code, count]) => {
      const tone = reasonTone(code);
      const pillClass = tone === "good" ? "live" : tone === "bad" ? "error" : tone === "warn" ? "warning" : "neutral";
      return `<span class="pill ${pillClass}" data-prefix-reason="${escape(code)}">${escape(reason(code))} ×${escape(count)}</span>`;
    }).join("")}</div>
  </div>`;
}

export { kpiPercent };

function clampRatioPct(ratio) {
  if (!finite(ratio)) return 0;
  return Math.max(0, Math.min(100, ratio * 100));
}

function barHTML(ratio, tone = "teal") {
  const known = finite(ratio);
  const width = known ? clampRatioPct(ratio).toFixed(2) : "0";
  return `<div class="usage-bar-track ${known ? "" : "is-unknown"}" aria-hidden="true"><span class="usage-bar-fill tone-${escape(tone)}" style="width:${width}%"></span></div>`;
}

function enrichRowsWithRawRequests(rows, rawRequests) {
  if (!Array.isArray(rawRequests) || !rawRequests.length) return rows;
  const byId = new Map();
  for (const raw of rawRequests) {
    if (raw && raw.local_request_id) byId.set(raw.local_request_id, raw);
  }
  return rows.map(row => {
    const raw = row.local_request_id ? byId.get(row.local_request_id) : null;
    if (!raw) return row;
    return {
      ...row,
      native_search_requested: typeof row.native_search_requested === "boolean"
        ? row.native_search_requested
        : (typeof raw.native_search_requested === "boolean" ? raw.native_search_requested : row.native_search_requested),
      cache_epoch_reason: row.cache_epoch_reason ?? raw.cache_epoch_reason,
    };
  });
}

export function requestUsageText(request) {
  const u = usage(request);
  return `模型 ${request.model || request.model_name || "未记录"}：已发送 ${request.reasoning_effort_sent || request.reasoning_effort || "未知"}，服务回报 ${request.reported_reasoning_effort || "未知"}；输入 ${number(u.input_tokens)}，缓存读取 ${number(u.cache_read_tokens)}，未缓存输入 ${number(u.uncached_input_tokens)}，输出 ${number(u.output_tokens)}，推理 ${number(u.reasoning_tokens)}，可见输出 ${number(u.visible_output_tokens)}，缓存写入 ${number(u.cache_write_tokens)}；${stage(request.stage)}，${reason(request.prefix_diagnostic?.change_reason)}；费用${request.cost_known && request.cost != null ? "见服务计费原始记录" : "未知"}。`;
}

export function compactUsageStrip(summary) {
  if (!summary?.cache_summary) return "";
  const c = summary.cache_summary;
  const totals = summary.normalized_totals || {};
  const ratioText = percent(c.weighted_read_ratio);
  const ratioTone = !finite(c.weighted_read_ratio)
    ? "neutral"
    : c.weighted_read_ratio >= 0.5 ? "live" : c.weighted_read_ratio > 0 ? "warning" : "neutral";
  return `<div class="job-usage-strip"><span class="pill ${ratioTone}">加权缓存读取率 ${escape(ratioText)}</span><span class="pill neutral">${escape(c.requests)} 次请求 · 完整 ${escape(c.complete_requests)}/${escape(c.requests)}</span>${finite(totals.input_tokens) ? `<span class="pill neutral">输入 ${escape(number(totals.input_tokens))}</span>` : ""}${finite(totals.output_tokens) ? `<span class="pill neutral">输出 ${escape(number(totals.output_tokens))}</span>` : ""}${summary.duration_ms != null ? `<span class="pill neutral">${escape(number(summary.duration_ms))} ms</span>` : ""}${c.anomalous_requests ? `<span class="pill warning">异常计数 ${escape(c.anomalous_requests)}</span>` : ""}</div>`;
}

export function aggregateJobUsage(jobs, rawRequestsLookup = null) {
  const validJobs = (jobs || [])
    .map(j => ({ job: j, summary: j?.result?.model_usage || j?.model_usage_summary }))
    .filter(item => item.summary?.cache_summary);
  if (!validJobs.length) return null;
  let requests = 0, completeRequests = 0, unknownRequests = 0, anomalousRequests = 0;
  let reportedInput = 0, cacheRead = 0, durationMs = 0, hasDuration = false, hasPairedTokens = false;
  let inputKnown = 0, inputSum = 0, outputKnown = 0, outputSum = 0;
  const sequence = [];
  const prefixReasonCounts = {};
  const buckets = {
    non_search_all: createBucketAccumulator(),
    non_search_cross_turn: createBucketAccumulator(),
    search_all: createBucketAccumulator(),
  };
  let hasBreakdownSource = false;

  for (const { job, summary: s } of validJobs) {
    const c = s.cache_summary;
    requests += c.requests || 0;
    completeRequests += c.complete_requests || 0;
    unknownRequests += c.unknown_requests || 0;
    anomalousRequests += c.anomalous_requests || 0;
    if (finite(c.reported_input_tokens) && finite(c.cache_read_tokens)) {
      reportedInput += c.reported_input_tokens;
      cacheRead += c.cache_read_tokens;
      hasPairedTokens = true;
    }
    if (finite(s.duration_ms)) { durationMs += s.duration_ms; hasDuration = true; }
    const t = s.normalized_totals || {};
    if (finite(t.input_tokens)) { inputSum += t.input_tokens; inputKnown += t.known_requests?.input_tokens || 0; }
    if (finite(t.output_tokens)) { outputSum += t.output_tokens; outputKnown += t.known_requests?.output_tokens || 0; }

    if (Array.isArray(s.sequence) && s.sequence.length) {
      const rawReqs = typeof rawRequestsLookup === "function"
        ? rawRequestsLookup(job?.id)
        : (rawRequestsLookup instanceof Map ? rawRequestsLookup.get(job?.id) : null);
      const rows = enrichRowsWithRawRequests(s.sequence, rawReqs);
      sequence.push(...rows);
      for (const r of rows) {
        const code = r.prefix_diagnostic?.change_reason || r.cache_epoch_reason || "unknown";
        prefixReasonCounts[code] = (prefixReasonCounts[code] || 0) + 1;
      }
      const rowBreakdown = breakdownStatsFromRows(rows);
      addBucket(buckets.non_search_all, rowBreakdown.non_search_all);
      addBucket(buckets.non_search_cross_turn, rowBreakdown.non_search_cross_turn);
      addBucket(buckets.search_all, rowBreakdown.search_all);
      if (rowBreakdown.known_search_requests > 0) hasBreakdownSource = true;
    } else {
      if (s.prefix_reason_counts && typeof s.prefix_reason_counts === "object") {
        for (const [code, cnt] of Object.entries(s.prefix_reason_counts)) {
          if (finite(cnt) && cnt > 0) {
            prefixReasonCounts[code] = (prefixReasonCounts[code] || 0) + cnt;
          }
        }
      }
      if (s.native_search_breakdown && typeof s.native_search_breakdown === "object") {
        addBucket(buckets.non_search_all, s.native_search_breakdown.non_search_all);
        addBucket(buckets.non_search_cross_turn, s.native_search_breakdown.non_search_cross_turn);
        addBucket(buckets.search_all, s.native_search_breakdown.search_all);
        hasBreakdownSource = true;
      }
    }
  }
  const hasPaired = completeRequests > 0 && hasPairedTokens;
  return {
    jobCount: validJobs.length,
    requests,
    duration_ms: hasDuration ? Math.round(durationMs * 100) / 100 : null,
    cache_summary: {
      requests,
      complete_requests: completeRequests,
      unknown_requests: unknownRequests,
      coverage: requests ? completeRequests / requests : null,
      reported_input_tokens: hasPaired ? reportedInput : null,
      cache_read_tokens: hasPaired ? cacheRead : null,
      uncached_input_tokens: hasPaired ? reportedInput - cacheRead : null,
      weighted_read_ratio: hasPaired && reportedInput > 0 ? cacheRead / reportedInput : null,
      anomalous_requests: anomalousRequests,
    },
    normalized_totals: {
      input_tokens: inputKnown ? inputSum : null,
      output_tokens: outputKnown ? outputSum : null,
    },
    prefix_reason_counts: prefixReasonCounts,
    native_search_breakdown: hasBreakdownSource ? {
      non_search_all: finalizeBucket(buckets.non_search_all),
      non_search_cross_turn: finalizeBucket(buckets.non_search_cross_turn),
      search_all: finalizeBucket(buckets.search_all),
    } : null,
    sequence,
  };
}

export function modelUsageView(summary, identity = "usage", rawRequests = null) {
  if (!summary?.cache_summary) return "";
  const c = summary.cache_summary;
  const totals = summary.normalized_totals;
  const totalFields = [
    ["input_tokens", "输入"],
    ["cache_read_tokens", "缓存读取"],
    ["uncached_input_tokens", "未缓存输入"],
    ["output_tokens", "输出"],
    ["reasoning_tokens", "推理"],
    ["visible_output_tokens", "可见输出"],
    ["cache_write_tokens", "缓存写入"],
  ];
  const totalView = totals
    ? `<p class="small usage-totals">已报告总量：${totalFields.map(([field, label]) =>
        `${label} ${number(totals[field])}（覆盖 ${percent(totals.coverage?.[field])}）`).join(" · ")}。${summary.duration_ms == null ? "" : `累计耗时 ${number(summary.duration_ms)} ms。`}总量按各字段已回报请求累计，缓存率只采用输入与缓存读取均有回报的成对样本。</p>`
    : "";

  const baseRows = Array.isArray(summary.sequence) ? summary.sequence : [];
  const rows = enrichRowsWithRawRequests(baseRows, rawRequests);
  const stats = rows.length ? breakdownStatsFromRows(rows) : breakdownStatsFromSummary(summary);
  const searchDisplay = formatSearchBreakdown(stats);

  // Prefix stability distribution across sequence rows or summary counts.
  const reasonCounts = new Map();
  if (rows.length) {
    for (const r of rows) {
      const code = r.prefix_diagnostic?.change_reason || r.cache_epoch_reason || "unknown";
      reasonCounts.set(code, (reasonCounts.get(code) || 0) + 1);
    }
  }

  // Group rows by Agent for per-agent cache read bars, and by Agent+Session for details tables.
  const agentGroups = new Map();
  const sessions = new Map();
  for (const r of rows) {
    const agentKey = r.agent_id || "未知实例";
    if (!agentGroups.has(agentKey)) agentGroups.set(agentKey, []);
    agentGroups.get(agentKey).push(r);
    const sessionKey = `${agentKey}:${r.session_id || "未知会话"}`;
    if (!sessions.has(sessionKey)) sessions.set(sessionKey, []);
    sessions.get(sessionKey).push(r);
  }

  const kpiStrip = `<div class="usage-kpi-grid" aria-label="Prompt Cache 指标概览">
    <div class="usage-kpi">
      <div class="usage-kpi-label">总体加权缓存读取率</div>
      <div class="usage-kpi-value">${escape(kpiPercent(c.weighted_read_ratio))}</div>
      ${barHTML(c.weighted_read_ratio, "teal")}
      <div class="usage-kpi-meta">成对输入 ${escape(number(c.reported_input_tokens))} · 读取 ${escape(number(c.cache_read_tokens))}</div>
    </div>
    <div class="usage-kpi">
      <div class="usage-kpi-label">非联网跨轮缓存率</div>
      <div class="usage-kpi-value">${escape(searchDisplay.nonSearchCrossValue)}</div>
      ${barHTML(stats.non_search_cross_turn.weighted_read_ratio, "teal")}
      <div class="usage-kpi-meta">${escape(searchDisplay.nonSearchCrossMeta)} · 非联网全部 ${escape(searchDisplay.nonSearchAllText)}${searchDisplay.nonSearchAllMeta ? `（${escape(searchDisplay.nonSearchAllMeta)}）` : ""}</div>
    </div>
    <div class="usage-kpi">
      <div class="usage-kpi-label">联网请求缓存率</div>
      <div class="usage-kpi-value">${escape(searchDisplay.searchValue)}</div>
      ${barHTML(stats.search_all.weighted_read_ratio, "amber")}
      <div class="usage-kpi-meta">${escape(searchDisplay.searchMeta)}</div>
    </div>
    <div class="usage-kpi">
      <div class="usage-kpi-label">请求计量覆盖率</div>
      <div class="usage-kpi-value">${escape(kpiPercent(c.coverage))}</div>
      ${barHTML(c.coverage, "slate")}
      <div class="usage-kpi-meta">完整 ${escape(c.complete_requests)}/${escape(c.requests)} · 未知 ${escape(c.unknown_requests)}${c.anomalous_requests ? ` · 异常 ${escape(c.anomalous_requests)}` : ""}</div>
    </div>
    <div class="usage-kpi">
      <div class="usage-kpi-label">Token 与耗时累计</div>
      <div class="usage-kpi-value">${totals ? escape(number(totals.input_tokens)) : "未知"}<span class="usage-kpi-unit"> 输入</span></div>
      <div class="usage-kpi-meta">输出 ${totals ? escape(number(totals.output_tokens)) : "未知"} · 耗时 ${summary.duration_ms == null ? "未知" : `${escape(number(summary.duration_ms))} ms`}</div>
    </div>
  </div>`;

  const prefixSection = prefixDistHTML(rows.length ? reasonCounts : summary.prefix_reason_counts);

  const agentBarsSection = agentGroups.size
    ? `<div class="agent-cache-card">
        <div class="prefix-dist-head">
          <strong>按 Agent / 实例缓存读取分布</strong>
          <span class="small muted">按成对样本加权计算（不均分单次百分比）</span>
        </div>
        <div class="agent-bar-list">${[...agentGroups.entries()].map(([agentKey, reqs]) => {
          const agentStats = weightedRatioForRows(reqs);
          const breaks = reqs.filter(r => ["warn", "bad"].includes(reasonTone(r.prefix_diagnostic?.change_reason || r.cache_epoch_reason))).length;
          return `<div class="agent-bar-row" data-agent-cache="${escape(agentKey)}">
            <div class="agent-bar-head">
              <span class="agent-bar-name">${escape(agentTitle(agentKey))}</span>
              <span class="agent-bar-stats numeric">${escape(percent(agentStats.ratio))} · ${escape(agentStats.pairedCount)}/${escape(agentStats.totalCount)} 次成对${breaks ? ` · 前缀变更 ${escape(breaks)}` : ""}</span>
            </div>
            ${barHTML(agentStats.ratio, breaks ? "amber" : "teal")}
          </div>`;
        }).join("")}</div>
      </div>`
    : "";

  const summaryOnlyNote = !rows.length
    ? `<p class="small muted">当前列表仅展示汇总指标；打开“查看任务详情”或“任务结果与调用统计”可查看逐次请求序列。</p>`
    : "";

  // Important DOM invariant: only session blocks inside .model-usage use <details> and <table>.
  return `<details class="model-usage" data-key="${escape(identity)}:model-usage"><summary>模型调用与缓存 · ${escape(c.requests)} 次请求 · 加权缓存读取率 ${escape(percent(c.weighted_read_ratio))}</summary><div class="section-gap">${kpiStrip}<p class="small">已报告样本加权缓存读取率 ${percent(c.weighted_read_ratio)}（缓存读取总量 ÷ 同口径输入总量）；计量完整 ${escape(c.complete_requests)} / ${escape(c.requests)}，未知 ${escape(c.unknown_requests)}，覆盖率 ${percent(c.coverage)}。</p>${totalView}<p class="small">成对样本输入 ${number(c.reported_input_tokens)} · 缓存读取 ${number(c.cache_read_tokens)} · 未缓存输入 ${number(c.uncached_input_tokens)}${c.anomalous_requests ? ` · 异常计数 ${escape(c.anomalous_requests)} 次，保留原值` : ""}。缺失不计为 0；本地工具缓存不计入此比例。费用按可靠计费回报展示，未知时不估算。</p>${prefixSection}${agentBarsSection}${summaryOnlyNote}${[...sessions.entries()].map(([key, requests]) => `<details data-key="${escape(identity)}:session:${escape(key)}"><summary>Agent ${escape(requests[0].agent_id || "未记录")} · 会话 ${escape(requests[0].session_id || "未记录")} · ${requests.length} 次</summary><div class="table-wrap"><table><thead><tr><th>请求 / epoch</th><th>阶段 / 前缀</th><th>输入</th><th>缓存读取</th><th>未缓存输入</th><th>输出</th><th>耗时 / 费用</th></tr></thead><tbody>${requests.map((r, index) => {
    const u = usage(r), p = r.prefix_diagnostic || {};
    const changeCode = p.change_reason || r.cache_epoch_reason;
    const searchTag = r.native_search_requested === true ? "原生联网" : r.native_search_requested === false ? "非联网" : "";
    return `<tr data-read-key="${escape(identity)}:request:${escape(r.local_request_id || `${key}:${index}`)}"><td>${escape(r.local_request_id || index + 1)}<br>epoch ${escape(r.cache_epoch ?? "未知")}</td><td>${escape(stage(r.stage))}<br>${escape(reason(changeCode))}${p.first_changed_path ? `<br><span class="mono small muted">路径 ${escape(p.first_changed_path)}</span>` : ""}<br>${escape(category(r.request_category))}${searchTag ? ` · ${escape(searchTag)}` : ""}${r.error ? `<br>${escape(r.error)}` : ""}</td><td>${number(u.input_tokens)}</td><td>${number(u.cache_read_tokens)}</td><td>${number(u.uncached_input_tokens)}</td><td>${number(u.output_tokens)}<br>推理 ${number(u.reasoning_tokens)}<br>可见 ${number(u.visible_output_tokens)}<br>缓存写入 ${number(u.cache_write_tokens)}</td><td>${r.duration_ms == null ? "未知" : `${number(r.duration_ms)} ms`}<br>${r.cost_known && r.cost != null ? escape(typeof r.cost === "object" ? JSON.stringify(r.cost) : r.cost) : "费用未知"}</td></tr>`;
  }).join("")}</tbody></table></div></details>`).join("")}</div></details>`;
}
