// Focused runner for latest-dialog-intent and overview record identity regressions.
import {spawn} from 'node:child_process';
import {mkdtemp,rm,mkdir,writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {verifyDialogIntents} from './twelfth_dialog_cases.mjs';
const directory=await mkdtemp(join(tmpdir(),'guanlan-dialog-intents-'));
const child=spawn(process.env.PYTHON||'.venv/bin/python',['tests/twelfth_browser_server.py',directory],{env:{...process.env,PYTHON_DOTENV_DISABLED:'1'},stdio:['ignore','pipe','pipe']});
let browser,page,log='';child.stderr.on('data',data=>log+=data);
try{
 const fixture=await new Promise((resolve,reject)=>{let output='';child.stdout.on('data',data=>{output+=data;if(output.includes('\n'))resolve(JSON.parse(output.split('\n')[0]));});child.once('exit',code=>reject(Error(`fixture ${code} ${log}`)));});
 const base=`http://127.0.0.1:${fixture.port}`;for(let n=0;;n++){try{if((await fetch(base+'/api/status')).ok)break;}catch{}if(n>100)throw Error(log);await new Promise(r=>setTimeout(r,50));}
 const api=async(path,body)=>{const r=await fetch(base+path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const text=await r.text();assert.ok(r.ok,text);return JSON.parse(text);};
 browser=await chromium.launch({headless:true,...(process.env.BROWSER_EXECUTABLE?{executablePath:process.env.BROWSER_EXECUTABLE}:{})});page=await browser.newPage({viewport:{width:1360,height:900}});await page.addInitScript(id=>localStorage.setItem('guanlan.account',id),fixture.account_id);await page.goto(base+'/#holdings');await page.locator('#trade-form').waitFor();
 const checks=await verifyDialogIntents(page,fixture,api);await mkdir('test-results/twelfth',{recursive:true});await writeFile('test-results/twelfth/dialog-evidence.json',JSON.stringify({passed:true,platform:process.platform,browser:browser.version(),checks},null,2));console.log(JSON.stringify({passed:true,checks},null,2));
}catch(error){console.error(log);throw error;}finally{if(browser)await browser.close();child.kill('SIGTERM');if(child.exitCode===null)await new Promise(r=>child.once('exit',r));await rm(directory,{recursive:true,force:true});}
