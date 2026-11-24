// Rendering consumes server-normalized usage. It never infers a cache miss
// from an absent field or averages request percentages in the browser.
const escape = value => String(value ?? "").replace(/[&<>"']/g, c =>
  ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"})[c]);
const number = value => typeof value === "number" && Number.isFinite(value)
  ? value.toLocaleString("zh-CN", {maximumFractionDigits: 6}) : "未知";
const percent = value => typeof value === "number" && Number.isFinite(value)
  ? `${number(value * 100)}%` : "未知";
const stage = value => ({research:"研究", synthesis:"汇总", final_submission:"最终提交",
  cold:"首次请求", continuation:"续接"})[value] || value || "未记录";
const reason = value => ({first:"首次请求", first_request:"首次请求", initial:"首次请求", append_only:"尾部追加",
  unchanged:"输入未变", compaction:"上下文压缩", synthesis:"进入汇总",
  tools_changed:"工具权限变化", context_epoch_changed:"上下文 epoch 变化",
  recovery_rebuild:"恢复重建"})[value] || value || "未记录";
const category = value => ({failed:"失败请求", first_session_request:"会话首轮",
  first_epoch_request:"新 epoch 首轮", continuation:"正常续接"})[value] || value || "未记录";
const usage = request => request.normalized_usage || request;

export function requestUsageText(request) {
  const u = usage(request);
  return `模型 ${request.model || request.model_name || "未记录"}：已发送 ${request.reasoning_effort_sent || request.reasoning_effort || "未知"}，服务回报 ${request.reported_reasoning_effort || "未知"}；输入 ${number(u.input_tokens)}，缓存读取 ${number(u.cache_read_tokens)}，未缓存输入 ${number(u.uncached_input_tokens)}，输出 ${number(u.output_tokens)}，推理 ${number(u.reasoning_tokens)}，可见输出 ${number(u.visible_output_tokens)}，缓存写入 ${number(u.cache_write_tokens)}；${stage(request.stage)}，${reason(request.prefix_diagnostic?.change_reason)}；费用${request.cost_known && request.cost != null ? "见服务计费原始记录" : "未知"}。`;
}

export function modelUsageView(summary, identity = "usage") {
  if (!summary?.cache_summary) return "";
  const c = summary.cache_summary;
  const totals = summary.normalized_totals;
  const totalFields = [["input_tokens", "输入"], ["cache_read_tokens", "缓存读取"],
    ["uncached_input_tokens", "未缓存输入"], ["output_tokens", "输出"],
    ["reasoning_tokens", "推理"], ["visible_output_tokens", "可见输出"], ["cache_write_tokens", "缓存写入"]];
  const totalView = totals ? `<p class="small usage-totals">已报告总量：${totalFields.map(([field, label]) =>
    `${label} ${number(totals[field])}（覆盖 ${percent(totals.coverage?.[field])}）`).join(" · ")}。${summary.duration_ms == null ? "" : `累计耗时 ${number(summary.duration_ms)} ms。`}总量按各字段已回报请求累计，缓存率只采用输入与缓存读取均有回报的成对样本。</p>` : "";
  const rows = Array.isArray(summary.sequence) ? summary.sequence : [];
  const sessions = new Map();
  for (const r of rows) {
    const key = `${r.agent_id || "未知实例"}:${r.session_id || "未知会话"}`;
    if (!sessions.has(key)) sessions.set(key, []);
    sessions.get(key).push(r);
  }
  return `<details class="model-usage" data-key="${escape(identity)}:model-usage"><summary>模型调用与缓存 · ${escape(c.requests)} 次请求</summary><div class="section-gap"><p class="small">已报告样本加权缓存读取率 ${percent(c.weighted_read_ratio)}（缓存读取总量 ÷ 同口径输入总量）；计量完整 ${escape(c.complete_requests)} / ${escape(c.requests)}，未知 ${escape(c.unknown_requests)}，覆盖率 ${percent(c.coverage)}。</p>${totalView}<p class="small">成对样本输入 ${number(c.reported_input_tokens)} · 缓存读取 ${number(c.cache_read_tokens)} · 未缓存输入 ${number(c.uncached_input_tokens)}${c.anomalous_requests ? ` · 异常计数 ${escape(c.anomalous_requests)} 次，保留原值` : ""}。缺失不计为 0；本地工具缓存不计入此比例。费用按可靠计费回报展示，未知时不估算。</p>${[...sessions.entries()].map(([key, requests]) => `<details data-key="${escape(identity)}:session:${escape(key)}"><summary>Agent ${escape(requests[0].agent_id || "未记录")} · 会话 ${escape(requests[0].session_id || "未记录")} · ${requests.length} 次</summary><div class="table-wrap"><table><thead><tr><th>请求 / epoch</th><th>阶段 / 前缀</th><th>输入</th><th>缓存读取</th><th>未缓存输入</th><th>输出</th><th>耗时 / 费用</th></tr></thead><tbody>${requests.map((r, index) => {
    const u = usage(r), p = r.prefix_diagnostic || {};
    return `<tr data-read-key="${escape(identity)}:request:${escape(r.local_request_id || `${key}:${index}`)}"><td>${escape(r.local_request_id || index + 1)}<br>epoch ${escape(r.cache_epoch ?? "未知")}</td><td>${escape(stage(r.stage))}<br>${escape(reason(p.change_reason || r.cache_epoch_reason))}<br>${escape(category(r.request_category))}${r.error ? `<br>${escape(r.error)}` : ""}</td><td>${number(u.input_tokens)}</td><td>${number(u.cache_read_tokens)}</td><td>${number(u.uncached_input_tokens)}</td><td>${number(u.output_tokens)}<br>推理 ${number(u.reasoning_tokens)}<br>可见 ${number(u.visible_output_tokens)}<br>缓存写入 ${number(u.cache_write_tokens)}</td><td>${r.duration_ms == null ? "未知" : `${number(r.duration_ms)} ms`}<br>${r.cost_known && r.cost != null ? escape(typeof r.cost === "object" ? JSON.stringify(r.cost) : r.cost) : "费用未知"}</td></tr>`;
  }).join("")}</tbody></table></div></details>`).join("")}</div></details>`;
}
