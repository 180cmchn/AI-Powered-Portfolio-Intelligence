// Deterministic Chromium UI regression using explicit HTTP fixtures.
// No real provider/model calls. Run: npm ci && npx playwright install chromium && npm run test:browser
import { createRequire } from 'node:module';
import { createServer } from 'node:http';
import { readFile, mkdir } from 'node:fs/promises';
import assert from 'node:assert/strict';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright');
const id = 'a'.repeat(32), mid = 'b'.repeat(32), rid = 'c'.repeat(32), unknownId = 'd'.repeat(32), privateId = 'e'.repeat(32);
let price = '10', version = 1, eventSeq = 1, detailFetches = 0, materialBodyFetches = 0, activeCount = 2;
let configurationErrors=[];
const providerId='6'.repeat(32);
const roleIds=['manager','filing_researcher','market_researcher','industry_researcher','etf_researcher','portfolio_researcher'];
const providerFixture={id:providerId,display_name:'受控供应商',base_url:'https://fixture.example/v1',protocol:'responses',revision:1,key_configured:true,catalog:{status:'success',complete:true,selectable:true,models:[{id:'fixture/Model-A'},{id:'fixture/Model-B'}]}};
const roleFixtures=roleIds.map(role_id=>({role_id,name:role_id,responsibility:'受控角色职责',system_prompt:'受控全局提示词',enabled:true,version:1,tools:[role_id==='manager'?'finish_analysis':'submit_report'],default_binding:{provider_id:providerId,model_id:'fixture/Model-A',reasoning_effort:'medium'},override:{},resolved:{provider_id:providerId,model_id:'fixture/Model-A',reasoning_effort:'medium',binding_source:'global_default',effort_source:'global_default'}}));
const requests = [];
const extractionId = 'f'.repeat(32), generatedId = '1'.repeat(32), failedId = '2'.repeat(32);
const material = { id: mid, title: '长财报', text: '原文来自 text 字段', access: 'full_text', truncated: true, page_count: 32, returned_range: {start_page: 25, end_page: 26}, url: 'https://example.com/report.pdf', fetched_at: '2026-10-22T15:00:00+08:00', document_version: 1, representation_kind: 'original_document' };
const reference = {material_id:mid, status:'resolved', scope:'public', access:'full_text', url:material.url, document_version:1, range:{returned_range:material.returned_range, truncated:true, char_count:12000}};
const extraction = {...material, id:extractionId, title:'受控 Agent 提取', text:'Agent 提取的受控陈述', classification:'disclosed_fact', representation_kind:'agent_extraction', origin_status:'unlinked', origin_references:[], returned_range:{start_page:28,end_page:28}};
const searchMaterial={id:'4'.repeat(32),title:'PDF 检索结果',access:'pdf_search',representation_kind:'summary',summary:'仅保存结构化检索结果'};
const extractionRef = {material_id:extractionId,status:'resolved',representation_kind:'agent_extraction',origin_status:'unlinked',origin_references:[],url:material.url,range:{char_count:20,read_completeness:'agent_authored'},claimed_source_range:{returned_range:extraction.returned_range}};
const generatedRef = {material_id:generatedId,status:'resolved',representation_kind:'generated_analysis',origin_status:'linked_materials',origin_references:[reference,{material_id:'3'.repeat(32),status:'unresolved',reason:'unknown_or_inaccessible'}],range:{char_count:30,read_completeness:'agent_authored'}};
const report = {id: rid, status: 'current', input_version: 1, report: `确定性浏览器报告 · 证据 ${mid}`, result: {universe_version:1, shared_research_used:[{id:'shared-public-fixture',universe_version:1,scope:'public',applicability:'current'},{id:'private-research-fixture',universe_version:2,scope:'account',applicability:'current'}], material_ids: [mid, unknownId, privateId], references:[reference, extractionRef, generatedRef, {material_id:failedId,status:'source_link',url:'https://example.com/failed.pdf',reason:'saved_retrieval_failed'}, {material_id:unknownId,status:'unresolved',reason:'材料不存在或不在可访问范围'}, {material_id:privateId,status:'unresolved',reason:'材料不存在或不在可访问范围'}, {url:'https://example.com/link-only',status:'source_link',reason:'仅有来源链接，未取得本地原文'}]}};
function account() { return { id, name: '测试账户', version, analysis_state: 'running', holdings: [{symbol: 'SH:600519', quantity: '100'}], preferences: {}, metrics: { positions: [] }}; }
function recommendation(n, historical = false) { return {id:`${historical ? 'history' : 'active'}-${n}`, symbol:n === 0 ? 'SH:600519' : 'SZ:000001', action:'hold', state:historical ? 'expired' : 'needs_review', review_status:historical ? 'expired' : 'needs_review', price_state:historical ? 'not_met' : 'met', observed_price:'120', input_version:1, valid_until:historical ? '2020-01-01T00:00:00+08:00' : null, references:n === 0 ? [reference] : [], review_followup:historical ? null : {state:'pending',job_id:null}, basis:historical ? `历史记录 ${n}` : `活跃记录 ${n}`}; }
function json(url) {
  const path = url.pathname;
  if(path === '/api/status') return {mode: 'demo', model_configured: true, configuration_errors:configurationErrors, worker: {running: true}, calendar:{phase:'closed',session:'lunch_break',trading_day:true,has_closed_today:false}, settings: {quote_provider: 'manual'}};
  if(path === '/api/settings') return {quote_provider: 'manual'};
  if(path === '/api/providers') return [providerFixture];
  if(path === `/api/providers/${providerId}/models`) return providerFixture.catalog;
  if(path === '/api/agent-roles' || path === `/api/accounts/${id}/agent-models`) return roleFixtures;
  if(path === '/api/accounts') return [account()];
  if(path === `/api/accounts/${id}`) return account();
  if(path === `/api/accounts/${id}/analyses`) return [{id: rid, status: 'current', input_version: 1}];
  if(path === `/api/analyses/${rid}`) { detailFetches++; return report; }
  if(path === `/api/accounts/${id}/jobs`) return [{id: 'job-1', input_version: 1, state: 'running', reason: 'scheduled', synthesis_checkpoint:{available:true,recovery_state:'needs_research',source_coverage:{fixture:{status:'needs_research',reason:'delegated_report_not_delivered'}},report_access:[{source_agent_id:'named-child',role_id:'filing_researcher',research_round:1,access:'full_report'},{source_agent_id:'summary-child',role_id:'market_researcher',research_round:2,access:'report_summary',summary_truncated:true}]}, event_count: eventSeq, last_event_seq: eventSeq}];
  if(path === `/api/accounts/${id}/recommendations`) {
    assert.equal(url.searchParams.get('envelope'), 'true');
    const history = url.searchParams.get('view') === 'history', total = history ? 104 : activeCount;
    const offset = Number(url.searchParams.get('offset')), limit = Number(url.searchParams.get('limit'));
    return {items:Array.from({length:Math.min(limit, Math.max(0,total-offset))}, (_,i) => recommendation(i+offset, history)), total, offset, limit, has_more:offset+limit<total, next_offset:offset+limit<total ? offset+limit : null, counts:{active:activeCount,history:104,needs_review:activeCount,triggered:activeCount}};
  }
  if(path === '/api/global-research') return [{id:'global-1',kind:'candidate_scan',state:'completed',universe_version:1,applicability:'stale'}];
  if(path === '/api/global-research/global-1') return {id:'global-1',universe_version:1,applicability:'stale',result:{report:'公共研究协议样例',model_calls:2}};
  if(path === '/api/research') return [{id:'research-1',kind:'candidate',title:'当前公共结论',universe_version:2,applicability:'current',summary:'当前候选范围的研究',references:[{material_id:unknownId,status:'unresolved',reason:'材料不存在或不在可访问范围'}]}];
  if(path === '/api/demands') return [];
  if(path === '/api/universe') return {universe_version:2, watchlist: ['SH:600519'], candidates: [], monitored: ['SH:600519'], quotes: [{symbol:'SH:600519',price, status:'ok'}]};
  if(path === '/api/materials') return [material,extraction,searchMaterial];
  if(path === `/api/materials/${mid}`) { materialBodyFetches++; return material; }
  return null;
}
const server = createServer(async (req, res) => {
  try {
    const url = new URL(req.url, 'http://localhost'); requests.push(url.pathname + url.search);
    if(url.pathname.startsWith('/api/')) {
      let body;
      if(url.pathname === `/api/materials/${extractionId}/range`) {
        body={...extraction,start:0,end:extraction.text.length,total_chars:extraction.text.length,next_start:null};
      } else if(url.pathname === `/api/materials/${mid}/range`) {
        assert.equal(url.searchParams.get('account_id'),id,'evidence access must carry account scope');
        const start = Number(url.searchParams.get('start'));
        body = {...material, text: start ? '第二段关键数据' : '首段正文', start, end: start + 6000, total_chars: 12000, next_start: start ? null : 6000};
      } else if(url.pathname === '/api/jobs/job-1/events') {
        const after = Number(url.searchParams.get('after'));
        body = {events: Array.from({length: Math.max(0, eventSeq - after)}, (_,i) => ({seq:after+i+1, type:'progress', message:`事件 ${after+i+1}`})), next_after:eventSeq, has_more:false};
      } else body = json(url);
      res.writeHead(body === null ? 404 : 200, {'Content-Type':'application/json'}); res.end(JSON.stringify(body)); return;
    }
    const filename = url.pathname === '/' ? 'index.html' : url.pathname.split('/').pop();
    const content = await readFile(new URL(`../app/static/${filename}`, import.meta.url)); res.writeHead(200, {'Content-Type': filename.endsWith('.js') ? 'application/javascript' : filename.endsWith('.css') ? 'text/css' : 'text/html'}); res.end(content);
  } catch(error) {res.writeHead(500);res.end(String(error));}
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
let browser, page;
try {
  browser = await chromium.launch({headless:true, ...(process.env.BROWSER_EXECUTABLE ? {executablePath:process.env.BROWSER_EXECUTABLE} : {})});
  page = await browser.newPage();
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  await page.clock.install({time:new Date('2026-10-25T10:00:00+08:00')});
  await page.goto(`http://127.0.0.1:${server.address().port}/#market`);
  await page.locator('#watchlist').fill('SZ:000001\nSH:510300');
  await page.locator('#candidates').fill('SZ:159915');
  await page.locator('#watchlist').focus();
  await page.locator('#watchlist').evaluate(el => el.setSelectionRange(2, 6));
  price = '77.125';
  await page.clock.fastForward(4000);
  await page.waitForFunction(() => document.querySelector('tbody')?.textContent.includes('77.125'));
  assert.equal(await page.locator('#watchlist').inputValue(), 'SZ:000001\nSH:510300');
  assert.equal(await page.locator('#candidates').inputValue(), 'SZ:159915');
  assert.equal(await page.evaluate(() => document.activeElement.id), 'watchlist');
  assert.deepEqual(await page.locator('#watchlist').evaluate(el => [el.selectionStart,el.selectionEnd]), [2,6]);
  await page.getByText('候选范围 v2',{exact:true}).waitFor();
  await page.locator('details[data-key="global-runs"] > summary').click();
  await page.getByText('历史研究范围 · 不作为当前公共结论',{exact:true}).waitFor();
  await page.locator('[data-action="global-result"]').click();
  await page.getByText('公共研究协议样例', {exact:true}).waitFor();
  assert.ok((await page.locator('#dialog').textContent()).includes('候选范围 v1'));
  await page.locator('#dialog .dialog-header button').click();
  await page.locator('nav a[href="#holdings"]').click();
  await page.locator('#name').fill('未保存账户草稿');
  await page.locator('input[name^="quantity-"]').fill('321');
  version = 2;
  await page.clock.fastForward(4000);
  await page.waitForFunction(() => document.querySelector('.account-list')?.textContent.includes('v2'));
  assert.equal(await page.locator('#name').inputValue(), '未保存账户草稿');
  assert.equal(await page.locator('input[name^="quantity-"]').inputValue(), '321');
  assert.equal(await page.locator('#account-form').getAttribute('data-version'), '1');
  await page.locator('nav a[href="#market"]').click();
  assert.equal(await page.locator('#watchlist').inputValue(), 'SZ:000001\nSH:510300');
  await page.locator('nav a[href="#research"]').click();
  await page.getByText('原文来自 text 字段', {exact:true}).waitFor();
  assert.equal(await page.locator('#active-recommendations .recommendation').count(),2,'104 history rows must not hide either active suggestion');
  assert.equal(await page.locator('#recommendation-history .recommendation').count(),0,'history starts collapsed');
  assert.equal(await page.locator('#active-recommendations').getByText('价格条件已满足',{exact:true}).count(),2);
  assert.equal(await page.locator('#active-recommendations').getByText('判断待复核',{exact:true}).count(),2);
  assert.ok((await page.locator('[data-pagination="recommendations"]').textContent()).includes('共 2 条'));
  await page.locator('[data-action="recommendation-history"]').click();
  await page.locator('[data-recommendation-id="history-0"]').waitFor();
  assert.equal(await page.locator('#recommendation-history .recommendation').count(),10);
  await page.locator('[data-pagination="recommendationHistory"]').getByText('下一页',{exact:true}).click();
  await page.locator('[data-recommendation-id="history-10"]').waitFor();
  assert.ok((await page.locator('[data-pagination="recommendationHistory"]').textContent()).includes('11–20 / 共 104 条'));
  assert.equal(await page.locator('#active-recommendations .recommendation').count(),2);
  activeCount=12;
  await page.clock.fastForward(4000);
  await page.locator('[data-recommendation-id="active-9"]').waitFor();
  await page.locator('[data-pagination="recommendations"]').getByText('下一页',{exact:true}).click();
  await page.locator('[data-recommendation-id="active-11"]').waitFor();
  assert.equal(await page.locator('#active-recommendations .recommendation').count(),2);
  assert.ok(await page.locator('[data-pagination="recommendations"]').getByText('下一页',{exact:true}).isDisabled());
  assert.ok((await page.locator('[data-pagination="recommendations"]').textContent()).includes('11–12 / 共 12 条'));
  assert.ok(await page.getByText('已过期',{exact:true}).count());
  await page.getByText('本次报告研究范围 v1',{exact:true}).waitFor();
  assert.equal(await page.getByText('private-research-fixture',{exact:true}).count(),0);
  await page.getByText('复用的公共研究（1 条）',{exact:true}).click();
  await page.getByText('shared-public-fixture',{exact:true}).waitFor();
  assert.equal(await page.locator('.report > .evidence-references > [data-reference-status="unresolved"]').count(),2);
  assert.equal(await page.locator('.report [data-reference-status="source_link"] a').count(),2);
  const extractionEvidence=page.locator('.report [data-reference-kind="agent_extraction"]');
  assert.ok((await extractionEvidence.textContent()).includes('已定位 Agent 提取 · 不是外部原文'));
  assert.ok((await extractionEvidence.textContent()).includes('未关联原文材料ID，来源与页码为提取者提供'));
  assert.ok((await extractionEvidence.textContent()).includes('提取者标注页码 28–28'));
  assert.ok(!(await extractionEvidence.textContent()).includes('已保存页码'));
  const generatedEvidence=page.locator('.report [data-reference-kind="generated_analysis"]');
  assert.ok((await generatedEvidence.textContent()).includes('已定位生成报告 · 不是外部原文'));
  await page.locator('.report .evidence-references > summary').first().click();
  await generatedEvidence.locator('.origin-references > summary').click();
  assert.equal(await generatedEvidence.locator('[data-reference-status="unresolved"]').count(),1);
  assert.equal(await generatedEvidence.locator('.material-reference').count(),2);
  assert.equal(await page.locator(`.material-reference[data-id="${failedId}"]`).count(),0);
  assert.ok((await page.locator('.report').textContent()).includes('原文获取失败，仅保留来源链接'));
  assert.equal(await page.locator(`#material-${searchMaterial.id} .button[data-action="open-material"]`).textContent(),'按段查看已保存检索结果');
  await extractionEvidence.locator('.material-reference').click();
  await page.locator('#dialog .material-text').waitFor();
  assert.ok((await page.locator('#dialog').textContent()).includes('提取者标注页码 28–28'));
  assert.ok(!(await page.locator('#dialog').textContent()).includes('已保存页码'));
  assert.ok((await page.locator('#dialog').textContent()).includes('未关联原文材料ID，来源与页码为提取者提供'));
  await page.locator('#dialog .dialog-header button').click();
  assert.equal(await page.locator(`.material-reference[data-id="${unknownId}"], .material-reference[data-id="${privateId}"]`).count(),0,'unresolved and inaccessible references are never evidence links');
  await page.locator(`.report .material-reference[data-id="${mid}"]`).first().click();
  await page.locator('#dialog .material-text').waitFor();
  assert.equal(await page.locator('#dialog .material-text').textContent(), '首段正文');
  assert.ok((await page.locator('#dialog').textContent()).includes('25–26'));
  assert.ok((await page.locator('#dialog').textContent()).includes('部分内容 · 未读取全文'));
  await page.locator('#dialog button[data-action="material-range"]').last().click();
  await page.waitForFunction(() => document.querySelector('#dialog .material-text')?.textContent === '第二段关键数据');
  await page.locator('#dialog .dialog-header button').click();
  await page.locator('nav a[href="#agents"]').click();
  await page.getByText('事件 1', {exact:true}).waitFor();
  const checkpoint=page.locator('.synthesis-checkpoint');
  assert.ok((await checkpoint.textContent()).includes('研究成果或资料仍有未解决范围'));
  assert.ok((await checkpoint.textContent()).includes('子报告尚未完整交付'));
  assert.ok((await checkpoint.textContent()).includes('收到完整子报告（非主 Agent 亲读原文）'));
  assert.ok((await checkpoint.textContent()).includes('仅收到子报告摘要 · 摘要已截断'));
  assert.ok(!(await checkpoint.textContent()).includes('材料更正仍待处理'));

  eventSeq = 2;
  await page.clock.fastForward(4000);
  await page.getByText('事件 2', {exact:true}).waitFor();
  assert.equal(await page.getByText('事件 1', {exact:true}).count(), 1);
  assert.ok(requests.some(url => url.includes('/events?after=1')));
  assert.ok(requests.filter(url => url.startsWith('/api/materials?')).every(url => url.includes('index=true') && url.includes('limit=10')));
  assert.equal(detailFetches, 1, 'unchanged reports must not be re-downloaded every poll');
  assert.equal(materialBodyFetches, 0, 'material details use bounded ranges');
  configurationErrors=['reasoning_effort 非法值：unexpected；允许 low、medium、high、xhigh、max'];
  await page.clock.fastForward(4000);
  await page.locator('#configuration-errors').waitFor();
  assert.ok((await page.locator('#configuration-errors').textContent()).includes(configurationErrors[0]));
  await page.locator('nav a[href="#settings"]').click();
  await page.locator('#settings-form').waitFor();
  assert.ok((await page.locator('main').textContent()).includes('市场：午间休市 · 交易日'));
  assert.ok(!(await page.locator('main').textContent()).includes('市场：已收盘'));
  assert.equal(await page.locator('#settings-form #model_name, #settings-form #model_key_env, #settings-form #model_protocol, #settings-form #reasoning_effort').count(),0);
  await page.locator('nav a[href="#providers"]').click();
  await page.locator('#provider-connection-form').waitFor();
  assert.deepEqual(await page.locator('#provider-connection-form [name]').evaluateAll(nodes=>nodes.map(node=>node.name).sort()),['api_key','base_url','protocol']);
  await page.locator('nav a[href="#roles"]').click();
  await page.locator('.role-model-form[data-role-id="manager"]').waitFor();
  await page.locator('.loading-line').waitFor({state:'hidden'});
  assert.deepEqual(await page.locator('.role-card').evaluateAll(cards=>cards.map(card=>card.dataset.roleId)),roleIds);
  const managerForm=page.locator('.role-model-form[data-role-id="manager"]');
  assert.deepEqual(await managerForm.locator('[name="reasoning_effort"] option').evaluateAll(options=>options.map(option=>option.value)),['low','medium','high','xhigh','max']);
  assert.deepEqual(await managerForm.locator('[name="model_id"] option').evaluateAll(options=>options.map(option=>option.value)),['','fixture/Model-A','fixture/Model-B']);
  await managerForm.locator('[data-model-filter]').fill('does-not-exist');
  assert.deepEqual(await managerForm.locator('[name="model_id"] option').evaluateAll(options=>options.map(option=>option.value)),['']);
  await page.locator('nav a[href="#account-models"]').click();
  await page.locator(`.role-model-form[data-role-id="manager"][data-account-id="${id}"]`).waitFor();
  await page.locator('.loading-line').waitFor({state:'hidden'});
  assert.deepEqual(await page.locator('.role-card').evaluateAll(cards=>cards.map(card=>card.dataset.roleId)),roleIds);
  assert.equal(await page.locator('.role-model-form [name="enabled"], .role-model-form [name="system_prompt"], .role-model-form [name="tools"]').count(),0);
  assert.deepEqual(errors, []);
  console.log('PASS (deterministic HTTP fixtures + Chromium): polling, protected drafts/focus/version, active/history pagination (106 rows, 12 active), price/review separation, resolved/source-only/unresolved evidence, selected PDF ranges, scope version labels, extraction/report provenance and unresolved origins, failed-source links, incremental events, three-field provider form, discovered-only model filtering and identical six-role account matrices.');
} catch (error) {
  await mkdir('test-results',{recursive:true});
  if(page) { await page.screenshot({path:'test-results/frontend-failure.png',fullPage:true}); await import('node:fs/promises').then(async fs=>fs.writeFile('test-results/frontend-failure.html',await page.content())); }
  throw error;
} finally { await browser?.close(); await new Promise(resolve => server.close(resolve)); }
