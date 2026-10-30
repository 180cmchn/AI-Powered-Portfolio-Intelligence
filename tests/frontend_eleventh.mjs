// Actual production app and static modules on temporary SQLite. Only directory,
// saved report and background event fixtures are controlled, no real model claim.
import {spawn,execFileSync} from 'node:child_process';
import {mkdtemp,rm,mkdir,writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
const directory=await mkdtemp(join(tmpdir(),'guanlan-eleventh-ui-'));
const child=spawn(process.env.PYTHON||'.venv/bin/python',['tests/eleventh_browser_server.py',directory],{env:{...process.env,PYTHON_DOTENV_DISABLED:'1'},stdio:['ignore','pipe','pipe']});
let browser,page,log='';child.stderr.on('data',data=>log+=data.toString());
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms)), evidence={baseline:[],fixed:[],navigation:[],checks:[]};
try {
 const fixture=await new Promise((resolve,reject)=>{let output='';const timer=setTimeout(()=>reject(Error('server timeout '+log)),30000);child.stdout.on('data',data=>{output+=data;if(output.includes('\n')){clearTimeout(timer);resolve(JSON.parse(output.split('\n')[0]));}});child.once('exit',code=>{clearTimeout(timer);reject(Error('server exit '+code+' '+log));});});
 const base=`http://127.0.0.1:${fixture.port}`;
 for(let n=0;;n++){try{if((await fetch(base+'/api/status')).ok)break;}catch{}if(n>100)throw Error('server unavailable '+log);await sleep(50);}
 const api=async(path,body,method='POST')=>{const r=await fetch(base+path,body===undefined?{}:{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const text=await r.text();assert.ok(r.ok,`${method} ${path}: ${r.status} ${text}`);return JSON.parse(text);};
 browser=await chromium.launch({headless:true,...(process.env.BROWSER_EXECUTABLE?{executablePath:process.env.BROWSER_EXECUTABLE}:{})});
 await mkdir('test-results/eleventh',{recursive:true});
 const setup=async baseline=>{const p=await browser.newPage({viewport:{width:1360,height:900}});await p.addInitScript(id=>localStorage.setItem('guanlan.account',id),fixture.account_id);if(baseline)for(const file of ['app.js','sixth.js','style.css'])await p.route(`**/static/${file}`,route=>route.fulfill({contentType:file.endsWith('.css')?'text/css':'text/javascript',body:execFileSync('git',['show',`874b987:app/static/${file}`],{encoding:'utf8'})}));await p.goto(base+'/#roles');await p.locator('.role-model-form').first().waitFor();return p;};
 const scenario=async(p,kind,baseline=false)=>{
   const form=p.locator('.role-model-form').first(), text=form.locator('textarea[name=system_prompt]');
   if(!await form.locator('details').first().evaluate(el=>el.open))await form.locator('details > summary').first().click();
   await text.scrollIntoViewIfNeeded();
   if(kind!=='unfocused'){await text.focus();await text.evaluate(el=>el.setSelectionRange(0,4,'backward'));}
   else await text.evaluate(()=>document.activeElement.blur());
   if(kind==='dirty'){await text.evaluate(el=>{el.value+='\n本地未保存草稿';el.dispatchEvent(new Event('input',{bubbles:true}));});}
   await text.hover();await p.mouse.wheel(240,1200);await sleep(200);
   await text.evaluate(el=>{el.scrollLeft=180;window.__roleReadingNode=el;window.__roleForm=el.form;window.__roleOriginalValue=el.value;});
   const snapshot=()=>text.evaluate(el=>({scrollTop:el.scrollTop,scrollLeft:el.scrollLeft,start:el.selectionStart,end:el.selectionEnd,direction:el.selectionDirection,focus:document.activeElement===el,details:el.closest('details').open,windowY:scrollY,sameNode:window.__roleReadingNode===el,sameForm:window.__roleForm===el.form,sameValue:el.value===window.__roleOriginalValue}));
   const before=await snapshot();assert.ok(before.scrollTop>500,JSON.stringify(before));
   const after=[];
   for(let i=0;i<(baseline?1:2);i++){await api('/__fixture/touch',{});await sleep(4300);after.push(await snapshot());}
   if(!baseline)for(const value of after){assert.ok(Math.abs(value.scrollTop-before.scrollTop)<=1,JSON.stringify({kind,before,value}));assert.equal(value.scrollLeft,before.scrollLeft);assert.deepEqual([value.start,value.end,value.direction],[before.start,before.end,before.direction]);assert.equal(value.focus,before.focus);assert.equal(value.details,true);assert.equal(value.sameNode,true);assert.equal(value.sameForm,true);assert.equal(value.sameValue,true);assert.ok(Math.abs(value.windowY-before.windowY)<=1);}
   return {kind,before,after};
 };
 if(!process.env.ELEVENTH_SKIP_SCROLL) { page=await setup(true);
 for(const kind of ['unfocused','focused','dirty']){await page.reload();await page.locator('.role-model-form').first().waitFor();evidence.baseline.push(await scenario(page,kind,true));}
 await page.close();page=await setup(false);
 for(const kind of ['unfocused','focused','dirty']){await page.reload();await page.locator('.role-model-form').first().waitFor();evidence.fixed.push(await scenario(page,kind));}
 const form=page.locator('.role-model-form').first(),text=form.locator('textarea[name=system_prompt]');
 await text.dispatchEvent('compositionstart',{data:'输入'});await text.evaluate(el=>{window.__imeNode=el;window.__imeEnd=0;el.addEventListener('compositionend',()=>window.__imeEnd++);});await api('/__fixture/touch',{});await sleep(4300);assert.ok(await text.evaluate(el=>window.__imeNode===el&&document.activeElement===el));assert.equal(await page.evaluate(()=>window.__imeEnd),0);await text.dispatchEvent('compositionend',{data:'输入'});
 evidence.checks.push('same connected DOM and active selection survive composition event + poll; no synthetic compositionend');
 const dirty=await text.inputValue();await api('/__fixture/role-change',{});await sleep(4300);assert.equal(await text.inputValue(),dirty);assert.ok(await form.locator('.role-version-notice').isVisible());await form.locator('[type=submit]').click();await page.locator('.loading-line').waitFor({state:'hidden'});assert.equal(await text.inputValue(),dirty);assert.ok((await page.locator('#toast-region').textContent()).includes('409'),await page.locator('#toast-region').textContent());
 await form.locator('[data-sixth-action=role-reload]').click();await page.locator('.loading-line').waitFor({state:'hidden'});assert.ok((await text.inputValue()).startsWith('已保存的新版本提示'));
 await text.fill('已明确保存的新提示');await form.locator('[type=submit]').click();await page.locator('.loading-line').waitFor({state:'hidden'});assert.equal(await text.inputValue(),'已明确保存的新提示');
 evidence.checks.push('server revision leaves dirty prompt intact, stale save conflicts, explicit reload and current save succeed');
 } else page=await setup(false);
 const errors=[];page.on('pageerror',error=>errors.push(error.message));
 const navigate=async route=>{await page.locator(`nav a[href="#${route}"]`).click();await page.locator(`nav a[href="#${route}"][aria-current=page]`).waitFor();await page.locator('.loading-line').waitFor({state:'hidden'});};
 for(const route of ['overview','holdings','market','research','agents','providers','roles','account-models','forecasts','settings']){await navigate(route);const count=await page.locator('.task-status-card').count();assert.equal(count,route==='overview'?1:0,route);evidence.navigation.push({route,fullCards:count});}
 await navigate('account-models');await page.locator('.role-model-form').first().locator('[name=reasoning_effort]').selectOption('high');await page.locator('#account-switch').selectOption(fixture.other_id);await page.locator('.loading-line').waitFor({state:'hidden'});assert.equal(await page.locator('.role-model-form').first().locator('[name=reasoning_effort]').inputValue(),'');await page.locator('.role-model-form').first().locator('[name=reasoning_effort]').selectOption('low');await page.locator('#account-switch').selectOption(fixture.account_id);await page.locator('.loading-line').waitFor({state:'hidden'});assert.equal(await page.locator('.role-model-form').first().locator('[name=reasoning_effort]').inputValue(),'high');evidence.checks.push('account-role drafts use account + role identity and never cross accounts');
 await navigate('overview');assert.ok((await page.locator('.task-status-card').textContent()).includes('最近研究任务 · 已完成'));assert.ok(!(await page.locator('.task-status-card').textContent()).includes('最近研究任务 · 汇总'));
 const aJob=(await api(`/api/accounts/${fixture.account_id}/jobs`))[0].id;await page.locator('#account-switch').selectOption(fixture.other_id);await page.locator('.loading-line').waitFor({state:'hidden'});assert.ok(!(await page.locator('.task-status-card').textContent()).includes(aJob));await page.locator('#account-switch').selectOption(fixture.account_id);await page.locator('.loading-line').waitFor({state:'hidden'});assert.ok((await page.locator('.task-status-card').textContent()).includes(aJob));
 evidence.checks.push('only overview shows full card; completion outranks synthesis; account isolation');
 await navigate('holdings');await page.locator('#trade-form').waitFor();
 const beforeOther=await api(`/api/accounts/${fixture.other_id}/ledger`);
 const trade=async(symbol,price,side='buy')=>{await page.locator('#trade-side').selectOption(side);await page.locator('#trade-symbol').fill(symbol);await page.locator('#trade-quantity').fill('1000');await page.locator('#trade-price').fill(price);await page.locator('#trade-time').fill('2026-10-30T10:00');await page.locator('#trade-form [name=price_confirmed]').check();await page.locator('#trade-form [data-ledger-action=preview]').click();await page.locator('.loading-line').waitFor({state:'hidden'});assert.equal(await page.locator('#trade-form [type=submit]').isDisabled(),false,await page.locator('#toast-region').textContent());await page.locator('#trade-form [type=submit]').dblclick();await page.locator('.loading-line').waitFor({state:'hidden'});};
 await trade('002354','10.00');let ledger=await api(`/api/accounts/${fixture.account_id}/ledger`);assert.equal(ledger.cash,'89994.90');assert.equal(ledger.positions.find(p=>p.symbol==='SZ:002354').quantity,'1000');assert.ok((await page.locator('#ledger-overview').textContent()).includes('1,000 股'));assert.ok((await page.locator('#ledger-overview').textContent()).includes('天娱数科'));
 await trade('159599','2.500');ledger=await api(`/api/accounts/${fixture.account_id}/ledger`);assert.equal(ledger.cash,'87489.90');
 let calls=0;await page.route(`**/api/accounts/${fixture.account_id}/trades`,async route=>{calls++;const response=await route.fetch();return route.abort('connectionreset');});
 await trade('518880','7.000');ledger=await api(`/api/accounts/${fixture.account_id}/ledger`);assert.equal(ledger.cash,'80484.90');assert.equal(calls,1,'response loss reads receipt without repeating trade');await page.unroute(`**/api/accounts/${fixture.account_id}/trades`);
 await trade('518880','7.200','sell');ledger=await api(`/api/accounts/${fixture.account_id}/ledger`);assert.equal(ledger.cash,'87679.90');assert.equal(ledger.positions.find(p=>p.symbol==='SH:518880').quantity,'0');
 // Explicit actual-fee correction appends a referenced record, preserving the original trade.
 const originalTrade=ledger.entries[0];await page.locator('[data-key^="fee-adjustment:"] > summary').click();await page.locator('#adjust-commission').fill('2.50');await page.locator('#adjust-reason').fill('受控交割单费用样例');await page.locator('#fee-adjustment-form [type=submit]').click();await page.locator('.loading-line').waitFor({state:'hidden'});ledger=await api(`/api/accounts/${fixture.account_id}/ledger`);assert.equal(ledger.cash,'87682.40');assert.equal(ledger.entries[0].kind,'fee_adjustment');assert.ok((await page.locator('#trade-history tbody tr').first().textContent()).includes('费用调整'));assert.deepEqual(ledger.entries.find(row=>row.id===originalTrade.id),originalTrade);
 await page.locator('[data-key^="funds:"] > summary').click();await page.locator('#fund-amount').fill('1000.00');await page.locator('#fund-reason').fill('受控外部资金转入');await page.locator('#fund-form [type=submit]').click();await page.locator('.loading-line').waitFor({state:'hidden'});ledger=await api(`/api/accounts/${fixture.account_id}/ledger`);assert.equal(ledger.cash,'88682.40');assert.equal(ledger.summary.realized_pnl,'192.50');
 assert.equal((await api(`/api/accounts/${fixture.other_id}/ledger`)).cash,beforeOther.cash);
 await page.waitForFunction(()=>document.querySelectorAll('#toast-region .toast').length===0);await page.screenshot({path:'test-results/eleventh/trade-ledger.png',fullPage:true});evidence.checks.push('three securities through production trade form; exact cash, ETF fees, T+0 full sale, explicit fee adjustment, external deposit excluded from PnL, lost-response receipt, double-click and account isolation');
 assert.equal(errors.length,0,errors.join('\n'));
 await writeFile('test-results/eleventh/browser-evidence.json',JSON.stringify(evidence,null,2));console.log(JSON.stringify({passed:true,checks:evidence.checks,baseline:evidence.baseline.map(x=>({kind:x.kind,before:x.before.scrollTop,after:x.after[0].scrollTop})),fixed:evidence.fixed.map(x=>({kind:x.kind,scroll:x.after.map(a=>a.scrollTop)}))},null,2));
} catch(error) {if(page)await page.screenshot({path:'test-results/eleventh/failure.png',fullPage:true}).catch(()=>{});console.error(log);throw error;}
finally {if(browser)await browser.close();child.kill('SIGTERM');if(child.exitCode===null)await new Promise(resolve=>child.once('exit',resolve));await rm(directory,{recursive:true,force:true});}
