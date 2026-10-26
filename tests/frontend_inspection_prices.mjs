// Regression for the read-only acceptance helper; explicit DOM fixtures, no DB/server/provider.
import { createRequire } from 'node:module';
import assert from 'node:assert/strict';
import { holdingNumbers } from '../scripts/inspect_fourth_browser.mjs';

const require = createRequire(import.meta.url);
const {chromium} = require('playwright');
const browser = await chromium.launch({headless:true, ...(process.env.BROWSER_EXECUTABLE
  ? {executablePath:process.env.BROWSER_EXECUTABLE} : {channel:'chrome'})});
try {
  const page = await browser.newPage();
  // Same direct-text + secondary-span structure as production positionsTable().
  await page.setContent(`<table><tbody>
    <tr><td>贵州茅台</td><td>200<span class="cell-secondary">股</span></td><td>1,349.98<span class="cell-secondary">10/26 15:34 · sina:public-website</span><span class="pill">已收盘</span></td></tr>
    <tr><td>平安银行</td><td>500<span class="cell-secondary">股</span></td><td>11.3<span class="cell-secondary">10/26 16:30 · sina:public-website</span><span class="pill">已收盘</span></td></tr>
    <tr><td>缺价</td><td>100<span class="cell-secondary">股</span></td><td>—<span class="cell-secondary">10/26 16:30 · 来源未知</span><span class="pill">获取失败</span></td></tr>
  </tbody></table>`);
  const rows = page.locator('tr');
  assert.ok((await rows.nth(0).locator('td').nth(2).textContent()).startsWith('1,349.9810/26'),
    'Fixture reproduces the old price/date concatenation');
  assert.deepEqual(await holdingNumbers(rows.nth(0)), {quantity:'200',price:'1,349.98'});
  assert.deepEqual(await holdingNumbers(rows.nth(1)), {quantity:'500',price:'11.3'});
  assert.deepEqual(await holdingNumbers(rows.nth(2)), {quantity:'100',price:'—'});
  console.log('PASS: inspection reads direct price/quantity text without date digits; missing price stays missing.');
} finally {
  await browser.close();
}
