// Real local FastAPI/SQLite. No traces, screenshots, payload logs, or secret artifacts.
import {spawn} from 'node:child_process';
import {createRequire} from 'node:module';
import {mkdtemp, rm, mkdir, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import assert from 'node:assert/strict';
const {chromium}=createRequire(import.meta.url)('playwright');
const directory=await mkdtemp(join(tmpdir(),'guanlan-ninth-browser-'));
const child=spawn(process.env.PYTHON || 'python',['tests/ninth_browser_server.py',directory],{env:{...process.env,PYTHON_DOTENV_DISABLED:'1'},stdio:['ignore','pipe','pipe']});
let browser, page, diagnostic='';
child.stderr.on('data',data=>diagnostic+=data.toString());
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
try {
 const fixture=await new Promise((resolve,reject)=>{let output='';const timer=setTimeout(()=>reject(Error('temporary server timeout')),30000);child.stdout.on('data',data=>{output+=data.toString();if(output.includes('\n')){clearTimeout(timer);resolve(JSON.parse(output.split('\n')[0]));}});child.once('exit',code=>{clearTimeout(timer);reject(Error(`server exited ${code}`));});});
 const base=`http://127.0.0.1:${fixture.port}`;
 for(let i=0;;i++){try {if((await fetch(base+'/api/status')).ok)break;}catch{}if(i>100)throw Error('API did not become ready');await sleep(50);}
 browser=await chromium.launch({headless:true,...(process.env.BROWSER_EXECUTABLE?{executablePath:process.env.BROWSER_EXECUTABLE}:{})});
 page=await browser.newPage();
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 let providerReadMode='empty', providerReads=0, writes=0, loseWrite=false, serverErrorWrite=false, delayedWrite=null;
 await page.route('**/api/**',async route=>{
   const request=route.request(), path=new URL(request.url()).pathname;
   if(path==='/api/providers' && request.method()==='GET'){
     providerReads++;
     if(providerReadMode==='empty')return route.fulfill({status:200,contentType:'application/json',body:'[]'});
     if(providerReadMode==='404')return route.fulfill({status:404,contentType:'application/json',body:'{"detail":"Not Found"}'});
     if(providerReadMode==='500')return route.fulfill({status:500,contentType:'application/json',body:'{"detail":"controlled local error"}'});
     if(providerReadMode==='disconnect')return route.abort('connectionfailed');
     if(providerReadMode==='delay')await sleep(1500);
   }
   if(path==='/api/providers' && request.method()==='POST'){
     writes++;
     if(delayedWrite){const gate=delayedWrite;delayedWrite=null;const response=await route.fetch();gate.started();await gate.wait;return route.fulfill({response});}
     if(serverErrorWrite){serverErrorWrite=false;await route.fetch();return route.fulfill({status:500,contentType:'application/json',body:'{"detail":"controlled failure after commit"}'});}
     if(loseWrite){loseWrite=false;await route.fetch();return route.abort('connectionreset');}
   }
   return route.continue();
 });
 await page.addInitScript(id=>localStorage.setItem('guanlan.account',id),fixture.account_id);
 await page.goto(base+'/#providers');
 const form=page.locator('#app #provider-connection-form');
 await form.waitFor();
 const key=form.locator('[name=api_key]'), url=form.locator('[name=base_url]');
 // Slow input spans a background poll and a deterministic missing route.
 const secret='ninth-browser-transient-secret';
 await key.fill(secret);
 await url.focus();
 providerReadMode='404';
 await url.pressSequentially('https://authfail.example.test/v1',{delay:160});
 await page.getByText('GET /api/providers · HTTP 404：',{exact:false}).first().waitFor();
 assert.ok(await key.inputValue()===secret,'secret input must survive a local route failure');
 assert.ok(await url.inputValue()==='https://authfail.example.test/v1','slow URL input must survive polls');
 assert.ok(await url.evaluate(el=>document.activeElement===el),'focus stays in the same live input node');
 assert.ok(await form.locator('[type=submit]').isDisabled(),'missing write API explained and save disabled');
 assert.equal(await page.getByText('本地服务连接中断。',{exact:false}).count(),0);
 const once=providerReads;await sleep(4300);assert.equal(providerReads,once,'deterministic missing API is not polled repeatedly');
 // Selection edits and paste-equivalent replacement retain cursor during recovery.
 await url.evaluate(el=>el.setSelectionRange(8,16));
 const selection=await url.evaluate(el=>[el.selectionStart,el.selectionEnd]);
 providerReadMode='normal';
 await page.locator('[data-sixth-action=resource-retry]').first().click();
 await page.waitForFunction(()=>!document.querySelector('#app #provider-connection-form [type=submit]')?.disabled);
 assert.ok(await key.inputValue()===secret,'reconnect preserves transient input');
 assert.deepEqual(await url.evaluate(el=>[el.selectionStart,el.selectionEnd]),selection,'selection survives DOM-preserving recovery');
 await key.focus();await key.press('ControlOrMeta+A');await page.keyboard.insertText(secret);
 assert.ok(await key.inputValue()===secret,'paste-style insertText replaces selected password without storage');
 await url.fill('https://authfail.example.test/v1');
 // 500 and disconnected resource errors do not turn healthy status into offline.
 for(const mode of ['500','disconnect','delay']){
   providerReadMode=mode;
   await page.locator('[data-sixth-action=resource-retry]').first().click();
   await url.focus();await url.press('End');await url.press('ArrowLeft');
   await page.locator('.loading-line').waitFor({state:'hidden'});
   assert.ok(await key.inputValue()===secret,'local failures must preserve password input');
   assert.ok(await url.evaluate(el=>document.activeElement===el),'delayed reads preserve focus');
   assert.equal(await page.getByText('本地服务连接中断。',{exact:false}).count(),0);
 }
 providerReadMode='normal';await page.locator('[data-sixth-action=resource-retry]').first().click();await page.locator('.loading-line').waitFor({state:'hidden'});
 await form.locator('[type=submit]').click();
 await page.getByText('连接保存成功，模型目录失败：',{exact:false}).waitFor();
 assert.ok(await key.inputValue()==='','successful save clears temporary credential');
 const saved=(await (await fetch(base+'/api/providers')).json()).find(p=>p.base_url.includes('authfail'));
 assert.ok(saved && saved.catalog.status==='authentication_failed');
 assert.equal(writes,1);
 assert.ok(!JSON.stringify(saved).includes(secret),'GET cannot return the submitted credential');
 await page.locator(`[data-sixth-action=provider-edit][data-id="${saved.id}"]`).click();
 const edit=page.locator('#dialog #provider-connection-form');
 await edit.getByText('API Key',{exact:true}).click();
 assert.ok(await edit.locator('[name=api_key]').evaluate(el=>document.activeElement===el),'edit label focuses the modal key field, not the background create form');
 await edit.locator('[name=base_url]').fill('https://catalog.example.test/v1');
 await edit.locator('[name=protocol]').selectOption('chat_completions');
 await edit.locator('[type=submit]').click();await page.locator('#dialog').waitFor({state:'hidden'});
 await page.locator('nav a[href="#roles"]').click();
 const manager=page.locator('.role-model-form[data-role-id=manager]');await manager.waitFor();
 await manager.locator('[name=provider_id]').selectOption(saved.id);
 await manager.locator('[name=model_id]').selectOption('catalog/observed-B');
 await manager.locator('[name=reasoning_effort]').selectOption('max');
 await manager.locator('[type=submit]').click();await page.locator('.loading-line').waitFor({state:'hidden'});
 const childRole=page.locator('.role-model-form[data-role-id=market_researcher]');
 await childRole.locator('[name=provider_id]').selectOption(saved.id);
 await childRole.locator('[name=model_id]').selectOption('catalog/observed-A');
 await childRole.locator('[type=submit]').click();await page.locator('.loading-line').waitFor({state:'hidden'});
 const reopened=await (await fetch(base+'/__fixture/reopen')).json();
 assert.equal(reopened.roles.find(r=>r.role_id==='manager').resolved.model_id,'catalog/observed-B');
 assert.equal(reopened.roles.find(r=>r.role_id==='manager').resolved.reasoning_effort,'max');
 assert.equal(reopened.roles.find(r=>r.role_id==='market_researcher').resolved.model_id,'catalog/observed-A');
 assert.equal(reopened.readiness.native_search,'protocol_unsupported');
 // A committed write whose response disappears is reconciled, never resent.
 await page.locator('nav a[href="#providers"]').click();await form.waitFor();
 await key.fill(secret);await url.fill('https://lost-response.example.test/v1');loseWrite=true;
 await form.locator('[type=submit]').dblclick();
 await page.locator('.loading-line').waitFor({state:'hidden'});
 assert.equal(writes,2,'double-click and lost response produce one additional POST only');
 assert.ok(await key.inputValue()==='','reconciled committed operation clears transient secret');
 const all=await (await fetch(base+'/api/providers')).json();
 assert.equal(all.filter(p=>p.base_url.includes('lost-response')).length,1);
 // An HTTP 500 after commit is also an unknown write outcome, not a safe retry.
 await key.fill(secret);await url.fill('https://committed-before-500.example.test/v1');serverErrorWrite=true;
 await form.locator('[type=submit]').click();await page.locator('.loading-line').waitFor({state:'hidden'});
 assert.equal(writes,3,'committed 500 outcome is reconciled without a second POST');
 assert.ok(await key.inputValue()==='','reconciled 500 clears only the submitted secret');
 const after500=await (await fetch(base+'/api/providers')).json();
 assert.equal(after500.filter(p=>p.base_url.includes('committed-before-500')).length,1);
 // Late success must not erase input typed after the submitted request.
 let releaseLate, observeLate;
 delayedWrite={wait:new Promise(resolve=>{releaseLate=resolve;}),started:()=>observeLate()};
 const lateStarted=new Promise(resolve=>{observeLate=resolve;});
 await key.fill(secret);await url.fill('https://submitted-before-edit.example.test/v1');
 await form.locator('[type=submit]').click();await lateStarted;
 await key.fill(secret+'-new-input');await url.fill('https://new-unsaved.example.test/v1');releaseLate();
 await page.locator('.loading-line').waitFor({state:'hidden'});
 assert.ok(await key.inputValue()===secret+'-new-input','late success preserves a newer password input session');
 assert.ok(await url.inputValue()==='https://new-unsaved.example.test/v1','late success preserves new unsaved connection input');
 await form.locator('[data-sixth-action=provider-cancel]').click();
 assert.ok(await key.inputValue()==='','explicit cancel clears the new secret');
 const storage=await page.evaluate(()=>JSON.stringify({local:{...localStorage},session:{...sessionStorage}}));
 assert.ok(!storage.includes(secret),'no credential in browser storage');
 assert.deepEqual(errors,[]);
 await mkdir('test-results',{recursive:true});
 await writeFile('test-results/ninth-browser.json',JSON.stringify({passed:true,kind:'production_FastAPI_temporary_SQLite_explicit_vault_catalog_doubles',scenarios:['empty-provider-list','slow-input-poll','404-circuit-breaker','500-local-error','resource-disconnect','delayed-recovery','focus-selection','save-with-catalog-401','edit-three-fields','actual-directory-bindings-reopen','lost-write-reconcile','duplicate-submit','committed-500-reconcile','late-success-preserves-new-input','no-secret-in-GET-or-storage'],writes,console_errors:errors},null,2));
 console.log('Ninth provider browser regressions passed');
} finally {
 if(browser)await browser.close();child.kill('SIGTERM');
 await new Promise(resolve=>{if(child.exitCode!==null)return resolve();child.once('exit',resolve);setTimeout(resolve,3000);});
 await rm(directory,{recursive:true,force:true});
}
