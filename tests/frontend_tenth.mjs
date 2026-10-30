// Production FastAPI + temporary SQLite; model results/directory are declared doubles.
import {spawn} from 'node:child_process';
import {createRequire} from 'node:module';
import {mkdtemp,rm,mkdir,writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import assert from 'node:assert/strict';
const {chromium}=createRequire(import.meta.url)('playwright');
const directory=await mkdtemp(join(tmpdir(),'guanlan-tenth-browser-'));
const child=spawn(process.env.PYTHON || 'python',['tests/tenth_browser_server.py',directory],{env:{...process.env,PYTHON_DOTENV_DISABLED:'1'},stdio:['ignore','pipe','pipe']});
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
let browser,page,diagnostic='';child.stderr.on('data',data=>diagnostic+=data.toString());
const checks=[];
try {
 const fixture=await new Promise((resolve,reject)=>{let output='';const timer=setTimeout(()=>reject(Error('temporary server timeout: '+diagnostic)),30000);child.stdout.on('data',data=>{output+=data;if(output.includes('\n')){clearTimeout(timer);resolve(JSON.parse(output.split('\n')[0]));}});child.once('exit',code=>{clearTimeout(timer);reject(Error(`server exited ${code}: ${diagnostic}`));});});
 const base=`http://127.0.0.1:${fixture.port}`;
 for(let i=0;;i++){try{if((await fetch(base+'/api/status')).ok)break;}catch{}if(i>100)throw Error('API unavailable');await sleep(50);}
 const api=async(path,body)=>{const response=await fetch(base+path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});assert.ok(response.ok,`${path}: ${response.status}`);return response.json();};
 browser=await chromium.launch({headless:true,...(process.env.BROWSER_EXECUTABLE?{executablePath:process.env.BROWSER_EXECUTABLE}:{})});
 page=await browser.newPage({viewport:{width:1280,height:800}});
 await mkdir('test-results',{recursive:true});
 const errors=[];page.on('pageerror',error=>errors.push(error.message));
 await page.addInitScript(id=>localStorage.setItem('guanlan.account',id),fixture.account_id);
 const idle=()=>page.locator('.loading-line').waitFor({state:'hidden'});
 const navigate=async route=>{await page.locator(`nav a[href="#${route}"]`).click();await page.locator(`nav a[href="#${route}"][aria-current=page]`).waitFor();await idle();};
 await page.goto(base+'/#research');await page.locator(`[data-report-id="${fixture.report_id}"]`).waitFor();
 assert.ok((await page.locator('#main').textContent()).includes('尚未分类'));
 assert.ok((await page.locator(`#material-${fixture.rumor_id}`).textContent()).includes('传闻 / 待核实'));
 assert.ok(await page.locator('button').filter({hasText:'查看检索元数据'}).count()>0);
 const testedRuntime=(await api('/api/status')).runtime_version;
 assert.equal(testedRuntime.api_contract,11);
 assert.equal(await page.getByText('页面与后端接口版本尚未确认一致',{exact:false}).count(),0,'current source and backend API contract must agree');
 const refs=page.locator(`[data-report-id="${fixture.report_id}"] .evidence-references`);
 assert.equal(await refs.getAttribute('open'),null);await refs.screenshot({path:'test-results/tenth-report-collapsed.png'});
 assert.ok((await refs.locator('summary').textContent()).includes('100条'));
 await refs.locator('summary').click();assert.equal(await refs.locator('.evidence-reference').count(),100);
 await refs.scrollIntoViewIfNeeded();const scroll=await page.evaluate(()=>scrollY);
 await api('/__fixture/touch',{});
 await sleep(4200);assert.ok(await refs.evaluate(el=>el.open));assert.ok(Math.abs(await page.evaluate(()=>scrollY)-scroll)<5,JSON.stringify({before:scroll,after:await page.evaluate(()=>scrollY)}));
 checks.push('100 references default collapsed; expansion and scroll survive polling');
 await navigate('overview');
 await page.locator('.task-status-card [data-action=open-report]').click();await page.locator('#dialog[open]').waitFor();
 for(const [width,height,zoom] of [[1280,800,1],[1024,680,1],[390,740,1],[1280,800,1.25],[1280,800,2]]) {
   await page.setViewportSize({width,height});await page.evaluate(zoom=>document.documentElement.style.zoom=String(zoom),zoom);
   for(const fraction of [0,.5,1]) {
     await page.locator('.dialog-content').evaluate((el,fraction)=>{el.scrollTop=(el.scrollHeight-el.clientHeight)*fraction;},fraction);
     const geometry=await page.locator('#dialog .dialog-header [data-action=close-dialog]').evaluate(el=>{const r=el.getBoundingClientRect();return{x:r.x,y:r.y,bottom:r.bottom,right:r.right,visible:document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)===el || el.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)),width:innerWidth,height:innerHeight};});
     assert.ok(geometry.y>=0 && geometry.bottom<=geometry.height && geometry.x>=0 && geometry.right<=geometry.width && geometry.visible,JSON.stringify({width,height,zoom,fraction,geometry}));
   }
 }
 await page.screenshot({path:'test-results/tenth-dialog-200.png',fullPage:false});
 await page.locator('#dialog .dialog-header [data-action=close-dialog]').click();assert.ok(!await page.locator('#dialog').evaluate(el=>el.open));await page.locator('.task-status-card [data-action=open-report]').click();await page.locator('#dialog[open]').waitFor();for(let n=0;n<12;n++){await page.keyboard.press(n%2?'Tab':'Shift+Tab');assert.ok(await page.evaluate(()=>document.querySelector('#dialog').contains(document.activeElement)),'modal keyboard focus stays contained');}await page.keyboard.press('Escape');assert.ok(!await page.locator('#dialog').evaluate(el=>el.open));
 assert.equal(await page.evaluate(()=>document.activeElement?.dataset.action),'open-report');
 await page.evaluate(()=>document.documentElement.style.zoom='1');await page.setViewportSize({width:1280,height:800});
 checks.push('modal close visible/clickable at top/middle/bottom desktop/small/125%/200%; Escape and focus restore');
 // Full task card is now overview-only; task detail reads preserve its page.
 const initialTask=(await api(`/api/accounts/${fixture.account_id}/jobs`))[0];
 for(const routeName of ['overview']) {
   await navigate(routeName);
   await page.route(`**/api/jobs/${initialTask.id}`,async route=>{await sleep(100);return route.continue();},{times:1});
   await page.locator('.task-status-card [data-action=view-task]').click();
   await page.locator('#dialog[open]').waitFor({timeout:2500});
   assert.ok((await page.locator('#dialog').textContent()).includes(initialTask.id));
   assert.ok(page.url().endsWith('#'+routeName),'opening task details keeps the originating page');
   await page.locator('#dialog .dialog-header [data-action=close-dialog]').click();
   assert.ok(!await page.locator('#dialog').evaluate(el=>el.open));
   assert.equal(await page.evaluate(()=>document.activeElement?.dataset.action),'view-task');
 }
 for(const routeName of ['market','holdings','research','agents','providers','roles','account-models','forecasts','settings']) {await navigate(routeName);assert.equal(await page.locator('.task-status-card').count(),0,routeName);}
 checks.push('overview task dialog preserves page and survives delayed GET; all other navigation hides full card');

 await navigate('holdings');await page.locator('#snapshot-correction > summary').click();
 await page.locator('.holding-row .field:first-child small').evaluate(el=>el.textContent='会换行的证券识别帮助与校验说明。'.repeat(14));
 const positions=await page.locator('.holding-row input').evaluateAll(els=>els.map(el=>{const r=el.getBoundingClientRect();return{top:r.top,height:r.height};}));
 assert.ok(positions.every(p=>Math.abs(p.top-positions[0].top)<1 && Math.abs(p.height-positions[0].height)<1),JSON.stringify(positions));
 const removeTop=await page.locator('.holding-row [data-action=remove-holding]').evaluate(el=>el.getBoundingClientRect().top);assert.ok(Math.abs(removeTop-positions[0].top)<1);await page.locator('.holding-row').screenshot({path:'test-results/tenth-holdings-alignment.png'});
 await page.setViewportSize({width:390,height:740});assert.ok(await page.locator('.holding-row input').evaluateAll(els=>els.every(el=>el.getBoundingClientRect().width>70)));await page.setViewportSize({width:1280,height:800});
 checks.push('holding label/help wrap input and remove-button alignment; narrow layout');
 await navigate('market');await page.locator('#watchlist').fill('518880\n688825\n562500');await page.locator('#candidates').fill('159278,000001；SZ:159278');
 await page.locator('#universe-form [type=submit]').click();await idle();
 const universe=await api('/api/universe');assert.deepEqual(universe.watchlist,['SH:518880','SH:688825','SH:562500']);assert.deepEqual(universe.candidates,['SZ:159278','SZ:000001']);assert.ok(universe.monitored.length>=5);
 assert.ok((await page.locator('#main').textContent()).includes('5 只证券'));
 await page.locator('#watchlist').fill('518880\nabc');await page.locator('#universe-form [type=submit]').click();await idle();assert.equal(await page.locator('#watchlist').inputValue(),'518880\nabc');assert.deepEqual((await api('/api/universe')).watchlist,universe.watchlist);
 await page.locator('#account-switch').selectOption(fixture.other_id);await idle();await page.locator('#account-switch').selectOption(fixture.account_id);await idle();assert.equal(await page.locator('#watchlist').inputValue(),'518880\nabc');
 checks.push('bare-code atomic save, prefix/leading-zero/dedup, monitored refresh, invalid draft and account switch');
 await navigate('overview');
 let writes=0, lose=false, gate=null;
 await page.route('**/api/accounts/*/analyze',async route=>{if(route.request().method()!=='POST')return route.continue();writes++;const response=await route.fetch();if(gate){const current=gate;gate=null;current.started();await current.wait;}if(lose){lose=false;return route.abort('connectionreset');}return route.fulfill({response});});
 let holdAccountRead=true,releaseAccountRead,observeAccountRead;const accountReadGate=new Promise(resolve=>releaseAccountRead=resolve),accountReadStarted=new Promise(resolve=>observeAccountRead=resolve);await page.route(`**/api/accounts/${fixture.account_id}`,async route=>{if(route.request().method()==='GET' && holdAccountRead){holdAccountRead=false;observeAccountRead();await accountReadGate;}return route.continue();});
 let release,observe;gate={wait:new Promise(resolve=>release=resolve),started:()=>observe()};const started=new Promise(resolve=>observe=resolve);
 await page.locator('.page-heading [data-action=analyze]').dblclick();await started;assert.ok((await page.locator('.task-status-card').textContent()).includes('正在提交'));release();await accountReadStarted;assert.ok((await page.locator('.task-status-card').textContent()).includes('排队中'),'accepted task must display before slow follow-up reads finish');releaseAccountRead();await idle();assert.equal(writes,1);
 let jobs=await api(`/api/accounts/${fixture.account_id}/jobs`), job=jobs[0];assert.equal(job.state,'queued');assert.ok((await page.locator('.task-status-card').textContent()).includes(job.id));assert.ok(!(await page.locator('.task-status-card').textContent()).includes('已开始计算'));
 await page.locator('.page-heading [data-action=analyze]').click();await idle();assert.equal((await api(`/api/accounts/${fixture.account_id}/jobs`))[0].id,job.id);assert.ok((await page.locator('#toast-region').textContent()).includes('合并到已有任务'));
 await api('/__fixture/claim/'+fixture.account_id,{});await navigate('agents');await navigate('overview');assert.ok((await page.locator('.task-status-card').textContent()).includes('研究中'));
 await api('/__fixture/complete/'+job.id,{});await page.reload();await page.locator('.task-status-card [data-action=open-report]').waitFor();
 lose=true;await navigate('overview');await page.locator('.page-heading [data-action=analyze]').dblclick();await idle();assert.equal(writes,3);jobs=await api(`/api/accounts/${fixture.account_id}/jobs`);job=jobs[0];assert.ok((await page.locator('.task-status-card').textContent()).includes(job.id));assert.equal(await page.locator('[data-action=reconcile-task]').count(),0);
 // A second lost response also loses its first receipt read; reload must recover by ID.
 let blockReceipt=true;
 await page.route('**/api/task-operations/*',route=>{if(blockReceipt){blockReceipt=false;return route.abort('connectionreset');}return route.continue();});
 lose=true;await page.locator('.page-heading [data-action=analyze]').click();await idle();assert.equal(writes,4);
 await page.locator('[data-action=reconcile-task]').waitFor();
 await page.reload();await page.locator('.task-status-card').waitFor();await page.locator('[data-action=reconcile-task]').waitFor({state:'hidden'});assert.ok((await page.locator('.task-status-card').textContent()).includes(job.id));
 await page.locator('#account-switch').selectOption(fixture.other_id);await idle();assert.ok(!(await page.locator('.task-status-card').textContent()).includes(job.id));assert.ok(!(await page.locator('#toast-region').textContent()).includes(job.id),'previous account task toast must be removed');await page.locator('#account-switch').selectOption(fixture.account_id);await idle();
 await api('/__fixture/waiting-config/'+job.id,{});await navigate('agents');await navigate('overview');assert.ok((await page.locator('.task-status-card').textContent()).includes('等待配置'));
 await api('/__fixture/fail/'+job.id,{});await page.reload();await page.locator('.task-status-card [data-action=retry-job]').waitFor();await page.locator('.task-status-card [data-action=retry-job]').click();await idle();
 const retried=(await api(`/api/accounts/${fixture.account_id}/jobs`))[0];assert.equal(retried.parent_job_id,job.id);assert.notEqual(retried.id,job.id);
 checks.push('submit/queued/running/waiting_config/merged/lost-response/reload/account-isolation/failed-retry identity');
 await api('/__fixture/claim/'+fixture.account_id,{});await api('/__fixture/complete/'+retried.id,{});await navigate('holdings');if(!await page.locator('#snapshot-correction').evaluate(el=>el.open))await page.locator('#snapshot-correction > summary').click();await page.locator('#correction-reason').fill('明确受控期初快照校正，不是交易');const beforeSave=await api('/api/accounts/'+fixture.account_id);await page.locator('.holding-row input[name^=quantity]').fill('20000');await page.locator('.holding-row input[name^=symbol]').fill('159278');let accountWrites=0;await page.route(`**/api/accounts/${fixture.account_id}/corrections`,async route=>{if(route.request().method()!=='POST')return route.continue();accountWrites++;await route.fetch();return route.abort('connectionreset');});await page.locator('#account-form [type=submit]').dblclick();await idle();assert.equal(accountWrites,1);
 const saved=await api('/api/accounts/'+fixture.account_id);assert.equal(saved.cash,'10000.00');assert.equal(saved.holdings[0].quantity,'20000');assert.equal(saved.version,beforeSave.version+1);await api('/__fixture/claim/'+fixture.account_id,{});await api('/__fixture/complete/'+saved.latest_job_id,{});await navigate('overview');await page.locator('.task-status-card [data-action=open-report]').click();await page.locator('#dialog[open]').waitFor();await page.locator('#dialog').getByText(`输入版本 v${saved.version}`,{exact:true}).waitFor();assert.ok((await page.locator('#dialog .research-delivery').textContent()).includes('本次新调度专题完成 0 / 0'));assert.ok((await page.locator('#dialog .research-delivery').textContent()).includes('复用成果仍有 1 项原专题缺口'));await page.locator('#dialog .recovered-research-gaps > summary').click();await page.locator('#dialog').getByText('受控原行情专题缺口 · 失败：受控原专题超时',{exact:true}).waitFor();assert.ok((await page.locator('#dialog').textContent()).includes(`输入版本 v${saved.version}`));await page.keyboard.press('Escape');
 checks.push('explicit snapshot correction preserves confirmed cash, has required reason, publishes new version and opens exact report');
 await navigate('agents');const completed=page.locator('.job-card').filter({has:page.locator('[data-action=open-report]')}).last();await completed.waitFor();assert.equal(await completed.locator('.job-checkpoint-details[open]').count(),0);assert.ok(await completed.locator('[data-action=open-report]').isVisible());
 const latest=await api('/__fixture/history/'+fixture.account_id,{});await page.reload();await page.locator('[data-pagination=jobs] button').last().click();await idle();
 assert.equal(await page.locator('.task-status-card').count(),0);await navigate('overview');assert.ok((await page.locator('.task-status-card').textContent()).includes(latest.id),'returning from history pagination restores the latest overview task');
 await page.locator('.task-status-card [data-action=view-task]').click();await page.locator('#dialog[open]').waitFor();await page.locator('#dialog .job-event-details > summary').click();await page.locator('#dialog').getByText('弹窗可读取事件 11',{exact:true}).waitFor();await page.keyboard.press('Escape');
 const merged=await api('/__fixture/merged-demands/'+fixture.account_id,{});await page.reload();await page.locator('.task-status-card').waitFor();await page.locator('.task-status-card [data-action=view-task]').click();await page.locator('#dialog[open]').waitFor();
 assert.ok((await page.locator('#dialog').textContent()).includes('本次明确目标：SH:518880'));
 await page.locator('#dialog .task-demands > summary').click();const demandText=await page.locator('#dialog .task-demands').textContent();assert.ok(demandText.includes('收盘总结') && demandText.includes('正式预测批次') && demandText.includes('2026-10-30'));await page.screenshot({path:'test-results/tenth-task-demands.png',fullPage:false});await page.keyboard.press('Escape');
 const retryMerge=await api('/__fixture/merged-retry/'+fixture.account_id,{});await page.reload();await page.locator('.task-status-card [data-action=view-task]').click();await page.locator('#dialog[open]').waitFor();await page.locator('#dialog details[data-key^=sources] > summary').click();await page.locator(`#dialog details[data-key^=sources] [data-action=view-task][data-id="${retryMerge.failed_id}"]`).waitFor();assert.ok((await page.locator('#dialog').textContent()).includes(retryMerge.job.id));await page.keyboard.press('Escape');
 checks.push('latest task survives history pagination; modal events load; merged demand types/trading day/explicit target and original failed retry chain remain distinct');
 await page.screenshot({path:join(directory,'completed-task.png'),fullPage:false});
 // An empty target never silently launches security research; watchlist is not a holding/candidate.
 await navigate('market');await page.locator('#watchlist').fill('518880');await page.locator('#candidates').fill('');await page.locator('#universe-form [type=submit]').click();await idle();
 await navigate('holdings');await page.locator('.page-heading [data-action=new-account]').click();await page.locator('#new-account-name').fill('受控现金范围确认');await page.locator('#new-account-cash').fill('30000');await page.locator('#new-account-form [type=submit]').click();await idle();
 await navigate('overview');await page.locator('.task-status-card').waitFor();assert.ok((await page.locator('.task-status-card').textContent()).includes('等待确认范围'));
 await page.locator('.task-status-card [data-action=analyze]').click();await page.locator('#dialog [data-action=analyze-cash]').waitFor();assert.ok((await page.locator('#dialog').textContent()).includes('不包含证券方向研究'));
 await page.locator('#dialog [data-action=analyze-cash]').click();await idle();
 const cashAccount=(await api('/api/accounts')).find(a=>a.name==='受控现金范围确认');const cashJob=(await api(`/api/accounts/${cashAccount.id}/jobs`))[0];assert.equal(cashJob.research_scope,'cash_only');assert.equal(cashJob.state,'queued');assert.deepEqual(cashJob.target.symbols,[]);
 checks.push('empty cash account requires explicit cash-only scope; watchlist does not become holding/candidate');
 assert.deepEqual(errors,[]);
 await mkdir('test-results',{recursive:true});await writeFile('test-results/tenth-browser.json',JSON.stringify({passed:true,kind:'production_FastAPI_temporary_SQLite_explicit_directory_and_model_result_doubles',checks,console_errors:errors,runtime_version:testedRuntime},null,2));
 await page.waitForFunction(()=>document.querySelectorAll('#toast-region .toast').length===0);await page.screenshot({path:'test-results/tenth-task-trace.png',fullPage:false});
 console.log('Tenth production browser regressions passed');
} catch(error) { console.error(diagnostic); throw error; }
finally {if(browser)await browser.close();child.kill('SIGTERM');await new Promise(resolve=>{if(child.exitCode!==null)return resolve();child.once('exit',resolve);setTimeout(resolve,3000);});await rm(directory,{recursive:true,force:true});}
