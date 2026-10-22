// Deterministic browser regression using HTTP fixtures. No real provider or model calls.
// Run: NODE_PATH=/path/to/node_modules node tests/frontend_iteration.mjs
import { createRequire } from 'node:module';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import assert from 'node:assert/strict';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright');
const id = 'a'.repeat(32), mid = 'b'.repeat(32), rid = 'c'.repeat(32);
let price = '10', version = 1, eventSeq = 1, detailFetches = 0, materialBodyFetches = 0;
const requests = [];
const material = { id: mid, title: '长财报', text: '原文来自 text 字段', access: 'full_text', truncated: true, page_count: 32, returned_range: {start_page: 25, end_page: 26}, url: 'https://example.com/report.pdf', fetched_at: '2026-10-22T15:00:00+08:00' };
const report = {id: rid, status: 'current', input_version: 1, report: `证据 ${mid}`, result: {material_ids: [mid]}};
function account() { return { id, name: '测试账户', version, analysis_state: 'running', holdings: [{symbol: 'SH:600519', quantity: '100'}], preferences: {}, metrics: { positions: [] }}; }
function json(path) {
  if(path === '/api/status') return {mode: 'demo', model_configured: true, worker: {running: true}, settings: {quote_provider: 'manual'}};
  if(path === '/api/settings') return {quote_provider: 'manual'};
  if(path === '/api/accounts') return [account()];
  if(path === `/api/accounts/${id}`) return account();
  if(path === `/api/accounts/${id}/analyses`) return [{id: rid, status: 'current', input_version: 1}];
  if(path === `/api/analyses/${rid}`) { detailFetches++; return report; }
  if(path === `/api/accounts/${id}/jobs`) return [{id: 'job-1', input_version: 1, state: 'running', reason: 'scheduled', event_count: eventSeq, last_event_seq: eventSeq}];
  if(path === `/api/accounts/${id}/recommendations`) return [{id:'rec-1', symbol: 'SH:600519', action: 'hold', state: 'watching', input_version: 1, valid_until: '2020-01-01T00:00:00+08:00'}];
  if(path === '/api/global-research') return [{id:'global-1',kind:'candidate_scan',state:'completed'}];
  if(path === '/api/global-research/global-1') return {id:'global-1',result:{report:'公共研究协议样例',model_calls:2}};
  if(['/api/research','/api/demands'].includes(path)) return [];
  if(path === '/api/universe') return { watchlist: ['SH:600519'], candidates: [], monitored: ['SH:600519'], quotes: [{symbol:'SH:600519',price, status:'ok'}]};
  if(path === '/api/materials') return [material];
  if(path === `/api/materials/${mid}`) { materialBodyFetches++; return material; }
  return null;
}
const server = createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost'); requests.push(url.pathname + url.search);
  if(url.pathname.startsWith('/api/')) {
    let body;
    if(url.pathname === `/api/materials/${mid}/range`) {
      const start = Number(url.searchParams.get('start'));
      body = {...material, text: start ? '第二段关键数据' : '首段正文', start, end: start + 6000, total_chars: 12000, next_start: start ? null : 6000};
    } else if(url.pathname === '/api/jobs/job-1/events') {
      const after = Number(url.searchParams.get('after'));
      body = {events: Array.from({length: Math.max(0, eventSeq - after)}, (_,i) => ({seq:after+i+1, type:'progress', message:`事件 ${after+i+1}`})), next_after:eventSeq, has_more:false};
    } else body = json(url.pathname);
    res.writeHead(body === null ? 404 : 200, {'Content-Type':'application/json'}); res.end(JSON.stringify(body)); return;
  }
  const filename = url.pathname === '/' ? 'index.html' : url.pathname.split('/').pop();
  try { const content = await readFile(new URL(`../app/static/${filename}`, import.meta.url)); res.writeHead(200, {'Content-Type': filename.endsWith('.js') ? 'application/javascript' : filename.endsWith('.css') ? 'text/css' : 'text/html'}); res.end(content); }
  catch { res.writeHead(404);res.end(); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const browser = await chromium.launch({headless:true, ...(process.env.BROWSER_EXECUTABLE ? {executablePath:process.env.BROWSER_EXECUTABLE} : {})});
const page = await browser.newPage();
const errors = []; page.on('pageerror', error => errors.push(error.message));
try {
  await page.goto(`http://127.0.0.1:${server.address().port}/#market`);
  await page.locator('#watchlist').fill('SZ:000001\nSH:510300');
  await page.locator('#candidates').fill('SZ:159915');
  await page.locator('#watchlist').focus();
  await page.locator('#watchlist').evaluate(el => el.setSelectionRange(2, 6));
  price = '77.125';
  await page.waitForFunction(() => document.querySelector('tbody')?.textContent.includes('77.125'), {timeout: 10000});
  assert.equal(await page.locator('#watchlist').inputValue(), 'SZ:000001\nSH:510300');
  assert.equal(await page.locator('#candidates').inputValue(), 'SZ:159915');
  assert.equal(await page.evaluate(() => document.activeElement.id), 'watchlist');
  assert.deepEqual(await page.locator('#watchlist').evaluate(el => [el.selectionStart,el.selectionEnd]), [2,6]);
  await page.locator('details[data-key="global-runs"] > summary').click();
  await page.locator('[data-action="global-result"]').click();
  await page.getByText('公共研究协议样例', {exact:true}).waitFor();
  await page.locator('#dialog .dialog-header button').click();
  await page.locator('nav a[href="#holdings"]').click();
  await page.locator('#name').fill('未保存账户草稿');
  await page.locator('input[name^="quantity-"]').fill('321');
  version = 2;
  await page.waitForTimeout(4500);
  assert.equal(await page.locator('#name').inputValue(), '未保存账户草稿');
  assert.equal(await page.locator('input[name^="quantity-"]').inputValue(), '321');
  assert.equal(await page.locator('#account-form').getAttribute('data-version'), '1');
  await page.locator('nav a[href="#market"]').click();
  assert.equal(await page.locator('#watchlist').inputValue(), 'SZ:000001\nSH:510300');
  await page.locator('nav a[href="#research"]').click();
  await page.getByText('原文来自 text 字段', {exact:true}).waitFor();
  assert.ok(await page.getByText('部分内容 · 未读取全文', {exact:false}).count());
  assert.ok(await page.getByText('已过期', {exact:true}).count());
  await page.locator(`.report .material-reference[data-id="${mid}"]`).click();
  await page.locator('#dialog .material-text').waitFor();
  assert.equal(await page.locator('#dialog .material-text').textContent(), '首段正文');
  assert.ok((await page.locator('#dialog').textContent()).includes('25–26'));
  await page.locator('#dialog button[data-action="material-range"]').last().click();
  await page.waitForFunction(() => document.querySelector('#dialog .material-text')?.textContent === '第二段关键数据');
  await page.locator('#dialog .dialog-header button').click();
  await page.locator('nav a[href="#agents"]').click();
  await page.getByText('事件 1', {exact:true}).waitFor();
  eventSeq = 2;
  await page.getByText('事件 2', {exact:true}).waitFor({timeout:10000});
  assert.equal(await page.getByText('事件 1', {exact:true}).count(), 1);
  assert.ok(requests.some(url => url.includes('/events?after=1')));
  assert.ok(requests.filter(url => url.startsWith('/api/materials?')).every(url => url.includes('index=true') && url.includes('limit=10')));
  assert.equal(detailFetches, 1, 'unchanged reports must not be re-downloaded every poll');
  assert.equal(materialBodyFetches, 0, 'material details use bounded ranges');
  assert.deepEqual(errors, []);
  console.log('PASS: market polling, draft/focus/version preservation, text/PDF evidence, expiry display, report caching, incremental events, global report/usage detail.');
} finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
