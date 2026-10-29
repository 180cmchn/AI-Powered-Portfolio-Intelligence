// Real FastAPI/SQLite regression; data and clocks are explicitly synthetic.
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { mkdtemp, rm, mkdir, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import assert from 'node:assert/strict';
const { chromium }=createRequire(import.meta.url)('playwright');
const directory=await mkdtemp(join(tmpdir(),'guanlan-seventh-'));
const child=spawn(process.env.PYTHON || 'python',['tests/seventh_browser_server.py',directory],{env:{...process.env,PYTHON_DOTENV_DISABLED:'1'},stdio:['ignore','pipe','pipe']});
let browser,page,diagnostic='';
child.stderr.on('data',data=>diagnostic+=data.toString());
try {
  const fixture=await new Promise((resolve,reject)=>{
    let output='';const timer=setTimeout(()=>reject(Error(diagnostic || 'fixture timeout')),30000);
    child.stdout.on('data',data=>{output+=data.toString();if(output.includes('\n')){clearTimeout(timer);resolve(JSON.parse(output.split('\n')[0]));}});
    child.once('exit',code=>{clearTimeout(timer);reject(Error(`fixture exited ${code}: ${diagnostic}`));});
  });
  const base=`http://127.0.0.1:${fixture.port}`;
  browser=await chromium.launch({headless:true,...(process.env.BROWSER_EXECUTABLE?{executablePath:process.env.BROWSER_EXECUTABLE}:{})});
  page=await browser.newPage({viewport:{width:1440,height:1050}});
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.addInitScript(id=>localStorage.setItem('guanlan.account',id),fixture.account_a);
  await page.goto(base+'/#forecasts');
  await page.locator('#forecast-filter-form').waitFor();
  const form=page.locator('#forecast-filter-form');
  await form.locator('[name="model_id"]').fill('fixture/seventh-A');
  await form.getByRole('button',{name:'筛选',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#forecast-attempt-statistics')?.textContent.includes('未发布样本'));
  const data=await (await fetch(base+`/api/forecasts?account_id=${fixture.account_a}&model_id=fixture%2Fseventh-A`)).json();
  assert.equal(data.page.total,3);
  assert.deepEqual(data.statistics.groups.map(g=>g.states),[{failed:1},{failed:1},{failed:1}]);
  assert.ok((await page.locator('main').textContent()).includes('3 个预期样本'));
  assert.ok((await page.locator('#forecast-attempt-statistics').textContent()).includes('1 次'));
  await page.locator('[data-sixth-action="forecast-batch-detail"]').first().click();
  await page.locator('#dialog').getByText('受控前置失败；未发出模型请求',{exact:false}).first().waitFor();
  assert.ok((await page.locator('#dialog').textContent()).includes('fixture/seventh-A'));
  await page.locator('#dialog .dialog-header button').click();
  const windows=page.locator('#forecast-window-records');
  assert.ok((await windows.textContent()).includes('错过已知窗口'));
  assert.ok((await windows.textContent()).includes('不归到当前模型'));
  const firstWindow=await windows.locator('tbody tr').first().textContent();
  let releaseWindow, observeWindow;
  const windowGate=new Promise(resolve=>{releaseWindow=resolve;});
  const windowStarted=new Promise(resolve=>{observeWindow=resolve;});
  await page.route('**/api/forecast-window-runs?*',async route=>{observeWindow();await windowGate;await route.continue();},{times:1});
  await windows.locator('[data-sixth-action="window-next"]').click();
  let gateTimer;
  try {
    await Promise.race([windowStarted,new Promise((_,reject)=>{gateTimer=setTimeout(()=>reject(Error('window request did not start')),10000);})]);
    await form.locator('[name="kind"]').selectOption('exploratory');
  } finally {clearTimeout(gateTimer);releaseWindow();}
  await page.waitForFunction(first=>document.querySelector('#forecast-window-records tbody tr')?.textContent!==first,firstWindow);
  assert.equal(await form.locator('[name="kind"]').inputValue(),'exploratory','in-flight window read must preserve the newer filter draft');
  await form.locator('[name="kind"]').selectOption('exploratory');
  await form.getByRole('button',{name:'筛选',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('main')?.textContent.includes('36 个预期样本'));
  assert.equal(await page.locator('.forecast-accuracy').count(),3,'same scoring rule groups all 12 exploration batches');
  assert.ok((await page.locator('.forecast-accuracy').first().textContent()).includes('暂无已评估样本'));
  assert.ok((await page.locator('main').textContent()).includes('统计规则身份'));
  await page.locator('[data-sixth-action="forecast-next"]').click();
  await page.waitForFunction(()=>document.querySelector('main')?.textContent.includes('第 2 页'));
  assert.equal(await page.locator('[data-sixth-action="forecast-detail"]').count(),6);
  const compare=page.locator('#forecast-comparison-form');
  await compare.locator(`input[value="${fixture.account_a}"]`).check();
  await compare.locator(`input[value="${fixture.account_b}"]`).check();
  await compare.getByRole('button',{name:'比较共同样本',exact:true}).click();
  await page.getByText('探索没有预先锁定共同信息窗口，累计展示全部记录但不作跨账户配对',{exact:false}).waitFor();
  const matched=await (await fetch(base+`/api/forecasts/compare?kind=exploratory&account_ids=${fixture.account_a}&account_ids=${fixture.account_b}`)).json();
  assert.equal(matched.overlap_expected,0);
  assert.deepEqual(matched.unmatched_counts,{[fixture.account_a]:36,[fixture.account_b]:3});
  assert.deepEqual(errors,[]);
  await mkdir('test-results',{recursive:true});
  await page.screenshot({path:'test-results/seventh-forecasts.png',fullPage:true});
  await writeFile('test-results/seventh-browser.json',JSON.stringify({kind:'engineering_production_API_temporary_SQLite',passed:true,console_errors:errors,scenarios:['failed-model-filter','attempt-details','missed-window-and-paging','exploration-many-batches','canonical-rule-groups','empty-mature-samples','prediction-paging','no-post-hoc-pairing']},null,2));
  console.log('Seventh production browser regressions passed');
} catch(error) {
  await mkdir('test-results',{recursive:true});
  if(page)await page.screenshot({path:'test-results/seventh-failure.png',fullPage:true});
  console.error(diagnostic);throw error;
} finally {
  if(browser)await browser.close();child.kill('SIGTERM');
  await new Promise(resolve=>{if(child.exitCode!==null)return resolve();child.once('exit',resolve);setTimeout(resolve,3000);});
  await rm(directory,{recursive:true,force:true});
}
