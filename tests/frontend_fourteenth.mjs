// Actual production ModelClient fixture -> Agent -> Service -> SQLite -> browser.
import {spawn} from 'node:child_process';
import {createRequire} from 'node:module';
import {mkdtemp,rm,mkdir,writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import assert from 'node:assert/strict';
const {chromium}=createRequire(import.meta.url)('playwright');
const directory=await mkdtemp(join(tmpdir(),'guanlan-fourteenth-'));
const child=spawn(process.env.PYTHON||'python',['tests/fourteenth_browser_server.py',directory],{env:{...process.env,PYTHON_DOTENV_DISABLED:'1'},stdio:['ignore','pipe','pipe']});
let browser,page,log='';
child.stderr.on('data',data=>log+=data.toString());
try {
  const fixture=await new Promise((resolve,reject)=>{
    let output='';const timer=setTimeout(()=>reject(Error(log||'fixture startup timeout')),30000);
    child.stdout.on('data',data=>{output+=data.toString();if(output.includes('\n')){clearTimeout(timer);resolve(JSON.parse(output.split('\n')[0]));}});
    child.once('exit',code=>{clearTimeout(timer);reject(Error(`fixture exited ${code}: ${log}`));});
  });
  const base=`http://127.0.0.1:${fixture.port}`;
  browser=await chromium.launch({headless:true,...(process.env.BROWSER_EXECUTABLE?{executablePath:process.env.BROWSER_EXECUTABLE}:{})});
  page=await browser.newPage({viewport:{width:1360,height:900}});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(id=>localStorage.setItem('guanlan.account',id),fixture.account_id);
  await page.goto(base+'/#agents');
  const job=page.locator(`[data-task-id="${fixture.failed_job_id}"]`);
  await job.waitFor();
  assert.ok((await job.textContent()).includes('交付未完成'));
  assert.ok((await job.textContent()).includes('final_submission_missing'));
  assert.equal(await job.locator('[data-action="retry-job"]').count(),1);
  const saved=await (await fetch(base+'/api/jobs/'+fixture.failed_job_id)).json();
  assert.equal(saved.state,'failed');assert.equal(saved.delivery.status,'incomplete');
  await page.locator('nav a[href="#research"]').click();
  const report=page.locator(`[data-report-id="${fixture.report_id}"]`);
  await report.waitFor();
  assert.ok((await report.textContent()).includes('有限报告已发布'));
  assert.ok((await report.textContent()).includes('保留现金100元'));
  assert.ok((await report.textContent()).includes('证券专题尚未开展'));
  assert.ok(!(await report.textContent()).includes('正在整理最终报告'));
  const reports=await (await fetch(base+`/api/accounts/${fixture.account_id}/analyses`)).json();
  assert.equal(reports.length,1);assert.equal(reports[0].status,'current');
  assert.equal(reports[0].result.delivery.content_assessment,'not_evaluated');
  // Reload into the second isolated account: production HTTP fixture first
  // fails scope preflight, then corrects within the same Agent budget.
  await page.close();
  page=await browser.newPage({viewport:{width:1360,height:900}});
  page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(id=>localStorage.setItem('guanlan.account',id),fixture.structured_account_id);
  await page.goto(base+'/#research');
  const structured=page.locator(`[data-report-id="${fixture.structured_report_id}"]`);
  await structured.waitFor();
  const receipt=structured.locator('.delivery-application');
  await receipt.locator('summary').click();
  assert.ok((await receipt.textContent()).includes('结构化提交 2 项 · 已应用 1 项'));
  assert.ok((await receipt.textContent()).includes('建议 · SZ:159599：已应用'));
  assert.ok((await receipt.textContent()).includes('预测 · SZ:159599 · 1日：待数据，未发布'));
  const structuredJob=await (await fetch(base+'/api/jobs/'+fixture.structured_job_id)).json();
  assert.equal(structuredJob.delivery_application.suggestions[0].status,'applied');
  assert.equal(structuredJob.delivery_application.forecasts[0].status,'waiting_data');
  await page.locator('nav a[href="#agents"]').click();
  const taskReceipt=page.locator(`[data-task-id="${fixture.structured_job_id}"] .delivery-application`);
  await taskReceipt.locator('summary').click();
  assert.ok((await taskReceipt.textContent()).includes('待数据，未发布'));
  assert.deepEqual(errors,[]);
  await mkdir('test-results/fourteenth',{recursive:true});
  await page.screenshot({path:'test-results/fourteenth/delivery.png',fullPage:true});
  await writeFile('test-results/fourteenth/browser-evidence.json',JSON.stringify({kind:'controlled_production_full_chain',passed:true,...fixture,browser:browser.version(),scenarios:['failed_submission_api_page','finite_report_body_scope','old_current_report_preserved','explicit_retry_visible','scope_corrected_before_acceptance','applied_suggestion_receipt','waiting_data_forecast_receipt','job_and_report_receipts_agree'],console_errors:errors},null,2));
  console.log('Fourteenth production delivery browser regression passed; paid model requests: 0');
} catch(error){console.error(log);throw error;}
finally{if(browser)await browser.close();child.kill('SIGTERM');if(child.exitCode===null)await new Promise(resolve=>child.once('exit',resolve));await rm(directory,{recursive:true,force:true});}
