#!/usr/bin/env node
// Read-only UI/API inspection of an already running local evaluation server.
// No fixture seeding, POST/PUT, model probes, quote refreshes, or database writes.
import { createRequire } from 'node:module';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import { createHash } from 'node:crypto';

const require = createRequire(import.meta.url);
const result = {kind: 'read_only_chrome_ui_persisted_api_comparison', started_at: new Date().toISOString(),
  ok: false, checks: {}, observations: [], screenshots: [], errors: [], page_errors: [], blocked_mutations: [],
  comparison_scope: 'Displayed production pages compared with persisted FastAPI records; SQLite is not opened by this script.'};
let browser, outputDir;
const check = (name, passed, expected, actual, note) => {
  result.checks[name] = Boolean(passed);
  result.observations.push({name, passed: Boolean(passed), expected, actual, ...(note ? {note} : {})});
};
const sha256 = value => createHash('sha256').update(String(value)).digest('hex');
const normal = value => String(value ?? '').replace(/\s+/g, ' ').trim();
const label = value => ({completed:'已完成', current:'当前版本', stale:'已过期', expired:'已过期', failed:'失败',
  waiting_config:'等待配置', cancelled:'已取消', superseded:'版本已更新', queued:'排队中', running:'研究中',
  closed:'已收盘', ok:'已获取', error:'获取失败', error_cached:'刷新失败 · 使用缓存', missing:'未获取',
  unknown_time:'行情时间未知', delayed:'延迟', fresh:'已更新'}[value] || value);
const number = value => value === null || value === undefined || value === '' ? null : Number(String(value).replaceAll(',', ''));
// Production app/static/app.js state.pageSize; inspect the same active first page.
const recommendationPageSize = 10;
const recommendationPriceLabels = {met:'价格条件已满足', not_met:'价格条件未满足', stale:'报价过期 · 条件待更新',
  uncomputable:'价格条件暂不可计算', no_conditions:'未设价格条件', unknown:'价格条件尚未检查'};
const recommendationReviewLabels = {valid:'判断已复核', watching:'判断仍在跟踪', triggered:'判断仍在跟踪',
  needs_review:'判断待复核', expired:'已过期', revoked:'已撤销'};
const recommendationReview = (item, account) => {
  const saved = item.review_status || item.state || item.status || 'watching';
  if (['revoked','expired'].includes(saved)) return saved;
  if (item.valid_until && new Date(item.valid_until).getTime() <= Date.now()) return 'expired';
  if (item.input_version !== undefined && item.input_version !== account.version) return 'needs_review';
  return saved;
};
const recommendationSignature = envelope => ({total:envelope?.total, items:(envelope?.items || []).map(item=>({
  id:item.id, price_state:item.price_state, review_status:item.review_status, state:item.state, status:item.status,
  input_version:item.input_version, valid_until:item.valid_until}))});
const displayedDate = value => {
  if (!value) return '时间未知';
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return `${value}（源仅日期）`;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat('zh-CN', {
    timeZone:'Asia/Shanghai', year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit'}).format(date);
};

try {
  const [rawBase, rawOutput] = process.argv.slice(2);
  if (!rawBase || !rawOutput) throw new Error('Usage: node scripts/inspect_fourth_browser.mjs BASE_URL OUTPUT_DIR');
  const base = new URL(rawBase);
  if (!['http:', 'https:'].includes(base.protocol) || !['127.0.0.1','localhost','[::1]'].includes(base.hostname)
      || base.username || base.password || base.search || base.hash || !['','/'].includes(base.pathname)) {
    throw new Error('Inspection requires a local server origin without credentials or extra path.');
  }
  outputDir = resolve(rawOutput);
  await mkdir(outputDir, {recursive:true});
  result.base_url = base.origin;
  const get = async path => {
    const response = await fetch(base.origin + path, {signal:AbortSignal.timeout(20000)});
    if (!response.ok) throw new Error(`GET ${path}: HTTP ${response.status}`);
    return response.json();
  };
  const [accounts, globalRuns, universe, status] = await Promise.all([
    get('/api/accounts'), get('/api/global-research?limit=100'), get('/api/universe'), get('/api/status')]);
  let evaluation = {};
  try { evaluation = JSON.parse(await readFile(join(outputDir,'summary.json'),'utf8')); }
  catch (error) { if (error.code !== 'ENOENT') result.errors.push({stage:'read_evaluation_metadata', message:error.message}); }
  const select = key => accounts.find(account => account.id === evaluation.account_ids?.[key])
    || accounts.find(account => new RegExp(`(?:账户|验收)\\s*${key.toUpperCase()}$`).test(account.name));
  const chosen = {a:select('a'), b:select('b')};
  check('two_identified_accounts', Boolean(chosen.a && chosen.b && chosen.a.id !== chosen.b.id),
    'Two distinct A/B evaluation accounts', Object.fromEntries(Object.entries(chosen).map(([key, account]) => [key, account?.id || null])),
    'Account IDs come from evaluation summary.json when present, otherwise explicit account-name suffixes A/B; no arbitrary account substitution.');
  const snapshots = {};
  for (const [key, account] of Object.entries(chosen)) {
    if (!account) continue;
    const [analyses, jobs, records, recommendations] = await Promise.all([
      get(`/api/accounts/${account.id}/analyses?limit=100`),
      get(`/api/accounts/${account.id}/jobs?limit=100`),
      get(`/api/research?account_id=${account.id}&limit=30`),
      get(`/api/accounts/${account.id}/recommendations?view=active&envelope=true&limit=${recommendationPageSize}&offset=0`)]);
    snapshots[key] = {account, analyses, jobs, records, recommendations};
  }
  const apiSnapshot = {captured_at:new Date().toISOString(), accounts:snapshots, global_runs:globalRuns, universe,
    status:{mode:status.mode, timezone:status.timezone, worker:status.worker, native_search:status.native_search}};
  await writeFile(join(outputDir,'browser-api-snapshot.json'), JSON.stringify(apiSnapshot,null,2)+'\n');
  result.api_snapshot_file = join(outputDir,'browser-api-snapshot.json');
  const {chromium} = require('playwright');
  const launch = process.env.BROWSER_EXECUTABLE
    ? {executablePath:process.env.BROWSER_EXECUTABLE} : {channel:'chrome'};
  browser = await chromium.launch({headless:true, ...launch});
  result.browser = {engine:'chromium', requested_channel:launch.channel || null,
    executable_path:launch.executablePath || null, version:browser.version()};
  const newPage = async accountId => {
    const context = await browser.newContext({viewport:{width:1440,height:1050}, locale:'zh-CN', timezoneId:'Asia/Shanghai'});
    await context.route('**/*', async route => {
      const request = route.request();
      if (!['GET','HEAD'].includes(request.method())) {
        result.blocked_mutations.push({method:request.method(),url:request.url()});
        return route.abort('blockedbyclient');
      }
      if (new URL(request.url()).origin !== base.origin) return route.abort('blockedbyclient');
      return route.continue();
    });
    const page = await context.newPage();
    page.setDefaultTimeout(20000);
    page.on('pageerror', error => result.page_errors.push(error.message));
    await page.addInitScript(id => localStorage.setItem('guanlan.account',id),accountId);
    return {context,page};
  };
  const ready = async page => {
    await page.locator('#account-switch').waitFor();
    await page.locator('.loading-line').waitFor({state:'hidden'});
  };
  const navigate = async (page, route) => {
    const request = page.waitForResponse(response => response.url().endsWith('/api/status') && response.status() === 200);
    await page.locator(`nav a[href="#${route}"]`).click();
    await request;
    await ready(page);
  };
  const screenshot = async (page,name) => {
    const path = join(outputDir,`browser-${name}.png`);
    await page.screenshot({path,fullPage:true});
    result.screenshots.push(path);
  };
  const findReport = async (page,id) => {
    for (let attempt=0;attempt<10;attempt++) {
      const card = page.locator('.report').filter({has:page.locator('.analysis-meta .mono').getByText(id,{exact:true})});
      if (await card.count()) return card.first();
      const next = page.locator('[data-pagination="analyses"]').getByText('下一页',{exact:true});
      if (!await next.count() || await next.isDisabled()) return null;
      const response = page.waitForResponse(item => item.url().includes('/analyses?summary=true'));
      await next.click(); await response; await ready(page);
    }
    return null;
  };

  for (const [key,snapshot] of Object.entries(snapshots)) {
    const {account,analyses,jobs,recommendations} = snapshot;
    const {context,page} = await newPage(account.id);
    try {
      await page.goto(`${base.origin}/#overview`,{waitUntil:'domcontentloaded'}); await ready(page);
      await page.locator('.overview-bottom').waitFor();
      check(`${key}_selected_account`, await page.locator('#account-switch').inputValue() === account.id, account.id,
        await page.locator('#account-switch').inputValue());
      const footer = normal(await page.locator('.overview-bottom').innerText());
      check(`${key}_input_version_visible`, footer.includes(`账户输入版本 v${account.version}`), account.version,footer);
      for (const position of account.metrics?.positions || []) {
        const row = page.locator('table tbody tr').filter({has:page.locator('.security-code').filter({hasText:position.symbol})});
        const count = await row.count();
        check(`${key}_${position.symbol}_holding_visible`, count === 1,1,count);
        if (count !== 1) continue;
        const cells = await row.locator('td').allTextContents();
        const quantity = normal(cells[1]).match(/^-?[\d,.]+/)?.[0];
        const price = normal(cells[2]).match(/^-?[\d,.]+/)?.[0];
        check(`${key}_${position.symbol}_quantity_matches`, number(quantity) === number(position.quantity),position.quantity,quantity);
        check(`${key}_${position.symbol}_price_matches`, number(price) === number(position.price),position.price,price || null);
        check(`${key}_${position.symbol}_price_is_known`, number(position.price) !== null && number(position.price) > 0,
          'Positive known price; missing values never pass valuation acceptance',position.price);
        check(`${key}_${position.symbol}_quote_source_visible`, Boolean(position.quote?.source) && cells[2].includes(position.quote.source),position.quote?.source,cells[2]);
      }
      await screenshot(page,`${key}-overview`);
      await navigate(page,'research');
      const recommendationSection = page.locator('#active-recommendations');
      await recommendationSection.waitFor();
      const recommendationItems = Array.isArray(recommendations?.items) ? recommendations.items : [];
      const recommendationCards = recommendationSection.locator('.recommendation');
      const actualRecommendationIds = await recommendationCards.evaluateAll(cards=>cards.map(card=>card.dataset.recommendationId));
      const expectedRecommendationIds = recommendationItems.map(item=>item.id);
      const recommendationHeader = normal(await recommendationSection.locator('.card-header').innerText());
      const visibleRecommendationTotal = recommendationHeader.match(/共\s+(\d+)\s+条/)?.[1];
      check(`${key}_active_recommendation_api_page`, Array.isArray(recommendations?.items)
        && Number.isInteger(recommendations.total) && recommendations.total >= 0
        && recommendations.view === 'active' && recommendations.limit === recommendationPageSize && recommendations.offset === 0
        && recommendationItems.length === Math.min(recommendations.total,recommendationPageSize),
        {view:'active',limit:recommendationPageSize,offset:0},
        {view:recommendations?.view,limit:recommendations?.limit,offset:recommendations?.offset,
          total:recommendations?.total,returned:recommendationItems.length,ids:expectedRecommendationIds});
      check(`${key}_active_recommendation_first_page_ids`, JSON.stringify(actualRecommendationIds) === JSON.stringify(expectedRecommendationIds),
        expectedRecommendationIds,actualRecommendationIds,'Exact ordered first-page IDs; no requirement to manufacture recommendations.');
      check(`${key}_active_recommendation_count_matches`, actualRecommendationIds.length === recommendationItems.length
        && visibleRecommendationTotal !== undefined && Number(visibleRecommendationTotal) === recommendations?.total,
        {first_page:recommendationItems.length,total:recommendations?.total},
        {first_page:actualRecommendationIds.length,total:visibleRecommendationTotal === undefined ? null : Number(visibleRecommendationTotal)});
      const emptyMessageVisible = await recommendationSection.getByText('当前没有仍需关注的结构化建议。',{exact:true}).isVisible();
      const expectedEmpty = recommendations?.total === 0 && recommendationItems.length === 0;
      check(`${key}_active_recommendation_empty_state`, emptyMessageVisible === expectedEmpty
        && (!expectedEmpty || actualRecommendationIds.length === 0),
        {empty:expectedEmpty,total:recommendations?.total},{empty_message:emptyMessageVisible,visible_ids:actualRecommendationIds},
        expectedEmpty ? 'The persisted API contains no active recommendations; the UI must explicitly show the empty state.' : 'Existing active recommendations must be displayed, without an empty-state substitute.');
      for (const item of recommendationItems) {
        const index = actualRecommendationIds.indexOf(item.id);
        const card = index >= 0 ? recommendationCards.nth(index) : null;
        const actualReview = card ? normal(await card.locator('.split-heading .pill').innerText()) : null;
        const actualPrice = card ? normal(await card.locator(':scope > .source-meta').first().locator('.pill').innerText()) : null;
        const effectiveReview = recommendationReview(item,account);
        const expectedReview = recommendationReviewLabels[effectiveReview] || effectiveReview;
        const expectedPrice = recommendationPriceLabels[item.price_state] || recommendationPriceLabels.unknown;
        check(`${key}_recommendation_${item.id}_business_states`, actualReview === expectedReview && actualPrice === expectedPrice,
          {price_state:item.price_state,review_status:item.review_status,effective_review_status:effectiveReview,
            price_label:expectedPrice,review_label:expectedReview},
          {price_label:actualPrice,review_label:actualReview},
          'Price-condition state and investment-review state are separate; expired/version-mismatched display follows the production UI rules.');
      }
      const current = analyses.find(analysis => analysis.status === 'current' && analysis.input_version === account.version);
      check(`${key}_current_success_in_api`, Boolean(current),'Current successful report for latest account version',
        current ? {id:current.id,input_version:current.input_version,status:current.status} : analyses.map(a=>({id:a.id,input_version:a.input_version,status:a.status})));
      const latest = current || analyses[0];
      if (latest) {
        const card = await findReport(page,latest.id);
        check(`${key}_latest_report_visible`, Boolean(card),latest.id,card ? latest.id : null);
        if (card) {
          const text = normal(await card.innerText());
          const tokens = [...new Set(String(latest.report || latest.result?.report || '').replace(/https?:\/\/\S+/g,'').match(/[\p{L}\p{N}]{4,}/gu) || [])].slice(0,5);
          check(`${key}_report_version_matches`, text.includes(`输入版本 v${latest.input_version}`),latest.input_version,text.slice(0,400));
          const expired = latest.result?.valid_until && new Date(latest.result.valid_until) <= new Date();
          check(`${key}_report_status_matches`, normal(await card.locator('.report-header').innerText()).includes(label(expired ? 'expired' : latest.status)),
            label(expired ? 'expired' : latest.status),normal(await card.locator('.report-header').innerText()));
          check(`${key}_report_body_visible`, tokens.length > 0 && tokens.every(token=>text.includes(token)),tokens,
            {visible_chars:text.length,visible_sha256:sha256(text),raw_report_sha256:sha256(latest.report || latest.result?.report || '')},
            'Checks visible text anchors plus exact persisted report ID/version/status; hashes retain an auditable snapshot without treating Markdown as raw text.');
        }
      }
      await screenshot(page,`${key}-research`);
      if (key === 'a') {
        const older = analyses.filter(analysis=>analysis.input_version < account.version);
        check('a_previous_success_is_stale_in_api', older.length > 0 && older.every(analysis=>analysis.status === 'stale'),
          'At least one successful earlier input version, all marked stale',older.map(a=>({id:a.id,version:a.input_version,status:a.status})));
        const previous = older[0];
        if (previous) {
          const card = await findReport(page,previous.id);
          const text = card ? normal(await card.innerText()) : '';
          check('a_stale_history_label_visible', Boolean(card) && text.includes('账户已修改 · 历史版本') && text.includes('已过期'),
            {id:previous.id,status:'stale',version:previous.input_version},text.slice(0,600));
          await screenshot(page,'a-stale-history');
        }
      }
      await navigate(page,'agents');
      const job = jobs.find(item=>item.id === account.latest_job_id);
      const jobCard = page.locator('.job-card').filter({has:page.locator(`details[data-job-id="${account.latest_job_id}"]`)});
      const visibleJob = await jobCard.count() === 1 ? normal(await jobCard.innerText()) : '';
      check(`${key}_latest_job_visible_and_matches`, Boolean(job) && visibleJob.includes(`输入 v${job.input_version}`) && visibleJob.includes(label(job.state)),
        job ? {id:job.id,state:job.state,input_version:job.input_version} : null,visibleJob.slice(0,700));
      await screenshot(page,`${key}-jobs`);
    } catch (error) {
      check(`${key}_inspection_completed`,false,'No browser inspection error',error.message);
      await screenshot(page,`${key}-inspection-error`).catch(()=>{});
    } finally { await context.close(); }
  }

  const account = chosen.a || chosen.b;
  if (account) {
    const {context,page} = await newPage(account.id);
    try {
      await page.goto(`${base.origin}/#market`,{waitUntil:'domcontentloaded'}); await ready(page);
      await page.getByRole('heading',{name:'行情快照',exact:true}).waitFor();
      const quoteCard = page.locator('section.card').filter({has:page.getByRole('heading',{name:'行情快照',exact:true})});
      const symbols = [...new Set(Object.values(snapshots).flatMap(item=>item.account.holdings.map(h=>h.symbol)))];
      for (const symbol of symbols) {
        const quote = universe.quotes.find(item=>item.symbol === symbol);
        const row = quoteCard.locator('tbody tr').filter({has:page.locator('.security-code').getByText(symbol,{exact:true})});
        const cells = await row.count() === 1 ? await row.locator('td').allTextContents() : [];
        check(`market_${symbol}_quote_matches`, Boolean(quote) && cells.length >= 5 && number(cells[1]) === number(quote.price)
          && normal(cells[2]) === normal(displayedDate(quote.as_of)) && normal(cells[3]) === normal(quote.source)
          && normal(cells[4]).includes(label(quote.status)),
          quote ? {price:quote.price,as_of:quote.as_of,source:quote.source,status:quote.status} : null,cells.slice(1,5));
      }
      const runs = page.locator('details[data-key="global-runs"]');
      if (await runs.count()) await runs.locator(':scope > summary').click();
      const currentGlobal = globalRuns.find(run=>run.state === 'completed' && run.applicability === 'current');
      check('public_current_success_in_api',Boolean(currentGlobal),'Completed current public research',
        currentGlobal ? {id:currentGlobal.id,universe_version:currentGlobal.universe_version,state:currentGlobal.state} : globalRuns.map(r=>({id:r.id,state:r.state,applicability:r.applicability})));
      if (currentGlobal) {
        const button = page.locator(`[data-action="global-result"][data-id="${currentGlobal.id}"]`);
        check('public_current_run_visible',await button.count() === 1,currentGlobal.id,await button.count());
        if (await button.count()) {
          const detail = await get(`/api/global-research/${currentGlobal.id}`);
          await button.click(); await page.locator('#dialog').waitFor({state:'visible'});
          const text = normal(await page.locator('#dialog').innerText());
          const tokens = [...new Set(String(detail.result?.report || '').match(/[\p{L}\p{N}]{4,}/gu) || [])].slice(0,4);
          check('public_report_body_visible',tokens.length > 0 && tokens.every(token=>text.includes(token)),tokens,
            {visible_chars:text.length,visible_sha256:sha256(text)});
          await screenshot(page,'public-report');
          await page.locator('#dialog .dialog-header button').click();
        }
      }
      const records = snapshots.a?.records || snapshots.b?.records || [];
      const publicRecords = records.filter(record=>record.scope === 'public' || !record.account_id);
      const marketText = normal(await page.locator('main').innerText());
      check('public_research_records_visible',publicRecords.length > 0 && publicRecords.every(record=>marketText.includes(normal(record.title || record.symbol || record.security_id || record.sector || '公共研究'))),
        publicRecords.map(record=>({id:record.id,title:record.title})),marketText.slice(-2000));
      await screenshot(page,'market-quotes-public');
    } catch (error) {
      check('market_inspection_completed',false,'No browser inspection error',error.message);
      await screenshot(page,'market-inspection-error').catch(()=>{});
    } finally { await context.close(); }
  }

  // Do not mislabel normal asynchronous updates as a stable API/UI comparison.
  const finalAccounts = await get('/api/accounts');
  for (const [key,snapshot] of Object.entries(snapshots)) {
    const after = finalAccounts.find(item=>item.id === snapshot.account.id);
    const signature = item=>({id:item?.id,version:item?.version,latest_job_id:item?.latest_job_id});
    check(`${key}_input_stable_during_inspection`,JSON.stringify(signature(after)) === JSON.stringify(signature(snapshot.account)),
      signature(snapshot.account),signature(after),'A changed task/version requires a new inspection; this script never modifies the server.');
    const afterRecommendations = await get(`/api/accounts/${snapshot.account.id}/recommendations?view=active&envelope=true&limit=${recommendationPageSize}&offset=0`);
    check(`${key}_active_recommendations_stable_during_inspection`,
      JSON.stringify(recommendationSignature(afterRecommendations)) === JSON.stringify(recommendationSignature(snapshot.recommendations)),
      recommendationSignature(snapshot.recommendations),recommendationSignature(afterRecommendations),
      'A concurrent change to IDs, totals, or business states invalidates the stable UI/API comparison.');
  }
  check('no_browser_runtime_errors',result.page_errors.length === 0,[],result.page_errors);
  check('no_browser_mutating_requests',result.blocked_mutations.length === 0,[],result.blocked_mutations);
} catch (error) {
  result.errors.push({stage:'inspection',type:error.name,message:error.message});
  check('inspection_started_and_completed',false,'Inspection executable without infrastructure errors',error.message);
} finally {
  await browser?.close().catch(error=>result.errors.push({stage:'close_browser',message:error.message}));
  result.finished_at = new Date().toISOString();
  result.ok = Object.keys(result.checks).length > 0 && Object.values(result.checks).every(Boolean) && result.errors.length === 0;
  result.inconsistencies = result.observations.filter(item=>!item.passed);
  if (outputDir) await writeFile(join(outputDir,'browser-inspection.json'),JSON.stringify(result,null,2)+'\n').catch(error=>{
    result.ok=false;result.errors.push({stage:'write_report',message:error.message});
  });
  process.stdout.write(JSON.stringify(result)+'\n');
  process.exitCode = result.ok ? 0 : 2;
}
