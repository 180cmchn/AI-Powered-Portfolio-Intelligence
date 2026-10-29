// Provider secrets stay only in the write form until submission; never in state/drafts.
export function createSixthUI({state, api, e, render, toast, operation, field, selectField, dialog, objectView}) {
  const routes = new Set(['providers', 'roles', 'account-models', 'forecasts']);
  const data = {providers: [], roles: [], catalogs: {}, predictions: null, batches: [], rule: null, filters: {}, offset: 0, comparison: null, comparisonAccounts: [], windows: null, windowOffset: 0};
  let read = 0;
  const enc = encodeURIComponent;
  const direction = {up:'上涨', sideways:'震荡', down:'下跌', abstain:'弃权'};
  const statuses = {waiting_config:'等待配置',planned:'计划使用',requested:'已观察到请求',historical_request_unknown:'历史调用状态未知',published:'已发布', open:'待发布', not_fetched:'尚未获取', fetching:'获取中', success:'已获取', empty:'真实空列表', partial:'部分列表', auth_error:'认证失败', authentication_failed:'认证失败', rate_limited:'限流', unsupported:'接口未支持', timeout:'超时', format_error:'格式异常', network_error:'网络错误', stale:'历史缓存', predicted:'已预测', pending_maturity:'待到期', waiting_data:'待数据', unavailable:'不可评估', unevaluable:'不可评估', unassessable:'不可评估', abstain:'明确弃权', failed:'任务失败', uncovered:'未覆盖', missed:'超过截止', evaluated:'已评估'};
  const title = v => statuses[v] || v || '尚未获取';
  const sourceTitle = v => ({global_default:'全局默认',account_override:'账户覆盖'}[v] || v || '未配置');
  const providerTitle = id => data.providers.find(p=>p.id===id)?.display_name || id || '未配置';
  const pct = v => v === null || v === undefined ? '暂无已评估样本' : `${(100 * Number(v)).toFixed(1)}%`;
  const button = (label, action, id='', extra='') => `<button type="button" class="button" data-sixth-action="${e(action)}" data-id="${e(id)}" ${extra}>${e(label)}</button>`;
  const info = text => `<p class="small muted">${e(text)}</p>`;
  const providers = () => data.providers.filter(p => !p.archived);
  async function load() {
    if (!routes.has(state.route)) return;
    const generation = ++read, route = state.route, account = state.accountId;
    let result;
    if (route === 'providers') result = await api('/api/providers');
    else if (route === 'roles' || route === 'account-models') {
      result = await Promise.all([api('/api/providers'), route === 'roles' ? api('/api/agent-roles') : account ? api(`/api/accounts/${enc(account)}/agent-models`) : Promise.resolve([])]);
    } else {
      const filters = {...data.filters};
      if (!Object.hasOwn(filters, 'account_id') && account) filters.account_id = account;
      const query = new URLSearchParams({...Object.fromEntries(Object.entries(filters).filter(([,v])=>String(v ?? '').trim())), offset: data.offset, limit: 30});
      data.comparisonAccounts=data.comparisonAccounts.filter(id=>state.accounts.some(a=>a.id===id));
      const comparisonQuery=new URLSearchParams();
      for(const key of ['kind','security_id','rule_version','start','end'])if(filters[key]?.trim())comparisonQuery.set(key,filters[key].trim());
      for(const id of data.comparisonAccounts)comparisonQuery.append('account_ids',id);
      result = await Promise.all([api(`/api/forecasts?${query}`), api(`/api/forecast-batches${filters.account_id ? '?account_id='+enc(filters.account_id):''}`), account ? api(`/api/accounts/${enc(account)}/forecast-settings`) : Promise.resolve(null), data.comparisonAccounts.length>=2 ? api(`/api/forecasts/compare?${comparisonQuery}`) : Promise.resolve(null), api(`/api/forecast-window-runs?${new URLSearchParams({...Object.fromEntries(['account_id','start','end'].filter(k=>filters[k]).map(k=>[k,filters[k]])),limit:30,offset:data.windowOffset})}`)]);
    }
    if (generation !== read || route !== state.route || account !== state.accountId) return;
    if (route === 'providers') data.providers = result;
    else if (route === 'roles' || route === 'account-models') {
      [data.providers, data.roles] = result;
      for (const provider of providers()) {
        const cat = await api(`/api/providers/${enc(provider.id)}/models`);
        if (generation !== read || route !== state.route || account !== state.accountId) return;
        data.catalogs[provider.id] = cat;
      }
    } else [data.predictions, data.batches, data.rule, data.comparison, data.windows] = result;
  }
  function providerForm(provider=null) {
    return `<form id="provider-connection-form" data-provider-id="${e(provider?.id || '')}" autocomplete="off"><div class="form-grid">${field('API Key', 'api_key', '', {type:'password', help:provider ? '留空保留现有凭据。替换会生成新凭据修订。' : '保存到本机安全凭据存储，数据库和 GET 不返回原文。'})}${field('Base URL', 'base_url', provider?.base_url || '', {placeholder:'https://api.example.com/v1'})}${selectField('协议', 'protocol', provider?.protocol || 'responses', [['responses','Responses'],['chat_completions','Chat Completions']])}</div><div class="form-actions"><span class="small muted">保存后自动获取该连接实际返回的模型列表。</span><button class="button primary" type="submit">保存连接并获取模型</button></div></form>`;
  }
  function providerPage() {
    return `<section class="card"><div class="card-header"><h2>添加模型供应商</h2></div><div class="card-body">${providerForm()}</div></section>${data.providers.map(p=>{
      const c=p.catalog || {}, models=c.models || [];
      return `<section class="card"><div class="card-header"><div><h2>${e(p.display_name)}</h2>${info(`${p.protocol} · 连接修订 ${p.revision} · ${p.key_configured ? '凭据已配置' : '未配置凭据'}${p.archived ? ' · 已归档' : ''}`)}</div><span class="pill">${e(title(c.status))}</span></div><div class="card-body"><p class="mono">${e(p.base_url)}</p>${info(c.error || `${models.length} 个模型 · ${c.complete ? '完整列表' : '覆盖范围未完整'} · ${c.fetched_at || '尚未发现'}`)}${c.cache_expired ? '<p class="warning">缓存已过期；历史发现不代表当前服务验证。</p>':''}<details><summary>查看实际模型目录</summary><div class="model-list">${models.map(m=>`<span class="pill" title="${e(m.suitability || '')}">${e(m.id)}</span>`).join('') || info('没有可选择的已发现模型；请检查此供应商是否提供目录接口。')}</div></details><div class="actions section-gap">${button('修改连接','provider-edit',p.id)}${button('刷新模型列表','provider-refresh',p.id)}${c.next_url ? button('继续加载模型','provider-more',p.id):''}${button('清除凭据','provider-clear',p.id)}${!p.archived ? button('归档连接','provider-archive',p.id):''}</div></div></section>`;
    }).join('')}<section class="card"><div class="card-body">${info('模型列出、请求被接受、本地函数可用、原生联网可用与服务明确回报 effort 是不同状态。目录发现不进行批量付费模型探测。Chat 协议保留现有原生联网限制。')}${info('备份只包含凭据引用。换机后请在此重新录入 Key；旧环境变量凭据可继续兼容使用。')}</div></section>`;
  }
  function modelOptions(providerId, selected, query='') {
    const c=data.catalogs[providerId] || data.providers.find(p=>p.id===providerId)?.catalog || {};
    const models = c.selectable === false ? [] : c.models || [];
    const filtered=models.filter(m=>m.id.toLowerCase().includes(query.toLowerCase()));
    return `<option value="">请选择已发现模型</option>${selected && !models.some(m=>m.id===selected) ? `<option disabled selected value="">当前绑定 ${e(selected)}（历史绑定，需重新确认）</option>`:''}${filtered.map(m=>`<option value="${e(m.id)}" ${m.id===selected?'selected':''}>${e(m.id)}${m.suitability==='unsuitable'?'（非对话用途）':''}</option>`).join('')}`;
  }
  function roleCard(role, accountMode) {
    const resolved=role.resolved || {}, bind=role.default_binding || {}, override=role.override || {};
    const selected=accountMode ? override.provider_id || '' : bind.provider_id || '';
    const selectedModel=accountMode ? override.model_id || '' : bind.model_id || '';
    const effort=accountMode ? override.reasoning_effort || '' : bind.reasoning_effort || 'medium';
    const id=`role-${accountMode?state.accountId:'global'}-${role.role_id}`;
    const effective=`${resolved.provider_display_name || resolved.provider_id || '未选择'} / ${resolved.model_id || '未选择模型'} / ${resolved.reasoning_effort || 'medium'}`;
    return `<section class="card role-card" data-role-id="${e(role.role_id)}"><div class="card-header"><div><h2>${e(role.name)} <span class="small mono">${e(role.role_id)}</span></h2>${info(role.responsibility)}</div><span class="pill">v${e(role.version)}${role.enabled?'':' · 全局归档'}</span></div><form class="card-body role-model-form" data-role-id="${e(role.role_id)}" data-account-id="${accountMode?e(state.accountId):''}" data-draft-key="${e(id)}"><p class="small"><strong>最终值：</strong>${e(effective)}<br><strong>来源：</strong>模型 ${e(sourceTitle(resolved.binding_source))} · 强度 ${e(sourceTitle(resolved.effort_source))}</p>${accountMode? info(`全局默认：${providerTitle(bind.provider_id)} / ${bind.model_id || '未配置'} / ${bind.reasoning_effort || 'medium'}`):''}${role.config_error?`<p class="error-text">配置错误：${e(role.config_error)}</p>`:''}${role.config_warning?info(role.config_warning):''}<div class="form-grid three"><div class="field"><label>供应商<select name="provider_id" data-role-provider><option value="">${accountMode?'继承全局模型绑定':'请选择供应商'}</option>${providers().map(p=>`<option value="${e(p.id)}" ${selected===p.id?'selected':''}>${e(p.display_name)}</option>`).join('')}</select></label></div><div class="field"><label>搜索已发现模型<input type="search" data-model-filter placeholder="仅筛选目录，不创建模型名" autocomplete="off"></label><label>模型<select name="model_id" data-role-model ${!selected?'disabled':''}>${modelOptions(selected,selectedModel)}</select></label><small data-catalog-status>${e(accountMode&&!selected?'继承全局模型目录':title(data.catalogs[selected]?.status))}</small></div>${selectField('reasoning_effort','reasoning_effort',effort,[...(accountMode?[['','继承全局强度']]:[]),...['low','medium','high','xhigh','max'].map(x=>[x,x])])}</div>${!accountMode?`<details class="section-gap"><summary>编辑全局职责、提示词与工具</summary><div class="form-grid section-gap">${field('角色名称','name',role.name)}${field('职责','responsibility',role.responsibility)}<label class="field span-two">系统提示词<textarea name="system_prompt" rows="9">${e(role.system_prompt)}</textarea></label><label class="field span-two">工具（每行一个已注册名称）<textarea name="tools" rows="4">${e((role.tools || []).join('\n'))}</textarea></label>${role.role_id==='manager'?'':selectField('全局状态','enabled',String(role.enabled),[['true','全部账户可用'],['false','全部账户归档']])}</div></details>`:''}<div class="form-actions">${info(accountMode?'清除覆盖只恢复默认，不会移除角色。':'所有账户使用相同的职责和工具。正在运行的任务保留原快照。')}<div class="actions">${accountMode?button('清除账户覆盖','role-reset',role.role_id,`data-account-id="${e(state.accountId)}"`):''}<button type="submit" class="button primary">${accountMode?'保存本账户覆盖':'保存全局角色'}</button></div></div></form></section>`.replace(/ (id|for)="([^"]+)"/g, (_, attr, value)=>` ${attr}="${e(id)}-${value}"`);
  }
  function rolesPage(accountMode=false) {
    if (accountMode && !state.accountId) return '<section class="card"><div class="card-body">请先创建虚拟账户。</div></section>';
    return `<div class="notice"><p>${accountMode?'此账户继承全部统一角色，仅覆盖每行的供应商模型和思考强度。':'统一角色目录适用于全部账户；主 Agent 按每轮目标自主选择子角色和任务。'} 本轮实际调用请查看 <a href="#agents">Agent 工作台</a>。</p></div>${data.roles.map(r=>roleCard(r,accountMode)).join('')}`;
  }
  function rulesForm() {
    const r=data.rule;if(!r)return '';
    return `<section class="card"><details><summary class="card-header">当前账户的固定批次设置 · ${r.enabled?'已启用，每交易日生成研究会产生模型费用':'未启用新批次'}</summary><form class="card-body" id="forecast-rule-form" data-account-id="${e(state.accountId)}" data-draft-key="forecast-rule-${e(state.accountId)}"><div class="form-grid three">${selectField('正式预测批次','enabled',String(r.enabled),[['false','关闭新批次'],['true','启用，每个交易日一次']])}${field('交易日期限（逗号分隔）','horizons',r.horizons.join(','))}${field('对应标签（逗号分隔）','labels',r.labels.join(','))}${field('震荡阈值（±百分数）','threshold',String(Number(r.threshold)*100),{type:'number',min:0,max:99,step:'any'})}${field('批次触发时间（上海）','start_time',r.start_time,{type:'time'})}${field('当日生成截止（上海）','deadline_time',r.deadline_time,{type:'time'})}</div>${info('基准为 D 收盘，D 本身不计入期限。默认 ±0.5% 为整个周期的固定区间，边界属于震荡。修改只影响以后批次；新功能不会自动补造历史预测。')}<div class="form-actions"><span>旧预测继续按原规则到期评分。</span><button type="submit" class="button primary">保存预测规则</button></div></form></details></section>`;
  }
  function comparisonCard() {
    if(state.accounts.length<2)return `<section class="card"><div class="card-body"><h2>账户共同样本比较</h2>${info('创建至少两个账户后，可比较相同批次日、证券、期限和规则的共同样本。')}</div></section>`;
    const comparison=data.comparison;
    const unmatched=comparison ? Object.entries(comparison.unmatched_counts).map(([id,count])=>`${state.accounts.find(a=>a.id===id)?.name || id}：${count}`).join('；') : '';
    return `<section class="card"><div class="card-header"><h2>账户共同样本比较</h2></div><div class="card-body"><form id="forecast-comparison-form" data-draft-key="forecast-comparison"><fieldset><legend>选择 2 至 10 个账户</legend><div class="actions">${state.accounts.map(a=>`<label><input type="checkbox" name="account_ids" value="${e(a.id)}" ${data.comparisonAccounts.includes(a.id)?'checked':''}> ${e(a.name)}</label>`).join('')}</div></fieldset>${info('沿用下方记录类型、证券、起止日期与规则筛选。单账户、主模型与团队筛选不限制此处的账户对比。')}<div class="form-actions"><span>只对全部所选账户共同且均已评估的样本计算并排准确率。</span><button type="submit" class="button primary">比较共同样本</button></div></form>${comparison ? `<div class="notice"><p>同基准交易日、同证券、同周期、同规则：${comparison.overlap_expected} 个共同预期样本，其中 ${comparison.overlap_evaluated} 个全部已评估。<br>各账户未重合样本：${e(unmatched)}。<br>${e(comparison.pairing_rule || '')}<br>${e(comparison.note)}</p></div>${comparison.groups.length ? comparison.groups.map(g=>`<section class="section-gap"><h3>${g.horizon} 个交易日 · 规则 ${e(g.rule_version.slice(0,12))}</h3>${info(`共同预期 ${g.overlap_expected}；共同已评估 ${g.overlap_evaluated}`)}<div class="table-wrap"><table><thead><tr><th>账户</th><th>共同样本命中 / 已评估</th><th>共同样本准确率</th></tr></thead><tbody>${g.accounts.map(a=>`<tr><td>${e(a.name)}</td><td>${a.hits} / ${a.evaluated}</td><td>${pct(a.accuracy)}</td></tr>`).join('')}</tbody></table></div></section>`).join('') : info('没有同时满足基准日、证券、周期和规则的共同样本；不比较各自不重合样本的百分比。')}` : info('选择账户后显示共同样本和未重合数量，不生成模型排名。')}</div></section>`;
  }
  function windowsCard() {
    const page=data.windows?.page || {}, stats=data.windows?.statistics || {};
    const labels={scheduled:'尚未到窗口',pending:'窗口内待建立',batch_created:'已建立批次',completed:'批次已完成',failed:'批次失败',incomplete:'批次未完成',missed:'错过已知窗口',disabled:'明确暂停 / 未启用',non_trading_day:'非交易日',not_created:'账户尚未创建',history_unknown:'历史启用状态未知',calendar_unknown:'日历未覆盖',outside_enabled_window:'有效启用时段未覆盖窗口',empty_scope:'已知范围为空'};
    const scopeLabels={unknown:'范围未知',known_snapshot:'历史快照已知',known_batch:'已建立批次范围',changed_during_window:'窗口内范围变更'};
    return `<section class="card" id="forecast-window-records"><div class="card-header"><h2>时间运行完整性</h2><span>${stats.known_expected_windows || 0} 个已知应执行窗口</span></div><div class="card-body">${info(stats.method || '按账户交易日统计，与方向准确率和批次内部覆盖分别计算。')}${info(page.history_note || '')}${info('模型归属未知的漏跑日独立展示，不归到当前模型。此表仅沿用账户与日期筛选；模型和规则筛选仅作用于样本与尝试。')}${info(`启用状态记录始于 ${page.history_started_at || '尚未记录'}；此前历史不完整。`)}<p>${Object.entries(stats.states || {}).map(([k,v])=>`<span class="pill">${e(labels[k] || k)} ${v}</span>`).join(' ')}</p></div><div class="table-wrap"><table><thead><tr><th>计划日期</th><th>账户</th><th>运行状态</th><th>范围</th><th>实际发现时间</th><th>证据</th></tr></thead><tbody>${(page.items || []).map(r=>`<tr><td>${e(r.planned_date)}</td><td>${e(state.accounts.find(a=>a.id===r.account_id)?.name || r.account_id)}</td><td>${e(labels[r.status] || r.status)}</td><td>${e(scopeLabels[r.scope_status] || r.scope_status)}${r.expected_count==null?'':` · ${r.expected_count} 个范围样本（不加入批次分母）`}</td><td>${e(r.discovered_at)}</td><td><details><summary>运行证据</summary>${objectView(r.detail)}</details></td></tr>`).join('') || '<tr><td colspan="6">尚无运行窗口记录</td></tr>'}</tbody></table></div><div class="form-actions"><span>共 ${page.total || 0} 个日期记录</span><div class="actions">${data.windowOffset?button('上一组日期','window-prev'):''}${page.has_more?button('下一组日期','window-next'):''}</div></div></section>`;
  }
  function attemptsCard(statistics) {
    const a=statistics.attempt_statistics || {};
    return `<section class="card" id="forecast-attempt-statistics"><div class="card-header"><h2>执行尝试</h2><span>${a.total || 0} 次（不重复增加预期样本）</span></div><div class="card-body">${info('未发布样本按最近已冻结配置归属；已发布样本按实际发布团队归属。早先失败尝试保留原模型，另行计数。仅计划使用不代表模型已调用。')}<div class="table-wrap"><table><thead><tr><th>状态</th><th>尝试数</th><th>有请求记录</th><th>成功发布</th></tr></thead><tbody>${(a.states || []).map(r=>`<tr><td>${e(title(r.state))}</td><td>${r.count}</td><td>${r.requested || 0}</td><td>${r.published || 0}</td></tr>`).join('') || '<tr><td colspan="4">没有符合筛选的执行尝试</td></tr>'}</tbody></table></div>${button('查看配置未确定 / 历史归属未知样本','forecast-unknown')}</div></section>`;
  }
  function forecastsPage() {
    const result=data.predictions || {}, statistics=result.statistics || {}, page=result.page || {};
    const filters=data.filters;
    const groups=statistics.groups || [];
    return `${(filters.kind==='test')?'<div class="notice"><p><strong>工程测试夹具</strong> · 此处数据只验证计算与页面，不代表真实前瞻表现。</p></div>':(filters.kind==='exploratory')?'<div class="notice"><p><strong>探索记录</strong> · 盘中与临时研究单独展示，不进入正式固定批次准确率。</p></div>':''}${rulesForm()}${comparisonCard()}${windowsCard()}${attemptsCard(statistics)}<section class="card"><form id="forecast-filter-form" data-draft-key="forecast-filters:${e(state.accountId || 'all')}" class="card-body"><div class="form-grid three">${selectField('账户','account_id',filters.account_id ?? state.accountId ?? '',[['','全部账户'],...state.accounts.map(a=>[a.id,a.name])])}${selectField('记录类型','kind',filters.kind || 'formal',[['formal','正式固定批次'],['exploratory','探索记录'],['test','工程测试夹具']])}${field('证券','security_id',filters.security_id || '',{placeholder:'SZ:159278'})}${field('主模型 ID','model_id',filters.model_id || '')}${selectField('主模型 effort','reasoning_effort',filters.reasoning_effort || '',[['','全部'],...['low','medium','high','xhigh','max'].map(x=>[x,x])])}${field('研究团队版本','team_id',filters.team_id || '')}${field('统计规则身份（可用原版本查询）','rule_version',filters.rule_version || '')}${field('基准日从','start',filters.start || '',{type:'date'})}${field('基准日到','end',filters.end || '',{type:'date'})}</div><div class="form-actions"><span class="small muted">每个周期与规则单独统计。</span><div class="actions">${button('检查到期数据','forecast-evaluate')}<button class="button primary" type="submit">筛选</button></div></div></form></section><div class="notice"><p>${e(statistics.method || '尚无预测记录。真实前瞻表现从启用后开始积累。')}<br>${e(statistics.comparison_note || '不同证券或批次的准确率不能直接用于模型排名。')}</p></div>${groups.length?groups.map(g=>`<section class="card"><div class="card-header"><h2>${e(g.label)} · ${g.horizon} 个交易日</h2><strong class="forecast-accuracy">${pct(g.accuracy)}</strong></div><div class="card-body"><div class="forecast-metrics"><span>命中 / 已评估 <b>${g.hits} / ${g.evaluated}</b></span><span>预期样本 <b>${g.expected}</b></span><span>预测 / 弃权覆盖率 <b>${pct(g.coverage)}</b></span><span>阈值 <b>±${Number(g.threshold)*100}%</b></span></div><p>${Object.entries(g.states).map(([k,v])=>`<span class="pill">${e(title(k))} ${v}</span>`).join(' ')}</p><details><summary>三分类对照与每日表现 · 规则 ${e(g.rule_version.slice(0,12))}</summary><div class="form-grid section-gap"><div class="table-wrap"><table><thead><tr><th>预测 ↓ / 实际 →</th><th>上涨</th><th>震荡</th><th>下跌</th></tr></thead><tbody>${['up','sideways','down'].map(d=>`<tr><th>${direction[d]}</th>${['up','sideways','down'].map(a=>`<td>${g.confusion_matrix[d][a]}</td>`).join('')}</tr>`).join('')}</tbody></table>${info(`预测分类：${JSON.stringify(g.predicted_classes)}；实际分类：${JSON.stringify(g.actual_classes)}`)}</div><div><table><thead><tr><th>基准交易日</th><th>命中 / 已评估</th><th>准确率</th></tr></thead><tbody>${g.by_day.map(d=>`<tr><td>${e(d.date)}</td><td>${d.hits} / ${d.evaluated}</td><td>${pct(d.accuracy)}</td></tr>`).join('')}</tbody></table>${info('累计按总命中数 / 总已评估数计算，不平均每日百分比。')}</div></div></details></div></section>`).join(''):'<section class="card"><div class="card-body"><h2>暂无已评估样本</h2><p>新发的 5 / 20 交易日预测需等待真实到期；不会回填已知行情上的预测。</p></div></section>'}<section class="card"><div class="card-header"><h2>预测与覆盖记录</h2><span>${page.total || 0} 个预期样本</span></div><div class="table-wrap"><table><thead><tr><th>证券</th><th>期限</th><th>基准 / 目标</th><th>原预测</th><th>真实方向 / 收益率</th><th>状态</th><th>详情</th></tr></thead><tbody>${(page.items || []).map(r=>`<tr><td>${e(r.symbol)}</td><td>${r.horizon} 交易日</td><td>${e(r.base_date)}<br>${e(r.target_date || '日历缺覆盖')}</td><td>${e(direction[r.direction] || '未预测')}</td><td>${e(direction[r.actual_direction] || '—')} / ${r.return_ratio==null?'—':(Number(r.return_ratio)*100).toFixed(3)+'%'}</td><td>${e(title(r.display_state))}${r.hit==null?'':r.hit?' · 命中':' · 未命中'}${(r.reason || r.evaluation_reason || r.waiting_reason)?`<br><small>${e(r.reason || r.evaluation_reason || r.waiting_reason)}</small>`:''}</td><td>${r.forecast_id?button('原记录','forecast-detail',r.forecast_id):button('批次与尝试','forecast-batch-detail',r.batch_id)}</td></tr>`).join('') || '<tr><td colspan="7">没有符合筛选条件的记录</td></tr>'}</tbody></table></div><div class="form-actions"><span>第 ${Math.floor(data.offset/30)+1} 页</span><div class="actions">${data.offset?button('上一页','forecast-prev'):''}${page.has_more?button('下一页','forecast-next'):''}</div></div></section><section class="card"><div class="card-header"><h2>批次范围与覆盖</h2></div><div class="card-body">${(data.batches || []).map(b=>`<details><summary>${e(b.base_date)} · ${e(b.kind)} · 预期 ${b.expected_count} 样本 · ${e(title(b.state))}</summary>${objectView(b)}${button('批次与尝试详情','forecast-batch-detail',b.id)}</details>`).join('') || info('尚未创建批次。')}</div></section>`;
  }
  async function repaint() { await load(); render(); }
  document.addEventListener('input', event=>{
    if(!event.target.matches('[data-model-filter]'))return;
    const form=event.target.closest('form'), select=form.querySelector('[data-role-model]');
    select.innerHTML=modelOptions(form.elements.provider_id.value, select.value, event.target.value);
  });
  document.addEventListener('change',event=>{
    if(!event.target.matches('[data-role-provider]'))return;
    const form=event.target.closest('form'), provider=event.target.value, select=form.querySelector('[data-role-model]');
    event.target.dataset.changed='true';
    select.innerHTML='<option value="">正在获取模型列表…</option>';select.disabled=true;
    form.querySelector('[data-model-filter]').value='';
    operation(async()=>{
      if(provider)data.catalogs[provider]=await api(`/api/providers/${enc(provider)}/models`);
      if(!form.isConnected || form.elements.provider_id.value!==provider)return;
      select.innerHTML=modelOptions(provider,'');select.disabled=!provider;
      form.querySelector('[data-catalog-status]').textContent=title(data.catalogs[provider]?.status);
    },{key:`models:${provider}`,label:'读取模型目录'});
  });
  document.addEventListener('click',event=>{
    const target=event.target.closest('[data-sixth-action]');if(!target)return;
    const {sixthAction:action,id}=target.dataset;
    const account=target.dataset.accountId;
    operation(async()=>{
      if(action==='provider-edit') {const p=data.providers.find(p=>p.id===id); dialog('修改供应商连接',providerForm(p));return;}
      if(action==='provider-refresh'||action==='provider-more') await api(`/api/providers/${enc(id)}/models/${action==='provider-more'?'continue':'refresh'}`,{method:'POST'});
      if(action==='provider-clear') {if(!confirm('明确清除该供应商凭据？冻结的旧凭据将撤销，相关运行任务可能失败。历史预测保留。'))return;await api(`/api/providers/${enc(id)}/credential`,{method:'DELETE'});}
      if(action==='provider-archive') {if(!confirm('归档此连接后，新研究的引用绑定将显示不可用；历史记录保留。'))return;await api(`/api/providers/${enc(id)}/archive`,{method:'POST'});}
      if(action==='role-reset')await api(`/api/accounts/${enc(account)}/agent-models/${enc(id)}`,{method:'DELETE'});
      if(action==='forecast-detail'){const row=await api(`/api/forecasts/${enc(id)}`);dialog('不可变预测 · 报告与评估修订',objectView(row)+button('请求重新检查行情更正','forecast-recheck',id));return;}
      if(action==='forecast-batch-detail'){const row=await api(`/api/forecast-batches/${enc(id)}`);dialog('批次 · 配置归属与执行尝试',objectView(row));return;}
      if(action==='forecast-recheck'){await api(`/api/forecasts/${enc(id)}/recheck`,{method:'POST'});toast('已登记重新检查；原有效评分在取得更正证据前保留。');return;}
      if(action==='forecast-unknown'){data.filters={...data.filters,model_id:'__unknown__'};data.offset=0;const form=document.querySelector('#forecast-filter-form');if(form){form.dataset.discard='true';state.drafts.delete(form.dataset.draftKey);}}
      if(action==='window-prev')data.windowOffset=Math.max(0,data.windowOffset-30);
      if(action==='window-next')data.windowOffset+=30;
      if(action==='forecast-evaluate')await api('/api/forecasts/evaluate',{method:'POST'});
      if(action==='forecast-prev')data.offset=Math.max(0,data.offset-30);
      if(action==='forecast-next')data.offset+=30;
      await repaint();
    },{key:`sixth:${action}:${account || ''}:${id}`,label:target.textContent});
  });
  document.addEventListener('submit',event=>{
    const form=event.target;
    if(!['provider-connection-form','forecast-rule-form','forecast-filter-form','forecast-comparison-form'].includes(form.id)&&!form.classList.contains('role-model-form'))return;
    event.preventDefault();
    const values=Object.fromEntries(new FormData(form)), account=form.dataset.accountId;
    // Clear before any asynchronous work; never retain key in the generic draft map.
    if(form.id==='provider-connection-form')form.elements.api_key.value='';
    operation(async()=>{
      if(form.id==='provider-connection-form'){
        if(!values.api_key)delete values.api_key;
        const pid=form.dataset.providerId;
        try{await api(`/api/providers${pid?'/'+enc(pid):''}`,{method:pid?'PUT':'POST',body:JSON.stringify(values)});}finally{delete values.api_key;}
        if(form.closest('dialog'))form.closest('dialog').close();else form.reset();
      } else if(form.classList.contains('role-model-form')) {
        const payload={};
        if(values.provider_id){
          if(values.model_id){payload.provider_id=values.provider_id;payload.model_id=values.model_id;}
          else if(form.querySelector('[data-role-provider]').dataset.changed==='true')throw Error('请选择此供应商实际发现的模型');
        }
        if(values.reasoning_effort)payload.reasoning_effort=values.reasoning_effort;
        if(!account){for(const key of ['name','responsibility','system_prompt'])payload[key]=values[key];payload.tools=values.tools.split('\n').map(x=>x.trim()).filter(Boolean);if(values.enabled!==undefined)payload.enabled=values.enabled==='true';}
        const path=account?`/api/accounts/${enc(account)}/agent-models/${enc(form.dataset.roleId)}`:`/api/agent-roles/${enc(form.dataset.roleId)}`;
        await api(path,{method:'PUT',body:JSON.stringify(payload)});
        form.dataset.discard='true';state.drafts.delete(form.dataset.draftKey);
      } else if(form.id==='forecast-rule-form') {
        const payload={enabled:values.enabled==='true',horizons:values.horizons.split(/[,，]/).map(Number),labels:values.labels.split(/[,，]/).map(x=>x.trim()),threshold:String(Number(values.threshold)/100),start_time:values.start_time,deadline_time:values.deadline_time};
        await api(`/api/accounts/${enc(account)}/forecast-settings`,{method:'PUT',body:JSON.stringify(payload)});
        form.dataset.discard='true';state.drafts.delete(form.dataset.draftKey);
      } else if(form.id==='forecast-comparison-form') {
        const selected=new FormData(form).getAll('account_ids');
        if(selected.length<2 || selected.length>10)throw Error('请选择 2 至 10 个账户');
        data.comparisonAccounts=selected;
      } else {data.filters=Object.fromEntries(Object.entries(values).map(([k,v])=>[k,String(v).trim()]).filter(([k,v])=>v||k==='account_id'));data.offset=0;data.windowOffset=0;}
      await repaint();toast('已更新');
    },{key:`sixth-save:${form.id || form.dataset.draftKey}`,label:form.id==='forecast-comparison-form'?'比较共同样本':'保存配置'});
  });
  return {load, has:route=>routes.has(route), snapshot:()=>JSON.stringify(data), page:()=>state.route==='providers'?providerPage():state.route==='roles'?rolesPage():state.route==='account-models'?rolesPage(true):forecastsPage()};
}
