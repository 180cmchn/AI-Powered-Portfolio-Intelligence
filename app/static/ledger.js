// All accounting arithmetic is performed by the server. This module submits
// decimal strings and displays the exact persisted receipt, never JS estimates.
export function createLedgerUI({state,api,e,field,selectField,render,toast,operation,refresh,number,date,objectView,clearDraft,finishSave}) {
  const enc=encodeURIComponent, snapshots=new Map(), errors=new Map(), previews=new WeakMap();
  const storageKey='guanlan.ledger-submissions';
  const pending=()=>{try{return JSON.parse(sessionStorage.getItem(storageKey)||'{}');}catch{return {};}};
  const savePending=value=>sessionStorage.setItem(storageKey,JSON.stringify(value));
  const endpoint=(id,suffix)=>`/api/accounts/${enc(id)}/${suffix}`;
  const latest=()=>snapshots.get(state.accountId);
  const button=(label,action,extra='')=>`<button class="button" type="button" data-ledger-action="${action}" ${extra}>${e(label)}</button>`;
  const money=value=>value===null||value===undefined?'未知':`${number(value)} 元`;
  const kinds={trade:'虚拟成交',buy:'买入',sell:'卖出',deposit:'资金转入',withdraw:'资金转出',opening:'期初资金',opening_baseline:'迁移期初',correction:'快照校正',reversal:'末笔撤销',fee_adjustment:'费用调整'};
  const inputTime=()=>new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).format(new Date()).replace(' ','T');
  async function load() {
    if(state.route!=='holdings'||!state.accountId)return;
    const id=state.accountId;
    try {const result=await api(endpoint(id,'ledger'));if(state.accountId===id){snapshots.set(id,result);errors.delete(id);}}
    catch(error){if(error.code!=='superseded_read')errors.set(id,error.message);}
  }
  function feeRows(fees) {
    if(!fees)return '';
    const entries=Array.isArray(fees.items)?fees.items:Array.isArray(fees.components)?fees.components:Object.entries(fees.components||fees).filter(([key,v])=>['commission','transfer','stamp'].includes(key)).map(([key,v])=>typeof v==='object'?{name:key,...v}:{name:key,amount:v});
    return entries.map(row=>`<span>${e(({commission:'佣金',transfer:'过户费',stamp:'印花税'}[row.name||row.code]||row.label||row.name||row.code))}：${money(row.amount??row.charged??row.fee)}${row.included?'（佣金已含，不重复扣收）':''}</span>`).join(' · ');
  }
  function page() {
    const a=state.account, data=latest(), plan=data?.fee_plan, problem=errors.get(a.id), unknown=pending()[a.id];
    if(!data)return `<section class="card"><div class="card-body"><h2>买入 / 卖出记账</h2><p>${e(problem||'正在读取资金与交易账本…')}</p>${button('刷新账本','refresh')}</div></section>`;
    const identities=new Map((a.metrics?.positions||[]).map(position=>[position.symbol,position]));
    const rows=(data.positions||[]).map(position=>({...identities.get(position.symbol),...position}));
    return `<section class="card" id="ledger-overview"><div class="card-header"><h2>资金与持仓</h2><span>账户输入 v${e(data.input_version)}</span></div><div class="card-body"><p>模拟可用现金 <strong>${money(data.cash)}</strong> · 已实现盈亏 ${money(data.summary?.realized_pnl)} · 累计实际扣费 ${money(data.summary?.fees_total??data.summary?.total_fees)}</p><p class="small muted">普通现金账户的手动虚拟成交，不连接券商或交易所。卖出资金可用不代表当日可取；未模拟完整清算。买入费用计入成本，累计手续费不再从权益重复扣减。</p>${problem?`<p class="error-detail">${e(problem)}</p>`:''}</div><div class="table-wrap"><table><thead><tr><th>证券</th><th>持有量</th><th>可卖量</th><th>剩余总成本</th><th>成本依据</th></tr></thead><tbody>${rows.map(p=>`<tr><td>${e(p.name||p.symbol)}<br><small>${e(p.symbol)}</small></td><td>${number(p.quantity)} ${p.kind==='stock'?'股':p.kind==='etf'||p.kind==='fund'?'份':'股 / 份（待确认）'}</td><td>${p.available_quantity==null?'待确认期初可卖量':number(p.available_quantity)+(Number(p.availability_unknown_quantity)>0?'（另有 '+number(p.availability_unknown_quantity)+' 期初待确认）':'')}</td><td>${money(p.total_cost??p.remaining_cost)}</td><td>${e(p.cost_status||((p.total_cost??p.remaining_cost)==null?'成本依据不足':'移动加权平均 · 含买入费用'))}</td></tr>`).join('')||'<tr><td colspan="5">尚无持仓，可先记录一笔买入。</td></tr>'}</tbody></table></div></section>${unknown?`<p class="notice warning" role="status">记账结果待核对 · 操作 ${e(unknown.operationId)}。不会自动重发。${button('核对记账结果','reconcile')}</p>`:''}<section class="card"><div class="card-header"><h2>记录买入 / 卖出</h2><span>一笔记录按一笔收费</span></div><form id="trade-form" class="card-body" data-account-id="${e(a.id)}" data-draft-key="trade:${e(a.id)}" data-version="${e(data.input_version)}" data-fee-version="${e(plan.version)}"><div class="form-grid three">${selectField('买卖方向','side','buy',[['buy','买入'],['sell','卖出']],'','trade-side')}${field('证券代码','symbol','',{id:'trade-symbol',required:true,placeholder:'518880 / 002354 / 159599',help:'接受六位代码；以实际目录识别股票与 ETF。'})}${field('整数股 / 份','quantity','',{id:'trade-quantity',required:true,type:'number',min:1,step:1,help:'不是手数；后台校验整手、零股和可卖日。'})}${field('成交单价（元）','price','',{id:'trade-price',required:true,inputmode:'decimal',placeholder:'输入已确认价格'})}${field('成交时间（上海）','traded_at',inputTime(),{id:'trade-time',required:true,type:'datetime-local',step:1})}${selectField('业务标识','mode','simulated',[['simulated','模拟记账'],['historical','补录已成交业务']],'补录保留实际录入时间；不支持任意历史插单重算。','trade-mode')}${field('备注（选填）','note','',{id:'trade-note',full:true})}</div><p class="trade-security-reference small muted" aria-live="polite">可参考已获取行情，提交始终使用这里确认的成交价格。行情不可用时仍可手动填写。</p><label class="ledger-confirm"><input type="checkbox" name="price_confirmed" required> 我已确认成交价格、数量和时间；这是一笔虚拟成交记录。</label><p class="small muted" data-trade-fee-summary>费用方案 v${e(plan.version)} · ${e(plan.name)} · ${plan.confirmed?'已由用户确认':'估算模板，待核对券商协议'}。预览与保存都由后端精确计算，账户或费率版本变化需重新预览。</p><details data-key="trade-fee-override:${e(a.id)}" class="section-gap"><summary>有依据的单笔实际费用覆盖</summary><p class="small muted">留空使用方案估算；覆盖需填写完整费用与交割单依据，不改变历史方案。</p><div class="form-grid three">${field('实际佣金','override_commission','',{id:'trade-override-commission'})}${field('实际过户费','override_transfer','',{id:'trade-override-transfer'})}${field('实际印花税','override_stamp','',{id:'trade-override-stamp'})}${field('覆盖依据 / 原因','override_reason','',{id:'trade-override-reason',full:true})}</div></details><div class="trade-preview section-gap" aria-live="polite"></div><div class="form-actions"><span class="form-status">记账成功后异步更新分析；模型失败不会撤销交易。</span><div class="actions">${button('预览现金与费用','preview')}<button type="submit" class="button primary" disabled>确认记账</button></div></div></form></section>${fundForm(data)}${feeForm(plan,a.id)}${history(data)}`;
  }
  function fundForm(data) {
    return `<details class="card ledger-detail" data-key="funds:${e(state.accountId)}"><summary>资金转入 / 转出 / 确认期初现金</summary><form id="fund-form" class="card-body" data-account-id="${e(state.accountId)}" data-draft-key="funds:${e(state.accountId)}" data-version="${data.input_version}"><div class="form-grid three">${selectField('资金类型','kind',data.cash==null?'opening':'deposit',[['deposit','转入'],['withdraw','转出'],['opening','补充未知期初现金']],'资金转入不计投资收益。','fund-kind')}${field('金额（元）','amount','',{id:'fund-amount',required:true})}${field('来源 / 原因','reason','',{id:'fund-reason',required:true})}</div><div class="form-actions"><span>资金调整保存独立流水并更新研究输入。</span><button type="submit" class="button primary">保存资金记录</button></div></form></details>`;
  }
  function feeOverrides(plan) {
    const rows=[], labels={SH:'上海',SZ:'深圳',stock:'普通 A 股',etf:'场内 ETF',buy:'买入',sell:'卖出'};
    const describe=rule=>Object.entries(rule||{}).filter(([key])=>!['buy','sell'].includes(key)).map(([key,value])=>{
      if(key==='commission_rate')return `佣金率 ${value}（比例）`;
      if(key==='minimum_commission')return `最低佣金 ${value} 元`;
      if(key==='transfer_included')return value?'过户费已含佣金':'过户费另列';
      if(key==='included_fees')return `已含 ${(value||[]).map(item=>({handling:'经手费',regulatory:'监管费',transfer:'过户费'}[item]||item)).join('、') || '未列明'}`;
      return `${({transfer_rate:'过户费率',stamp_rate:'印花税率'}[key]||key)} ${value}`;
    }).join('；');
    for(const kind of ['stock','etf'])for(const side of ['buy','sell'])if(plan[kind]?.[side]&&Object.keys(plan[kind][side]).length)rows.push(`${labels[kind]} · ${labels[side]}：${describe(plan[kind][side])}`);
    for(const [market,kinds] of Object.entries(plan.market_overrides||{}))for(const [kind,rule] of Object.entries(kinds)){
      const common=describe(rule);if(common)rows.push(`${labels[market]||market} · ${labels[kind]||kind}：${common}`);
      for(const side of ['buy','sell'])if(rule[side]&&Object.keys(rule[side]).length)rows.push(`${labels[market]||market} · ${labels[kind]||kind} · ${labels[side]}：${describe(rule[side])}`);
    }
    return `<div class="notice ${rows.length?'warning':''}" data-fee-overrides><div><strong>${rows.length?'已配置市场 / 买卖方向覆盖':'未配置单独市场 / 买卖方向覆盖'}</strong>${rows.length?`<ul>${rows.map(row=>`<li>${e(row)}</li>`).join('')}</ul>`:''}<p>下面的基础费率只适用于未被覆盖的项目；保存基础表单会保留已有覆盖。本页暂不提供覆盖编辑器，请核对交易预览中的实际分项。</p></div></div>`;
  }
  function feeForm(plan,id) {
    const f=(kind,label)=>`<h3>${label}</h3><div class="form-grid three">${field('佣金率（小数）',kind+'_rate',plan[kind]?.commission_rate??'0.0003',{id:`fees-${kind}-rate`,required:true,help:'0.0003 = 0.03% = 0.3‰ = 万分之三'})}${field('每笔最低佣金（元）',kind+'_minimum',plan[kind]?.minimum_commission??'5',{id:`fees-${kind}-minimum`,required:true,help:'ETF 最低收费按实际协议填写；不默认免最低额。'})}${kind==='stock'?selectField('过户费是否已含佣金','transfer_included',String(plan.stock?.transfer_included??false),[['false','另列扣收'],['true','已包含，不重复扣收']],'股票税费与 ETF 分开适用。','fees-transfer'):''}</div>`;
    return `<details class="card ledger-detail" data-key="fee-plan:${e(id)}"><summary>费用方案 · ${e(plan.name)} · v${e(plan.version)} · ${plan.confirmed?'用户已确认':'待用户确认 / 估算'}</summary><form id="fee-plan-form" class="card-body" data-account-id="${e(id)}" data-draft-key="fees:${e(id)}" data-fee-version="${e(plan.version)}">${field('方案名称','name',plan.name,{id:'fees-name',required:true})}${field('生效日期','effective_from',String(plan.effective_from||'2023-08-28').slice(0,10),{id:'fees-effective',type:'date',required:true})}${feeOverrides(plan)}${f('stock','普通 A 股')}${f('etf','场内 ETF')}<p class="small muted">股票卖出印花税 0.0005 = 0.05% = 0.5‰；股票另列过户费 0.00001 = 0.001% = 0.01‰。ETF 买卖不收股票印花税与股票过户费；经手费 / 监管费已含佣金，不再额外扣款；管理费 / 托管费不按每笔交易重复收年费。</p><label class="ledger-confirm"><input type="checkbox" name="confirmed" ${plan.confirmed?'checked':''}> 已按本账户实际佣金与最低收费协议确认</label><div class="fee-sources">${(plan.sources||[]).map(source=>typeof source==='string'?`<p>${e(source)}</p>`:`<p>${source.url?.startsWith('https://')?`<a href="${e(source.url)}" target="_blank" rel="noopener noreferrer">${e(source.title||source.name||'规则来源')}</a>`:e(source.title||source.name||'规则来源')} · 生效 ${e(source.effective_from||source.effective_date||'见来源')} · 核对 ${e(source.checked_at||'见方案')}</p>`).join('')}</div><div class="form-actions"><span>只影响新记录；历史费率版本与扣费结果保持不变。</span><button type="submit" class="button primary">保存费用方案</button></div></form></details>`;
  }
  function feeAdjustmentForm(row,data) {
    const fees=row.fees?.components || {};
    return `<details class="section-gap" data-key="fee-adjustment:${e(row.id)}"><summary>更正末笔实际费用</summary><form id="fee-adjustment-form" data-draft-key="fee-adjustment:${e(row.id)}" data-account-id="${e(state.accountId)}" data-entry-id="${e(row.id)}" data-version="${data.input_version}"><p class="small muted">根据交割单修正这笔实际扣费；追加调整记录并引用原单。仅末笔成交可直接调整，已有后续记录时需先核对校正路径。</p>${field('实际佣金','commission',fees.commission || '0',{id:'adjust-commission',required:true})}${field('实际过户费','transfer',fees.transfer || '0',{id:'adjust-transfer',required:true})}${field('实际印花税','stamp',fees.stamp || '0',{id:'adjust-stamp',required:true})}${field('费用更正依据','reason','',{id:'adjust-reason',required:true})}<button class="button primary" type="submit">保存费用更正</button></form></details>`;
  }
  function history(data) {
    const entries=data.entries||[], cash=data.cash_entries||[];
    return `<section class="card" id="trade-history"><div class="card-header"><h2>交易与校正流水</h2>${button('刷新流水','refresh')}</div><div class="card-body"><p class="small muted">原始流水不可直接改写。录错仅支持受约束的末笔撤销，不按反向交易再次收费；较早记录请保留原单并使用有依据校正。</p></div><div class="table-wrap"><table><thead><tr><th>类型 / 证券</th><th>成交 / 录入时间</th><th>数量 × 单价</th><th>现金变化 / 扣费</th><th>详情</th></tr></thead><tbody>${entries.map(row=>`<tr><td>${e(kinds[row.kind==='trade'?row.side:row.kind]||row.kind)} ${e(row.symbol||'')}<br><small>${e(row.mode==='historical'?'补录':row.mode==='simulated'?'模拟':'')}</small></td><td>${date(row.traded_at||row.recorded_at||row.created_at)}<br><small>录入 ${date(row.recorded_at||row.created_at)}</small></td><td>${number(row.quantity)} × ${number(row.price)}</td><td>${money(row.cash_effect??row.cash_delta)}<br><small>${feeRows(row.fees)}</small></td><td><details><summary>记录 ${e(row.id?.slice(0,8))}</summary>${objectView(row)}</details>${(row.reversible||(entries[0]?.id===row.id&&!['opening','reversal'].includes(row.kind)))?button('撤销末笔','reverse',`data-entry-id="${e(row.id)}"`):''}${entries[0]?.id===row.id&&row.kind==='trade'?feeAdjustmentForm(row,data):''}</td></tr>`).join('')||'<tr><td colspan="5">尚无新交易；迁移期初不代表猜测出的历史买卖。</td></tr>'}</tbody></table></div></section><details class="card ledger-detail" data-key="cash-history:${e(state.accountId)}"><summary>资金记录 · ${cash.length} 条</summary><div class="table-wrap"><table><thead><tr><th>录入时间</th><th>来源</th><th>变动金额</th><th>变动后余额</th></tr></thead><tbody>${cash.map(row=>`<tr><td>${date(row.recorded_at||row.created_at)}</td><td>${e(kinds[row.kind]||row.kind)} ${e(row.reason||row.body?.reason||'')}</td><td>${money(row.amount??row.delta??row.cash_delta)}</td><td>${money(row.balance??row.balance_after??row.cash_after)}</td></tr>`).join('')||'<tr><td colspan="4">暂无资金记录</td></tr>'}</tbody></table></div></details>`;
  }
  function tradePayload(form) {
    const f=Object.fromEntries(new FormData(form));
    const result={expected_version:Number(form.dataset.version),fee_plan_version:Number(form.dataset.feeVersion),symbol:f.symbol.trim(),side:f.side,quantity:f.quantity,price:f.price,traded_at:f.traded_at+'+08:00',mode:f.mode,note:f.note};
    if([f.override_commission,f.override_transfer,f.override_stamp,f.override_reason].some(Boolean))result.fees_override={commission:f.override_commission,transfer:f.override_transfer,stamp:f.override_stamp,reason:f.override_reason};
    return result;
  }
  async function reconcile(id) {
    const submissions=pending(), saved=submissions[id]; if(!saved)return null;
    const receipt=await api(endpoint(id,`ledger-operations/${enc(saved.operationId)}`),{signal:null}).catch(()=>null);
    if(!receipt)return null;
    delete submissions[id];savePending(submissions);return receipt;
  }
  async function write(id,suffix,payload) {
    if(pending()[id]) {const receipt=await reconcile(id);if(receipt){await refresh(true);render();throw Error('已找回上次记账。请核对流水与余额后再提交这次输入。');}throw Error('上次记账结果仍未确认；先核对原操作，不会自动重发。');}
    const operationId=crypto.randomUUID();savePending({...pending(),[id]:{operationId,suffix}});
    try {const receipt=await api(endpoint(id,suffix),{method:'POST',body:JSON.stringify({...payload,operation_id:operationId})});const submissions=pending();delete submissions[id];savePending(submissions);return receipt;}
    catch(error) {if(error.code==='write_outcome_unknown'){const receipt=await reconcile(id);if(receipt)return receipt;}else{const submissions=pending();delete submissions[id];savePending(submissions);}render();throw error;}
  }
  function receiptText(receipt) {
    return `记账成功 · ${receipt.trade_id||receipt.entry_id||receipt.id||'流水已保存'} · v${receipt.input_version??receipt.account_version??receipt.version??'已更新'} · 现金 ${receipt.cash_before??receipt.before?.cash??'见流水'} → ${receipt.cash_after??receipt.after?.cash??'见流水'}。分析 ${receipt.research_status||receipt.research?.state||'已登记，待处理'}；可在总览或 Agent 工作台查看。`;
  }
  async function identify(form) {
    const input=form.elements.symbol, raw=input.value.trim(), id=form.dataset.accountId, node=form.querySelector('.trade-security-reference');if(!raw)return;
    const result=await api('/api/securities/resolve',{method:'POST',body:JSON.stringify({symbols:[raw],context:'stock_etf'})});
    if(!form.isConnected||input.value.trim()!==raw||state.accountId!==id)return;
    const item=result.items?.[0];if(!result.ok||item?.status!=='resolved'){node.textContent=item?.message||'证券身份待确认；预览时会再次校验。';return;}
    const quote=(state.universe.quotes||[]).find(q=>q.symbol===item.symbol),price=quote?.price??quote?.last_price;
    node.textContent=`${item.symbol} · ${item.name} · ${item.market} · ${item.kind==='stock'?'股':'份'}。${price?`参考价 ${price} 元 · ${quote.source||'来源未知'} · ${date(quote.as_of)} · ${quote.status||'状态未知'}；仅供参考，请确认成交价。`:'当前无参考行情，可手动填写成交价。'}`;
    if(price&&!form.elements.price.value){form.elements.price.value=String(price);form.elements.price_confirmed.checked=false;form.dataset.dirty='true';}
  }
  document.addEventListener('focusout',event=>{if(event.target.id==='trade-symbol')operation(()=>identify(event.target.form),{key:'trade-identify',label:'识别交易证券'});});
  document.addEventListener('input',event=>{const form=event.target.closest('#trade-form');if(form){previews.delete(form);form.querySelector('[type=submit]').disabled=true;form.querySelector('.trade-preview').textContent='输入已改变，请重新预览。';}});
  document.addEventListener('change',event=>{const form=event.target.closest('#trade-form');if(form){previews.delete(form);form.querySelector('[type=submit]').disabled=true;}});
  document.addEventListener('click',event=>{
    const target=event.target.closest('[data-ledger-action]');if(!target)return;
    const id=state.accountId,action=target.dataset.ledgerAction,form=target.closest('form');
    operation(async()=>{
      if(action==='preview') {
        if(!form.reportValidity())return;
        form.dataset.version=String(latest().input_version);form.dataset.feeVersion=String(latest().fee_plan.version);
        const payload=tradePayload(form), signature=JSON.stringify([...new FormData(form)]);
        const result=await api(endpoint(id,'trades/preview'),{method:'POST',body:JSON.stringify({...payload,operation_id:crypto.randomUUID()})});
        if(!form.isConnected||state.accountId!==id||signature!==JSON.stringify([...new FormData(form)]))return;
        previews.set(form,{payload,signature,result});
        const plan=latest().fee_plan;form.querySelector('[data-trade-fee-summary]').textContent=`本次预览费用方案 v${result.fees?.plan_version??plan.version} · ${plan.name} · ${result.fees?.estimated_only?'估算，待核对券商协议':'以已确认方案 / 实际费用为准'}。确认保存时再次核对账户与费率版本。`;
        form.querySelector('.trade-preview').innerHTML=`<div class="notice"><div><strong>后端精确费用与资金预览</strong><p>成交 ${money(result.gross??result.gross_amount??result.amount)} · 实际扣费 ${money(result.fees?.total??result.total_fees??result.fee_total)}</p><p>${feeRows(result.fees)}</p><p>现金 ${money(result.cash_before??result.before?.cash)} → ${money(result.cash_after??result.after?.cash)}；持有量 ${number(result.quantity_before??result.before?.quantity)} → ${number(result.quantity_after??result.after?.quantity)}</p><p class="small muted">确认后按此价格和方案版本重算保存；不会使用后来更新的报价替换。</p></div></div>`;
        form.querySelector('[type=submit]').disabled=false;return;
      }
      if(action==='reconcile'){const receipt=await reconcile(id);if(!receipt)throw Error('仍未查到提交结果；保留原操作标识，稍后只读核对。');toast(receiptText(receipt),false,id,{id:receipt.research?.job_id});}
      if(action==='reverse'){const reason=prompt('末笔撤销原因（保留原单引用，不再次收费）：');if(!reason?.trim())return;const receipt=await write(id,`ledger/${enc(target.dataset.entryId)}/reverse`,{expected_version:latest().input_version,reason:reason.trim()});toast(receiptText(receipt),false,id,{id:receipt.research?.job_id});}
      await refresh(true);render();
    },{key:`ledger:${action}:${id}`,label:target.textContent});
  });
  document.addEventListener('submit',event=>{
    const form=event.target;if(!['trade-form','fund-form','fee-plan-form','fee-adjustment-form'].includes(form.id))return;event.preventDefault();
    const id=form.dataset.accountId, values=Object.fromEntries(new FormData(form)), signature=JSON.stringify([...new FormData(form)]);
    operation(async()=>{
      let receipt;
      if(form.id==='trade-form') {const preview=previews.get(form);if(!preview||preview.signature!==signature)throw Error('请先按当前输入重新预览费用与资金。');receipt=await write(id,'trades',preview.payload);previews.delete(form);}
      else if(form.id==='fund-form')receipt=await write(id,'funds',{expected_version:Number(form.dataset.version),kind:values.kind,amount:values.amount,reason:values.reason});
      else if(form.id==='fee-adjustment-form')receipt=await write(id,`ledger/${enc(form.dataset.entryId)}/fees`,{expected_version:Number(form.dataset.version),fees:{commission:values.commission,transfer:values.transfer,stamp:values.stamp},reason:values.reason});
      else {receipt=await api(endpoint(id,'fee-plan'),{method:'PUT',body:JSON.stringify({expected_fee_plan_version:Number(form.dataset.feeVersion),name:values.name,effective_from:values.effective_from,confirmed:values.confirmed==='on',stock:{...latest().fee_plan.stock,commission_rate:values.stock_rate,minimum_commission:values.stock_minimum,transfer_included:values.transfer_included==='true'},etf:{...latest().fee_plan.etf,commission_rate:values.etf_rate,minimum_commission:values.etf_minimum},market_overrides:latest().fee_plan.market_overrides||{}})});}
      finishSave(form,signature,receipt.input_version??receipt.account_version);await refresh(true);render();toast(form.id==='fee-plan-form'?'费用方案已保存；历史交易保持原扣费结果，请重新预览尚未提交的交易。':receiptText(receipt),false,id,{id:receipt.research?.job_id});
    },{key:`ledger-save:${id}:${form.id}`,label:form.id==='trade-form'?'保存虚拟成交':'保存账本配置'});
  });
  return {load,page,write,receiptText,snapshot:()=>JSON.stringify([latest(),errors.get(state.accountId),pending()[state.accountId]])};
}
