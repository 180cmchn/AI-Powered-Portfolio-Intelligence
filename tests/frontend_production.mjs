// Chromium integration against the production FastAPI routes and a disposable SQLite DB.
// The Python server seeds explicit deterministic fixtures; its native probe is a
// protocol double. It never starts model/provider workers or real network research.
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { mkdtemp, rm, mkdir, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import assert from 'node:assert/strict';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright');
const directory = await mkdtemp(join(tmpdir(), 'guanlan-browser-'));
const child = spawn(process.env.PYTHON || 'python', ['tests/browser_app_server.py', directory], {env:{...process.env, PYTHON_DOTENV_DISABLED:'1'}, stdio:['ignore','pipe','pipe']});
let diagnostic = '', browser, page;
const requestTrace=[];
const waitGate=(promise,label)=>new Promise((resolve,reject)=>{
  const timer=setTimeout(()=>reject(new Error(`Browser gate not observed: ${label}`)),10000);
  promise.then(value=>{clearTimeout(timer);resolve(value);},error=>{clearTimeout(timer);reject(error);});
});
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
  // Keep browser interception enabled for the whole test. Removing the last
  // matching route while a response starts follow-up fetches can strand a CDP
  // paused request. Gates below consume by method once, without live teardown.
  await page.route('**/api/**',route=>route.continue());
  const navigate=async name=>{
    await page.locator(`nav a[href="#${name}"]`).click();
    await page.locator(`nav a[href="#${name}"][aria-current="page"]`).waitFor();
  };
  const assertIdle=async()=>{
    await page.locator('.loading-line').waitFor({state:'hidden'});
    const snapshot=await page.evaluate(()=>window.__guanlanDiagnostics.snapshot());
    assert.deepEqual(snapshot.operations,[],'all registered operations must finish');
    assert.equal(snapshot.counts.operation_registered, snapshot.counts.operation_finished);
  };
  // This group types a provider key. Do not record trace snapshots or request bodies.
  page.on('request',request=>{if(request.url().includes('/api/')) requestTrace.push({event:'request',method:request.method(),path:new URL(request.url()).pathname});});
  page.on('response',response=>{if(response.url().includes('/api/')) requestTrace.push({event:'response',status:response.status(),path:new URL(response.url()).pathname});});
  page.on('requestfinished',request=>{if(request.url().includes('/api/'))requestTrace.push({event:'finished',method:request.method(),path:new URL(request.url()).pathname});});
  page.on('requestfailed',request=>{if(request.url().includes('/api/'))requestTrace.push({event:'failed',method:request.method(),path:new URL(request.url()).pathname,error:request.failure()?.errorText});});
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.addInitScript(()=>sessionStorage.setItem('guanlan.diagnostics','1'));
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
  await report.locator(".evidence-references > summary").click();
  await report.locator(`.material-reference[data-id="${fixture.material_id}"]`).first().click();
  await page.locator('#dialog .material-text').waitFor();
  assert.equal(await page.locator('#dialog .material-text').textContent(),'受控已保存 PDF 选页正文');
  assert.ok((await page.locator('#dialog').textContent()).includes('25–26'));
  await page.locator('#dialog .dialog-header button').click();
  await page.locator('[data-action="retry-recommendation"]').first().click();
  await page.getByText('已登记复核需求，处理状态可在建议与 Agent 工作台查看。',{exact:true}).waitFor();
  assert.ok((await page.locator('#active-recommendations').textContent()).includes('已安排研究'));
  await navigate('market');
  assert.equal(await page.getByText('当前范围结论',{exact:true}).count(),0,'public generated research does not become selected-account research');
  await page.locator('details[data-key="global-runs"] > summary').click();
  await page.getByText(`候选范围 v${fixture.universe_version}`,{exact:true}).first().waitFor();
  await page.getByText('历史研究范围 · 不作为当前公共结论',{exact:true}).waitFor();
  assert.equal(await page.getByText('历史范围结论',{exact:true}).count(),0);
  await navigate('holdings');
  await page.locator('.loading-line').waitFor({state:'hidden'});
  const draftName='账户 A 未保存草稿';
  await page.locator('#account-form #name').fill(draftName);
  // Hold navigation's status request so an account choice occurs while the UI is busy.
  // The user's latest choice must be applied after that read, without a reload.
  let releaseNavigation, observeNavigation;
  const navigationGate=new Promise(resolve=>{releaseNavigation=resolve;});
  const navigationStarted=new Promise(resolve=>{observeNavigation=resolve;});
  await page.route('**/api/status*',async route=>{observeNavigation();await navigationGate;await route.continue();},{times:1});
  await navigate('research');
  await waitGate(navigationStarted,'navigationStarted');
  await page.locator('#account-switch').selectOption(fixture.other_id);
  await page.locator('#account-switch').selectOption(fixture.account_id);
  await page.locator('#account-switch').selectOption(fixture.other_id);
  releaseNavigation();
  await page.locator('.loading-line').waitFor({state:'hidden'});
  assert.equal(await page.locator('#account-switch').inputValue(),fixture.other_id,'account selection during navigation must not be silently discarded');
  await page.getByText('当前没有仍需关注的结构化建议。',{exact:true}).waitFor();
  assert.equal(await page.locator('#active-recommendations .recommendation').count(),0);
  // The card can render before navigation's reads finish. This is a real usable
  // button: its retry POST must not be dropped by a global loading flag.
  let releaseAgentsNavigation, observeAgentsNavigation;
  const agentsNavigationGate=new Promise(resolve=>{releaseAgentsNavigation=resolve;});
  const agentsNavigationStarted=new Promise(resolve=>{observeAgentsNavigation=resolve;});
  await page.route('**/api/status*',async route=>{observeAgentsNavigation();await agentsNavigationGate;await route.continue();},{times:1});
  await navigate('agents');
  await waitGate(agentsNavigationStarted,'agentsNavigationStarted');
  const checkpointCard=page.locator('.job-card').filter({has:page.locator(`[data-action="retry-job"][data-id="${fixture.checkpoint_job_id}"]`)});
  await checkpointCard.locator('.job-checkpoint-details > summary').click();
  await checkpointCard.locator('.synthesis-checkpoint').waitFor();
  assert.ok((await checkpointCard.textContent()).includes('重试时优先恢复汇总'));
  assert.ok((await checkpointCard.textContent()).includes('不会将子报告当作最终分析'));
  const retried=page.waitForResponse(response=>response.url().endsWith(`/api/jobs/${fixture.checkpoint_job_id}/retry`) && response.request().method()==='POST', {timeout:3000});
  let retryResult;
  try {
    await checkpointCard.locator('[data-action="retry-job"]').click();
    retryResult=await (await retried).json();
  } finally {releaseAgentsNavigation();}
  assert.notEqual(retryResult.id,fixture.checkpoint_job_id);
  assert.equal(retryResult.account_id,fixture.other_id);
  assert.equal(retryResult.state,'queued');
  assert.equal((await (await fetch(base+`/api/jobs/${fixture.checkpoint_job_id}`)).json()).synthesis_checkpoint.available,true);
  await navigate('holdings');
  await page.locator('.loading-line').waitFor({state:'hidden'});
  assert.notEqual(await page.locator('#account-form #name').inputValue(),draftName,'account A draft must not appear in account B');
  await page.locator('#account-switch').selectOption(fixture.account_id);
  await page.locator('.loading-line').waitFor({state:'hidden'});
  assert.equal(await page.locator('#account-form #name').inputValue(),draftName,'account A draft survives the deferred switch');
  // Writes proceed while GET navigation is paused, keep the clicked account and
  // version, coalesce rapid repeats, and preserve edits made before the response.
  const gates=[];
  const pauseRoute=async(pattern, method='GET', afterServer=false)=>{
    let release, observe, complete, matched=false;
    const gateId=gates.length+1;
    const finished=new Promise(resolve=>{complete=resolve;});
    const gate=new Promise(resolve=>{release=resolve;});
    const started=new Promise(resolve=>{observe=resolve;});
    const handler=async route=>{
      if(route.request().method()!==method || matched) return route.fallback();
      matched=true;
      requestTrace.push({event:'gate_matched',gate:gateId,method});
      const response=afterServer ? await route.fetch() : null;
      observe(route.request());
      await gate;
      if(response) await route.fulfill({response}); else await route.continue();
      requestTrace.push({event:'gate_finished',gate:gateId});complete();
      // Retain a consumed handler until browser teardown; subsequent traffic falls through.
    };
    // Playwright counts a fallback GET toward times:1; remove only after
    // the intended method matches, so navigation cannot consume a PUT gate.
    await page.route(pattern,handler);
    const item={started,finished,release:()=>{requestTrace.push({event:'gate_released',gate:gateId});release();}};
    gates.push(item);return item;
  };
  for (let raceRound=0; raceRound<3; raceRound++) {
  const beforeB=await (await fetch(base+`/api/accounts/${fixture.other_id}`)).json();
  await navigate('research');
  await page.locator('.loading-line').waitFor({state:'hidden'});
  const saveNavigation=await pauseRoute('**/api/status*');
  await navigate('holdings');
  await waitGate(saveNavigation.started,'saveNavigation.started');
  const versionAtClick=Number(await page.locator('#account-form').getAttribute('data-version'));
  await page.locator('#name').fill('账户 A 提交时输入');
  const saveGate=await pauseRoute(`**/api/accounts/${fixture.account_id}`,'PUT',true);
  await page.locator('#account-form button[type="submit"]').click();
  const saveRequest=await waitGate(saveGate.started,'saveGate.started');
  await page.locator('#account-form button[type="submit"]').click();
  await page.getByText('保存账户仍在处理中，请勿重复提交；当前输入会保留。',{exact:true}).last().waitFor();
  await page.locator('#name').fill('账户 A 保存后继续输入');
  await page.locator('#account-switch').selectOption(fixture.other_id);
  await page.waitForFunction(other=>document.querySelector('#account-form')?.dataset.accountId===other,fixture.other_id);
  assert.equal(saveRequest.postDataJSON().name,'账户 A 提交时输入');
  assert.equal(saveRequest.postDataJSON().expected_version,versionAtClick);
  if (raceRound===0) {saveGate.release();saveNavigation.release();}
  else if (raceRound===1) {saveGate.release();await page.waitForResponse(r=>r.request()===saveRequest);saveNavigation.release();}
  else {saveNavigation.release();await page.waitForTimeout(35);saveGate.release();}
  await page.locator('.loading-line').waitFor({state:'hidden'});
  assert.equal(requestTrace.filter(item=>item.event==='request' && item.method==='PUT' && item.path===`/api/accounts/${fixture.account_id}`).length,raceRound+1);
  const savedA=await (await fetch(base+`/api/accounts/${fixture.account_id}`)).json();
  const afterB=await (await fetch(base+`/api/accounts/${fixture.other_id}`)).json();
  assert.equal(savedA.name,'账户 A 提交时输入');
  assert.equal(savedA.version,versionAtClick+1);
  assert.deepEqual([afterB.name,afterB.version,afterB.holdings,afterB.preferences],[beforeB.name,beforeB.version,beforeB.holdings,beforeB.preferences]);
  await page.locator('#account-switch').selectOption(fixture.account_id);
  await page.locator('.loading-line').waitFor({state:'hidden'});
  assert.equal(await page.locator('#name').inputValue(),'账户 A 保存后继续输入','late success must retain newer draft');
  assert.equal(Number(await page.locator('#account-form').getAttribute('data-version')),savedA.version);
  await assertIdle();
  }
  await page.route(`**/api/accounts/${fixture.account_id}`,async route=>{
    if(route.request().method()!=='PUT') return route.fallback();
    await route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({detail:'受控保存冲突，草稿应保留'})});
  },{times:1});
  await page.locator('#account-form button[type="submit"]').click();
  await page.getByText('受控保存冲突，草稿应保留',{exact:false}).waitFor();
  assert.equal(await page.locator('#name').inputValue(),'账户 A 保存后继续输入');
  await page.locator('#account-form button[type="submit"]').click();
  await page.locator('.loading-line').waitFor({state:'hidden'});
  assert.equal((await (await fetch(base+`/api/accounts/${fixture.account_id}`)).json()).name,'账户 A 保存后继续输入');
  // A transport failure after the server commits is reconciled by GET of the
  // clicked account, never by resending the write. Later draft/B remain intact.
  const beforeUncertain=await (await fetch(base+`/api/accounts/${fixture.account_id}`)).json();
  const putsBefore=requestTrace.filter(x=>x.event==='request' && x.method==='PUT').length;
  await page.locator('#name').fill('账户 A 已提交但响应丢失');
  let failCommitted, observedCommitted;
  const committedGate=new Promise(resolve=>{failCommitted=resolve;});
  const committedStarted=new Promise(resolve=>{observedCommitted=resolve;});
  let committedMatched=false;
  await page.route(`**/api/accounts/${fixture.account_id}`,async route=>{
    if(route.request().method()!=='PUT' || committedMatched)return route.fallback();
    committedMatched=true;
    assert.equal((await route.fetch()).status(),200);
    observedCommitted();await committedGate;await route.abort('connectionreset');
  });
  await page.locator('#account-form button[type="submit"]').click();
  await waitGate(committedStarted,'committed write before lost response');
  await page.locator('#name').fill('账户 A 响应丢失后新草稿');
  await page.locator('#account-switch').selectOption(fixture.other_id);
  await page.waitForFunction(id=>document.querySelector('#account-form')?.dataset.accountId===id,fixture.other_id);
  failCommitted();
  await assertIdle();
  assert.equal(await page.getByText('保存响应中断；已按操作标识确认原账户、输入版本与研究任务，未重复写入。',{exact:true}).count(),0,'account A save feedback must not appear in account B');
  assert.ok((await page.evaluate(()=>window.__guanlanDiagnostics.snapshot())).counts.save_reconciled>=1,'the original write is still reconciled by its receipt');
  assert.equal(requestTrace.filter(x=>x.event==='request' && x.method==='PUT').length,putsBefore+1);
  const uncertainSaved=await (await fetch(base+`/api/accounts/${fixture.account_id}`)).json();
  assert.equal(uncertainSaved.version,beforeUncertain.version+1);
  assert.equal(uncertainSaved.name,'账户 A 已提交但响应丢失');
  assert.equal(await page.locator('#account-switch').inputValue(),fixture.other_id);
  await page.locator('#account-switch').selectOption(fixture.account_id);
  await assertIdle();
  assert.equal(await page.locator('#name').inputValue(),'账户 A 响应丢失后新草稿');
  assert.equal(Number(await page.locator('#account-form').getAttribute('data-version')),uncertainSaved.version);
  // Exercise the real bounded GET lifecycle without a 15-second test sleep.
  await page.clock.install();
  const timeoutNavigation=await pauseRoute('**/api/status*');
  await navigate('research');
  await waitGate(timeoutNavigation.started,'timeout navigation');
  // Advance only the deliberately held request; let unrelated local reads finish.
  await page.waitForFunction(()=>{const requests=window.__guanlanDiagnostics.snapshot().requests;return requests.length===1 && requests[0].path==='/api/status';});
  await page.clock.fastForward(15001);
  await page.getByText('读取超时，保留已有内容；可刷新重试。',{exact:false}).first().waitFor();
  await assertIdle();
  timeoutNavigation.release();
  await navigate('holdings');await assertIdle();
  const analyzeNavigation=await pauseRoute('**/api/status*');
  await navigate('overview');
  await waitGate(analyzeNavigation.started,'analyzeNavigation.started');
  const analyzeGate=await pauseRoute(`**/api/accounts/${fixture.account_id}/analyze`,'POST',true);
  await page.locator('.page-heading [data-action="analyze"]').click();
  const analyzeRequest=await waitGate(analyzeGate.started,'analyzeGate.started');
  await page.locator('.page-heading [data-action="analyze"]').click();
  await page.getByText('立即分析仍在处理中，请勿重复提交；当前输入会保留。',{exact:true}).waitFor();
  await page.locator('#account-switch').selectOption(fixture.other_id);
  assert.equal(new URL(analyzeRequest.url()).pathname,`/api/accounts/${fixture.account_id}/analyze`);
  analyzeGate.release();analyzeNavigation.release();
  await page.locator('.loading-line').waitFor({state:'hidden'});
  assert.equal(await page.locator('#account-switch').inputValue(),fixture.other_id);
  assert.equal(requestTrace.filter(item=>item.event==='request' && item.method==='POST' && item.path===`/api/accounts/${fixture.account_id}/analyze`).length,1);
  await navigate('holdings');
  await page.locator('.loading-line').waitFor({state:'hidden'});
  await page.getByText('查找证券代码与基础资料',{exact:true}).click();
  let releaseLookup, observeLookup;
  const lookupGate=new Promise(resolve=>{releaseLookup=resolve;});
  const lookupStarted=new Promise(resolve=>{observeLookup=resolve;});
  await page.route('**/api/securities?*',async route=>{
    const old=new URL(route.request().url()).searchParams.get('q')==='old';
    if(old) {observeLookup();await lookupGate;}
    await route.fulfill({contentType:'application/json',body:JSON.stringify([{symbol:old?'SH:600519':'SZ:159278',name:old?'过时证券结果':'新查询结果',kind:'etf'}])});
  });
  await page.locator('#security-query').fill('old');
  await page.locator('[data-action="lookup"]').click();
  await waitGate(lookupStarted,'lookupStarted');
  await page.locator('#security-query').fill('new');
  await page.locator('[data-action="lookup"]').click();
  await page.getByText('新查询结果 · etf',{exact:true}).waitFor();
  releaseLookup();
  await page.locator('.loading-line').waitFor({state:'hidden'});
  assert.ok((await page.locator('#lookup-results').textContent()).includes('新查询结果'));
  assert.equal(await page.getByText('过时证券结果 · etf',{exact:true}).count(),0);
  await page.unroute('**/api/securities?*');
  await Promise.all(gates.map((gate,index)=>waitGate(gate.finished,`gate ${index+1} finished`)));
  const raceLifecycle=await page.evaluate(()=>window.__guanlanDiagnostics.snapshot());
  assert.ok(raceLifecycle.counts.request_cancelled>0);
  assert.equal(raceLifecycle.counts.read_timeout,1,'only the explicitly injected timeout is allowed');
  assert.ok(raceLifecycle.events.some(x=>x.event==='request_cancelled' && x.reason==='read_timeout'));
  assert.equal(raceLifecycle.counts.save_reconciled,1);
  assert.deepEqual(raceLifecycle.operations,[]);
  assert.deepEqual(raceLifecycle.requests,[]);
  assert.equal(raceLifecycle.counts.request_started,raceLifecycle.counts.request_finished);
  // Current UI: connection-only providers, discovered model choices and global roles.
  await navigate('settings');
  await page.locator('#model_native_search').waitFor();
  assert.equal(await page.locator('#settings-form #model_name, #settings-form #model_protocol, #settings-form #reasoning_effort, #settings-form #model_key_env').count(),0);
  assert.equal(await page.locator('#model_native_search').inputValue(),'true');
  await page.locator('#runtime-version').waitFor();
  assert.equal(await page.locator('#search_provider, #search_key_env').count(),0);
  const waitCapability=async label=>page.waitForFunction(text=>document.body.textContent.includes('全局原生联网检测：'+text),label);
  await waitCapability('尚未验证');
  const inFlightFixture=await (await fetch(base+'/__fixture/start_model_snapshot',{method:'POST'})).json();
  assert.ok(inFlightFixture.jobs.length>0);
  const saveSettings=async()=>{
    const saved=page.waitForResponse(response=>response.url().endsWith('/api/settings') && response.request().method()==='PUT');
    await page.locator('#settings-form button[type="submit"]').click();
    assert.equal((await saved).status(),200);
    await page.locator('.loading-line').waitFor({state:'hidden'});
  };
  const openRoles=async()=>{
    await navigate('roles');
    await page.locator('.role-model-form[data-role-id="manager"][data-account-id=""]').waitFor();
    await page.locator('.loading-line').waitFor({state:'hidden'});
    assert.equal(await page.locator('.role-card').count(),6);
  };
  const saveManager=async({effort,model}={})=>{
    const form=page.locator('.role-model-form[data-role-id="manager"]');
    if(effort)await form.locator('[name="reasoning_effort"]').selectOption(effort);
    if(model)await form.locator('[name="model_id"]').selectOption(model);
    const saved=page.waitForResponse(response=>response.url().endsWith('/api/agent-roles/manager') && response.request().method()==='PUT');
    await form.locator('button[type="submit"]').click();
    const response=await saved;assert.equal(response.status(),200);
    await page.locator('.loading-line').waitFor({state:'hidden'});
    return response.json();
  };
  await openRoles();
  const manager=page.locator('.role-model-form[data-role-id="manager"]');
  assert.deepEqual(await manager.locator('[name="reasoning_effort"] option').evaluateAll(options=>options.map(option=>option.value)),['low','medium','high','xhigh','max']);
  assert.equal(await manager.locator('[name="model_id"]').inputValue(),'browser-native-observed');
  await manager.locator('[data-model-filter]').fill('does-not-exist');
  assert.deepEqual(await manager.locator('[name="model_id"] option').evaluateAll(options=>options.map(option=>option.value)),['']);
  await manager.locator('[data-model-filter]').fill('');
  await manager.locator('[name="model_id"]').selectOption('browser-native-observed');
  for (const effort of ['low','medium','high','xhigh','max']) {
    const role=await saveManager({effort});
    assert.equal(role.resolved.reasoning_effort,effort);
    assert.equal((await (await fetch(base+'/__fixture/reopen')).json()).reasoning_effort,effort);
  }
  await page.reload();
  await page.locator('.role-model-form[data-role-id="manager"]').waitFor();
  assert.equal(await manager.locator('[name="reasoning_effort"]').inputValue(),'max');
  await page.locator('#account-switch').selectOption(fixture.other_id);
  await page.locator('.loading-line').waitFor({state:'hidden'});
  await navigate('account-models');
  await page.locator(`.role-model-form[data-role-id="manager"][data-account-id="${fixture.other_id}"]`).waitFor();
  await page.locator('.role-card').last().waitFor();
  await page.locator('.loading-line').waitFor({state:'hidden'});
  assert.equal(await page.locator('.role-card').count(),6);
  assert.equal(await page.locator('.role-model-form [name="enabled"], .role-model-form [name="system_prompt"], .role-model-form [name="tools"]').count(),0);
  const accountManager=page.locator('.role-model-form[data-role-id="manager"]');
  await accountManager.locator('[name="reasoning_effort"]').selectOption('low');
  const overrideSaved=page.waitForResponse(response=>response.url().endsWith(`/api/accounts/${fixture.other_id}/agent-models/manager`) && response.request().method()==='PUT');
  await accountManager.locator('button[type="submit"]').click();
  assert.equal((await overrideSaved).status(),200);
  await page.locator('.loading-line').waitFor({state:'hidden'});
  const globalManager=(await (await fetch(base+'/api/agent-roles')).json()).find(row=>row.role_id==='manager');
  assert.equal(globalManager.resolved.reasoning_effort,'max');
  const reset=page.waitForResponse(response=>response.url().endsWith(`/api/accounts/${fixture.other_id}/agent-models/manager`) && response.request().method()==='DELETE');
  await accountManager.locator('[data-sixth-action="role-reset"]').click();
  assert.equal((await reset).status(),200);
  await page.locator('.loading-line').waitFor({state:'hidden'});
  assert.equal(await page.locator('.role-card').count(),6);
  await navigate('providers');
  await page.locator('#provider-connection-form').waitFor();
  await page.locator('.loading-line').waitFor({state:'hidden'});
  assert.deepEqual(await page.locator('#provider-connection-form [name]').evaluateAll(nodes=>nodes.map(node=>node.name).sort()),['api_key','base_url','protocol']);
  await page.locator('#provider-connection-form [name="api_key"]').fill('browser-form-only-fixture-secret');
  await page.locator('#provider-connection-form [name="base_url"]').fill('https://new.fixture.invalid/v1/responses');
  const created=page.waitForResponse(response=>response.url().endsWith('/api/providers') && response.request().method()==='POST');
  await page.locator('#provider-connection-form button[type="submit"]').click();
  const createdResponse=await created;
  const newProvider=await createdResponse.json();
  assert.equal(createdResponse.status(),201,JSON.stringify(newProvider));
  assert.equal(newProvider.catalog.status,'success');
  assert.equal(newProvider.base_url,'https://new.fixture.invalid/v1');
  await page.locator('.loading-line').waitFor({state:'hidden'});
  assert.equal(await page.locator('#provider-connection-form [name="api_key"]').inputValue(),'');
  assert.ok(!JSON.stringify(await (await fetch(base+'/api/providers')).json()).includes('browser-form-only-fixture-secret'));
  const changeProtocol=async protocol=>{
    await navigate('providers');
    await page.locator(`[data-sixth-action="provider-edit"][data-id="${fixture.provider_id}"]`).click();
    const form=page.locator('#dialog #provider-connection-form');
    await form.locator('[name="protocol"]').selectOption(protocol);
    assert.equal(await form.locator('[name="api_key"]').inputValue(),'');
    const saved=page.waitForResponse(response=>response.url().endsWith('/api/providers/'+fixture.provider_id) && response.request().method()==='PUT');
    await form.locator('button[type="submit"]').click();
    assert.equal((await saved).status(),200);
    await page.locator('#dialog').waitFor({state:'hidden'});
    await page.locator('.loading-line').waitFor({state:'hidden'});
    await navigate('settings');
    await page.locator('#model_native_search').waitFor();
    await page.locator('.loading-line').waitFor({state:'hidden'});
  };
  await navigate('settings');
  await page.locator('#model_native_search').waitFor();
  assert.ok((await page.locator('#inflight-model-settings').textContent()).includes('reasoning_effort：medium'));
  await mkdir('test-results',{recursive:true});
  await page.screenshot({path:'test-results/frontend-production-settings.png',fullPage:true});
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
  await saveSettings();
  await changeProtocol('chat_completions');
  await waitCapability('当前协议或端点不支持');
  await changeProtocol('responses');
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
  await openRoles();await saveManager({model:'browser-native-auth'});
  await navigate('settings');
  await page.locator('#model_native_search').waitFor();
  await waitCapability('尚未验证');
  assert.equal((await probe()).status,'authentication_failed');
  await waitCapability('认证失败');
  await openRoles();await saveManager({model:'browser-native-failure'});
  await navigate('settings');
  await page.locator('#model_native_search').waitFor();
  assert.equal((await probe()).status,'request_failed');
  await waitCapability('请求失败');
  let releaseProbe, observeProbe;
  const localProbeGate=new Promise(resolve=>{releaseProbe=resolve;});
  const localProbeStarted=new Promise(resolve=>{observeProbe=resolve;});
  await page.route('**/api/settings/probe',async route=>{
    observeProbe();await localProbeGate;
    await route.fulfill({contentType:'application/json',body:JSON.stringify({note:'受控本地工具续接结果'})});
  },{times:1});
  await page.locator('[data-action="probe"]').click();
  await waitGate(localProbeStarted,'localProbeStarted');
  await navigate('holdings');
  await page.locator('#account-form').waitFor();
  releaseProbe();
  await page.locator('#dialog').getByText('本地工具续接实测结果',{exact:true}).waitFor();
  assert.ok((await page.locator('#dialog').textContent()).includes('受控本地工具续接结果'));
  await page.locator('#dialog .dialog-header button').click();
  await assertIdle();
  const lifecycle=await page.evaluate(()=>window.__guanlanDiagnostics.snapshot());

  await mkdir('test-results',{recursive:true});
  await writeFile('test-results/frontend-production-lifecycle.json',JSON.stringify({browser:browser.version(),raceLifecycle,gates:requestTrace.filter(x=>x.event.startsWith('gate_')),operations:lifecycle.operations,requests:lifecycle.requests,counts:lifecycle.counts,events:lifecycle.events},null,2));
  assert.deepEqual(errors,[]);
  console.log('PASS (production FastAPI + temporary SQLite + Chromium; native probe protocol double): 106 recommendations, evidence scope and description-only excerpts, paused-navigation retry/analyze/save (3 release orders), cancelled/timeout reads, lost-write-response reconciliation without resend, empty operation/request sets, duplicate write coalescing, click-time account/version/input, newer and failed draft preservation, account isolation, provider three-field creation/automatic catalogs, shared six-role matrices, searched models, five reasoning_effort values/save/reopen, account overrides/reset, frozen in-flight snapshots, native setting migration, capability states and probe caching.');
} catch(error) {
  await mkdir('test-results',{recursive:true});
  if(page) await page.screenshot({path:'test-results/frontend-production-failure.png',fullPage:true});
  // No trace: even fixture keys must stay out of artifacts.
  await writeFile('test-results/frontend-production-requests.json', JSON.stringify({error:String(error),requests:requestTrace,lifecycle:page?await page.evaluate(()=>window.__guanlanDiagnostics?.snapshot()).catch(()=>null):null,server:diagnostic},null,2));
  throw error;
} finally {
  await browser?.close();
  if(child.exitCode===null) {const ended=new Promise(resolve=>child.once('exit',resolve));child.kill('SIGTERM');await ended;}
  await rm(directory,{recursive:true,force:true});
}
