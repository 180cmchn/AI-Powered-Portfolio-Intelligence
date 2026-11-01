// Production FastAPI, native application forms and temporary SQLite. No model worker.
import {spawn} from 'node:child_process';
import {mkdtemp,rm,mkdir,writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
const directory=await mkdtemp(join(tmpdir(),'guanlan-thirteenth-correction-'));
const child=spawn(process.env.PYTHON||'.venv/bin/python',['tests/eleventh_browser_server.py',directory],{env:{...process.env,PYTHON_DOTENV_DISABLED:'1'},stdio:['ignore','pipe','pipe']});
let browser,page,log='';child.stderr.on('data',d=>log+=d);
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const evidence={checks:[],requests:[],realModelCalls:0};
try {
 const fixture=await new Promise((resolve,reject)=>{let output='';const timer=setTimeout(()=>reject(Error(log)),30000);child.stdout.on('data',d=>{output+=d;if(output.includes('\n')){clearTimeout(timer);resolve(JSON.parse(output.split('\n')[0]));}});child.once('exit',code=>{clearTimeout(timer);reject(Error('server '+code+' '+log));});});
 const base=`http://127.0.0.1:${fixture.port}`, aid=fixture.account_id;
 for(let n=0;;n++){try{if((await fetch(base+'/api/health')).ok)break;}catch{}if(n>100)throw Error(log);await sleep(50);}
 const api=async(path,body)=>{const r=await fetch(base+path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const t=await r.text();assert.ok(r.ok,`${r.status} ${path}: ${t}`);return JSON.parse(t);};
 for(const [symbol,quantity,price,fees] of [['SZ:002354','300','10',{commission:'10',transfer:'0.03',stamp:'0',reason:'受控费用'}],['SH:518880','100','10',null]]) {
   const a=await api(`/api/accounts/${aid}`),p=await api(`/api/accounts/${aid}/fee-plan`);
   await api(`/api/accounts/${aid}/trades`,{symbol,quantity,price,side:'buy',mode:'simulated',traded_at:'2026-10-30T10:00:00+08:00',operation_id:crypto.randomUUID(),expected_version:a.version,fee_plan_version:p.version,...(fees?{fees_override:fees}:{})});
 }
 const before=await api(`/api/accounts/${aid}/ledger`);evidence.before=before.positions;
 assert.equal(before.positions.find(p=>p.symbol==='SZ:002354').total_cost,'3010.03');
 browser=await chromium.launch({headless:true});page=await browser.newPage({viewport:{width:1360,height:900}});
 await page.addInitScript(id=>localStorage.setItem('guanlan.account',id),aid);
 page.on('request',r=>{if(r.method()==='POST'&&/\/corrections(?:\/preview)?$/.test(new URL(r.url()).pathname))evidence.requests.push({path:new URL(r.url()).pathname,body:r.postDataJSON()});});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(base+'/#holdings');await page.locator('#account-form').waitFor({state:'attached'});await page.locator('.loading-line').waitFor({state:'hidden'});
 await page.locator('#snapshot-correction > summary').click();
 await mkdir('test-results/thirteenth',{recursive:true});
 evidence.geometry=[];
 for(const width of [1360,800,390]) {
   await page.setViewportSize({width,height:1000});
   await page.locator('#holding-rows').scrollIntoViewIfNeeded();
   const geometry=await page.locator('#holding-rows').evaluate(root=>({viewport:innerWidth,documentWidth:document.documentElement.scrollWidth,rows:[...root.children].map(row=>({width:row.getBoundingClientRect().width,scrollWidth:row.scrollWidth,inputs:[...row.querySelectorAll('input:not([type=checkbox])')].map(input=>{const r=input.getBoundingClientRect();return {name:input.name,left:r.left,right:r.right,width:r.width,top:r.top,bottom:r.bottom};})}))}));
   assert.ok(geometry.documentWidth<=width+1,JSON.stringify(geometry));
   for(const row of geometry.rows){assert.ok(row.scrollWidth<=row.width+1,JSON.stringify(geometry));for(const input of row.inputs)assert.ok(input.left>=0&&input.right<=width&&input.width>=80,JSON.stringify(geometry));for(let a=0;a<row.inputs.length;a++)for(let b=a+1;b<row.inputs.length;b++){const x=row.inputs[a],y=row.inputs[b];assert.ok(x.right<=y.left+1||y.right<=x.left+1||x.bottom<=y.top+1||y.bottom<=x.top+1,JSON.stringify(geometry));}}
   evidence.geometry.push(geometry);await page.screenshot({path:`test-results/thirteenth/correction-layout-${width}.png`});
 }
 await page.setViewportSize({width:1360,height:900});
 const form=page.locator('#account-form'),submit=form.locator('[type=submit]');
 const preview=async()=>{const response=page.waitForResponse(r=>r.url().endsWith('/corrections/preview'));await form.locator('[data-ledger-action=correction-preview]').click();const r=await response;assert.equal(r.status(),200,await r.text());await page.locator('.loading-line').waitFor({state:'hidden'});return r.json();};
 const commit=async()=>{const response=page.waitForResponse(r=>r.url().endsWith('/corrections'));await submit.click();const r=await response;assert.equal(r.status(),201,await r.text());await page.locator('.loading-line').waitFor({state:'hidden'});return r.json();};
 await form.locator('[name=cash]').fill('96000');await form.locator('[name=reason]').fill('只校正现金，保留所有批次与精确成本');
 const plan=await preview();assert.deepEqual(plan.changes,[]);assert.equal(await submit.isEnabled(),true);
 assert.deepEqual(evidence.requests.at(-1).body.holdings,[]);
 await form.locator('[name=reason]').fill('修订校正原因');assert.equal(await submit.isDisabled(),true);await preview();
 evidence.formData=await form.evaluate(f=>[...new FormData(f)]);
 await commit();const after=await api(`/api/accounts/${aid}/ledger`);assert.deepEqual(after.positions,before.positions);assert.equal(after.cash,'96000.00');
 evidence.checks.push('real buys produce 3010.03 / 300 repeating unit cost; cash-only UI sends sparse holdings; preview/commit retain every exact position and lot');
 await form.locator('[name=reason]').fill('仅校正黄金成本');const gold=form.locator('.holding-row').filter({has:page.locator('input[value="SH:518880"]')});
 await gold.locator('input[name^=cost-]:not([type=checkbox])').fill('11.00');await preview();await commit();
 const other=await api(`/api/accounts/${aid}/ledger`);assert.deepEqual(other.positions.find(p=>p.symbol==='SZ:002354'),before.positions.find(p=>p.symbol==='SZ:002354'));
 evidence.checks.push('one security unit-cost correction leaves unrelated repeating-cost security exactly intact');
 await form.locator('[name=reason]').fill('预览后后台版本变化');await form.locator('[name=cash]').fill('97000');await preview();
 const current=await api(`/api/accounts/${aid}`);await api(`/api/accounts/${aid}/funds`,{operation_id:crypto.randomUUID(),expected_version:current.version,kind:'deposit',amount:'1',reason:'受控后台版本变化'});
 await page.waitForFunction(()=>document.querySelector('#account-form [type=submit]').disabled,{},{timeout:12000});
 assert.equal(await form.locator('[name=cash]').inputValue(),'97000');await preview();
 await form.locator('[data-action=add-holding]').click();assert.equal(await submit.isDisabled(),true);await form.locator('.holding-row').last().locator('[data-action=remove-holding]').click();
 evidence.checks.push('input edit, row addition/removal and background version invalidate preview while preserving draft');
 await form.locator('[name=reason]').fill('明确将成本设为未知');await gold.locator('input[name^=cost-unknown-]').check();await preview();await commit();
 const unknown=await api(`/api/accounts/${aid}/ledger`);assert.equal(unknown.positions.find(p=>p.symbol==='SH:518880').total_cost,null);
 assert.deepEqual(unknown.positions.find(p=>p.symbol==='SZ:002354'),before.positions.find(p=>p.symbol==='SZ:002354'));
 evidence.checks.push('explicit unknown checkbox differs from blank/preserve; unrelated known basis remains exact');
 // A dirty form survives route changes as the same editing boundary. A server
 // version update while away invalidates the old preview when it is restored.
 await form.locator('[name=reason]').fill('跨路由草稿');await form.locator('[name=cash]').fill('98000');await preview();
 await form.evaluate(f=>window.__correctionDraft=f);
 await page.locator('nav a[href="#research"]').click();await page.locator('.loading-line').waitFor({state:'hidden'});
 const prior=await api(`/api/accounts/${aid}`);await api(`/api/accounts/${aid}/funds`,{operation_id:crypto.randomUUID(),expected_version:prior.version,kind:'deposit',amount:'1',reason:'离页期间后台变更'});
 await page.locator('nav a[href="#holdings"]').click();await form.waitFor({state:'visible'});await page.locator('.loading-line').waitFor({state:'hidden'});
 assert.equal(await form.evaluate(f=>f===window.__correctionDraft),true);assert.equal(await form.locator('[name=cash]').inputValue(),'98000');assert.equal(await submit.isDisabled(),true);
 await preview();
 let release,received;const gate=new Promise(resolve=>release=resolve),ready=new Promise(resolve=>received=resolve);
 await page.route(`**/api/accounts/${aid}/corrections`,async route=>{const response=await route.fetch();received();await gate;await route.fulfill({response});});
 await submit.click();await ready;await form.locator('[name=cash]').fill('99000');await form.locator('[name=reason]').fill('保存途中继续编辑的草稿');release();await page.locator('.loading-line').waitFor({state:'hidden'});await page.unroute(`**/api/accounts/${aid}/corrections`);
 const persisted=await api(`/api/accounts/${aid}/ledger`);assert.equal(persisted.cash,'98000.00');assert.equal(await form.locator('[name=cash]').inputValue(),'99000');assert.equal(await submit.isDisabled(),true);
 const revised=await preview();assert.equal(revised.cash_before,'98000.00');assert.equal(revised.cash_after,'99000.00');await commit();
 assert.equal((await api(`/api/accounts/${aid}/ledger`)).cash,'99000.00');assert.equal(await form.locator('[name=reason]').inputValue(),'');assert.equal(await submit.isDisabled(),true);
 evidence.checks.push('same draft survives route navigation; intervening account version disables old preview; delayed receipt preserves a new draft and requires a new exact preview; clean save reloads baseline');
 const zero=await api('/api/accounts',{name:'明确零数量范围测试',cash:'100',holdings:[{symbol:'SZ:159599',quantity:'0',cost_price:'2.50'}]});
 await page.reload();await page.locator('#account-switch').selectOption(zero.id);await page.locator('.loading-line').waitFor({state:'hidden'});
 if(!await page.locator('#snapshot-correction').evaluate(el=>el.open))await page.locator('#snapshot-correction > summary').click();
 await form.locator('[name=cash]').fill('101');await form.locator('[name=reason]').fill('仅现金校正，原零数量范围保持');
 await preview();assert.deepEqual(evidence.requests.at(-1).body.holdings,[]);await commit();
 const preservedZero=await api(`/api/accounts/${zero.id}`);assert.deepEqual(preservedZero.holdings,zero.holdings);
 await form.locator('[name=reason]').fill('明确移除已为零的证券范围');await form.locator('[data-action=remove-holding]').click();
 const removed=await preview();assert.deepEqual(evidence.requests.at(-1).body.holdings,[{symbol:'SZ:159599',quantity:'0',remove:true}]);
 assert.equal(removed.changes.length,1);await commit();assert.deepEqual((await api(`/api/accounts/${zero.id}`)).holdings,[]);
 assert.deepEqual((await api(`/api/accounts/${aid}/ledger`)).positions,persisted.positions);
 evidence.checks.push('cash-only preserves a zero-quantity holding; explicit row removal carries remove:true and removes only that account research scope');
 assert.deepEqual(errors,[]);evidence.finalPositions=(await api(`/api/accounts/${aid}/ledger`)).positions;evidence.browser=browser.version();
 await mkdir('test-results/thirteenth',{recursive:true});await writeFile('test-results/thirteenth/correction-evidence.json',JSON.stringify(evidence,null,2));console.log(JSON.stringify({ok:true,checks:evidence.checks}));
} catch(error){if(page)await page.screenshot({path:'test-results/thirteenth/correction-failure.png',fullPage:true}).catch(()=>{});throw error;} finally {await browser?.close();child.kill('SIGTERM');if(child.exitCode===null)await new Promise(r=>child.once('exit',r));await rm(directory,{recursive:true,force:true});}
