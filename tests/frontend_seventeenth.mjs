import {spawn} from 'node:child_process';
import {mkdtemp, rm, mkdir, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import assert from 'node:assert/strict';
import {chromium} from 'playwright';

const directory = await mkdtemp(join(tmpdir(), 'guanlan-seventeenth-'));
const child = spawn(process.env.PYTHON || '.venv/bin/python', ['tests/seventeenth_browser_server.py', directory],
  {env:{...process.env, PYTHON_DOTENV_DISABLED:'1'}, stdio:['ignore','pipe','pipe']});
let browser, page, log = '';
child.stderr.on('data', data => log += data.toString());
const evidence = {platform:process.platform, realModelCalls:0};
try {
  const fixture = await new Promise((resolve, reject) => {
    let output = '';
    const timer = setTimeout(() => reject(Error('Server timeout ' + log)), 20000);
    child.stdout.on('data', data => {output += data; if(output.includes('\n')) {clearTimeout(timer); resolve(JSON.parse(output.split('\n')[0]));}});
    child.once('exit', code => {clearTimeout(timer); reject(Error(`Server exit ${code}: ${log}`));});
  });
  const base = `http://127.0.0.1:${fixture.port}`;
  browser = await chromium.launch({headless:true, ...(process.env.BROWSER_EXECUTABLE ? {executablePath:process.env.BROWSER_EXECUTABLE} : {})});
  page = await browser.newPage({viewport:{width:1440,height:1000}});
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(id => localStorage.setItem('guanlan.account', id), fixture.account_id);
  await page.goto(base + '/#settings');
  await page.locator('#settings-form').waitFor();
  const fields = ['max_model_calls', 'max_agent_turns', 'synthesis_max_calls'];
  for(const key of fields) {
    assert.equal(await page.locator(`#${key}`).inputValue(), '');
    assert.equal(await page.locator(`#${key}`).getAttribute('placeholder'), '不限次数');
  }
  const save = async () => {
    const pending = page.waitForResponse(r => r.url().endsWith('/api/settings') && r.request().method() === 'PUT');
    await page.locator('#settings-form button[type=submit]').click();
    const response = await pending;
    assert.equal(response.status(), 200);
    await page.locator('.loading-line').waitFor({state:'hidden'});
    return {sent:response.request().postDataJSON(), saved:await response.json()};
  };
  evidence.nullSave = await save();
  for(const key of fields) assert.equal(evidence.nullSave.sent[key], null);
  for(const [key, value] of fields.map((key, i) => [key, [44, 22, 6][i]])) await page.locator(`#${key}`).fill(String(value));
  evidence.finiteSave = await save();
  assert.deepEqual(fields.map(key => evidence.finiteSave.saved[key]), [44,22,6]);
  for(const key of fields) await page.locator(`#${key}`).fill('');
  evidence.clearedSave = await save();
  assert.ok(fields.every(key => evidence.clearedSave.saved[key] === null));
  await page.reload(); await page.locator('#settings-form').waitFor();
  for(const key of fields) assert.equal(await page.locator(`#${key}`).inputValue(), '');
  await page.locator('nav a[href="#research"]').click();
  const view = page.locator('article[data-report-id] .model-usage').first();
  await view.locator(':scope > summary').click();
  const text = await view.textContent();
  assert.ok(text.includes('加权缓存读取率 68%'), text);
  assert.ok(text.includes('计量完整 3 / 4，未知 1，覆盖率 75%'), text);
  assert.ok(text.includes('已报告总量：输入 27,000（覆盖 100%）'), text);
  assert.ok(text.includes('成对样本输入 25,000'), text);
  assert.ok(text.includes('输出 300（覆盖 75%）'), text);
  assert.ok(text.includes('推理 未知（覆盖 0%）'), text);
  const sessions = view.locator('details');
  assert.equal(await sessions.count(), 2);
  for(const detail of await sessions.all()) await detail.locator(':scope > summary').click();
  const rows = await view.locator('tbody tr').evaluateAll(rows => rows.map(row => [...row.cells].map(cell => cell.textContent)));
  assert.equal(rows.length, 4);
  assert.equal(rows[2][3], '0'); assert.equal(rows[3][3], '未知');
  assert.ok(rows[0][1].includes('会话首轮')); assert.ok(rows[1][1].includes('正常续接'));
  assert.ok(rows[3][1].includes('失败请求')); assert.ok(rows[3][1].includes('controlled_failure_without_cache_usage'));
  assert.equal(rows[0][4], '2,000'); assert.ok(rows.every(row => row[6].includes('费用未知')));
  // Polling and route changes preserve expanded session details and read keys.
  const detailKeys = await sessions.evaluateAll(items => items.map(item => item.dataset.key));
  await page.locator('nav a[href="#agents"]').click();
  await page.locator(`[data-task-id="${fixture.job_id}"] [data-action="job-result"]`).click();
  await page.locator('#dialog .model-usage').waitFor();
  assert.ok((await page.locator('#dialog .model-usage').textContent()).includes('加权缓存读取率 68%'));
  evidence.rows = rows; evidence.detailKeys = detailKeys;
  evidence.browser = browser.version(); evidence.passed = true;
  assert.deepEqual(errors, []);
  await mkdir('test-results/seventeenth', {recursive:true});
  await writeFile('test-results/seventeenth/browser-settings-usage.json', JSON.stringify(evidence, null, 2));
  console.log(JSON.stringify({passed:true, realModelCalls:0, requests:rows.length, sessions:detailKeys.length, browser:evidence.browser}));
} catch(error) {
  console.error(log);
  if(page) {await mkdir('test-results/seventeenth', {recursive:true}); await page.screenshot({path:'test-results/seventeenth/browser-failure.png', fullPage:true}).catch(() => {});}
  throw error;
} finally {
  if(browser) await browser.close();
  child.kill('SIGTERM');
  if(child.exitCode === null) await new Promise(resolve => child.once('exit', resolve));
  await rm(directory, {recursive:true, force:true});
}
