const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const escapeHTML = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const e = escapeHTML;
const state = {
  route: location.hash.slice(1) || "overview",
  accounts: [],
  account: null,
  status: {},
  universe: {},
  analyses: [],
  jobs: [],
  materials: [],
  settings: {},
  accountId: localStorage.getItem("guanlan.account"),
  busy: false,
  pendingAccountId: null,
  error: null,
  loaded: false,
  materialFilter: "all",
  materialQuery: "",
  pages: { analyses: 0, jobs: 0, materials: 0, recommendations: 0, recommendationHistory: 0 },
  pageSize: 10,
  recommendations: [],
  recommendationHistory: [],
  recommendationCoverage: {},
  showRecommendationHistory: false,
  publicResearch: [],
  demands: [],
  globalRuns: [],
  events: {},
  openedJobs: new Set(),
  reportCache: new Map(),
  drafts: new Map(),
};
const routes = {
  overview: ["账户总览", "overview", "PORTFOLIO OVERVIEW"],
  holdings: ["持仓管理", "wallet", "ACCOUNT & HOLDINGS"],
  market: ["市场与候选", "market", "MARKET & OPPORTUNITIES"],
  research: ["研究报告", "research", "RESEARCH & EVIDENCE"],
  agents: ["Agent 工作台", "agents", "AGENT WORKSPACE"],
  settings: ["系统设置", "settings", "LOCAL CONFIGURATION"],
};
const paths = {
  overview: "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
  wallet: "M3 7h16a2 2 0 0 1 2 2v11H3z M3 7V4h14v3 M16 12h5v5h-5z",
  market: "M3 20h18 M5 15l5-5 4 3 6-9 M15 4h5v5",
  research: "M5 3h10l4 4v14H5z M14 3v5h5 M8 12h8 M8 16h6",
  agents:
    "M12 3v4 M8 3h8 M4 8h16v11H4z M1 11v5 M23 11v5 M8 12h.1 M16 12h.1 M9 16h6",
  settings:
    "M9 3h6l1 4 4 1v7l-4 1-1 4H9l-1-4-4-1V8l4-1z M15 11.5a3 3 0 1 1-6 0 3 3 0 0 1 6 0",
  plus: "M12 5v14 M5 12h14",
  refresh: "M20 8a8 8 0 1 0 0 8 M20 3v5h-5",
  arrow: "M5 12h14 M14 7l5 5-5 5",
  info: "M12 10v7 M12 6v.1 M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0",
  close: "M6 6l12 12 M18 6 6 18",
  clock: "M12 7v5l3 2 M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0",
  link: "M9 15 15 9 M8 10 5 13a4 4 0 0 0 6 6l3-3 M10 8l3-3a4 4 0 0 1 6 6l-3 3",
  shield: "M12 3 3 6v6c0 5 9 9 9 9s9-4 9-9V6z M8 12l3 3 5-6",
  cash: "M3 6h18v13H3z M12 9a3 3 0 1 1 0 6 3 3 0 0 1 0-6 M6 12h.1 M18 12h.1",
  download: "M12 3v12 M7 10l5 5 5-5 M4 16v5h16v-5",
  search: "M10 3a7 7 0 1 1 0 14 7 7 0 0 1 0-14 M15 15l6 6",
};
const icon = (name) =>
  `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name] || paths.info}"/></svg>`;
function decimalFormat(value, precision = 6) {
  if (value === null || value === undefined || value === "") return "—";
  const text = String(value);
  if (!/^-?\d+(\.\d+)?$/.test(text)) return e(text);
  const [whole, fraction = ""] = text.split(".");
  const decimal = fraction.slice(0, precision).replace(/0+$/, "");
  return (
    whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + (decimal ? "." + decimal : "")
  );
}
const number = (value) => decimalFormat(value);
const quantity = (value) => decimalFormat(value);
const percent = (value) =>
  value === null || value === undefined ? "—" : `${number(value)}%`;
const date = (value, compact = false) => {
  if (!value) return "时间未知";
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return `${e(value)}（源仅日期）`;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return e(value);
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    ...(compact ? {} : { year: "numeric" }),
  }).format(parsed);
};
const safeURL = (value) => {
  try {
    const url = new URL(value);
    return url.protocol === "https:" ? url.href : null;
  } catch {
    return null;
  }
};
const link = (url, label) =>
  safeURL(url)
    ? `<a href="${e(safeURL(url))}" target="_blank" rel="noopener noreferrer">${e(label || url)} ↗</a>`
    : e(label || url || "未提供来源链接");
const array = (value, key) =>
  Array.isArray(value) ? value : Array.isArray(value?.[key]) ? value[key] : [];
const unit = (position) =>
  ["etf", "fund", "基金", "ETF"].includes(position?.kind) ||
  position?.unit === "份"
    ? "份"
    : position?.kind === "stock"
      ? "股"
      : "股 / 份（待确认）";
const badge = (text, type = "") =>
  `<span class="pill ${e(type)}">${e(text)}</span>`;
const button = (text, action, cls = "", glyph = "") =>
  `<button type="button" class="button ${cls}" data-action="${e(action)}">${glyph ? icon(glyph) : ""}${e(text)}</button>`;
const statusNames = {
  waiting_config: "等待配置",
  ok: "已获取",
  live: "当前行情",
  error: "获取失败",
  error_cached: "刷新失败 · 使用缓存",
  unknown_time: "行情时间未知",
  pending: "等待执行",
  queued: "排队中",
  running: "研究中",
  completed: "已完成",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
  stale: "已过期",
  outdated: "待更新",
  superseded: "版本已更新",
  blocked: "等待配置",
  fresh: "已更新",
  missing: "未获取",
  delayed: "延迟",
  closed: "已收盘",
  suspended: "停牌",
  demo: "演示数据",
  idle: "等待分析",
  current: "当前版本",
  watching: "观察中",
  triggered: "条件已触发",
  needs_review: "待重新分析",
  expired: "已过期",
  revoked: "已撤销",
};
const stateBadge = (value) =>
  badge(
    statusNames[value] || value || "等待分析",
    ["failed", "error", "error_cached"].includes(value)
      ? "error"
      : [
            "pending",
            "queued",
            "running",
            "stale",
            "outdated",
            "superseded",
            "blocked",
            "waiting_config",
            "delayed",
            "missing",
            "unknown_time",
            "triggered",
            "needs_review",
            "expired",
          ].includes(value)
        ? "warning"
        : ["completed", "succeeded", "fresh", "current", "ok", "live"].includes(
              value,
            )
          ? "live"
          : "neutral",
  );
const classificationNames = {
  fact: "披露 / 可观察事实",
  disclosed_fact: "披露 / 可观察事实",
  observable_fact: "披露 / 可观察事实",
  statement: "主体计划 / 预测 / 声明",
  plan: "主体计划 / 预测 / 声明",
  forecast: "主体计划 / 预测 / 声明",
  opinion: "外部观点 / 投资建议",
  external_opinion: "外部观点 / 投资建议",
  rumor: "传闻 / 待核实",
  uncertain: "传闻 / 待核实",
  conflict: "冲突材料",
  inference: "系统计算 / 推论",
  calculation: "系统计算 / 推论",
  agent_analysis: "系统生成研究 / 推论",
  unknown: "暂不确定",
};
function inlineMarkdown(text) {
  return e(text)
    .replace(/\[([^\]\n]+)\]\((https:\/\/[^\s)]+)\)/g, (_, label, url) => {
      const decoded = url.replaceAll("&amp;", "&");
      return safeURL(decoded)
        ? `<a href="${e(safeURL(decoded))}" target="_blank" rel="noopener noreferrer">${label} ↗</a>`
        : label;
    })
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`\n]+)`/g, '<code class="inline-code">$1</code>');
}
function markdown(value) {
  if (typeof value !== "string") return objectView(value);
  return `<div class="report-text">${value
    .split(/\n\s*\n/)
    .map((block) => {
      if (/^#{1,4} /.test(block))
        return block
          .split("\n")
          .map((line) =>
            /^#{1,4} /.test(line)
              ? `<h3>${inlineMarkdown(line.replace(/^#{1,4} /, ""))}</h3>`
              : `<p>${inlineMarkdown(line)}</p>`,
          )
          .join("");
      if (/^[-*] /.test(block))
        return `<ul>${block
          .split("\n")
          .map(
            (line) => `<li>${inlineMarkdown(line.replace(/^[-*] /, ""))}</li>`,
          )
          .join("")}</ul>`;
      return `<p>${inlineMarkdown(block).replaceAll("\n", "<br>")}</p>`;
    })
    .join("")}</div>`;
}
function objectView(value) {
  if (value === null || value === undefined)
    return '<p class="muted small">暂无内容</p>';
  if (typeof value !== "object")
    return `<p class="source-content">${e(value)}</p>`;
  if (Array.isArray(value))
    return value.map((item) => objectView(item)).join("");
  return Object.entries(value)
    .map(
      ([key, val]) =>
        `<div class="report-section"><h4>${e({ summary: "研究摘要", conclusion: "当前结论", rationale: "判断依据", facts: "支撑事实", conditions: "触发条件", risks: "主要风险", uncertainties: "不确定性", recommendations: "下一步建议", opportunities: "候选机会", changes: "与上次相比", evidence: "材料", findings: "核心发现", report: "研究报告", sources: "来源", limitations: "适用范围", next_steps: "下一步", counter_evidence: "反向证据", valid_until: "适用期限", title: "标题", content: "内容", symbol: "证券", classification: "材料分类" }[key] || key)}</h4>${typeof val === "string" ? markdown(val) : objectView(val)}</div>`,
    )
    .join("");
}
async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  let body;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (!response.ok) {
    const detail =
      body?.detail || body?.error || `请求失败 (${response.status})`;
    throw new Error(
      typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? detail
              .map(
                (d) =>
                  `${(d.loc || []).filter((x) => x !== "body").join(" / ")}：${d.msg || JSON.stringify(d)}`,
              )
              .join("；")
          : JSON.stringify(detail),
    );
  }
  return body;
}
function toast(message, error = false) {
  const node = document.createElement("div");
  node.className = `toast${error ? " error" : ""}`;
  node.textContent = message;
  $("#toast-region").append(node);
  setTimeout(() => node.remove(), error ? 11000 : 5500);
}
async function operation(callback) {
  if (state.busy) return;
  state.busy = true;
  const active = document.activeElement;
  if (active?.tagName === "BUTTON") active.disabled = true;
  const line = document.createElement("div");
  line.className = "loading-line";
  document.body.append(line);
  try {
    await callback();
  } catch (error) {
    toast(error.message, true);
  } finally {
    state.busy = false;
    if (active?.isConnected) active.disabled = false;
    line.remove();
    const pendingAccountId = state.pendingAccountId;
    state.pendingAccountId = null;
    if (pendingAccountId && pendingAccountId !== state.accountId)
      requestAccountSwitch(pendingAccountId);
  }
}
async function loadJobEvents(jobId) {
  const previous = state.events[jobId] || { items: [], cursor: 0 };
  const response = await api(`/api/jobs/${encodeURIComponent(jobId)}/events?after=${previous.cursor}&limit=100`);
  state.events[jobId] = {
    items: [...previous.items, ...array(response.events)],
    cursor: response.next_after ?? previous.cursor,
    more: Boolean(response.has_more),
  };
}
async function loadAccount() {
  if (!state.accountId) {
    state.account = null;
    state.analyses = [];
    state.jobs = [];
    state.materials = [];
    state.recommendations = [];
    state.recommendationHistory = [];
    state.recommendationCoverage = {};
    return;
  }
  const id = encodeURIComponent(state.accountId), size = state.pageSize;
  const results = await Promise.allSettled([
    api(`/api/accounts/${id}`),
    api(`/api/accounts/${id}/analyses?summary=true&limit=${size}&offset=${state.pages.analyses * size}`),
    api(`/api/accounts/${id}/jobs?events=false&limit=${size}&offset=${state.pages.jobs * size}`),
    state.route === "research" ? api(`/api/materials?account_id=${id}&index=true&limit=${size}&offset=${state.pages.materials * size}&query=${encodeURIComponent(state.materialQuery)}`) : Promise.resolve(null),
    api(`/api/accounts/${id}/recommendations?view=active&envelope=true&limit=${size}&offset=${state.pages.recommendations * size}`),
    state.route === "market" ? api(`/api/research?account_id=${id}&limit=30`) : Promise.resolve(null),
    state.route === "agents" ? api(`/api/demands?account_id=${id}&limit=50`) : Promise.resolve(null),
    state.route === "market" ? api("/api/global-research?limit=10") : Promise.resolve(null),
    state.route === "research" && state.showRecommendationHistory ? api(`/api/accounts/${id}/recommendations?view=history&envelope=true&limit=${size}&offset=${state.pages.recommendationHistory * size}`) : Promise.resolve(null),
  ]);
  if (results[0].status === "rejected") throw results[0].reason;
  if (id !== encodeURIComponent(state.accountId)) return;
  state.account = results[0].value;
  const failures = results.slice(1).filter((r) => r.status === "rejected");
  if (results[1].status === "fulfilled") {
    const headers = array(results[1].value, "analyses");
    const reports = await Promise.allSettled(headers.map(async (header) => {
      let report = state.reportCache.get(header.id);
      if (!report) {
        report = await api(`/api/analyses/${encodeURIComponent(header.id)}`);
        state.reportCache.set(header.id, report);
        if (state.reportCache.size > 100) state.reportCache.delete(state.reportCache.keys().next().value);
      }
      return { ...report, ...header };
    }));
    if (id !== encodeURIComponent(state.accountId)) return;
    state.analyses = reports.filter((r) => r.status === "fulfilled").map((r) => r.value);
    failures.push(...reports.filter((r) => r.status === "rejected"));
  }
  if (results[2].status === "fulfilled") state.jobs = array(results[2].value, "jobs");
  if (results[3].status === "fulfilled" && results[3].value) state.materials = array(results[3].value, "materials");
  if (results[4].status === "fulfilled") {
    state.recommendations = array(results[4].value, "items");
    state.recommendationCoverage.recommendations = results[4].value;
  }
  if (results[5].status === "fulfilled" && results[5].value) state.publicResearch = array(results[5].value, "records");
  if (results[6].status === "fulfilled" && results[6].value) state.demands = array(results[6].value, "demands");
  if (results[7].status === "fulfilled" && results[7].value) state.globalRuns = array(results[7].value, "runs");
  if (results[8].status === "fulfilled" && results[8].value) {
    state.recommendationHistory = array(results[8].value, "items");
    state.recommendationCoverage.recommendationHistory = results[8].value;
  }
  if (state.route === "agents") {
    const active = state.jobs.find((job) => job.state === "running");
    if (active && !state.events[active.id]) state.openedJobs.add(active.id);
    const updates = await Promise.allSettled(state.jobs.filter((job) => {
      const saved = state.events[job.id];
      return state.openedJobs.has(job.id) && (!saved || (!saved.more && (job.last_event_seq || 0) > saved.cursor));
    }).map((job) => loadJobEvents(job.id)));
    failures.push(...updates.filter((r) => r.status === "rejected"));
  }
  state.partialError = failures.length
    ? `部分研究记录暂时无法加载：${failures.map((r) => r.reason.message).join("；")}`
    : null;
}
async function refresh(full = false) {
  const loadUniverse = full || state.route === "market";
  const results = await Promise.allSettled([
    api("/api/status"),
    api("/api/accounts"),
    loadUniverse ? api("/api/universe") : Promise.resolve(null),
    full ? api("/api/settings") : Promise.resolve(null),
  ]);
  if (results[0].status === "fulfilled") state.status = results[0].value;
  if (results[1].status === "rejected") throw results[1].reason;
  state.accounts = array(results[1].value, "accounts");
  if (!state.accounts.some((a) => a.id === state.accountId)) {
    state.accountId = state.accounts[0]?.id || null;
    state.pages = { analyses: 0, jobs: 0, materials: 0, recommendations: 0, recommendationHistory: 0 };
    state.showRecommendationHistory = false;
    state.recommendationHistory = [];
    state.recommendationCoverage = {};
  }
  if (state.accountId) localStorage.setItem("guanlan.account", state.accountId);
  if (results[2].status === "fulfilled" && results[2].value) state.universe = results[2].value;
  if (results[3].status === "fulfilled" && results[3].value) state.settings = results[3].value;
  await loadAccount();
  state.error = results[0].status === "rejected" ? results[0].reason.message : null;
  state.loaded = true;
}
function pager(kind, rows) {
  const page = state.pages[kind];
  const coverage = state.recommendationCoverage[kind];
  const hasMore = coverage?.has_more ?? rows.length >= state.pageSize;
  const range = rows.length ? `${page * state.pageSize + 1}–${page * state.pageSize + rows.length}` : "0";
  const label = Number.isInteger(coverage?.total) ? `当前 ${range} / 共 ${coverage.total} 条` : `每页最多 ${state.pageSize} 条`;
  return `<div class="pagination" data-pagination="${kind}"><button class="button compact" data-action="page" data-kind="${kind}" data-page="${page - 1}" ${page === 0 ? "disabled" : ""}>上一页</button><span class="small muted">第 ${page + 1} 页 · ${label}</span><button class="button compact" data-action="page" data-kind="${kind}" data-page="${page + 1}" ${hasMore ? "" : "disabled"}>下一页</button></div>`;
}
function notice() {
  let html = "";
  if (state.error)
    html += `<div class="notice error">${icon("info")}<div>本地服务连接中断。页面保留最近一次结果。<p>${e(state.error)}</p></div>${button("重新连接", "refresh", "compact")}</div>`;
  if (state.partialError)
    html += `<div class="notice warning">${icon("info")}<div>${e(state.partialError)}</div></div>`;
  if (state.status.mode === "demo")
    html += `<div class="notice warning">${icon("info")}<div><strong>演示空间</strong> · 使用明确标识的固定样例数据，与真实研究历史隔离。示例行情不代表当前价格；没有模型配置时不会模拟 Agent 研究。</div></div>`;
  else if (!state.status.model_configured)
    html += `<div class="notice">${icon("info")}<div><strong>基础功能已就绪</strong> · 尚未配置运行时模型。可管理账户、刷新行情并计算持仓指标；联网 Agent 研究需要在系统设置中接入模型${!state.status.search_configured ? "与搜索服务" : ""}。</div><a class="button compact" href="#settings">配置服务 ${icon("arrow")}</a></div>`;
  return html;
}
function shell() {
  const route = routes[state.route] ? state.route : "overview";
  state.route = route;
  const current = routes[route];
  return `<div class="layout"><aside class="sidebar"><a class="brand" href="#overview" aria-label="观澜首页"><span class="brand-mark">观</span><div><div class="brand-name">观澜</div><div class="brand-caption">GUANLAN RESEARCH</div></div></a><div class="nav-label">研究工作台</div><nav class="nav" aria-label="主要导航">${Object.entries(
    routes,
  )
    .map(
      ([key, [label, glyph]]) =>
        `<a href="#${key}" class="${key === route ? "active" : ""}" ${key === route ? 'aria-current="page"' : ""}>${icon(glyph)}${label}</a>`,
    )
    .join(
      "",
    )}</nav><div class="sidebar-bottom"><div class="local-status"><span class="dot ${state.error ? "warn" : ""}"></span>${state.error ? "服务连接中断" : "本地研究服务"}</div><p class="sidebar-note">数据保存在本机<br>虚拟账户 · 不连接真实交易<br>Asia/Shanghai</p></div></aside><div class="main-wrap"><header class="topbar"><div class="breadcrumb">工作台 <span>/</span><span>${current[0]}</span></div><div class="top-tools"><span class="clock" id="clock">${date(new Date().toISOString())} · 上海</span>${badge(state.status.mode === "demo" ? "演示模式" : "本地模式", state.status.mode === "demo" ? "warning" : "neutral")}<label class="sr-only hidden" for="account-switch">当前账户</label><select id="account-switch" class="account-switch" aria-label="切换虚拟账户">${state.accounts.length ? state.accounts.map((a) => `<option value="${e(a.id)}" ${a.id === state.accountId ? "selected" : ""}>${e(a.name)}</option>`).join("") : '<option value="">尚无虚拟账户</option>'}</select></div></header><main id="main" class="main" tabindex="-1">${pageHeading()}${notice()}${{ overview: overview, holdings: holdings, market: market, research: research, agents: agents, settings: settings }[route]()}<footer class="footer-note">观澜提供研究分析与决策参考。账户为手动维护的虚拟快照，持仓数量不代表实际可卖数量；不连接券商、不执行交易。所有界面时间均为北京时间。</footer></main></div></div>`;
}
function pageHeading() {
  const [title, , eyebrow] = routes[state.route];
  const descriptions = {
    overview: "让持仓、公开信息与每一次判断，都有清晰的来路。",
    holdings: "维护账户快照。每次保存都会生成新版本，并自动更新研究任务。",
    market: "将自选观察、实际持仓与候选发现分开管理。",
    research: "从公开材料到具体账户判断，保留依据、分歧与适用条件。",
    agents: "查看任务分工、可见消息与工具来源，理解研究如何完成。",
    settings: "配置外部能力与本机运行节奏。密钥仅从后端环境变量读取。",
  };
  const actions = {
    overview: `${button("新建账户", "new-account", "", "plus")}${button("立即分析", "analyze", "primary", "agents")}`,
    holdings: button("新建账户", "new-account", "", "plus"),
    market: button("刷新行情", "refresh-quotes", "primary", "refresh"),
    research: button("立即分析", "analyze", "primary", "agents"),
    agents: button("刷新状态", "refresh", "", "refresh"),
    settings: button("备份本地数据", "backup", "", "download"),
  };
  return `<div class="page-heading"><div><div class="eyebrow">${eyebrow}</div><h1>${title}</h1><p class="subtitle">${descriptions[state.route]}</p></div><div class="actions">${actions[state.route]}</div></div>`;
}
function empty(
  title,
  description,
  action = "",
  label = "",
  glyph = "research",
) {
  return `<div class="empty"><div class="empty-icon">${icon(glyph)}</div><h3>${e(title)}</h3><p>${e(description)}</p>${action ? button(label, action, "compact") : ""}</div>`;
}
function noAccount() {
  return `<section class="card">${empty("从一个虚拟账户开始", "创建账户后，可录入持仓股数或份数，计算证券市值，并让 Agent 结合账户偏好开展研究。", "new-account", "创建第一个账户", "wallet")}</section>`;
}
const latestAnalysis = () =>
  state.analyses.find((a) => a.status === "current") ||
  state.analyses.find((a) => ["completed", "succeeded"].includes(a.status)) ||
  state.analyses[0];
function analysisState() {
  const value = state.account?.analysis_state;
  return typeof value === "object" ? value.status || value.state : value;
}
function stats() {
  const m = state.account.metrics || {},
    positions = m.positions || [],
    weights = positions
      .filter((p) => p.weight_pct !== null && p.weight_pct !== undefined)
      .map((p) => Number(p.weight_pct))
      .filter(Number.isFinite);
  const max = weights.length ? Math.max(...weights) : null;
  const count = state.account.holdings?.length || 0;
  const pnl = m.unrealized_pnl;
  return `<div class="stat-grid"><div class="stat"><div class="stat-label">已录入证券市值 ${icon("wallet")}</div><div class="stat-value"><span class="unit">¥</span>${number(m.securities_value ?? (m.missing_prices?.length ? null : m.known_priced_value))}</div><div class="stat-meta">${m.missing_prices?.length ? `部分行情缺失 · 已知估值 ¥${number(m.known_priced_value)}` : m.cash === null || m.cash === undefined ? "未录入现金，不代表账户完整总资产" : "已录入证券的最新估值"}</div></div><div class="stat"><div class="stat-label">现金余额 ${icon("cash")}</div><div class="stat-value">${m.cash === null || m.cash === undefined ? '<span class="unit">未录入</span>' : `<span class="unit">¥</span>${number(m.cash)}`}</div><div class="stat-meta">${m.total_assets !== null && m.total_assets !== undefined ? `已录入资产合计 ¥${number(m.total_assets)}` : "未推断可用资金或可买金额"}</div></div><div class="stat"><div class="stat-label">持仓证券 / 最大权重 ${icon("market")}</div><div class="stat-value">${count}<span class="unit"> 只</span><span class="unit"> · </span>${percent(max)}</div><div class="stat-meta">${m.missing_prices?.length ? "行情缺失，权重可能无法计算" : "按已录入持仓证券市值计算内部权重"}</div></div><div class="stat"><div class="stat-label">未实现盈亏 ${icon("shield")}</div><div class="stat-value ${Number(pnl) < 0 ? "negative" : ""}">${pnl === null || pnl === undefined ? '<span class="unit">无法完整计算</span>' : `<span class="unit">¥</span>${number(pnl)}`}</div><div class="stat-meta">${pnl === null || pnl === undefined ? "成本或行情不完整，保留缺失" : "基于录入成本与行情计算，不含费用"}</div></div></div>`;
}
function positionsTable() {
  const m = state.account?.metrics || {},
    positions = m.positions || [];
  if (!positions.length)
    return empty(
      "尚未录入持仓",
      "添加证券与股数或份数后，估值和内部权重会显示在这里。",
      "go-holdings",
      "录入持仓",
      "wallet",
    );
  return `<div class="table-wrap"><table><thead><tr><th>证券 / 交易所</th><th class="align-right">数量</th><th class="align-right">行情 / 时间</th><th class="align-right">市值 / 内部权重</th><th class="align-right">未实现盈亏</th></tr></thead><tbody>${positions.map((p) => `<tr><td><div class="security-name">${e(p.name || p.symbol)}</div><div class="security-code">${e(p.symbol)} · ${e({ stock: "股票", etf: "ETF", fund: "基金", unknown: "类型待确认" }[p.kind] || p.kind || "证券")}</div></td><td class="align-right numeric">${quantity(p.quantity)}<span class="cell-secondary">${unit(p)}</span></td><td class="align-right numeric">${number(p.price)}<span class="cell-secondary">${p.quote?.as_of ? date(p.quote.as_of, true) : "行情时间未知"} · ${e(p.quote?.source || "来源未知")}</span>${p.quote?.status && p.quote.status !== "fresh" ? stateBadge(p.quote.status) : ""}${p.quote?.last_error ? `<span class="cell-secondary negative">${e(p.quote.last_error)}</span>` : ""}</td><td class="align-right numeric">${number(p.market_value)}<span class="cell-secondary">${percent(p.weight_pct)}</span></td><td class="align-right numeric ${Number(p.unrealized_pnl) < 0 ? "negative" : ""}">${number(p.unrealized_pnl)}<span class="cell-secondary">${p.cost_price === null || p.cost_price === undefined ? "未录入成本" : `成本 ${number(p.cost_price)}`}</span></td></tr>`).join("")}</tbody></table></div>`;
}
function exposure() {
  const sector = state.account?.metrics?.sector_weights;
  const rows = Array.isArray(sector)
    ? sector.map((s) => [
        s.sector || s.name || "未分类",
        s.weight_pct ?? s.weight,
      ])
    : Object.entries(sector || {});
  if (!rows.length)
    return empty(
      "暂无板块暴露数据",
      "基础资料或行情缺失时保留未知；未分类不等于分散投资。",
      "",
      "",
      "market",
    );
  return `<div class="card-body exposure-list">${rows.map(([name, value]) => `<div class="exposure-row"><span>${e(name)}</span><span class="numeric">${percent(value)}</span><div class="exposure-track"><i style="width:${Math.min(100, Math.max(0, Number(value) || 0))}%"></i></div></div>`).join("")}</div>`;
}
function overview() {
  if (!state.account) return noAccount();
  const a = state.account,
    m = a.metrics || {},
    latest = latestAnalysis(),
    active = state.jobs.filter((j) =>
      ["running", "queued", "pending"].includes(j.state),
    );
  return `${stats()}<div class="grid-two"><section class="card"><div class="card-header"><div class="account-heading"><h2>${e(a.name)} · 持仓快照</h2>${badge(`v${a.version}`, "neutral")}</div><a class="small" href="#holdings">管理持仓 ↗</a></div>${positionsTable()}<div class="card-note">数量单位为股或份。修改快照不会被记为成交，也不会自动调整现金。</div></section><section class="card"><div class="card-header"><h2>板块分布</h2><small class="muted">证券市值内部权重</small></div>${exposure()}<div class="card-note">${m.missing_prices?.length ? "行情尚未完整，当前分布需谨慎解释。" : "证券所属行业按基础资料记录；ETF 分类不代表穿透后的全部持仓。"}</div></section></div><div class="grid-two"><section class="card"><div class="card-header"><h2>最近一次研究</h2>${stateBadge(latest?.status === "expired" || (latest?.result?.valid_until && new Date(latest.result.valid_until).getTime() <= Date.now()) ? "expired" : analysisState())}</div>${latest ? `<div class="card-body"><div class="analysis-meta">${badge(`输入 v${latest.input_version}`, "neutral")}<span>${date(latest.created_at)}</span><span>数据截至 ${date(latest.data_as_of)}</span>${latest.input_version !== a.version ? badge("已保存新版本 · 此结论待更新", "warning") : ""}</div><div class="report-inline">${markdown(latest.report || latest.result?.report || latest.result || "报告暂无正文")}</div><div class="divider"></div><a class="small" href="#research">查看研究报告与引用来源 ↗</a></div>` : empty("等待第一份研究", "配置支持原生联网的模型服务后，主 Agent 会结合持仓和候选范围决定如何开展研究。已有账户数据会继续保留。", "analyze", "立即分析")}</section><section class="card"><div class="card-header"><h2>研究动态</h2><a class="small" href="#agents">工作台 ↗</a></div><div class="card-body"><div class="analysis-meta"><span class="dot ${active.length ? "" : "warn"}"></span>${active.length ? `${active.length} 个账户任务进行中` : "暂无正在执行的账户任务"}</div>${
    state.jobs
      .slice(0, 3)
      .map(
        (j) =>
          `<div class="source-item"><div class="split-heading"><span class="source-title">${e(reasonLabel(j.reason))}</span>${stateBadge(j.state)}</div><div class="source-meta"><span>输入 v${j.input_version}</span><span>${date(j.created_at || j.updated_at)}</span></div>${j.error ? `<p class="error-detail">${e(j.error)}</p>` : ""}</div>`,
      )
      .join("") ||
    '<p class="small muted">保存持仓或点击“立即分析”后，会在这里记录任务状态。</p>'
  }</div><div class="card-note">关闭网页不会停止本地后台服务；Mac 睡眠期间任务暂停，唤醒后恢复。</div></section></div>${m.notes?.length ? `<div class="notice">${icon("info")}<div>${m.notes.map((n) => e(n)).join("<br>")}</div></div>` : ""}<div class="overview-bottom"><span>账户输入版本 v${a.version} · ${state.accounts.length} 个虚拟账户</span><span>行情时间与来源见每只证券明细</span></div>`;
}
function field(label, name, value = "", options = {}) {
  const {
    type = "text",
    placeholder = "",
    help = "",
    required = false,
    min,
    max,
    step,
  } = options;
  return `<div class="field ${options.full ? "span-two" : ""}"><label for="${e(name)}">${e(label)}${required ? " *" : ""}</label><input id="${e(name)}" name="${e(name)}" type="${e(type)}" value="${e(value ?? "")}" placeholder="${e(placeholder)}" ${required ? "required" : ""} ${min !== undefined ? `min="${min}"` : ""} ${max !== undefined ? `max="${max}"` : ""} ${step ? `step="${step}"` : ""} ${options.autocomplete ? 'autocomplete="off"' : ""}>${help ? `<small>${e(help)}</small>` : ""}</div>`;
}
function selectField(label, name, value, choices, help = "") {
  return `<div class="field"><label for="${e(name)}">${e(label)}</label><select id="${e(name)}" name="${e(name)}">${choices.map(([v, l]) => `<option value="${e(v)}" ${v === String(value) ? "selected" : ""}>${e(l)}</option>`).join("")}</select>${help ? `<small>${e(help)}</small>` : ""}</div>`;
}
let rowCounter = 0;
function holdingRow(holding = {}) {
  const id = ++rowCounter;
  return `<div class="holding-row">${field("证券代码与交易所", `symbol-${id}`, holding.symbol, { required: true, placeholder: "SH:600519 / SZ:000001", help: "股票用股，ETF / 基金用份" })}${field("持有数量（股 / 份）", `quantity-${id}`, holding.quantity, { type: "number", required: true, min: 0, step: 1, placeholder: "整数数量，不是手数" })}${field("单位成本价（选填）", `cost-${id}`, holding.cost_price, { type: "number", min: 0, step: "any", placeholder: "留空表示未知" })}<button type="button" class="button ghost danger" data-action="remove-holding" aria-label="移除此证券">${icon("close")}</button></div>`;
}
function holdings() {
  if (!state.account) return noAccount();
  const a = state.account,
    p = a.preferences || {};
  return `<form id="account-form" data-draft-key="account:${e(a.id)}" data-version="${a.version}"><section class="card"><div class="card-header"><h2>账户信息</h2>${badge(`已保存 v${a.version}`, "neutral")}</div><div class="card-body form-grid">${field("账户名称", "name", a.name, { required: true })}${field("现金余额（元，可留空）", "cash", a.cash, { type: "number", min: 0, step: "any", placeholder: "留空表示未录入，0 表示现金为零", help: "现金未知时只展示证券市值，不推断账户总资产。" })}</div></section><section class="card"><div class="card-header"><h2>持仓快照</h2><span class="small muted">保存后自动更新账户研究</span></div><div class="card-body"><div class="notice">${icon("info")}<div>证券身份由代码与交易所确定。未填写成本时不计算该证券的成本收益；修改数量只维护快照，不生成成交记录。</div></div><div id="holding-rows" class="holdings-edit">${(a.holdings || []).map(holdingRow).join("")}</div>${button("添加持仓证券", "add-holding", "add-row", "plus")}<details class="section-gap"><summary class="small muted">查找证券代码与基础资料</summary><div class="search-field section-gap"><input class="input" id="security-query" aria-label="证券代码或名称" placeholder="输入名称或代码，例如 沪深300 / 510300"><button type="button" class="button" data-action="lookup">${icon("search")}查找</button></div><div id="lookup-results" class="lookup-results" aria-live="polite"></div></details></div></section><section class="card"><div class="card-header"><h2>投资偏好</h2><span class="small muted">用于研究上下文，不替代 Agent 判断</span></div><div class="card-body form-grid">${selectField(
    "风险偏好",
    "risk",
    p.risk || "balanced",
    [
      ["conservative", "稳健"],
      ["balanced", "均衡"],
      ["aggressive", "进取"],
    ],
  )}${field("投资期限 / 关注窗口", "horizon", p.horizon || "", { placeholder: "例如：中长期，6–12 个月" })}${field("单只证券仓位偏好上限（%）", "max_position_pct", p.max_position_pct, { type: "number", min: 0, max: 100, step: "any", placeholder: "选填" })}${field("单一板块仓位偏好上限（%）", "max_sector_pct", p.max_sector_pct, { type: "number", min: 0, max: 100, step: "any", placeholder: "选填" })}</div><div class="form-actions"><span class="form-status">编辑尚未保存时，后台分析仍以版本 v${a.version} 为准。</span><button class="button primary" type="submit">保存并更新分析 ${icon("arrow")}</button></div></section></form><section class="card"><div class="card-header"><h2>虚拟账户</h2><span class="small muted">账户数据与分析独立保存</span></div><div class="card-body account-list">${state.accounts.map((account) => `<button class="account-mini ${account.id === a.id ? "current" : ""}" data-action="switch-account" data-id="${e(account.id)}"><h3>${e(account.name)}</h3><p>${account.holdings?.length ?? "—"} 只证券 · 输入 v${account.version}${account.id === a.id ? " · 当前账户" : ""}</p></button>`).join("")}</div><div class="form-actions"><span class="form-status">删除当前账户不会移除其他账户或自选范围仍需要的公共监控。</span>${button("删除此账户", "delete-account", "danger compact")}</div></section>`;
}
function symbolsText(list) {
  return array(list)
    .map((s) => (typeof s === "string" ? s : s.symbol))
    .filter(Boolean)
    .join("\n");
}
function market() {
  const u = state.universe,
    quotes = array(u.quotes);
  return `<div class="grid-equal"><form id="universe-form" data-draft-key="universe" class="card"><div class="card-header"><h2>配置研究范围</h2>${badge("公共范围", "neutral")}</div><div class="card-body form-grid"><div class="field span-two"><label for="watchlist">自选观察</label><textarea id="watchlist" name="watchlist" class="list-editor" placeholder="SH:600519\nSZ:000001">${e(symbolsText(u.watchlist))}</textarea><small>每行一个代码（也可用逗号分隔）。观察列表与实际持仓分开。</small></div><div class="field span-two"><label for="candidates">候选证券范围</label><textarea id="candidates" name="candidates" class="list-editor" placeholder="SH:510300\nSZ:159915">${e(symbolsText(u.candidates))}</textarea><small>Agent 可以在此范围内发现尚未持有的证券与板块机会。</small></div></div><div class="form-actions"><span class="form-status">持仓证券会自动纳入监控，无需重复填写。</span><button type="submit" class="button primary">保存范围</button></div></form><section class="card"><div class="card-header"><h2>当前监控集合</h2><small class="muted">${array(u.monitored).length} 只证券</small></div><div class="card-body">${
    array(u.monitored).length
      ? `<div class="tag-list">${array(u.monitored)
          .map(
            (s) =>
              `<span class="tag mono">${e(typeof s === "string" ? s : s.symbol)}${typeof s === "object" && s.name ? " · " + e(s.name) : ""}</span>`,
          )
          .join(
            "",
          )}</div><div class="divider"></div><p class="small muted">监控范围是全部账户持仓、自选、候选与仍在使用的研究需求的并集。单个账户移除证券，不会影响其他用途。</p>`
      : empty(
          "监控范围为空",
          "录入持仓或保存自选与候选证券，即可开始刷新行情。",
          "",
          "",
          "market",
        )
  }</div></section></div><section class="card"><div class="card-header"><h2>行情快照</h2><span class="small muted">数据源 ${e(state.settings.quote_provider || state.status.settings?.quote_provider || "待配置")}</span></div>${quotes.length ? `<div class="table-wrap"><table><thead><tr><th>证券</th><th class="align-right">最新价</th><th>行情时间（上海）</th><th>来源</th><th>状态</th><th></th></tr></thead><tbody>${quotes.map((q) => `<tr><td><div class="security-name">${e(q.name || q.symbol)}</div><div class="security-code">${e(q.symbol)}</div></td><td class="align-right numeric">${number(q.price ?? q.last_price)}</td><td>${date(q.as_of)}</td><td>${e(q.source || "未知")}</td><td>${stateBadge(q.status || "missing")}${q.error || q.last_error ? `<span class="cell-secondary negative">${e(q.error || q.last_error)}</span>` : ""}</td><td><button class="button compact" data-action="analyze-symbol" data-symbol="${e(q.symbol)}">研究此证券</button></td></tr>`).join("")}</tbody></table></div>` : empty("暂无行情快照", "点击“刷新行情”获取配置数据源的实际结果。失败时会保留原因，不使用随机价格填补。", "refresh-quotes", "刷新行情", "market")}<div class="card-note">行情刷新与深度研究独立运行。非交易时段、数据源延迟与停牌需结合实际状态解释；界面不承诺实时行情。</div></section>${publicResearch()}`;
}
function publicResearch() {
  return `<section class="card"><div class="card-header"><h2>公共市场、板块与候选研究</h2><a class="small" href="#research">查看账户建议 ↗</a></div><div class="card-body"><p class="small muted">公共发现可供多个账户复用；账户适配结论分别保存。不提供固定评分或未经验证的上涨概率。</p>${state.globalRuns.length ? `<details class="section-gap" data-key="global-runs"><summary>最近 ${state.globalRuns.length} 次全局研究任务</summary>${state.globalRuns.map((run) => `<div class="source-item"><div class="split-heading"><span>${e(reasonLabel(run.kind || run.reason || "candidate_scan"))}</span>${stateBadge(run.state)}</div>${researchVersion(run)}<p class="small muted">${date(run.created_at)}${run.error ? " · " + e(run.error) : ""}</p><button class="button compact" data-action="global-result" data-id="${e(run.id)}">报告与调用统计</button></div>`).join("")}</details>` : ""}${state.publicResearch.length ? state.publicResearch.map((record) => `<article class="source-item"><div class="split-heading"><h3>${e(record.title || record.symbol || record.security_id || record.sector || "公共研究")}</h3>${stateBadge(record.state || record.status)}</div>${researchVersion(record)}<div class="source-meta"><span>${e({ market: "市场研究", sector: "板块研究", candidate: "候选证券" }[record.kind] || record.kind || "研究记录")}</span><span>首次 ${date(record.first_seen_at || record.created_at)}</span><span>复核 ${date(record.reviewed_at || record.updated_at)}</span></div>${markdown(record.reason || record.summary || record.rationale || record.findings || record.body || "研究摘要未提供")}${record.findings ? `<details><summary>研究发现</summary>${objectView(record.findings)}</details>` : ""}${record.key_changes || record.changes ? `<h4>关键变化</h4>${objectView(record.key_changes || record.changes)}` : ""}${record.account_fit ? `<h4>当前账户适配</h4>${objectView(record.account_fit)}` : ""}${evidenceReferences(record)}</article>`).join("") : '<p class="small muted section-gap">尚无公共研究记录。全局扫描完成后在此展示；账户报告仍在研究页面单独保留。</p>'}</div></section>`;
}
function materialReference(id) {
  return `<button class="material-reference" type="button" data-action="open-material" data-id="${e(id)}">${e(id)}</button>`;
}
function evidenceReferences(record) {
  const resolvedReferences = new Map();
  let hasResolution = false;
  const collect = (value) => {
    if (Array.isArray(value)) value.forEach(collect);
    else if (value && typeof value === "object") {
      if (Array.isArray(value.references)) {
        hasResolution = true;
        value.references.forEach((ref) => {
          if (ref && typeof ref === "object") resolvedReferences.set(`${ref.status}:${ref.material_id || ""}:${ref.url || ""}`, ref);
        });
      }
      Object.entries(value).filter(([key]) => key !== "references").forEach(([, child]) => collect(child));
    }
  };
  collect(record.result || record);
  const references = [...resolvedReferences.values()];
  if (hasResolution) {
    return references.length ? `<div class="evidence-references"><h4>引用定位</h4>${references.map((ref) => referenceCard(ref)).join("")}</div>` : "";
  }
  const ids = new Set(String(record.report || record.result?.report || "").match(/\b[a-f0-9]{32}\b/g) || []);
  const visit = (value, key = "") => {
    if (typeof value === "string" && /^(material_id|material_ids|evidence_ids)$/.test(key)) ids.add(value);
    else if (Array.isArray(value)) value.forEach((item) => visit(item, key));
    else if (value && typeof value === "object") Object.entries(value).forEach(([name, item]) => visit(item, name));
  };
  visit(record.result || record);
  const refs = [...ids];
  return refs.length ? `<div class="evidence-references" data-reference-status="legacy_unknown">${badge("旧格式引用 · 尚未解析", "warning")}<p class="small muted">这些 ID 尚未确认可访问范围与原文位置。</p><div class="source-meta">${refs.map((id) => `<span class="mono">${e(id)}</span>`).join(" ")}</div></div>` : "";
}
function generatedMaterial(material) {
  return ["agent_extraction", "generated_analysis"].includes(material.representation_kind);
}
function originReferences(material, depth = 0) {
  if (!generatedMaterial(material)) return "";
  const origins = array(material.origin_references);
  if (material.origin_status !== "linked_materials" || !origins.length) {
    const unknown = material.origin_status === "linked_materials" || (!material.origin_status && (array(material.material_ids).length || array(material.source_material_ids).length));
    return `<p class="small muted origin-gap">${unknown ? "原文材料 ID 关联尚未解析，来源与页码为提取者提供。" : "未关联原文材料ID，来源与页码为提取者提供。"}</p>`;
  }
  return `<details class="origin-references"><summary class="small">关联来源材料（${origins.length} 条）</summary>${depth < 8 ? origins.map((origin) => referenceCard(origin, depth + 1)).join("") : '<p class="small muted">更深关联请按材料 ID 继续查看。</p>'}</details>`;
}
function referenceCard(ref, depth = 0) {
  const resolved = ref.status === "resolved" && ref.material_id;
  const sourceOnly = ref.status === "source_link";
  const generated = generatedMaterial(ref);
  const location = generated ? ref.claimed_source_range || ref.range : ref.range;
  const range = location?.returned_range;
  const requested = location?.requested_range;
  const pages = range?.start_page ? `${generated ? "提取者标注页码" : "已保存页码"} ${range.start_page}–${range.end_page || range.start_page}` : requested?.start_page ? `${generated ? "提取者请求页码" : "请求页码"} ${requested.start_page}–${requested.end_page || requested.start_page} · 实际返回范围未知` : "";
  const chars = ref.range?.char_count !== undefined ? `已保存${generated ? "生成文本" : ""} ${ref.range.char_count} 字符` : "";
  const access = resolved ? generated ? "已保存生成内容，不代表已保存外部原文" : accessLabel({ access: ref.access, truncated: ref.range?.truncated, read_completeness: ref.range?.read_completeness }) : "";
  const label = !resolved ? sourceOnly ? "仅来源链接 · 未定位本地原文" : "未解析引用" : ref.representation_kind === "agent_extraction" ? "已定位 Agent 提取 · 不是外部原文" : ref.representation_kind === "generated_analysis" ? "已定位生成报告 · 不是外部原文" : "已解析 · 可定位材料";
  return `<div class="evidence-reference" data-reference-status="${e(resolved ? "resolved" : sourceOnly ? "source_link" : "unresolved")}" data-reference-kind="${e(ref.representation_kind || "unknown")}"><div class="source-meta">${badge(label, resolved && !generated ? "neutral" : "warning")}${resolved ? materialReference(ref.material_id) : `<span class="mono">${e(ref.material_id || "")}</span>`}${ref.url ? link(ref.url, ref.title || "来源链接") : ""}</div>${resolved ? `<div class="source-meta"><span>${e(access)}</span>${ref.scope ? `<span>范围：${e(ref.scope === "public" ? "公开共享材料" : "当前账户材料")}</span>` : ""}${pages ? `<span>${e(pages)}</span>` : ""}${chars ? `<span>${e(chars)}</span>` : ""}${ref.document_version ? `<span>${generated ? "生成内容" : "文档"} v${e(ref.document_version)}</span>` : ""}</div>${originReferences(ref, depth)}` : ""}${ref.reason ? `<p class="small muted">${e({ unknown_or_inaccessible: "材料不存在或不在当前可访问范围", not_located_in_saved_material: "此来源链接尚未定位到已保存原文", saved_retrieval_failed: "原文获取失败，仅保留来源链接", reference_chain_not_resolved: "来源材料关联尚未解析" }[ref.reason] || ref.reason)}</p>` : ""}</div>`;
}
function researchVersion(record) {
  const version = record.universe_version;
  const current = record.applicability === "current";
  const label = current ? "当前研究范围" : record.applicability === "stale" ? "历史研究范围 · 不作为当前公共结论" : "旧格式 · 研究范围版本未知";
  return `<div class="source-meta research-version" data-applicability="${e(record.applicability || "legacy_unknown")}">${badge(label, current ? "neutral" : "warning")}${version !== null && version !== undefined ? `<span>候选范围 v${e(version)}</span>` : ""}</div>`;
}
function reportResearchScope(report) {
  const result = report.result || {};
  const shared = array(result.shared_research_used).filter((record) => record.scope === "public");
  return `<div class="report-research-scope"><div class="source-meta">${result.universe_version !== null && result.universe_version !== undefined ? badge(`本次报告研究范围 v${result.universe_version}`, "neutral") : badge("旧格式报告 · 研究范围版本未知", "warning")}</div>${shared.length ? `<details class="section-gap"><summary class="small">复用的公共研究（${shared.length} 条）</summary>${shared.map((record) => `<div class="source-meta"><span class="mono">${e(record.id)}</span><span>范围 ${record.universe_version !== null && record.universe_version !== undefined ? `v${e(record.universe_version)}` : "版本未知"}</span><span>使用时：${e({ current: "适用于当时范围", stale: "历史范围结论", legacy_unknown: "范围未知" }[record.applicability] || "范围未知")}</span></div>`).join("")}</details>` : ""}</div>`;
}
function accessLabel(material) {
  if (material.error || ["failed", "unavailable"].includes(material.access)) return "原文访问失败 / 不可访问";
  const limited = { source_link: "模型服务返回的来源链接 · 本地未下载原文", snippet: "仅检索摘要 · 未读取全文", search_excerpt: "仅检索摘要 · 未读取全文", pdf_search: "PDF 检索摘要 · 未读取全文", summary: "仅摘要 · 未读取全文", metadata: "仅文档元数据", generated_report: "系统生成报告 · 不是外部原文" }[material.access];
  if (limited) return limited;
  if ((material.source_truncated ?? material.truncated) || material.access === "excerpt" || material.read_completeness === "partial") return "部分内容 · 未读取全文";
  if (material.read_completeness === "unknown") return "读取完整性未知";
  if (["full", "full_text"].includes(material.access)) return material.read_completeness === "complete_extracted_text" ? "已保存完整可提取正文" : "已保存可提取正文 · 完整性未说明";
  return { reprint: "转载内容" }[material.access] || material.access || "访问范围未说明";
}
function materialVersion(material) {
  const kind = { native_search_source: "模型原生搜索来源", original_document: "原始文档的读取片段", summary: "摘要表示", text_representation: "文本表示 · 原始字节未知", agent_extraction: "Agent 提取结果", generated_analysis: "系统生成研究" }[material.representation_kind] || "历史材料 · 表示类型未知";
  const version = material.document_version ?? material.version;
  const identity = material.document_identity_kind === "raw_bytes_sha256" ? "已记录原始字节身份" : ["representation_sha256", "visible_excerpt_sha256", "source_metadata_sha256"].includes(material.document_identity_kind) ? "仅表示内容身份 · 不代表原文哈希" : "原始内容身份未知";
  return `<div class="source-meta material-version"><span>${e(kind)}</span>${version ? `<span>版本 v${e(version)}</span>` : version === 0 ? "<span>观测顺序未确认 · 不提升为最新更正</span>" : ""}<span>${e(identity)}</span>${material.document_version_id ? `<span class="mono">文档版本 ${e(material.document_version_id)}</span>` : ""}</div>`;
}
function sourceCard(material) {
  const category = material.classification || material.category || "unknown";
  const generated = generatedMaterial(material);
  const sources = material.source || {};
  const url = material.url || sources.url;
  const title = material.source_title || sources.title || material.title || "未提供来源标题";
  const summary = material.summary || material.claim || material.text || material.content || material.excerpt;
  const range = material.returned_range;
  const requested = material.requested_range || material.extracted_range;
  const savedContentLabel = material.access === "pdf_search" ? "检索结果" : material.access === "metadata" ? "结构化数据" : generated ? "生成内容" : "正文";
  const pages = Array.isArray(material.pages) ? material.pages : [];
  return `<article class="source-item" id="material-${e(material.id)}"><div class="split-heading">${badge(`${generated ? "提取者分类 · " : ""}${classificationNames[category] || category}`, generated || category.includes("opinion") ? "warning" : "neutral")}${material.id ? materialReference(material.id) : ""}</div><div class="source-title">${link(url, title)}</div>${generated ? `<p class="small muted">${material.representation_kind === "agent_extraction" ? "已定位 Agent 提取 · 不是外部原文" : "已定位生成报告 · 不是外部原文"}</p>${originReferences(material)}` : ""}<p class="source-content">${e(summary ? String(summary).slice(0, 900) : (material.access === "source_link" ? "仅保存服务返回的来源链接，本地没有网页正文。" : "索引暂无摘要，可按需打开已保存内容。"))}${String(summary || "").length > 900 ? "…" : ""}</p><div class="source-meta"><span>发布者：${e(material.publisher || sources.publisher || "未知")}</span><span>发布时间：${material.published_at ? date(material.published_at) : "未知"}</span><span>事件 / 报告期：${e(material.event_period || "未知")}</span><span>采集：${material.fetched_at ? date(material.fetched_at) : "未知"}</span></div><div class="source-meta"><span>访问范围：${e(generated ? "已保存生成内容，不代表已保存外部原文" : accessLabel(material))}</span>${material.page_count ? `<span>${generated ? "提取者标注文档" : "PDF 共"} ${e(material.page_count)} 页${generated ? "" : "（物理页码）"}</span>` : ""}${range?.start_page ? `<span>${generated ? "提取者标注页码" : "已保存页码"} ${e(range.start_page)}–${e(range.end_page || range.start_page)}；${link(`${url?.split("#")[0]}#page=${range.start_page}`, generated ? "查看提取者提供的来源位置" : "定位原 PDF")}</span>` : requested?.start_page ? `<span>请求 / 提取页码 ${e(requested.start_page)}–${e(requested.end_page || requested.start_page)} · 实际保存页码未知</span>` : ""}${array(material.symbols || material.security_ids).length ? `<span>关联：${e((material.symbols || material.security_ids).join("、"))}</span>` : ""}</div>${pages.length ? `<div class="source-meta">${pages.map((page) => link(page.source_url || `${url?.split("#")[0]}#page=${page.page}`, `${generated ? "提取者标注第" : "第"} ${page.page} 页${page.truncated ? "（部分）" : ""}`)).join(" · ")}</div>` : ""}${materialVersion(material)}${material.error || material.uncertainties || material.conflicts ? `<p class="error-detail">${e(material.error || material.uncertainties || material.conflicts)}</p>` : ""}${material.id ? `<button class="button compact section-gap" data-action="open-material" data-id="${e(material.id)}">按段查看已保存${savedContentLabel}</button>` : ""}</article>`;
}
async function openMaterial(id, start = 0) {
  const material = await api(`/api/materials/${encodeURIComponent(id)}/range?start=${start}&length=6000${state.accountId ? `&account_id=${encodeURIComponent(state.accountId)}` : ""}`);
  const text = material.text || material.content || material.claim || "此材料没有可读取的正文；请查看访问状态。";
  const total = material.total_chars ?? material.total_length ?? text.length;
  const end = material.end ?? start + text.length;
  dialog("研究证据 · 已保存材料", `${sourceCard(material)}<div class="source-meta">已保存文本字符 ${start + 1}–${end} / ${total}。此范围不代表已获取完整来源文档。</div><pre class="material-text">${e(text)}</pre>`, `<button class="button compact" data-action="material-range" data-id="${e(id)}" data-start="${Math.max(0, start - 6000)}" ${start === 0 ? "disabled" : ""}>上一段</button><button class="button compact" data-action="material-range" data-id="${e(id)}" data-start="${material.next_start ?? end}" ${material.next_start === null || end >= total ? "disabled" : ""}>下一段</button>${button("关闭", "close-dialog", "primary")}`);
}
function recommendationState(rec) {
  const saved = rec.review_status || rec.state || rec.status || "watching";
  if (["revoked", "expired"].includes(saved)) return saved;
  if (rec.valid_until && new Date(rec.valid_until).getTime() <= Date.now()) return "expired";
  if (rec.input_version !== undefined && rec.input_version !== state.account?.version) return "needs_review";
  return saved;
}
function recommendationCard(rec) {
  const actions = { watch: "观察", observe: "观察", buy_candidate: "买入候选", add_candidate: "增持候选", hold: "持有", reduce_candidate: "减仓候选", exit_candidate: "退出候选" };
  const review = recommendationState(rec);
  const priceLabels = { met: "价格条件已满足", not_met: "价格条件未满足", stale: "报价过期 · 条件待更新", uncomputable: "价格条件暂不可计算", no_conditions: "未设价格条件", unknown: "价格条件尚未检查" };
  const reviewLabels = { valid: "判断已复核", watching: "判断仍在跟踪", triggered: "判断仍在跟踪", needs_review: "判断待复核", expired: "已过期", revoked: "已撤销" };
  const followup = rec.review_followup;
  return `<article class="source-item recommendation" data-recommendation-id="${e(rec.id)}"><div class="split-heading"><h3>${e(rec.symbol || rec.security_id || "账户")} · ${e(actions[rec.action] || rec.action || "观察")}</h3>${badge(reviewLabels[review] || review, review === "needs_review" ? "warning" : "neutral")}</div><div class="source-meta">${badge(priceLabels[rec.price_state] || priceLabels.unknown, ["met", "stale", "uncomputable"].includes(rec.price_state) ? "warning" : "neutral")}${rec.observed_price !== undefined ? `<span>观察价格 ${number(rec.observed_price)}</span>` : ""}${rec.price_observed_at ? `<span>${date(rec.price_observed_at)}</span>` : ""}</div>${rec.price_state === "met" && review === "needs_review" ? '<p class="small muted">数值条件满足；投资判断尚未重新确认。</p>' : ""}<div class="source-meta"><span>持仓 v${e(rec.input_version)}</span><span>生成 ${date(rec.generated_at || rec.created_at)}</span><span>${rec.valid_until ? `有效期至 ${date(rec.valid_until)}` : "未设固定有效期"}</span></div>${rec.basis ? markdown(rec.basis) : ""}${array(rec.price_conditions).length ? `<div class="section-gap"><h4>可计算条件</h4>${objectView(rec.price_conditions)}</div>` : ""}${rec.agent_conditions ? `<div class="section-gap"><h4>仍需 Agent 判断</h4>${objectView(rec.agent_conditions)}</div>` : ""}${rec.review_reason ? `<p class="error-detail">${e(rec.review_reason)}</p>` : ""}${followup ? `<div class="source-meta recommendation-followup">复核处理：${e({ pending: "等待安排", assigned: "已安排研究", completed: "本轮研究已完成", waiting_review: "本轮未解决 · 可重新安排复核", cancelled: "已取消 · 可重新安排复核", superseded: "已由新版任务接替", failed: "处理失败，可重试" }[followup.state] || followup.state || "已登记")}${followup.job_id ? `<span>任务 ${e(followup.job_id)}</span>` : ""}</div>` : ""}${review === "needs_review" && !["pending", "assigned"].includes(followup?.state) ? `<button class="button compact section-gap" data-action="retry-recommendation" data-id="${e(rec.id)}">重新安排复核</button>` : ""}${evidenceReferences(rec)}</article>`;
}
function recommendations() {
  const activeTotal = state.recommendationCoverage.recommendations?.total;
  const historyTotal = state.recommendationCoverage.recommendations?.counts?.history;
  return `<section class="card" id="active-recommendations"><div class="card-header"><h2>活跃建议与待复核事项</h2><span class="small muted">${Number.isInteger(activeTotal) ? `共 ${activeTotal} 条 · ` : ""}条件观察不执行交易</span></div><div class="card-body">${state.recommendations.length ? state.recommendations.map(recommendationCard).join("") : '<p class="small muted">当前没有仍需关注的结构化建议。</p>'}${pager("recommendations", state.recommendations)}</div></section><section class="card" id="recommendation-history"><div class="card-header"><h2>建议历史</h2><button class="button compact" data-action="recommendation-history" aria-expanded="${state.showRecommendationHistory}">${state.showRecommendationHistory ? "收起历史" : "查看历史"}${Number.isInteger(historyTotal) ? `（${historyTotal}）` : ""}</button></div>${state.showRecommendationHistory ? `<div class="card-body"><p class="small muted">已撤销或已过期建议保留供追溯。</p>${state.recommendationHistory.length ? state.recommendationHistory.map(recommendationCard).join("") : '<p class="small muted">当前页没有历史建议。</p>'}${pager("recommendationHistory", state.recommendationHistory)}</div>` : ""}</section>`;
}
function reportCard(report, index) {
  const a = state.account, expired = report.result?.valid_until && new Date(report.result.valid_until).getTime() <= Date.now();
  return `<article class="card"><div class="report"><div class="report-header"><div><h3>${e(a.name)} · ${report.symbol ? e(report.symbol) : "账户综合研究"}</h3><div class="report-date">${date(report.created_at)} · 数据截至 ${date(report.data_as_of)}</div></div>${stateBadge(expired ? "expired" : report.status)}</div><div class="analysis-meta">${badge(`输入版本 v${report.input_version}`, "neutral")}${report.input_version !== a.version ? badge("账户已修改 · 历史版本", "warning") : badge("与当前输入版本一致", "neutral")}${report.result?.valid_until ? `<span>适用至 ${date(report.result.valid_until)}</span>` : ""}<span class="mono">${e(report.id)}</span></div>${index === 0 ? markdown(report.report || report.result?.report || report.result || "报告正文为空") : `<details data-key="report:${e(report.id)}"><summary class="small">展开历史报告</summary><div class="section-gap">${markdown(report.report || report.result?.report || report.result || "报告正文为空")}</div></details>`}${reportResearchScope(report)}${evidenceReferences(report)}</div>${report.result?.findings ? `<details class="disclosure" data-key="findings:${e(report.id)}"><summary>本次研究发现与材料索引</summary><div class="card-body">${objectView(report.result.findings)}</div></details>` : ""}${report.result?.agents ? `<details class="disclosure" data-key="agents:${e(report.id)}"><summary>子 Agent 研究成果</summary><div class="card-body">${objectView(report.result.agents)}</div></details>` : ""}</article>`;
}
function research() {
  if (!state.account) return noAccount();
  const filtered = state.materials.filter((m) => state.materialFilter === "all" || (m.classification || m.category || "unknown") === state.materialFilter);
  return `${recommendations()}<div class="grid-two"><div>${state.analyses.length ? state.analyses.map(reportCard).join("") : `<section class="card">${empty("当前页暂无研究报告", "配置真实模型后开始研究；历史报告可通过分页查看。", "analyze", "开始研究")}</section>`}${pager("analyses", state.analyses)}</div><section class="card"><div class="card-header"><h2>研究材料索引</h2><span class="small muted">当前页 ${state.materials.length} 条</span></div><div class="card-body"><form id="material-search-form" data-draft-key="material-search:${e(state.accountId)}" class="search-field"><input id="material-query" name="query" class="input" value="${e(state.materialQuery)}" placeholder="检索已保存正文、标题或证券" aria-label="检索材料"><button type="submit" class="button compact">检索</button></form><div class="split-heading section-gap"><span class="small muted">分类筛选当前页；保留未知与冲突</span><select id="material-filter" class="input source-filter" aria-label="筛选材料分类"><option value="all">全部材料</option>${[...new Set(state.materials.map((m) => m.classification || m.category || "unknown"))].map((c) => `<option value="${e(c)}" ${state.materialFilter === c ? "selected" : ""}>${e(classificationNames[c] || c)}</option>`).join("")}</select></div>${filtered.length ? filtered.map(sourceCard).join("") : empty("当前页暂无此类材料", "原文按需分段读取；摘要或访问失败会明确标注。")}${pager("materials", state.materials)}</div></section></div>`;
}
function reasonLabel(reason) {
  if (String(reason).startsWith("manual_security:"))
    return `指定证券研究 · ${String(reason).slice(16)}`;
  if (String(reason).startsWith("retry:"))
    return `重试 · ${reasonLabel(String(reason).slice(6))}`;
  return (
    {
      configuration_changed: "配置更新后的分析",
      universe_changed: "候选范围更新后的分析",
      manual: "手动立即分析",
      account_updated: "账户更新后的分析",
      account_changed: "账户更新后的分析",
      account_saved: "账户快照更新",
      holdings_changed: "持仓更新后的分析",
      scheduled: "定时账户研究",
      periodic: "定时账户研究",
      market_scan: "板块与候选扫描",
      candidate_scan: "全局板块与候选扫描",
      recommendation_review: "建议条件变化后的复核",
      close_summary: "收盘总结",
      price_change: "行情变化研究",
      news: "新公开信息研究",
      retry: "失败重试",
      recovery: "服务恢复后的研究",
    }[reason] ||
    reason ||
    "账户研究"
  );
}
function eventMessage(event) {
  if (event.message) return event.message;
  const p = event.payload || {};
  return (
    p.message ||
    p.summary ||
    p.content ||
    p.error ||
    p.note ||
    p.task ||
    p.goal ||
    p.tool_name ||
    p.tool ||
    statusNames[event.type] ||
    event.type
  );
}
function eventHTML(event) {
  const p = event.payload;
  return `<div class="event"><div class="event-head"><strong>${e({ manager: "主 Agent", system: "后台服务" }[event.agent || event.agent_id] || event.agent || event.agent_id || "主 Agent")}</strong><span>${e({ demo_summary: "演示数值摘要", published: "结果发布", tool_started: "工具开始", tool_result: "工具返回", agent_created: "创建子任务", agent_started: "任务开始", agent_completed: "任务完成", agent_progress: "阶段进展", agent_failed: "任务失败", agent_cancelled: "任务取消", message_sent: "发送补充消息", followup: "继续研究", native_search: "模型原生联网", synthesis_checkpoint: "汇总检查点已保存", synthesis_resuming: "正在恢复汇总", budget_closeout: "正在整理最终报告", model_usage: "模型请求诊断" }[event.type] || event.type || "进展")}</span><time>${date(event.at || event.timestamp, true)}</time></div><p>${e(eventMessage(event))}</p>${p && typeof p === "object" && Object.keys(p).length ? `<details><summary>查看事件与来源详情</summary><pre>${e(JSON.stringify(p, null, 2))}</pre></details>` : ""}</div>`;
}
function jobCard(job) {
  const events = state.events[job.id];
  const explanation = { waiting_config: "等待模型配置。补齐环境变量并重启服务，或保存可用设置后，最新持仓版本的任务会恢复。", running: "正在研究。工具与子任务事件按增量更新。", failed: "本次研究失败，已保存的账户数据不受影响。可查看原因后重试最新版本。" }[job.state];
  return `<article class="job-card"><div class="job-header"><div class="agent-avatar">${icon("agents")}</div><div class="job-heading"><h3>${e(reasonLabel(job.reason))}</h3><div class="job-meta">输入 v${job.input_version} · ${date(job.created_at || job.updated_at)} · <span class="mono">${e(job.id)}</span></div></div>${stateBadge(job.state)}</div>${job.synthesis_checkpoint?.available ? `<div class="card-body synthesis-checkpoint"><p>已保存研究检查点 · 重试时优先恢复汇总</p><p class="small muted">版本、行情、资料时效与新事件检查通过后复用；不会将子报告当作最终分析。</p></div>` : ""}${explanation || job.error ? `<div class="card-body"><div class="notice ${job.state === "failed" ? "error" : ""}"><div>${e(explanation || "任务状态说明")}${job.error ? `<p>${e(job.error)}</p>` : ""}</div></div></div>` : ""}<details class="job-event-details" data-job-id="${e(job.id)}" data-key="job:${e(job.id)}" ${state.openedJobs.has(job.id) ? "open" : ""}><summary>研究事件 · ${job.event_count ?? events?.items.length ?? 0} 条</summary><div class="job-events">${events?.items.length ? events.items.map(eventHTML).join("") : '<p class="small muted">展开后按需读取事件。尚无事件时等待工作器更新。</p>'}${events?.more ? `<button class="button compact" data-action="more-events" data-id="${e(job.id)}">继续读取后续事件</button>` : ""}</div></details><div class="job-actions">${["running", "pending", "queued", "waiting_config"].includes(job.state) ? `<button class="button compact danger" data-action="cancel-job" data-id="${e(job.id)}">取消任务</button>` : ""}${["failed", "cancelled", "blocked", "waiting_config", "superseded"].includes(job.state) ? `<button class="button compact" data-action="retry-job" data-id="${e(job.id)}">重试最新版本 ${icon("refresh")}</button>` : ""}<button class="button compact" data-action="job-result" data-id="${e(job.id)}">任务结果与调用统计</button></div></article>`;
}
function agents() {
  if (!state.account) return noAccount();
  return `<div class="notice">${icon("agents")}<div>主 Agent 自主决定任务拆分与补充调查。可见事件按需增量读取，不展示隐藏思维链。前端关闭后，本机后台服务仍会继续工作。</div></div><div class="split-heading"><h2>${e(state.account.name)} 的任务</h2><span class="small muted">当前页 ${state.jobs.length} 个任务</span></div><div class="jobs-list">${state.jobs.length ? state.jobs.map(jobCard).join("") : `<section class="card">${empty("当前页暂无任务", "保存账户或手动分析会创建任务。", "analyze", "立即分析", "agents")}</section>`}</div>${pager("jobs", state.jobs)}${demandTrace()}`;
}
function demandTrace() {
  if (!state.demands.length) return "";
  return `<section class="card"><div class="card-header"><h2>研究需求与合并记录</h2><span class="small muted">最近 ${state.demands.length} 条</span></div><div class="card-body"><p class="small muted">到期、事件与配置需求独立保留。合并或等待当前研究结束时，可按任务 ID 追溯。</p>${state.demands.map((demand) => `<div class="source-item"><div class="split-heading"><span>${e(reasonLabel(demand.kind || demand.reason))}</span>${badge({ pending: "待安排", assigned: "已绑定任务", completed: "已完成", superseded: "已被新版替代" }[demand.state] || demand.state, demand.state === "pending" ? "warning" : "neutral")}</div><div class="source-meta"><span>输入 v${e(demand.input_version)}</span><span>${date(demand.created_at)}</span><span>任务 ${e(demand.job_id || "尚未分配")}</span></div><p class="small muted">${e(demand.detail || demand.reason || "等待安排")}</p></div>`).join("")}</div></section>`;
}
function runtimeStatus() {
  const w = state.status.worker || {},
    c = state.status.calendar || {};
  if (typeof w !== "object" || typeof c !== "object")
    return e(String(w) + " · " + String(c));
  return `后台服务：${w.running ? "运行中" : "未启动"} · 当前任务 ${e(w.active ?? 0)}<br>市场：${{ closed: "已收盘", preopen: "开盘前", morning: "上午交易时段", afternoon: "下午交易时段", lunch: "午间休市", unknown: "日历状态未知", non_trading: "非交易日", open: "交易时段" }[c.phase] || e(c.phase || "未知")} · ${c.trading_day === true ? "交易日" : c.trading_day === false ? "非交易日" : "交易日待确认"}<br>日历覆盖：${e(c.coverage?.start || "未知")} — ${e(c.coverage?.end || "未知")}${c.reason ? `<br>${e(c.reason)}` : ""}${c.source ? `<br>${link(c.source, "查看日历来源")}` : ""}${w.last_error ? `<p class="error-detail">最近错误：${e(w.last_error)}</p>` : ""}`;
}
function nativeSearchStatus() {
  const status = state.status.native_search || {status: "unverified"};
  return e({disabled: "已关闭", unverified: "尚未验证", observed: "已观察到原生搜索", unsupported: "当前协议或端点不支持", authentication_failed: "认证失败", request_failed: "请求失败"}[status.status] || "尚未验证");
}
function settings() {
  const s = state.settings;
  return `<form id="settings-form" data-draft-key="settings"><section class="card"><div class="settings-section"><div class="settings-title">${icon("agents")}<div><h2>运行时模型</h2><p>服务商、协议与模型均可替换。需实测工具调用能力。</p></div></div><div class="form-grid">${selectField(
    "模型接口协议",
    "model_protocol",
    s.model_protocol || "responses",
    [
      ["chat_completions", "OpenAI 兼容 Chat Completions"],
      ["responses", "OpenAI Responses"],
    ],
    "“兼容”声明不代表已通过工具调用与状态续接验证。",
  )}${field("模型名称", "model_name", s.model_name || "", { placeholder: "填写服务商实际提供的模型 ID" })}${field("模型基础 URL", "model_base_url", s.model_base_url || "", { placeholder: "https://api.openai.com/v1" })}${field("密钥环境变量名称", "model_key_env", s.model_key_env || "MODEL_API_KEY", { placeholder: "MODEL_API_KEY", help: "只填写变量名，不填写密钥值。请在本机 .env 配置密钥并重启服务。" })}${selectField(
    "本机无密钥模型服务",
    "model_allow_no_key",
    String(s.model_allow_no_key || false),
    [
      ["false", "关闭：需要密钥"],
      ["true", "允许：仅限 localhost / 127.0.0.1"],
    ],
    "只有模型地址为本机回环地址时才能免密钥。",
  )}${field("模型请求超时（秒）", "model_request_timeout_seconds", s.model_request_timeout_seconds ?? 90, { type: "number", min: 5, max: 600, step: 1 })}</div></div><div class="settings-section"><div class="settings-title">${icon("search")}<div><h2>搜索与数据源</h2><p>行情元数据与网页研究材料分别记录。</p></div></div><div class="form-grid">${selectField(
    "模型原生联网",
    "model_native_search",
    String(s.model_native_search ?? true),
    [["true", "开启：沿用当前模型服务与密钥"], ["false", "关闭"]],
    "主 Agent 与子 Agent 自主搜索；需端点实际支持原生联网及本地工具共存，无需独立搜索 Key。",
  )}${selectField(
    "行情数据适配器",
    "quote_provider",
    s.quote_provider || "eastmoney",
    [
      ["eastmoney", "东方财富公开行情"],
      ["manual", "仅使用已录入行情 / 禁止联网刷新"],
    ],
    "公开接口可能限流、延迟或变更；刷新失败会保留实际错误。",
  )}${selectField("备用行情数据源", "quote_fallback_provider", s.quote_fallback_provider || "sina", [["sina", "新浪公开网页报价"], ["none", "关闭"]], "东方财富请求失败时使用；保留实际来源与时间，不绕过限流，不需要付费密钥。")}${field("行情刷新间隔（秒）", "quote_interval_seconds", s.quote_interval_seconds ?? 60, { type: "number", min: 30, max: 3600, step: 1, help: "实际频率受数据源能力、市场状态与网络约束。" })}<div class="field span-two"><label for="news_queries">公开信息观察检索（每行一个）</label><textarea id="news_queries" name="news_queries" placeholder="可按行业、政策或公告主题配置">${e(array(s.news_queries).join("\n"))}</textarea><small>新材料进入研究上下文，由主 Agent 判断相关性与是否继续调查。</small></div></div></div><div class="settings-section"><div class="settings-title">${icon("research")}<div><h2>资讯扫描与研究数据</h2><p>交易时段之外仍按配置扫描；数据权限和真实联网能力需单独验证。</p></div></div><div class="form-grid three">${field("资讯扫描间隔（分钟）", "news_interval_minutes", s.news_interval_minutes ?? 30, { type: "number", min: 1, max: 1440, step: 1 })}${field("资讯扫描开始时（上海）", "news_start_hour", s.news_start_hour ?? 8, { type: "number", min: 0, max: 24, step: 1 })}${field("资讯扫描结束时（上海）", "news_end_hour", s.news_end_hour ?? 23, { type: "number", min: 0, max: 24, step: 1 })}${selectField("研究数据适配器", "research_provider", s.research_provider || "eastmoney", [["eastmoney", "东方财富公开接口"], ["tushare", "Tushare（需要凭据与权限）"]])}${field("Tushare 密钥环境变量", "tushare_token_env", s.tushare_token_env || "TUSHARE_TOKEN", { help: "只保存环境变量名，不填写实际 Token。" })}${field("报价缓存复用（秒）", "quote_cache_seconds", s.quote_cache_seconds ?? 30, { type: "number", min: 0, max: 3600, step: 1 })}${field("行情刷新总超时（秒）", "quote_refresh_timeout_seconds", s.quote_refresh_timeout_seconds ?? 45, { type: "number", min: 5, max: 300, step: 1 })}${field("文档下载缓存（秒）", "read_cache_seconds", s.read_cache_seconds ?? 300, { type: "number", min: 0, max: 3600, step: 1 })}</div></div><div class="settings-section"><div class="settings-title">${icon("clock")}<div><h2>后台研究节奏</h2><p>Asia/Shanghai · 非交易日不按正常交易时段触发</p></div></div><div class="form-grid three">${selectField("定时研究", "scheduled_research_enabled", String(s.scheduled_research_enabled ?? true), [["true", "开启"], ["false", "暂停定时研究"]], "暂停后持仓保存、手动研究与已持久化事件仍正常处理。")}${field("板块及候选扫描（分钟）", "scan_interval_minutes", s.scan_interval_minutes ?? 5, { type: "number", min: 1, step: 1 })}${field("账户综合分析（分钟）", "analysis_interval_minutes", s.analysis_interval_minutes ?? 15, { type: "number", min: 1, step: 1 })}${field("收盘后总结时间", "close_summary_time", s.close_summary_time || "15:15", { type: "time" })}${field("行情变化研究阈值（%）", "price_trigger_pct", s.price_trigger_pct ?? 3, { type: "number", min: 0, step: "any", help: "触发增量研究的工程参数，不是买卖信号。" })}</div></div><div class="settings-section"><div class="settings-title">${icon("shield")}<div><h2>任务资源与预算</h2><p>用于本机负载、超时与调用成本管理，不对材料观点进行准入评分。</p></div></div><div class="form-grid three">${field("并行子 Agent 上限", "max_concurrent_agents", s.max_concurrent_agents ?? 3, { type: "number", min: 1, max: 8, step: 1 })}${field("每次研究模型调用上限", "max_model_calls", s.max_model_calls ?? 24, { type: "number", min: 2, max: 300, step: 1 })}${field("单个 Agent 超时（秒）", "agent_timeout_seconds", s.agent_timeout_seconds ?? 180, { type: "number", min: 5, max: 1800, step: 1 })}${field("账户分析超时（秒）", "analysis_timeout_seconds", s.analysis_timeout_seconds ?? 600, { type: "number", min: 10, max: 3600, step: 1 })}${field("Agent 工具循环轮数上限", "max_agent_turns", s.max_agent_turns ?? 12, { type: "number", min: 2, max: 60, step: 1 })}${field("单次输出 token 上限", "max_output_tokens", s.max_output_tokens ?? 4096, { type: "number", min: 256, max: 32000, step: 1 })}</div></div><div class="form-actions"><span class="form-status">配置变更保存后生效。环境变量变更需要重启后端。</span><div class="actions">${button("检测原生联网", "probe-native", "", "search")}${button("检测本地工具续接", "probe", "", "refresh")}<button type="submit" class="button primary">保存设置</button></div></div></section></form><section class="card"><div class="card-header"><h2>运行状态与数据维护</h2>${badge(state.status.mode === "demo" ? "演示数据库" : "真实模式数据库", state.status.mode === "demo" ? "warning" : "neutral")}</div><div class="card-body"><div class="form-grid"><div><h3>模型与搜索</h3><p class="small muted section-gap">模型：${state.status.model_configured ? "已配置密钥（需实测能力）" : "尚未配置"}<br>原生联网：${nativeSearchStatus()}<br>统一时区：${e(state.status.timezone || "Asia/Shanghai")}</p></div><div><h3>后台工作器与交易日历</h3><div class="small muted section-gap">${runtimeStatus()}</div></div></div><div id="probe-result"></div><div class="divider"></div><p class="small muted">备份由 SQLite 在线备份机制生成。恢复前请停止后台服务，并按项目 README 中的恢复命令操作。Mac 睡眠时不执行任务；唤醒或重启后刷新过期数据，旧提醒不集中补发。</p>${state.status.mode === "demo" ? `<div class="section-gap">${button("创建固定演示账户", "demo-seed", "", "plus")}<span class="small muted"> 仅写入演示数据库；不伪造 Agent 运行。</span></div>` : ""}</div></section>`;
}
function rememberDrafts() {
  $$('form[data-draft-key]').forEach((form) => {
    if (form.dataset.discard !== "true" && (form.dataset.dirty === "true" || form.contains(document.activeElement)))
      state.drafts.set(form.dataset.draftKey, form);
  });
}
function clearDraft(form) {
  form.dataset.discard = "true";
  state.drafts.delete(form.dataset.draftKey);
}
function render() {
  rememberDrafts();
  const focused = document.activeElement;
  const selection = focused && "selectionStart" in focused ? [focused.selectionStart, focused.selectionEnd] : null;
  const openDetails = new Set($$("#main details[open][data-key]").map((node) => node.dataset.key));
  const scroll = window.scrollY;
  $("#app").innerHTML = shell();
  $$('form[data-draft-key]').forEach((form) => {
    const draft = state.drafts.get(form.dataset.draftKey);
    if (draft) form.replaceWith(draft);
  });
  $$("#main details[data-key]").forEach((node) => { if (openDetails.has(node.dataset.key)) node.open = true; });
  if (focused?.isConnected) {
    focused.focus({ preventScroll: true });
    if (selection && focused.setSelectionRange && selection[0] !== null) focused.setSelectionRange(...selection);
  }
  window.scrollTo({ top: scroll });
}
function dialog(title, content, footer = "") {
  $("#dialog").innerHTML =
    `<div class="dialog-header"><h2>${e(title)}</h2>${button("关闭", "close-dialog", "ghost", "close")}</div><div class="dialog-content">${content}</div>${footer ? `<div class="dialog-footer">${footer}</div>` : ""}`;
  $("#dialog").showModal();
}
function createAccountDialog() {
  dialog(
    "创建虚拟账户",
    `<p>创建独立的持仓与研究空间。可先只填写名称，随后添加证券和投资偏好。</p><form id="new-account-form">${field("账户名称", "new-account-name", "", { required: true, placeholder: "例如：长期观察账户" })}<div class="section-gap">${field("现金余额（选填）", "new-account-cash", "", { type: "number", min: 0, step: "any", placeholder: "留空表示未知" })}</div><div class="actions section-gap"><button type="submit" class="button primary">创建账户</button></div></form>`,
  );
  $("#new-account-name").focus();
}
function parseSymbols(text) {
  return [
    ...new Set(
      text
        .split(/[\s,，;；]+/)
        .map((s) => s.trim().toUpperCase())
        .filter(Boolean),
    ),
  ];
}
function requestAccountSwitch(id) {
  // Navigation and writes may still be settling; retain the latest user choice.
  if (state.busy) {
    state.pendingAccountId = id;
    return;
  }
  return operation(() => switchAccount(id));
}
async function switchAccount(id) {
  rememberDrafts();
  state.pages = { analyses: 0, jobs: 0, materials: 0, recommendations: 0, recommendationHistory: 0 };
  state.showRecommendationHistory = false;
  state.recommendationHistory = [];
  state.recommendationCoverage = {};
  state.accountId = id;
  localStorage.setItem("guanlan.account", id);
  await loadAccount();
  render();
}
async function analyze(symbol) {
  if (!state.account) {
    createAccountDialog();
    return;
  }
  const job = await api(
    `/api/accounts/${encodeURIComponent(state.accountId)}/analyze`,
    { method: "POST", body: JSON.stringify(symbol ? { symbol } : {}) },
  );
  toast(`已创建${symbol ? "证券" : "账户"}研究任务，绑定当前输入版本。`);
  await loadAccount();
  render();
  return job;
}
document.addEventListener("click", (event) => {
  if (event.target.closest(".skip-link")) {
    event.preventDefault();
    $("#main")?.focus();
    $("#main")?.scrollIntoView();
    return;
  }
  const target = event.target.closest("[data-action]");
  if (!target) return;
  const action = target.dataset.action;
  if (action === "new-account") return createAccountDialog();
  if (action === "close-dialog") return $("#dialog").close();
  if (action === "add-holding") {
    $("#account-form").dataset.dirty = "true";
    return $("#holding-rows").insertAdjacentHTML("beforeend", holdingRow());
  }
  if (action === "remove-holding") {
    $("#account-form").dataset.dirty = "true";
    return target.closest(".holding-row").remove();
  }
  if (action === "go-holdings") {
    location.hash = "holdings";
    return;
  }
  if (action === "delete-account") {
    dialog(
      "删除当前虚拟账户",
      `<p>将删除「${e(state.account.name)}」及其账户专属数据。其他账户与公共证券监控不受影响。此操作无法直接撤销，可先备份本地数据。</p>`,
      `${button("取消", "close-dialog")}${button("确认删除账户", "confirm-delete", "danger")}`,
    );
    return;
  }
  operation(async () => {
    if (action === "page") {
      const kind = target.dataset.kind;
      if (!(kind in state.pages)) return;
      state.pages[kind] = Math.max(0, Number(target.dataset.page) || 0);
      await loadAccount();
      render();
    } else if (action === "retry-recommendation") {
      await api(`/api/accounts/${encodeURIComponent(state.accountId)}/recommendations/${encodeURIComponent(target.dataset.id)}/retry`, { method: "POST", body: "{}" });
      await loadAccount();
      render();
      toast("已登记复核需求，处理状态可在建议与 Agent 工作台查看。");
    } else if (action === "recommendation-history") {
      state.showRecommendationHistory = !state.showRecommendationHistory;
      await loadAccount();
      render();
    } else if (action === "open-material" || action === "material-range") {
      await openMaterial(target.dataset.id, Number(target.dataset.start) || 0);
    } else if (action === "more-events") {
      await loadJobEvents(target.dataset.id);
      render();
    } else if (action === "global-result") {
      const run = await api(`/api/global-research/${encodeURIComponent(target.dataset.id)}`);
      dialog("公共研究报告与调用统计", `${researchVersion(run)}${objectView(run.result || { state: run.state, error: run.error })}${evidenceReferences(run)}`);
    } else if (action === "job-result") {
      const job = await api(`/api/jobs/${encodeURIComponent(target.dataset.id)}`);
      dialog("任务结果与可观察统计", objectView(job.result || { state: job.state, error: job.error, synthesis_checkpoint: job.synthesis_checkpoint, message: "尚无已提交结果；调用统计可查看工具事件。" }));
    } else if (action === "refresh") {
      await refresh(true);
      render();
      toast("状态已更新");
    } else if (action === "switch-account")
      await switchAccount(target.dataset.id);
    else if (action === "analyze") await analyze();
    else if (action === "analyze-symbol") await analyze(target.dataset.symbol);
    else if (action === "confirm-delete") {
      await api(`/api/accounts/${encodeURIComponent(state.accountId)}`, {
        method: "DELETE",
      });
      $("#dialog").close();
      state.accountId = null;
      await refresh(true);
      render();
      toast("虚拟账户已删除");
    } else if (action === "refresh-quotes") {
      const result = await api("/api/quotes/refresh", {
        method: "POST",
        body: "{}",
      });
      await refresh(true);
      render();
      const errors = result?.errors;
      toast(
        errors &&
          (Array.isArray(errors) ? errors.length : Object.keys(errors).length)
          ? `行情刷新完成，部分数据失败：${typeof errors === "string" ? errors : JSON.stringify(errors)}`
          : "行情刷新已完成，请以快照中的时间和状态为准。",
        Boolean(
          errors &&
            (Array.isArray(errors)
              ? errors.length
              : Object.keys(errors).length),
        ),
      );
    } else if (action === "lookup") {
      const q = $("#security-query").value.trim();
      if (!q) return;
      const result = await api(`/api/securities?q=${encodeURIComponent(q)}`);
      const securities = array(result, "securities");
      $("#lookup-results").innerHTML = securities.length
        ? securities
            .map(
              (s) =>
                `<button type="button" class="lookup-result" data-action="pick-security" data-symbol="${e(s.symbol)}"><span>${e(s.name)} · ${e(s.kind || "证券")}</span><span class="mono">${e(s.symbol)} ＋</span></button>`,
            )
            .join("")
        : '<p class="small muted">未找到证券。可直接填写交易所与代码，保存时由后端校验。</p>';
    } else if (action === "pick-security") {
      $("#account-form").dataset.dirty = "true";
      $("#holding-rows").insertAdjacentHTML(
        "beforeend",
        holdingRow({ symbol: target.dataset.symbol }),
      );
      toast("已添加证券行，请填写持有数量");
    } else if (action === "cancel-job") {
      await api(`/api/jobs/${encodeURIComponent(target.dataset.id)}/cancel`, {
        method: "POST",
        body: "{}",
      });
      await loadAccount();
      render();
      toast("已请求取消任务");
    } else if (action === "retry-job") {
      await api(`/api/jobs/${encodeURIComponent(target.dataset.id)}/retry`, {
        method: "POST",
        body: "{}",
      });
      await loadAccount();
      render();
      toast("已按最新账户版本重新排队");
    } else if (action === "backup") {
      const result = await api("/api/backup", { method: "POST", body: "{}" });
      dialog(
        "本地备份已创建",
        `<p>备份保存在后端返回的本机位置。恢复前停止服务，并使用项目提供的恢复命令。</p><pre class="json-block">${e(result.path || result.filename || JSON.stringify(result))}</pre>`,
        button("完成", "close-dialog", "primary"),
      );
    } else if (action === "probe-native") {
      const result = await api("/api/settings/probe-native-search", {method: "POST", body: "{}"});
      await refresh(true);
      render();
      dialog("模型原生联网实测", objectView(result));
    } else if (action === "probe") {
      const result = await api("/api/settings/probe", {
        method: "POST",
        body: "{}",
      });
      $("#probe-result").innerHTML =
        `<div class="divider"></div><h3>已保存配置的实测结果</h3><pre class="json-block">${e(JSON.stringify(result, null, 2))}</pre>`;
      toast("能力测试已返回，请查看真实结果。");
    } else if (action === "demo-seed") {
      await api("/api/demo/seed", { method: "POST", body: "{}" });
      await refresh(true);
      render();
      toast("固定演示账户已创建；行情不代表当前价格。");
    }
  });
});
document.addEventListener("submit", (event) => {
  const form = event.target;
  if (form.id === "material-search-form") {
    event.preventDefault();
    return operation(async () => {
      state.materialQuery = new FormData(form).get("query").trim();
      clearDraft(form);
      state.pages.materials = 0;
      state.materialFilter = "all";
      await loadAccount();
      render();
    });
  }
  if (
    ![
      "account-form",
      "new-account-form",
      "universe-form",
      "settings-form",
    ].includes(form.id)
  )
    return;
  event.preventDefault();
  operation(async () => {
    const values = new FormData(form);
    if (form.id === "new-account-form") {
      const created = await api("/api/accounts", {
        method: "POST",
        body: JSON.stringify({
          name: values.get("new-account-name").trim(),
          cash: values.get("new-account-cash") || null,
          holdings: [],
          preferences: { risk: "balanced" },
        }),
      });
      state.accountId = created.id;
      $("#dialog").close();
      await refresh(true);
      location.hash = "holdings";
      render();
      toast("账户已创建，可以录入持仓");
    } else if (form.id === "account-form") {
      const holdings = $$(".holding-row", form).map((row) => ({
        symbol: $('input[name^="symbol-"]', row).value.trim().toUpperCase(),
        quantity: $('input[name^="quantity-"]', row).value,
        cost_price: $('input[name^="cost-"]', row).value || null,
      }));
      const payload = {
        name: values.get("name").trim(),
        cash: values.get("cash") || null,
        holdings,
        preferences: {
          ...state.account.preferences,
          risk: values.get("risk"),
          horizon: values.get("horizon") || "",
          max_position_pct: values.get("max_position_pct") || null,
          max_sector_pct: values.get("max_sector_pct") || null,
        },
        expected_version: Number(form.dataset.version),
      };
      await api(`/api/accounts/${encodeURIComponent(state.accountId)}`, {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      clearDraft(form);
      await refresh(true);
      render();
      toast(
        `账户已保存为 v${state.account.version}，指标已更新，新分析已排队。`,
      );
    } else if (form.id === "universe-form") {
      state.universe = await api("/api/universe", {
        method: "PUT",
        body: JSON.stringify({
          watchlist: parseSymbols(values.get("watchlist")),
          candidates: parseSymbols(values.get("candidates")),
        }),
      });
      clearDraft(form);
      await refresh(true);
      render();
      toast("自选与候选范围已保存");
    } else if (form.id === "settings-form") {
      const payload = { ...state.settings };
      for (const [key, value] of values.entries()) payload[key] = value;
      for (const key of [
        "max_concurrent_agents",
        "max_model_calls",
        "agent_timeout_seconds",
        "analysis_timeout_seconds",
        "max_agent_turns",
        "max_output_tokens",
        "quote_interval_seconds",
        "scan_interval_minutes",
        "analysis_interval_minutes",
        "price_trigger_pct",
        "model_request_timeout_seconds",
        "news_interval_minutes",
        "news_start_hour",
        "news_end_hour",
        "quote_cache_seconds",
        "quote_refresh_timeout_seconds",
        "read_cache_seconds",
      ])
        payload[key] = Number(payload[key]);
      payload.model_allow_no_key = payload.model_allow_no_key === "true";
      payload.model_native_search = [true, "true"].includes(payload.model_native_search);
      payload.scheduled_research_enabled = [true, "true"].includes(payload.scheduled_research_enabled);
      payload.news_queries = String(payload.news_queries)
        .split("\n")
        .map((s) => s.trim())
        .filter(Boolean);
      state.settings = await api("/api/settings", {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      clearDraft(form);
      await refresh(true);
      render();
      toast("运行配置已保存，密钥环境变量修改后需重启后端。");
    }
  });
});
document.addEventListener("toggle", (event) => {
  const details = event.target;
  if (!details.matches?.("details[data-job-id]") || !details.isConnected) return;
  const id = details.dataset.jobId;
  if (!details.open) {
    state.openedJobs.delete(id);
    return;
  }
  state.openedJobs.add(id);
  if (!state.events[id]) operation(async () => {
    await loadJobEvents(id);
    render();
  });
}, true);
document.addEventListener("input", (event) => {
  const form = event.target.closest('form[data-draft-key]');
  if (form) form.dataset.dirty = "true";
});
document.addEventListener("change", (event) => {
  const form = event.target.closest('form[data-draft-key]');
  if (form) form.dataset.dirty = "true";
  if (event.target.id === "account-switch")
    requestAccountSwitch(event.target.value);
  if (event.target.id === "material-filter") {
    state.materialFilter = event.target.value;
    render();
  }
});
window.addEventListener("hashchange", () => {
  state.route = location.hash.slice(1) || "overview";
  if (state.route !== "research") state.pages.analyses = 0;
  if (state.route !== "agents") state.pages.jobs = 0;
  render();
  window.scrollTo({ top: 0 });
  operation(async () => {
    await refresh(state.route === "settings");
    render();
  });
});
function changeSignature() {
  return JSON.stringify({
    account: state.account?.id,
    version: state.account?.version,
    status: state.account?.analysis_state,
    prices: state.account?.metrics?.positions,
    accounts: state.accounts.map((a) => [a.id, a.version]),
    reports: state.analyses.map((a) => [a.id, a.status]),
    jobs: state.jobs.map((j) => [j.id, j.state, j.last_event_seq, j.error]),
    materials: state.materials.map((m) => [m.id, m.version]),
    universe: state.universe,
    recommendations: state.recommendations,
    recommendationHistory: state.recommendationHistory,
    recommendationCoverage: state.recommendationCoverage,
    publicResearch: state.publicResearch,
    demands: state.demands,
    globalRuns: state.globalRuns,
    events: Object.entries(state.events).map(([id, events]) => [id, events.cursor]),
    runtime: [state.status.model_configured, state.status.worker?.running],
    error: state.error,
    partial: state.partialError,
  });
}
let polling = false;
setInterval(async () => {
  if (
    polling ||
    state.busy ||
    document.hidden ||
    $("#dialog").open
  )
    return;
  polling = true;
  const route = state.route,
    signature = changeSignature();
  try {
    await refresh();
    if (state.route !== route || state.busy || $("#dialog").open) return;
    if (signature !== changeSignature()) {
      render();
    }
    const clock = $("#clock");
    if (clock) clock.textContent = date(new Date().toISOString()) + " · 上海";
  } catch (error) {
    state.error = error.message;
    if (state.route === route && !state.busy) render();
  } finally {
    polling = false;
  }
}, 4000);
try {
  await refresh(true);
  render();
} catch (error) {
  state.error = error.message;
  $("#app").innerHTML =
    `<main class="startup-error"><div class="eyebrow">GUANLAN · LOCAL RESEARCH</div><h1>暂时无法连接本地服务</h1><p>请确认后端已启动，并从后端提供的本地地址打开页面。已保存的数据不会因页面连接失败而丢失。</p><pre class="json-block">${e(error.message)}</pre>${button("重新连接", "refresh", "primary", "refresh")}</main>`;
}
