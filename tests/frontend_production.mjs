// Chromium integration against the production FastAPI routes and a disposable SQLite DB.
// The Python server seeds explicit deterministic fixtures; its native probe is a
// protocol double. It never starts model/provider workers or real network research.
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
  assert.equal(await report.locator('[data-reference-status="source_link"]').count(),2);
  const nativeLink=page.locator(`#material-${fixture.native_link_id}`);
  const nativeExcerpt=page.locator(`#material-${fixture.native_excerpt_id}`);
  assert.ok((await nativeLink.textContent()).includes('模型服务返回的来源链接 · 本地未下载原文'));
  assert.ok((await nativeExcerpt.textContent()).includes('仅检索摘要 · 未读取全文'));
  assert.ok((await nativeExcerpt.textContent()).includes('不代表原文哈希'));
  await nativeExcerpt.locator('[data-action="open-material"]').first().click();
  await page.locator('#dialog .material-text').waitFor();
  assert.equal(await page.locator('#dialog .material-text').textContent(),'受控服务端返回的可见片段，不是全文');
  assert.ok((await page.locator('#dialog').textContent()).includes('仅检索摘要 · 未读取全文'));
  await page.locator('#dialog .dialog-header button').click();
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
  await page.locator('nav a[href="#agents"]').click();
  const checkpointCard=page.locator('.job-card').filter({has:page.locator(`[data-action="retry-job"][data-id="${fixture.checkpoint_job_id}"]`)});
  await checkpointCard.locator('.synthesis-checkpoint').waitFor();
  assert.ok((await checkpointCard.textContent()).includes('重试时优先恢复汇总'));
  assert.ok((await checkpointCard.textContent()).includes('不会将子报告当作最终分析'));
  const retried=page.waitForResponse(response=>response.url().endsWith(`/api/jobs/${fixture.checkpoint_job_id}/retry`) && response.request().method()==='POST');
  await checkpointCard.locator('[data-action="retry-job"]').click();
  const retryResult=await (await retried).json();
  assert.notEqual(retryResult.id,fixture.checkpoint_job_id);
  assert.equal(retryResult.account_id,fixture.other_id);
  assert.equal(retryResult.state,'queued');
  assert.equal((await (await fetch(base+`/api/jobs/${fixture.checkpoint_job_id}`)).json()).synthesis_checkpoint.available,true);
  await page.locator('nav a[href="#holdings"]').click();
  await page.locator('.loading-line').waitFor({state:'hidden'});
  assert.notEqual(await page.locator('#account-form #name').inputValue(),draftName,'account A draft must not appear in account B');
  await page.locator('#account-switch').selectOption(fixture.account_id);
  await page.locator('.loading-line').waitFor({state:'hidden'});
  assert.equal(await page.locator('#account-form #name').inputValue(),draftName,'account A draft survives the deferred switch');
  // Actual migration + settings writes: an old independent-provider disable does
  // not switch native search off; an explicit user disable survives Store reopen.
  await page.locator('nav a[href="#settings"]').click();
  await page.locator('#model_native_search').waitFor();
  assert.equal(await page.locator('#model_native_search').inputValue(),'true');
  assert.equal(await page.locator('#search_provider, #search_key_env').count(),0);
  const waitCapability=async label=>page.waitForFunction(text=>document.body.textContent.includes('原生联网：'+text),label);
  await waitCapability('尚未验证');
  const saveSettings=async()=>{
    const saved=page.waitForResponse(response=>response.url().endsWith('/api/settings') && response.request().method()==='PUT');
    await page.locator('#settings-form button[type="submit"]').click();
    assert.equal((await saved).status(),200);
    await page.locator('.loading-line').waitFor({state:'hidden'});
  };
  await page.locator('#model_native_search').selectOption('false');
  await saveSettings();
  await waitCapability('已关闭');
  await page.reload();
  await page.locator('#model_native_search').waitFor();
  assert.equal(await page.locator('#model_native_search').inputValue(),'false');
  const reopened=await (await fetch(base+'/__fixture/reopen')).json();
  assert.equal(reopened.model_native_search,false);
  assert.equal(reopened.legacy_search_provider,'disabled');
  await page.locator('#model_native_search').selectOption('true');
  await page.locator('#model_protocol').selectOption('chat_completions');
  await saveSettings();
  await waitCapability('当前协议或端点不支持');
  await page.locator('#model_protocol').selectOption('responses');
  await saveSettings();
  await waitCapability('尚未验证');
  const probe=async()=>{
    const response=page.waitForResponse(item=>item.url().endsWith('/api/settings/probe-native-search') && item.request().method()==='POST');
    await page.locator('[data-action="probe-native"]').click();
    const result=await (await response).json();
    await page.locator('#dialog').getByText('模型原生联网实测',{exact:true}).waitFor();
    assert.ok((await page.locator('#dialog').textContent()).includes('受控浏览器协议替身'));
    await page.locator('#dialog .dialog-header button').click();
    return result;
  };
  assert.equal((await probe()).status,'observed');
  await waitCapability('已观察到原生搜索');
  assert.equal((await probe()).cache_hit,true,'refreshing the capability should reuse its bounded probe result');
  assert.equal((await (await fetch(base+'/__fixture/reopen')).json()).probe_calls,1);
  await page.locator('#model_name').fill('browser-native-auth');
  await saveSettings();
  await waitCapability('尚未验证');
  assert.equal((await probe()).status,'authentication_failed');
  await waitCapability('认证失败');
  await page.locator('#model_name').fill('browser-native-failure');
  await saveSettings();
  assert.equal((await probe()).status,'request_failed');
  await waitCapability('请求失败');
  assert.deepEqual(errors,[]);
  console.log('PASS (production FastAPI + temporary SQLite + Chromium; native probe protocol double): 106 recommendations, history pages, price/review separation, evidence scope, native link/excerpt display, checkpoint retry, settings migration and explicit disable persistence, six capability states and probe caching, late global versions, draft preservation and account isolation.');
} catch(error) {
  await mkdir('test-results',{recursive:true});
  if(page) await page.screenshot({path:'test-results/frontend-production-failure.png',fullPage:true});
  throw error;
} finally {
  await browser?.close();
  if(child.exitCode===null) {const ended=new Promise(resolve=>child.once('exit',resolve));child.kill('SIGTERM');await ended;}
  await rm(directory,{recursive:true,force:true});
}
