// Imports the real production module and drives actual FastAPI pages. Baseline
// modules come from git, never an embedded duplicate of reconcile().
import {spawn} from 'node:child_process';
import {mkdtemp,rm,mkdir,writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {loadBrowserBaseline} from './browser-baseline.mjs';
const baseline=loadBrowserBaseline('e8cf4f6e937779b11ea4d7aad511fc070bb2d0ae',['view-state.js','app.js']);
const directory=await mkdtemp(join(tmpdir(),'guanlan-thirteenth-forms-'));
const child=spawn(process.env.PYTHON||'.venv/bin/python',['tests/thirteenth_browser_server.py',directory],{env:{...process.env,PYTHON_DOTENV_DISABLED:'1'},stdio:['ignore','pipe','pipe']});
let browser,page,log='';child.stderr.on('data',data=>log+=data.toString());
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const evidence={platform:process.platform,baseline:{},controls:[],pages:[],realModelCalls:0};
try {
 const fixture=await new Promise((resolve,reject)=>{let output='';const timer=setTimeout(()=>reject(Error('server timeout '+log)),30000);child.stdout.on('data',data=>{output+=data;if(output.includes('\n')){clearTimeout(timer);resolve(JSON.parse(output.split('\n')[0]));}});child.once('exit',code=>{clearTimeout(timer);reject(Error(`server exit ${code} ${log}`));});});
 const base=`http://127.0.0.1:${fixture.port}`;
 for(let n=0;;n++){try{if((await fetch(base+'/api/status')).ok)break;}catch{}if(n>100)throw Error('server unavailable');await sleep(50);}
 const api=async(path,body,method='POST')=>{const r=await fetch(base+path,body===undefined?{}:{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const text=await r.text();assert.ok(r.ok,`${path}: ${r.status} ${text}`);return JSON.parse(text);};
 const universe=body=>api('/api/universe',body,'PUT');
 browser=await chromium.launch({headless:true,...(process.env.BROWSER_EXECUTABLE?{executablePath:process.env.BROWSER_EXECUTABLE}:{})});
 evidence.browser=browser.version();await mkdir('test-results/thirteenth',{recursive:true});
 const setup=async old=>{const p=await browser.newPage({viewport:{width:1360,height:900}});await p.addInitScript(id=>localStorage.setItem('guanlan.account',id),fixture.account_id);if(old)for(const [file,body] of Object.entries(baseline))await p.route(`**/static/${file}`,route=>route.fulfill({contentType:'text/javascript',body}));await p.goto(base+'/#market');await p.locator('#universe-form').waitFor();await p.locator('.loading-line').waitFor({state:'hidden'});return p;};
 const controls=async p=>p.evaluate(async()=>{
   const {reconcile,patchHTML}=await import('/static/view-state.js');
   const cases=[
     ['textarea-empty-to-text','<textarea name="x"></textarea>','<textarea name="x">SH:518880</textarea>'],
     ['textarea-text-to-empty','<textarea name="x">old</textarea>','<textarea name="x"></textarea>'],
     ['textarea-same','<textarea name="x">same</textarea>','<textarea name="x">same</textarea>'],
     ['textarea-live-vs-default','<textarea name="x"></textarea>','<textarea name="x">server default</textarea>','textarea-live'],
     ['select-add-selected','<select name="x"><option value="a">A</option></select>','<select name="x"><option value="a">A</option><option value="b" selected>B</option></select>'],
     ['select-initialize','<select name="x"></select>','<select name="x"><option value="a">A</option><option value="b" selected>B</option></select>'],
     ['select-existing-options','<select name="x"><option value="a" selected>A</option><option value="b">B</option></select>','<select name="x"><option value="a">A</option><option value="b" selected>B</option></select>'],
     ['select-remove-selected','<select name="x"><option value="a">A</option><option value="b" selected>B</option></select>','<select name="x"><option value="a">A</option></select>'],
     ['select-remove-all','<select name="x"><option value="a">A</option></select>','<select name="x"></select>'],
     ['select-reorder','<select name="x"><option value="a">A</option><option value="b" selected>B</option></select>','<select name="x"><option value="b" selected>B</option><option value="a">A</option></select>'],
     ['select-duplicate-values','<select name="x"><option value="a">A</option></select>','<select name="x"><option value="a">First</option><option value="a" selected>Second</option></select>'],
     ['select-no-selection','<select name="x"><option value="a">A</option></select>','<select name="x"><option value="a">A</option><option value="b">B</option></select>','none'],
     ['select-disabled','<select name="x"><option value="a">A</option></select>','<select name="x" disabled><option value="a">A</option><option value="b" selected disabled>B</option></select>'],
     ['select-enabled','<select name="x" disabled><option value="a">A</option></select>','<select name="x"><option value="b" selected>B</option></select>'],
     ['multi-add-selected','<select name="x" multiple><option value="a" selected>A</option></select>','<select name="x" multiple><option value="b" selected>B</option><option value="a" selected>A</option><option value="b" selected>B2</option></select>'],
     ['multi-to-single','<select name="x" multiple><option value="a" selected>A</option><option value="b" selected>B</option></select>','<select name="x"><option value="b" selected>B</option><option value="a">A</option></select>'],
     ['checkbox-live-vs-default','<input name="x" type="checkbox" value="old">','<input name="x" type="checkbox" value="new" checked>','unchecked'],
     ['input-live-vs-default','<input name="x" value="old">','<input name="x" value="server default">','input-live'],
   ];
   const snapshot=form=>{const el=form.elements.x;return {value:el.value,defaultValue:el.defaultValue??null,textContent:el.textContent,checked:el.checked??null,defaultChecked:el.defaultChecked??null,disabled:el.disabled,selectedIndex:el.selectedIndex??null,selectedOptions:el.options?[...el.options].map((o,index)=>({index,value:o.value,text:o.text,selected:o.selected,defaultSelected:o.defaultSelected,disabled:o.disabled})):null,formData:[...new FormData(form)]};};
   return cases.map(([name,oldHTML,nextHTML,live])=>{
     const old=document.createElement('form'),incoming=document.createElement('form');old.innerHTML=oldHTML;incoming.innerHTML=nextHTML;document.body.append(old);
     // Dirty live state differs from defaults, even without business form retention.
     if(name.startsWith('textarea'))old.elements.x.value=old.elements.x.value;
     if(live==='textarea-live'||live==='input-live')incoming.elements.x.value='live server value';
     if(live==='none')incoming.elements.x.selectedIndex=-1;
     if(live==='unchecked')incoming.elements.x.checked=false;
     const before=snapshot(old),expected=snapshot(incoming),textNode=old.elements.x.firstChild;
     reconcile(old,incoming);const actual=snapshot(old),sameText=name==='textarea-same'?old.elements.x.firstChild===textNode:null;
     const viaPatch=document.createElement('form');viaPatch.innerHTML=oldHTML;document.body.append(viaPatch);patchHTML(viaPatch,nextHTML);const patchActual=snapshot(viaPatch);
     old.remove();viaPatch.remove();return {name,before,expected,actual,sameText,patchActual,checkPatch:!live};
   });
 });
 page=await setup(true);evidence.baseline.controls=await controls(page);
 for(const name of ['textarea-empty-to-text','select-add-selected','select-initialize']){const row=evidence.baseline.controls.find(r=>r.name===name);assert.notDeepEqual(row.actual,row.expected,name+' must reproduce on immutable baseline');}
 await universe({watchlist:['SH:518880'],candidates:['SZ:159599']});
 await page.waitForFunction(()=>document.querySelector('#watchlist').textContent==='SH:518880',null,{timeout:12000});
 evidence.baseline.page=await page.locator('#universe-form').evaluate(f=>({value:f.elements.watchlist.value,defaultValue:f.elements.watchlist.defaultValue,textContent:f.elements.watchlist.textContent,formData:[...new FormData(f)]}));
 assert.equal(evidence.baseline.page.value,'');assert.equal(evidence.baseline.page.textContent,'SH:518880');await page.close();
 await universe({watchlist:[],candidates:[]});page=await setup(false);const errors=[];page.on('pageerror',error=>errors.push(error.message));
 evidence.controls=await controls(page);
 for(const row of evidence.controls){assert.deepEqual(row.actual,row.expected,row.name);if(row.checkPatch)assert.deepEqual(row.patchActual,row.expected,row.name+' patchHTML');if(row.sameText!==null)assert.equal(row.sameText,true);}
 const snapshot=()=>page.locator('#universe-form').evaluate(f=>({version:f.dataset.sourceVersion,watchlist:{value:f.elements.watchlist.value,defaultValue:f.elements.watchlist.defaultValue,textContent:f.elements.watchlist.textContent,top:f.elements.watchlist.scrollTop},candidates:{value:f.elements.candidates.value,defaultValue:f.elements.candidates.defaultValue,textContent:f.elements.candidates.textContent},formData:[...new FormData(f)],dirty:f.dataset.dirty??null,composing:f.dataset.composing??null,status:f.querySelector('.form-status').textContent}));
 await universe({watchlist:['SH:518880'],candidates:['SZ:159599']});await page.waitForFunction(()=>document.querySelector('#watchlist').value==='SH:518880',null,{timeout:12000});
 let row=await snapshot();assert.deepEqual(row.formData,[['watchlist','SH:518880'],['candidates','SZ:159599']]);evidence.pages.push({name:'empty-to-server',actual:row,server:await api('/api/universe')});
 const save=page.waitForResponse(r=>r.url().endsWith('/api/universe')&&r.request().method()==='PUT');await page.locator('#universe-form [type=submit]').click();const saved=await save;assert.equal(saved.status(),200);evidence.pages.push({name:'actual-submit',request:saved.request().postDataJSON(),server:await saved.json()});await page.locator('.loading-line').waitFor({state:'hidden'});
 // Submission focus is intentionally editing state until the user leaves it.
 await page.locator('.page-heading h1').click();await universe({watchlist:[],candidates:[]});await page.waitForFunction(()=>document.querySelector('#watchlist').value==='',null,{timeout:12000});evidence.pages.push({name:'server-to-empty',actual:await snapshot()});
 // A focused pristine editor retains its live value and selection on conflict.
 await page.locator('#watchlist').focus();await page.locator('#watchlist').evaluate(el=>{window.__focused=el;el.setSelectionRange(0,0);});await universe({watchlist:['SZ:002354'],candidates:[]});await page.waitForFunction(()=>document.querySelector('#universe-form .form-status').textContent.includes('后台研究范围已更新'),null,{timeout:12000});
 assert.equal(await page.locator('#watchlist').inputValue(),'');assert.equal(await page.locator('#watchlist').evaluate(el=>el===window.__focused&&document.activeElement===el),true);evidence.pages.push({name:'focused-pristine-conflict',actual:await snapshot()});
 // Pristine editors resume server updates after focus leaves them.
 await page.locator('.page-heading h1').click();await universe({watchlist:['SH:518880'],candidates:[]});await page.waitForFunction(()=>document.querySelector('#watchlist').value==='SH:518880',null,{timeout:12000});
 await page.locator('#watchlist').fill('518880\n002354');await page.locator('#watchlist').evaluate(el=>{el.setSelectionRange(2,8,'backward');window.__draft=el;});await universe({watchlist:['SZ:159599'],candidates:['SZ:002354']});await page.waitForFunction(()=>document.querySelector('#universe-form .form-status').textContent.includes('后台研究范围已更新'),null,{timeout:12000});
 assert.deepEqual(await page.locator('#watchlist').evaluate(el=>({same:el===window.__draft,value:el.value,start:el.selectionStart,end:el.selectionEnd,direction:el.selectionDirection,focus:document.activeElement===el})),{same:true,value:'518880\n002354',start:2,end:8,direction:'backward',focus:true});evidence.pages.push({name:'dirty-draft-conflict',actual:await snapshot(),server:await api('/api/universe')});
 await page.reload();await page.locator('#universe-form').waitFor();await page.locator('.loading-line').waitFor({state:'hidden'});
 // Synthetic DOM composition lifecycle proves the production contract, not an
 // OS IME certification. Blur during composition must not permit replacement.
 await page.locator('#watchlist').evaluate(el=>{el.focus();el.dispatchEvent(new CompositionEvent('compositionstart',{bubbles:true,data:''}));el.value='中文输入中';el.blur();window.__composing=el;});
 await universe({watchlist:['SH:518880'],candidates:[]});await page.waitForFunction(()=>document.querySelector('#universe-form .form-status').textContent.includes('后台研究范围已更新'),null,{timeout:12000});assert.equal(await page.locator('#watchlist').inputValue(),'中文输入中');assert.equal(await page.locator('#watchlist').evaluate(el=>el===window.__composing),true);evidence.pages.push({name:'synthetic-composition-blurred',actual:await snapshot()});
 await page.locator('#watchlist').evaluate(el=>el.dispatchEvent(new CompositionEvent('compositionend',{bubbles:true,data:'中文输入中'})));assert.equal((await snapshot()).dirty,'true');
 await page.reload();await page.locator('#universe-form').waitFor();await page.locator('.loading-line').waitFor({state:'hidden'});
 // Dynamic native filter lives outside a form. Material index updates are real
 // SQLite records; selected category stays coherent through insertion/removal.
 await api('/__fixture/material',{classification:'official_disclosure'});await page.locator('nav a[href="#research"]').click();await page.locator('#material-filter option[value=official_disclosure]').waitFor({state:'attached'});await page.locator('.loading-line').waitFor({state:'hidden'});await page.locator('#material-filter').selectOption('official_disclosure');
 await api('/__fixture/material',{classification:'news'});await page.waitForFunction(()=>!!document.querySelector('#material-filter option[value=news]'),null,{timeout:12000});assert.equal(await page.locator('#material-filter').inputValue(),'official_disclosure');
 await page.locator('#material-filter').selectOption('news');await api('/__fixture/material',{classification:'unclassified'});await page.waitForFunction(()=>!!document.querySelector('#material-filter option[value=unclassified]'),null,{timeout:12000});
 evidence.pages.push({name:'dynamic-material-catalogue',actual:await page.locator('#material-filter').evaluate(el=>({value:el.value,selectedOptions:[...el.selectedOptions].map(o=>o.value),options:[...el.options].map(o=>o.value)}))});assert.equal(await page.locator('#material-filter').inputValue(),'news');
 // Changing the material search removes the selected category. Selection and
 // the displayed results must fall back together rather than disagreeing.
 for(let n=0;n<31;n++)await api('/__fixture/material',{classification:'official_disclosure'});await page.waitForFunction(()=>!document.querySelector('#material-filter option[value=news]'),null,{timeout:12000});assert.equal(await page.locator('#material-filter').inputValue(),'all');assert.ok((await page.locator('#app').textContent()).includes('明确工程材料 34'));assert.ok(!(await page.locator('#app').textContent()).includes('当前页暂无此类材料'));
 evidence.pages.push({name:'filtered-category-removed',actual:await page.locator('#material-filter').evaluate(el=>({value:el.value,selectedOptions:[...el.selectedOptions].map(o=>o.value)}))});
 assert.equal(errors.length,0,errors.join('\n'));evidence.passed=true;await writeFile('test-results/thirteenth/form-evidence.json',JSON.stringify(evidence,null,2));console.log(JSON.stringify({passed:true,controls:evidence.controls.length,pages:evidence.pages.map(r=>r.name),browser:evidence.browser,realModelCalls:0}));
} catch(error){if(page)await page.screenshot({path:'test-results/thirteenth/forms-failure.png',fullPage:true}).catch(()=>{});console.error(log);throw error;}
finally{if(browser)await browser.close();child.kill('SIGTERM');if(child.exitCode===null)await new Promise(resolve=>child.once('exit',resolve));await rm(directory,{recursive:true,force:true});}
