// Chromium integration against the production FastAPI routes and a disposable SQLite DB.
// The Python server seeds explicit deterministic fixtures; it never starts model/provider workers.
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { mkdtemp, rm, mkdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import assert from 'node:assert/strict';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright');
const directory = await mkdtemp(join(tmpdir(), 'guanlan-browser-'));
const child = spawn(process.env.PYTHON || 'python', ['tests/browser_app_server.py', directory], {env:{...process.env, PYTHON_DOTENV_DISABLED:'1'}, stdio:['ignore','pipe','pipe']});
let diagnostic = '', browser, page;
child.stderr.on('data', data => {diagnostic += data.toString();});
try {
  const fixture = await new Promise((resolve, reject) => {
    let output='';
    const timeout=setTimeout(()=>reject(new Error('Temporary browser server did not start: '+diagnostic)),30000);
    child.stdout.on('data', data => {
      output+=data.toString();
      if(output.includes('\n')) {clearTimeout(timeout);try {resolve(JSON.parse(output.split('\n')[0]));}catch(error){reject(error);}}
    });
    child.once('exit',code=>{clearTimeout(timeout);reject(new Error(`Temporary browser server exited ${code}: ${diagnostic}`));});
  });
  const base=`http://127.0.0.1:${fixture.port}`;
  for(let attempt=0; ; attempt++) {
    try {if((await fetch(base+'/api/status')).ok) break;} catch(error) {if(attempt>=50) throw error;}
    await new Promise(resolve=>setTimeout(resolve,50));
  }
  browser=await chromium.launch({headless:true,...(process.env.BROWSER_EXECUTABLE ? {executablePath:process.env.BROWSER_EXECUTABLE} : {})});
  page=await browser.newPage();
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.addInitScript(accountId=>localStorage.setItem('guanlan.account',accountId),fixture.account_id);
  await page.goto(base+'/#research');
  await page.locator('#active-recommendations .recommendation').first().waitFor();
  assert.equal(await page.locator('#active-recommendations .recommendation').count(),2);
  assert.ok((await page.locator('#active-recommendations').textContent()).includes('SH:600519'));
  assert.ok((await page.locator('#active-recommendations').textContent()).includes('SZ:000001'));
  assert.equal(await page.locator('#active-recommendations').getByText('价格条件已满足',{exact:true}).count(),2);
  assert.equal(await page.locator('#active-recommendations').getByText('判断待复核',{exact:true}).count(),2);
  await page.locator('[data-action="recommendation-history"]').click();
  await page.locator('#recommendation-history .recommendation').first().waitFor();
  assert.ok((await page.locator('[data-pagination="recommendationHistory"]').textContent()).includes('共 104 条'));
  const firstHistory=await page.locator('#recommendation-history .recommendation').first().getAttribute('data-recommendation-id');
  await page.locator('[data-pagination="recommendationHistory"]').getByText('下一页',{exact:true}).click();
  await page.waitForFunction(first=>document.querySelector('#recommendation-history .recommendation')?.dataset.recommendationId!==first,firstHistory);
  assert.ok((await page.locator('[data-pagination="recommendationHistory"]').textContent()).includes('11–20'));
  const report=page.locator('.report').first();
  assert.equal(await report.locator('[data-reference-status="unresolved"]').count(),2);
  assert.equal(await report.locator('[data-reference-status="source_link"]').count(),1);
  assert.equal(await page.getByText('账户 B 私有原文不得显示',{exact:false}).count(),0);
  assert.equal(await page.getByText('账户 B 私有资料标题',{exact:false}).count(),0);
  assert.equal(await page.locator(`.material-reference[data-id="${fixture.private_id}"]`).count(),0);
  await report.locator(`.material-reference[data-id="${fixture.material_id}"]`).first().click();
  await page.locator('#dialog .material-text').waitFor();
  assert.equal(await page.locator('#dialog .material-text').textContent(),'受控已保存 PDF 选页正文');
  assert.ok((await page.locator('#dialog').textContent()).includes('25–26'));
  await page.locator('#dialog .dialog-header button').click();
  await page.locator('[data-action="retry-recommendation"]').first().click();
  await page.getByText('已登记复核需求，处理状态可在建议与 Agent 工作台查看。',{exact:true}).waitFor();
  assert.ok((await page.locator('#active-recommendations').textContent()).includes('已安排研究'));
  await page.locator('nav a[href="#market"]').click();
  await page.locator("article .research-version").getByText(`候选范围 v${fixture.universe_version}`,{exact:true}).waitFor();
  await page.locator('details[data-key="global-runs"] > summary').click();
  await page.getByText('历史研究范围 · 不作为当前公共结论',{exact:true}).waitFor();
  assert.equal(await page.getByText('历史范围结论',{exact:true}).count(),0);
  await page.locator('nav a[href="#holdings"]').click();
  await page.locator('.loading-line').waitFor({state:'hidden'});
  const draftName='账户 A 未保存草稿';
  await page.locator('#account-form #name').fill(draftName);
  // Hold navigation's status request so an account choice occurs while the UI is busy.
  // The user's latest choice must be applied after that read, without a reload.
  let releaseNavigation, observeNavigation;
  const navigationGate=new Promise(resolve=>{releaseNavigation=resolve;});
  const navigationStarted=new Promise(resolve=>{observeNavigation=resolve;});
  await page.route('**/api/status',async route=>{observeNavigation();await navigationGate;await route.continue();},{times:1});
  await page.locator('nav a[href="#research"]').click();
  await navigationStarted;
  await page.locator('#account-switch').selectOption(fixture.other_id);
  await page.locator('#account-switch').selectOption(fixture.account_id);
  await page.locator('#account-switch').selectOption(fixture.other_id);
  releaseNavigation();
  await page.locator('.loading-line').waitFor({state:'hidden'});
  assert.equal(await page.locator('#account-switch').inputValue(),fixture.other_id,'account selection during navigation must not be silently discarded');
  await page.getByText('当前没有仍需关注的结构化建议。',{exact:true}).waitFor();
  assert.equal(await page.locator('#active-recommendations .recommendation').count(),0);
  await page.locator('nav a[href="#holdings"]').click();
  await page.locator('.loading-line').waitFor({state:'hidden'});
  assert.notEqual(await page.locator('#account-form #name').inputValue(),draftName,'account A draft must not appear in account B');
  await page.locator('#account-switch').selectOption(fixture.account_id);
  await page.locator('.loading-line').waitFor({state:'hidden'});
  assert.equal(await page.locator('#account-form #name').inputValue(),draftName,'account A draft survives the deferred switch');
  assert.deepEqual(errors,[]);
  console.log('PASS (production FastAPI + temporary SQLite + Chromium): 106 recommendations, history pages, independent price/review status, persisted retry, evidence scope/selected pages, late global result version labels, deferred latest account selection, draft preservation and account isolation.');
} catch(error) {
  await mkdir('test-results',{recursive:true});
  if(page) await page.screenshot({path:'test-results/frontend-production-failure.png',fullPage:true});
  throw error;
} finally {
  await browser?.close();
  if(child.exitCode===null) {const ended=new Promise(resolve=>child.once('exit',resolve));child.kill('SIGTERM');await ended;}
  await rm(directory,{recursive:true,force:true});
}
