import {chromium} from 'playwright';
import fs from 'node:fs';
import path from 'node:path';
const out=path.resolve(process.argv[2]);
const readyPath=path.join(out,'browser-ready.json');
for(let i=0;!fs.existsSync(readyPath);i++){if(i>=1000)throw Error('Timed out waiting for completed acceptance');await new Promise(r=>setTimeout(r,1000));}
const ready=JSON.parse(fs.readFileSync(readyPath,'utf8'));
const browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_EXECUTABLE || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
const page=await browser.newPage({viewport:{width:1440,height:1100}}), errors=[];
page.on('pageerror',e=>errors.push(e.message));
const results=[];
try{
  await page.goto(ready.url);
  await page.waitForSelector('#account-switch');
  await page.selectOption('#account-switch',ready.account_id);
  for(const route of ['overview','research','agents','forecasts','settings','account-models']){
    await page.goto(ready.url+'/#'+route);
    await page.locator(`nav a[href="#${route}"][aria-current="page"]`).waitFor();
    await page.locator('.loading-line').waitFor({state:'hidden'});
    await page.waitForTimeout(350);
    if(route==='forecasts'){await page.selectOption('#forecast-filter-form select[name=kind]','exploratory');await page.locator('#forecast-filter-form button[type=submit]').click();await page.locator('.loading-line').waitFor({state:'hidden'});}
    await page.locator('details').evaluateAll(items=>items.forEach(item=>item.open=true));
    const text=await page.locator('main').innerText();
    await page.screenshot({path:path.join(out,'page-'+route+'.png'),fullPage:true});
    results.push({route,visible_text:text.slice(0,20000)});
  }
  fs.writeFileSync(path.join(out,'page-inspection.json'),JSON.stringify({kind:'real_independent_service_read_only_browser',errors,pages:results},null,2));
}finally{await browser.close();}
console.log(JSON.stringify({pages:results.length,errors}));
