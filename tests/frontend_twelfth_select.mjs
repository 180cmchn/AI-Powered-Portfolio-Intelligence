// Real FastAPI/static modules and temporary SQLite; directory, time and saved
// report are explicit fixtures. No model worker or external network is used.
import {spawn} from 'node:child_process';
import {mkdtemp,rm,mkdir,writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {openChoice,selectChoice} from './choice_helper.mjs';
const directory=await mkdtemp(join(tmpdir(),'guanlan-twelfth-select-'));
const child=spawn(process.env.PYTHON||'.venv/bin/python',['tests/eleventh_browser_server.py',directory],{env:{...process.env,PYTHON_DOTENV_DISABLED:'1'},stdio:['ignore','pipe','pipe']});
let browser,page,log='';child.stderr.on('data',data=>log+=data.toString());
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms)), evidence={checks:[],geometry:[],platform:process.platform,executable:process.env.BROWSER_EXECUTABLE||'Playwright Chromium',realModelCalls:0};
try {
 const fixture=await new Promise((resolve,reject)=>{let output='';const timer=setTimeout(()=>reject(Error('server timeout '+log)),30000);child.stdout.on('data',data=>{output+=data;if(output.includes('\n')){clearTimeout(timer);resolve(JSON.parse(output.split('\n')[0]));}});child.once('exit',code=>{clearTimeout(timer);reject(Error('server exit '+code+' '+log));});});
 const base=`http://127.0.0.1:${fixture.port}`;
 for(let n=0;;n++){try{if((await fetch(base+'/api/status')).ok)break;}catch{}if(n>100)throw Error('server unavailable '+log);await sleep(50);}
 const api=async(path,body,method='POST')=>{const r=await fetch(base+path,body===undefined?{}:{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const text=await r.text();assert.ok(r.ok,`${method} ${path}: ${r.status} ${text}`);return JSON.parse(text);};
 browser=await chromium.launch({headless:true,...(process.env.BROWSER_EXECUTABLE?{executablePath:process.env.BROWSER_EXECUTABLE}:{})});
 evidence.browserVersion=browser.version();
 page=await browser.newPage({viewport:{width:1360,height:900}});
 const errors=[],writes=[];page.on('pageerror',error=>errors.push(error.message));page.on('request',request=>{if(request.method()==='POST'&&/\/trades$/.test(new URL(request.url()).pathname))writes.push(request.postDataJSON());});
 await page.addInitScript(id=>localStorage.setItem('guanlan.account',id),fixture.account_id);
 await page.goto(base+'/#holdings');await page.locator('#trade-side-control').waitFor();await page.locator('.loading-line').waitFor({state:'hidden'});
 await mkdir('test-results/twelfth',{recursive:true});
 const trigger=page.locator('#trade-side-control'), native=page.locator('#trade-side');
 const formValues=()=>page.locator('#trade-form').evaluate(form=>({sides:new FormData(form).getAll('side'),visible:form.querySelector('[data-choice-value]').textContent,submittable:!form.querySelector('[type=submit]').disabled}));
 assert.deepEqual(await formValues(),{sides:['buy'],visible:'买入',submittable:false});
 assert.equal(await native.getAttribute('tabindex'),'-1');assert.equal(await native.isVisible(),false);
 await trigger.focus();await page.keyboard.press('Enter');assert.equal(await trigger.getAttribute('aria-expanded'),'true');
 await page.keyboard.press('ArrowDown');assert.equal(await page.locator('.choice-option.is-active').getAttribute('data-value'),'sell');
 assert.deepEqual((await formValues()).sides,['buy']);await page.keyboard.press('Escape');assert.equal(await trigger.getAttribute('aria-expanded'),'false');assert.deepEqual((await formValues()).sides,['buy']);assert.equal(await trigger.evaluate(el=>document.activeElement===el),true);
 await page.keyboard.press('Space');await page.keyboard.press('End');await page.keyboard.press('Enter');assert.deepEqual(await formValues(),{sides:['sell'],visible:'卖出',submittable:false});assert.equal(writes.length,0);
 await trigger.click();await page.keyboard.press('Home');await page.keyboard.press('Tab');assert.deepEqual((await formValues()).sides,['sell']);assert.equal(await trigger.getAttribute('aria-expanded'),'false');assert.equal(await page.locator('#trade-symbol').evaluate(el=>document.activeElement===el),true);
 await trigger.click();await page.locator('#trade-price').click();assert.equal(await trigger.getAttribute('aria-expanded'),'false');assert.deepEqual((await formValues()).sides,['sell']);
 evidence.checks.push('single successful buy/sell field; click, Enter/Space, arrows, Home/End, Escape rollback, Tab/outside-close, focus return, no implicit trade submission');
 await page.locator('#trade-form').evaluate(form=>form.reset());await sleep(50);assert.deepEqual(await formValues(),{sides:['buy'],visible:'买入',submittable:false});
 await native.evaluate(el=>el.disabled=true);await sleep(50);assert.equal(await trigger.isDisabled(),true);await native.evaluate(el=>el.disabled=false);await sleep(50);assert.equal(await trigger.isDisabled(),false);
 evidence.checks.push('form reset restores initial buy and invalidates preview; disabled native field disables and excludes custom trigger');

 // Preserve both the open popup and candidate while a genuine API update is
 // incorporated by normal four-second production polling.
 await trigger.click();await page.keyboard.press('End');await trigger.evaluate(el=>{window.__choiceTrigger=el;window.__choicePopup=document.querySelector('.choice-popup');});
 const beforeJob=(await api(`/api/accounts/${fixture.account_id}/jobs`))[0];
 await api('/__fixture/touch',{});await sleep(4400);
 const afterJob=(await api(`/api/accounts/${fixture.account_id}/jobs`))[0];assert.ok(afterJob.event_count>beforeJob.event_count);
 assert.equal(await trigger.evaluate(el=>window.__choiceTrigger===el&&document.querySelector('.choice-popup')===window.__choicePopup),true);
 assert.equal(await trigger.getAttribute('aria-expanded'),'true');assert.equal(await page.locator('.choice-option.is-active').getAttribute('data-value'),'sell');assert.deepEqual((await formValues()).sides,['buy']);await page.keyboard.press('Escape');
 evidence.checks.push('connected trigger, same open popup and uncommitted sell candidate survive real event update and normal production poll');

 // FormData, API preview and final persisted side agree, and direction changes
 // cannot reuse the earlier preview. A new server version also disables it.
 await page.locator('#trade-symbol').fill('518880');await page.locator('#trade-quantity').fill('1000');await page.locator('#trade-price').fill('7.000');await page.locator('#trade-time').fill('2026-10-30T10:00');await page.locator('#trade-form [name=price_confirmed]').check();
 const preview=async()=>{const response=page.waitForResponse(r=>r.url().endsWith('/trades/preview'));await page.locator('#trade-form [data-ledger-action=preview]').click();const result=await response;assert.equal(result.status(),200,await result.text());await page.locator('.loading-line').waitFor({state:'hidden'});return result.request().postDataJSON();};
 assert.equal((await preview()).side,'buy');assert.equal((await formValues()).submittable,true);
 await trigger.click();await page.locator('.choice-option[data-value=sell]').click();assert.equal((await formValues()).submittable,false);assert.ok((await page.locator('.trade-preview').textContent()).includes('重新预览'));
 await trigger.click();await page.locator('.choice-option[data-value=buy]').click();await preview();
 const oldLedger=await api(`/api/accounts/${fixture.account_id}/ledger`);
 await api(`/api/accounts/${fixture.account_id}/funds`,{operation_id:crypto.randomUUID(),expected_version:oldLedger.input_version,kind:'deposit',amount:'10',reason:'twelfth controlled version change'});
 await sleep(4400);assert.equal((await formValues()).submittable,false);assert.equal(await page.locator('#trade-symbol').inputValue(),'518880');assert.ok((await page.locator('#trade-form [data-ledger-version-notice]').textContent()).includes('新版本'));
 await preview();
 const oldPlan=await api(`/api/accounts/${fixture.account_id}/fee-plan`);
 await api(`/api/accounts/${fixture.account_id}/fee-plan`,{...oldPlan,name:'Twelfth controlled same-rate version',expected_fee_plan_version:oldPlan.version},'PUT');
 await sleep(4400);assert.equal((await formValues()).submittable,false);assert.deepEqual((await formValues()).sides,['buy']);
 await preview();await page.locator('#trade-form [type=submit]').click();await page.locator('.loading-line').waitFor({state:'hidden'});
 assert.equal(writes.length,1);assert.equal(writes[0].side,'buy');let ledger=await api(`/api/accounts/${fixture.account_id}/ledger`);assert.equal(ledger.entries[0].side,'buy');assert.equal(ledger.cash,'93005.00');
 await trigger.click();await page.locator('.choice-option[data-value=sell]').click();await page.locator('#trade-symbol').fill('518880');await page.locator('#trade-quantity').fill('1000');await page.locator('#trade-price').fill('7.200');await page.locator('#trade-time').fill('2026-10-30T10:01');await page.locator('#trade-form [name=price_confirmed]').check();assert.equal((await preview()).side,'sell');await page.locator('#trade-form [type=submit]').click();await page.locator('.loading-line').waitFor({state:'hidden'});
 ledger=await api(`/api/accounts/${fixture.account_id}/ledger`);assert.equal(writes.length,2);assert.equal(writes[1].side,'sell');assert.equal(ledger.entries[0].side,'sell');assert.equal(ledger.cash,'100200.00');
 evidence.checks.push('production preview and committed buy/sell agree with visible label and FormData; selection/new account/new fee-plan version invalidate old preview; exact T+0 ledger cash 100200.00');

 const geometry=async(label)=>{
   await trigger.scrollIntoViewIfNeeded();if(await trigger.getAttribute('aria-expanded')==='true')await trigger.click();await trigger.click();
   const g=await page.evaluate(()=>{const button=document.querySelector('#trade-side-control'),popup=document.querySelector('.choice-popup'),first=popup.querySelector('.choice-option'),b=button.getBoundingClientRect(),p=popup.getBoundingClientRect();return {trigger:{x:b.x,y:b.y,width:b.width,bottom:b.bottom},popup:{x:p.x,y:p.y,width:p.width,bottom:p.bottom},textX:button.querySelector('[data-choice-value]').getBoundingClientRect().x,optionTextX:first.firstChild.getBoundingClientRect().x,placement:popup.dataset.placement,viewport:{width:visualViewport.width,height:visualViewport.height,scale:visualViewport.scale},topLayer:popup.matches(':popover-open'),hit:document.elementFromPoint(p.x+p.width/2,p.y+12)?.closest('.choice-option')!==null};});
   assert.ok(Math.abs(g.trigger.x-g.popup.x)<1,JSON.stringify(g));assert.ok(Math.abs(g.trigger.width-g.popup.width)<1,JSON.stringify(g));assert.ok(Math.abs(g.textX-g.optionTextX)<1,JSON.stringify(g));assert.ok(g.popup.y>=g.trigger.bottom||g.popup.bottom<=g.trigger.y,JSON.stringify(g));assert.equal(g.hit,true);evidence.geometry.push({label,...g});
   await page.screenshot({path:`test-results/twelfth/select-${label}.png`,fullPage:false});await page.keyboard.press('Escape');
 };
 await geometry('desktop-100');
 // CDP visual-viewport page scaling is explicit evidence, separate from actual
 // macOS menu-bar browser zoom and Safari validation documented by the operator.
 const cdp=await page.context().newCDPSession(page);
 for(const factor of [1.25,2]){await cdp.send('Emulation.setPageScaleFactor',{pageScaleFactor:factor});await geometry(`page-scale-${factor}`);}
 await cdp.send('Emulation.setPageScaleFactor',{pageScaleFactor:1});
 for(const width of [800,390]){await page.setViewportSize({width,height:900});await geometry(`width-${width}`);}
 await page.setViewportSize({width:1360,height:700});await trigger.scrollIntoViewIfNeeded();await trigger.click();await page.mouse.wheel(0,100);await sleep(80);if(await trigger.getAttribute('aria-expanded')==='true'){const gap=await page.evaluate(()=>{const a=document.querySelector('#trade-side-control').getBoundingClientRect(),b=document.querySelector('.choice-popup').getBoundingClientRect();return Math.abs(a.x-b.x);});assert.ok(gap<1);}await page.keyboard.press('Escape');
 evidence.checks.push('popup geometry: aligned edges, width and first text origin; top layer escapes card clipping; desktop, visual viewport 125/200% scale, 800/390px narrow layouts and scroll anchoring');

 // Same reusable component inside a modal: Escape dismisses menu before modal,
 // and the portal remains in its modal accessibility/interaction surface.
 await page.evaluate(async()=>{const {choiceField}=await import('/static/select-control.js');const modal=document.createElement('dialog');modal.id='choice-test-dialog';modal.innerHTML='<form>'+choiceField({e:String,label:'受控模态买卖方向',name:'side',value:'buy',options:[['buy','买入'],['sell','卖出']],id:'modal-side',scope:'explicit-test-modal'})+'</form>';document.body.append(modal);modal.showModal();});
 const modalTrigger=page.locator('#modal-side-control');await modalTrigger.click();assert.equal(await page.locator('#choice-test-dialog .choice-popup').count(),1);await page.locator('.choice-option[data-value=sell]').click();assert.equal(await page.locator('#modal-side').inputValue(),'sell');await modalTrigger.click();await page.keyboard.press('Escape');assert.equal(await page.locator('#choice-test-dialog').evaluate(el=>el.open),true);await page.keyboard.press('Escape');await page.locator('#choice-test-dialog').evaluate(el=>el.remove());
 evidence.checks.push('reusable component in modal: option pointer interaction, top-layer popup, first Escape closes menu and second closes dialog');

 // Component-level regressions for bounded review fixes F-02/F-03/F-04/F-07/F-10.
 // Each fixture is a real choiceField mounted on the live page, so the shared
 // document listeners, popover portal and native select are the production ones.
 const mountChoice=async(id,spec)=>page.evaluate(async({id,spec})=>{const{choiceField}=await import('/static/select-control.js');const holder=document.createElement('div');holder.id=`rf3-${id}`;holder.style.cssText='position:relative;margin:20px;padding:16px';const{id:ignored,...rest}=spec;holder.innerHTML=choiceField({e:String,scope:'rf3-controls',...rest});document.body.append(holder);},{id,spec});
 const removeChoice=async id=>page.evaluate(id=>document.getElementById(`rf3-${id}`)?.remove(),id);
 const longOptions=Array.from({length:45},(_,index)=>[`m-${String(index+1).padStart(2,'0')}`,`模型 ${index+1}`]);
 await mountChoice('long-search',{label:'长列表搜索',name:'long_search',value:'m-01',options:longOptions,searchable:true,searchPlaceholder:'搜索模型'});
 await mountChoice('long-plain',{label:'长列表普通',name:'long_plain',value:'m-01',options:longOptions});
 const assertScrollPreserved=async(selector,searchable)=>{
   const {popup}=await openChoice(page,selector);
   const scroller=searchable?popup.locator('.choice-options'):popup;
   const before=await scroller.evaluate(el=>{el.scrollTop=el.scrollHeight;el.dispatchEvent(new Event('scroll',{bubbles:true}));return{top:el.scrollTop,max:el.scrollHeight-el.clientHeight};});
   assert.ok(before.top>200,`${selector}: fixture list must actually scroll: ${JSON.stringify(before)}`);
   await page.evaluate(sel=>document.querySelector(sel).dispatchEvent(new Event('choice:sync',{bubbles:true})),selector);
   await sleep(150);
   const after=await scroller.evaluate(el=>el.scrollTop);
   assert.ok(after>200&&Math.abs(after-before.top)<=1,`${selector}: internal scroll must survive sync, before ${before.top} after ${after}`);
   await page.keyboard.press('Escape');await page.locator('.choice-popup').waitFor({state:'detached'});
   return{before,after};
 };
 const scrollSearchable=await assertScrollPreserved('#long_search',true);
 const scrollPlain=await assertScrollPreserved('#long_plain',false);
 evidence.checks.push('F-02: 45-option searchable and plain popups keep their internal scrollTop across an internal scroll event and choice:sync (no reset to 0)');
 await removeChoice('long-search');await removeChoice('long-plain');

 await mountChoice('ime',{label:'IME 保护',name:'ime_model',value:'a',options:[['a','选项 A'],['b','选项 B'],['c','选项 C']],searchable:true});
 const ime=await openChoice(page,'#ime_model');
 const imeSearch=ime.popup.locator('[data-choice-search]');
 assert.equal(await imeSearch.evaluate(el=>document.activeElement===el),true);
 await page.keyboard.press('ArrowDown');
 assert.equal(await page.locator('.choice-option.is-active').getAttribute('data-value'),'b');
 await imeSearch.evaluate(el=>{el.dispatchEvent(new CompositionEvent('compositionstart',{bubbles:true}));el.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',bubbles:true,cancelable:true,isComposing:true,keyCode:229}));});
 assert.equal(await page.locator('#ime_model').inputValue(),'a','IME-composed Enter must not commit the active row');
 assert.equal(await ime.popup.isVisible(),true,'IME-composed Enter must leave the popup open');
 await imeSearch.evaluate(el=>el.dispatchEvent(new CompositionEvent('compositionend',{bubbles:true,data:'b'})));
 await sleep(40);
 await page.keyboard.press('Enter');
 await ime.popup.waitFor({state:'detached'});
 assert.equal(await page.locator('#ime_model').inputValue(),'b','normal Enter after compositionend must commit the active row');
 evidence.checks.push('F-03: Enter during IME composition (isComposing/keyCode 229) leaves the popup open and uncommitted; the next normal Enter commits');
 await removeChoice('ime');

 await mountChoice('dynamic',{label:'动态选项',name:'dynamic_model',value:'m-1',options:[['m-1','模型 1'],['m-2','模型 2'],['m-3','模型 3'],['m-4','模型 4'],['m-5','模型 5']]});
 const dynamic=await openChoice(page,'#dynamic_model');
 await page.keyboard.press('ArrowDown');
 assert.equal(await page.locator('.choice-option.is-active').getAttribute('data-value'),'m-2');
 await page.evaluate(()=>{const select=document.querySelector('#dynamic_model');select.insertAdjacentHTML('afterbegin','<option value="m-0">模型 0</option>');select.dispatchEvent(new Event('choice:sync',{bubbles:true}));});
 await sleep(120);
 assert.equal(await page.locator('.choice-option').count(),6,'prepended native option must reach the open popup');
 assert.equal(await page.locator('.choice-option.is-active').getAttribute('data-value'),'m-2','uncommitted highlight must stay on the same value, not the old index');
 assert.equal(await page.locator('#dynamic_model').inputValue(),'m-1','dynamic sync must not commit the highlight');
 await page.keyboard.press('Escape');await dynamic.popup.waitFor({state:'detached'});
 evidence.checks.push('F-04: prepending a native option while m-2 is highlighted keeps the highlight on value m-2 and leaves the native value uncommitted');
 await removeChoice('dynamic');

 await mountChoice('empty-catalog',{label:'空目录',name:'empty_model',value:'',optionsHtml:'<option value="">清除绑定（恢复继承）</option><option value="legacy" disabled selected>历史绑定 legacy（需重新确认）</option>',searchable:true,searchPlaceholder:'搜索模型',emptyText:'目录为空：请先发现模型'});
 const emptyCatalog=await openChoice(page,'#empty_model');
 const emptyState=emptyCatalog.popup.locator('.choice-empty');
 assert.equal(await emptyState.count(),1,'empty catalog must render exactly one status row');
 assert.equal(await emptyState.getAttribute('role'),'status');
 assert.equal(await emptyState.isVisible(),true);
 assert.ok((await emptyState.textContent()).includes('目录为空'));
 const clearBinding=emptyCatalog.popup.locator('.choice-option[data-value=""]');
 assert.equal(await clearBinding.count(),1,'the clear-binding entry must remain alongside the empty state');
 assert.equal(await clearBinding.getAttribute('aria-disabled'),'false');
 assert.ok((await clearBinding.textContent()).includes('清除绑定'));
 await clearBinding.click();await emptyCatalog.popup.waitFor({state:'detached'});
 assert.equal(await page.locator('#empty_model').inputValue(),'');
 evidence.checks.push('F-07a: an empty model directory keeps the clear-binding option and shows .choice-empty[role=status]');
 await removeChoice('empty-catalog');

 await mountChoice('end-disabled',{label:'末尾禁用',name:'end_model',value:'a',optionsHtml:'<option value="a">甲</option><option value="b">乙</option><option value="c">丙</option><option value="legacy" disabled>历史绑定 legacy</option>'});
 const endTrigger=page.locator('#end_model-control');
 await endTrigger.focus();await page.keyboard.press('End');
 assert.equal(await endTrigger.getAttribute('aria-expanded'),'true','End on a closed trigger must open the popup');
 assert.equal(await page.locator('.choice-option.is-active').getAttribute('data-value'),'c','End must highlight the last enabled option, not the trailing disabled history entry');
 assert.equal(await page.locator('.choice-option.is-active').getAttribute('aria-disabled'),'false');
 assert.equal(await page.locator('#end_model').inputValue(),'a');
 await page.keyboard.press('Escape');await page.locator('.choice-popup').waitFor({state:'detached'});
 evidence.checks.push('F-07b: End on a closed popup highlights the last enabled option and skips trailing disabled history');
 await removeChoice('end-disabled');

 await mountChoice('options-html',{label:'HTML 实体',name:'entity_model',value:'',optionsHtml:'<option value="a" data-note="selected">普通&#9733;</option><option value="b" selected>正确&#9733;</option>'});
 assert.equal(await page.locator('#entity_model').inputValue(),'b','optionsHtml selected attribute must choose the real selected option');
 assert.equal((await page.locator('#entity_model-control [data-choice-value]').textContent()).trim(),'正确★','numeric entity must decode without mojibake or mis-selection');
 await removeChoice('options-html');
 evidence.checks.push('F-10: optionsHtml parses numeric entities through HTML semantics and an attribute value containing "selected" does not win');
 evidence.componentCases={scrollSearchable,scrollPlain};

 // Account navigation intentionally closes the old surface; late work cannot
 // carry an open popup or a selected side into another account's initial form.
 // F-05: spawn the account change itself while the trade popup is open, without
 // first clicking any other trigger, so the popup disposal path is really covered.
 await trigger.click();await page.locator('.choice-popup').waitFor({state:'visible'});
 assert.equal(await trigger.getAttribute('aria-expanded'),'true');
 assert.equal(await page.locator('.choice-popup').evaluate(el=>el.isConnected&&el.getClientRects().length>0),true);
 await page.evaluate(id=>{const select=document.querySelector('#account-switch');select.value=id;select.dispatchEvent(new Event('change',{bubbles:true}));},fixture.other_id);
 await page.waitForFunction(id=>document.querySelector('#account-meta-form')?.dataset.accountId===id,fixture.other_id);
 await page.locator('.loading-line').waitFor({state:'hidden'});
 assert.equal(await page.locator('.choice-popup').count(),0,'cross-account render must dispose the previously open trade popup');
 assert.deepEqual((await formValues()).sides,['buy']);
 evidence.checks.push('F-05: dispatching the account change while the trade popup is visibly open disposes the old popup and starts buy (no helper click pre-closes it)');
 await page.evaluate(id=>{const select=document.querySelector('#account-switch');select.value=id;select.dispatchEvent(new Event('change',{bubbles:true}));},fixture.account_id);
 await page.waitForFunction(id=>document.querySelector('#account-meta-form')?.dataset.accountId===id,fixture.account_id);
 await page.locator('.loading-line').waitFor({state:'hidden'});
 await trigger.click();await selectChoice(page, '#account-switch', fixture.other_id);await page.locator('.loading-line').waitFor({state:'hidden'});assert.equal(await page.locator('.choice-popup').count(),0);assert.deepEqual((await formValues()).sides,['buy']);
 evidence.checks.push('account switch closes old popup; new account starts with buy');
 await trigger.click();await page.locator('.choice-option[data-value=sell]').click();await page.locator('#trade-symbol').fill('518880');await page.locator('#trade-quantity').fill('1000');await page.locator('#trade-price').fill('7.000');await page.locator('#trade-time').fill('2026-10-30T10:00');await page.locator('#trade-form [name=price_confirmed]').check();
 const refused=page.waitForResponse(r=>r.url().endsWith('/trades/preview'));await page.locator('#trade-form [data-ledger-action=preview]').click();const refusedResponse=await refused;assert.equal(refusedResponse.status(),422);const refusal=await refusedResponse.text();assert.ok(refusal.includes('卖出数量超过持有量'),refusal);await page.locator('.loading-line').waitFor({state:'hidden'});assert.deepEqual((await formValues()).sides,['sell']);assert.equal((await formValues()).submittable,false);
 await trigger.click();await page.locator('.choice-option[data-value=buy]').click();await preview();
 let releaseReceipt,observeReceipt;const receiptGate=new Promise(resolve=>releaseReceipt=resolve),receiptReady=new Promise(resolve=>observeReceipt=resolve);
 await page.route(`**/api/accounts/${fixture.other_id}/trades`,async route=>{const response=await route.fetch();observeReceipt();await receiptGate;await route.fulfill({response});});
 await page.locator('#trade-form [type=submit]').click();await receiptReady;await trigger.click();await page.locator('.choice-option[data-value=sell]').click();releaseReceipt();await page.locator('.loading-line').waitFor({state:'hidden'});
 assert.deepEqual((await formValues()).sides,['sell']);assert.equal((await formValues()).visible,'卖出');assert.equal((await formValues()).submittable,false);assert.equal((await api(`/api/accounts/${fixture.other_id}/ledger`)).entries[0].side,'buy');assert.equal(writes.length,3);
 assert.equal(errors.length,0,errors.join('\n'));
 evidence.checks.push('backend validation failure retains sell; side edited while buy receipt is delayed remains an unsubmitted sell draft, while persisted trade stays buy; no JS errors or duplicated writes');
 await writeFile('test-results/twelfth/select-evidence.json',JSON.stringify(evidence,null,2));console.log(JSON.stringify({passed:true,...evidence},null,2));
} catch(error) {if(page)await page.screenshot({path:'test-results/twelfth/select-failure.png',fullPage:true}).catch(()=>{});console.error(log);throw error;}
finally {if(browser)await browser.close();child.kill('SIGTERM');if(child.exitCode===null)await new Promise(resolve=>child.once('exit',resolve));await rm(directory,{recursive:true,force:true});}
