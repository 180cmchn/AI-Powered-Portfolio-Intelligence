// Real FastAPI + temporary SQLite + Chromium verification for the unified
// custom selects, searchable model picker, modal confirmations and visual polish.
// No model worker or external network call is started; fixtures are explicit.
import { spawn } from 'node:child_process';
import { mkdtemp, rm, mkdir, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { choiceTrigger, openChoice, selectChoice, selectSearchableChoice, waitChoiceReady } from './choice_helper.mjs';

const directory = await mkdtemp(join(tmpdir(), 'guanlan-nineteenth-'));
const child = spawn(process.env.PYTHON || 'python', ['tests/thirteenth_browser_server.py', directory], {
  env: { ...process.env, PYTHON_DOTENV_DISABLED: '1' },
  stdio: ['ignore', 'pipe', 'pipe'],
});
let browser, page, diagnostic = '';
child.stderr.on('data', data => { diagnostic += data.toString(); });
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const evidence = {
  kind: 'real_fastapi_temporary_sqlite_chromium',
  checks: [], routes: [], layout: {}, modelSearch: {}, details: {}, dialogs: {},
  screenshots: [], errors: [],
  platform: process.platform, executable: process.env.BROWSER_EXECUTABLE || 'Playwright Chromium',
};

try {
  const fixture = await new Promise((resolve, reject) => {
    let output = '';
    const timer = setTimeout(() => reject(Error('fixture timeout ' + diagnostic)), 30000);
    child.stdout.on('data', data => {
      output += data.toString();
      if (output.includes('\n')) { clearTimeout(timer); resolve(JSON.parse(output.split('\n')[0])); }
    });
    child.once('exit', code => { clearTimeout(timer); reject(Error(`fixture exited ${code} ${diagnostic}`)); });
  });
  const base = `http://127.0.0.1:${fixture.port}`;
  for (let n = 0; ; n++) {
    try { if ((await fetch(base + '/api/status')).ok) break; } catch {}
    if (n > 100) throw Error('server unavailable ' + diagnostic);
    await sleep(50);
  }
  const api = async (path, body, method = 'POST') => {
    const response = await fetch(base + path, body === undefined ? {} : {
      method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    const text = await response.text();
    assert.ok(response.ok, `${method} ${path}: ${response.status} ${text}`);
    return text ? JSON.parse(text) : null;
  };
  const providers = await api('/api/providers', undefined, 'GET');
  const provider = providers.find(item => item.catalog?.models?.length) || providers[0];
  assert.ok(provider?.id, 'fixture must expose one provider');
  const modelName = provider.catalog.models[0].id;
  const accounts = await api('/api/accounts', undefined, 'GET');
  assert.equal(accounts.length, 2);
  await api('/__fixture/material', { classification: 'official_disclosure' });
  await api('/__fixture/material', { classification: 'news' });

  let importRequests = 0, deleteRequests = 0, archiveRequests = 0, reverseRequests = 0;
  browser = await chromium.launch({ headless: true, ...(process.env.BROWSER_EXECUTABLE ? { executablePath: process.env.BROWSER_EXECUTABLE } : {}) });
  evidence.browserVersion = browser.version();
  page = await browser.newPage({ viewport: { width: 1360, height: 900 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (request.method() === 'DELETE' && /^\/api\/providers\/[^/]+\/credential$/.test(path)) deleteRequests += 1;
    if (request.method() === 'POST' && /^\/api\/providers\/[^/]+\/archive$/.test(path)) archiveRequests += 1;
    if (request.method() === 'POST' && /^\/api\/accounts\/[^/]+\/ledger\/[^/]+\/reverse$/.test(path)) reverseRequests += 1;
  });
  // Native confirm/prompt must never be reached: record then fail loudly.
  await page.addInitScript(() => {
    window.__nativeDialogs = [];
    window.confirm = message => { window.__nativeDialogs.push(['confirm', String(message)]); throw new Error('native confirm called: ' + message); };
    window.prompt = (message, value) => { window.__nativeDialogs.push(['prompt', String(message), String(value ?? '')]); throw new Error('native prompt called: ' + message); };
  });
  await page.route('**/api/environment-connection**', async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    if (path === '/api/environment-connection/import' && request.method() === 'POST') {
      importRequests += 1;
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ id: provider.id, catalog: { status: 'success', models: [] } }) });
    }
    if (path === '/api/environment-connection' && request.method() === 'GET') {
      return route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          status: 'changed', action: '检测到环境连接目标变化，确认后显式导入',
          base_url: 'https://fixture.invalid/v1', protocol: 'responses', key_present: true,
          can_import: true, changed: true, preview_id: provider.id,
          source: { dotenv_path: '.env', restart_required: false, fields: {} },
        }),
      });
    }
    return route.continue();
  });
  await page.addInitScript(id => localStorage.setItem('guanlan.account', id), fixture.account_id);
  const routes = ['overview', 'holdings', 'market', 'research', 'agents', 'providers', 'roles', 'account-models', 'forecasts', 'settings'];
  const markers = {
    overview: '.page-heading', holdings: '#trade-form', market: '#universe-form', research: '#material-search-form',
    agents: '.page-heading', providers: '#provider-connection-form',
    roles: '.role-model-form[data-account-id=""]',
    'account-models': `.role-model-form[data-account-id="${fixture.account_id}"]`,
    forecasts: '#forecast-filter-form', settings: '#settings-form',
  };
  const go = async routeName => {
    await page.locator(`nav a[href="#${routeName}"]`).click();
    await page.locator(`nav a[href="#${routeName}"][aria-current="page"]`).waitFor();
    await page.locator(markers[routeName]).first().waitFor();
    if (routeName === 'holdings') await page.locator('details[data-key^="account-metadata:"]').evaluate(el => { el.open = true; });
    await page.locator('.loading-line').waitFor({ state: 'hidden' });
  };
  const assertNoNativeDialogs = async label => {
    assert.deepEqual(await page.evaluate(() => window.__nativeDialogs), [], `${label}: native confirm/prompt must never be called`);
  };
  const shot = async (name, options = {}) => {
    const path = `test-results/nineteenth/${name}.png`;
    await page.screenshot({ path, ...options });
    evidence.screenshots.push(path);
  };

  await page.goto(base + '/#roles');
  await page.locator('.role-card').first().waitFor();
  await page.locator('.loading-line').waitFor({ state: 'hidden' });
  await mkdir('test-results/nineteenth', { recursive: true });

  // 1. Every key route: zero visible native selects and zero native dialogs.
  for (const routeName of routes) {
    await go(routeName);
    const probe = await page.evaluate(() => ({
      nonNative: document.querySelectorAll('select:not([data-choice-native])').length,
      natives: [...document.querySelectorAll('select[data-choice-native]')].map(select => ({
        id: select.id, name: select.name, hidden: select.hidden,
        tabindex: select.getAttribute('tabindex'), ariaHidden: select.getAttribute('aria-hidden'),
        visible: select.getClientRects().length > 0,
        hasTrigger: Boolean(document.getElementById(`${select.id}-control`)),
      })),
      dialogs: window.__nativeDialogs.slice(),
    }));
    assert.equal(probe.nonNative, 0, `${routeName}: no bare native select may remain`);
    assert.ok(probe.natives.length >= 1, `${routeName}: fixture must render at least one choice field`);
    for (const item of probe.natives) {
      assert.equal(item.hidden, true, `${routeName} #${item.id}: native select must be hidden`);
      assert.equal(item.tabindex, '-1', `${routeName} #${item.id}: native select must be out of tab order`);
      assert.equal(item.ariaHidden, 'true');
      assert.equal(item.visible, false, `${routeName} #${item.id}: native select must have no layout box`);
      assert.equal(item.hasTrigger, true, `${routeName} #${item.id}: a visible custom trigger must exist`);
    }
    assert.deepEqual(probe.dialogs, [], `${routeName}: native confirm/prompt must never be called`);
    evidence.routes.push({ route: routeName, ...probe });
  }
  evidence.checks.push('10 routes: zero bare/visible native select; every select is data-choice-native hidden tabindex=-1 with a trigger; zero native confirm/prompt calls');

  // 2. Role & account-model cards: three triggers share one row, no external search input.
  const measureCards = async routeName => {
    await go(routeName);
    await page.locator('.role-card').first().waitFor();
    await page.locator('.loading-line').waitFor({ state: 'hidden' });
    const rows = await page.locator('.role-card').evaluateAll(cards => cards.map(card => {
      const form = card.querySelector('.role-model-form');
      const items = ['provider_id', 'model_id', 'reasoning_effort'].map(name => {
        const select = form.querySelector(`select[name="${name}"]`);
        const field = select.closest('[data-choice-field]');
        const trigger = field.querySelector('[data-choice-trigger]');
        const label = field.querySelector('label');
        const box = trigger.getBoundingClientRect();
        return {
          name, top: Number(box.top.toFixed(2)), height: Number(box.height.toFixed(2)), width: Number(box.width.toFixed(2)),
          labelTop: label ? Number(label.getBoundingClientRect().top.toFixed(2)) : null,
          disabled: trigger.disabled,
        };
      });
      return {
        roleId: card.dataset.roleId, accountId: form.dataset.accountId,
        items,
        externalSearchInputs: form.querySelectorAll('input[data-model-filter], .choice-popup').length,
      };
    }));
    assert.equal(rows.length, 6, `${routeName}: six unified role cards`);
    for (const row of rows) {
      const tops = row.items.map(item => item.top), heights = row.items.map(item => item.height);
      const labelTops = row.items.map(item => item.labelTop);
      assert.ok(Math.max(...tops) - Math.min(...tops) < 1, `${routeName}/${row.roleId}: trigger tops must align <1px: ${JSON.stringify(row.items)}`);
      assert.ok(Math.max(...heights) - Math.min(...heights) < 1, `${routeName}/${row.roleId}: trigger heights must align <1px`);
      assert.ok(Math.max(...labelTops) - Math.min(...labelTops) < 1, `${routeName}/${row.roleId}: labels must align <1px`);
      assert.equal(row.externalSearchInputs, 0, `${routeName}/${row.roleId}: no old standalone model search input`);
    }
    evidence.layout[routeName] = rows;
  };
  await measureCards('roles');
  await shot('roles-desktop');
  await measureCards('account-models');
  for (const row of evidence.layout['account-models']) assert.equal(row.accountId, fixture.account_id);
  await shot('account-models-desktop');
  evidence.checks.push('roles + account-models: 6 cards each, provider/model/effort triggers and labels top+height differences <1px, no standalone legacy search input');

  // Provider popup screenshot (non-searchable) and model searchable popup.
  await go('roles');
  const manager = page.locator('.role-model-form[data-role-id="manager"]');
  await manager.waitFor();
  const providerPopup = await openChoice(manager, '[name="provider_id"]');
  assert.equal(await providerPopup.popup.evaluate(el => el.classList.contains('is-searchable')), false);
  await shot('roles-provider-popup');
  await page.keyboard.press('Escape');

  // Provider switch must reload the model catalog and sync the trigger.
  const modelTrigger = choiceTrigger(manager, '[name="model_id"]');
  await selectChoice(manager, '[name="provider_id"]', '');
  await page.waitForFunction(() => document.querySelector('.role-model-form[data-role-id="manager"] select[name="model_id"]')?.disabled === true);
  assert.equal(await modelTrigger.isDisabled(), true, 'clearing the provider disables the model trigger');
  await selectChoice(manager, '[name="provider_id"]', provider.id);
  await waitChoiceReady(manager, '[name="model_id"]');
  const syncedValues = await manager.locator('[name="model_id"] option').evaluateAll(options => options.map(option => option.value));
  assert.ok(syncedValues.includes(modelName), `model catalog must resync: ${JSON.stringify(syncedValues)}`);
  evidence.modelSearch.catalogAfterProviderSwitch = syncedValues;

  // Shared helper contract: search the built-in box, prove the rows are
  // filtered, click the target option and check native value + visible text.
  const helperPick = await selectSearchableChoice(manager, '[name="model_id"]', modelName.slice(0, 9), modelName);
  assert.equal(helperPick.text, modelName);
  assert.equal(await manager.locator('[name="model_id"]').inputValue(), modelName);
  evidence.modelSearch.helper = { text: helperPick.text, filtered: helperPick.filtered };

  const modelSearch = await openChoice(manager, '[name="model_id"]');
  assert.equal(await modelSearch.popup.evaluate(el => el.classList.contains('is-searchable')), true);
  const filter = modelSearch.popup.locator('[data-model-filter]');
  assert.equal(await filter.evaluate(el => document.activeElement === el), true, 'focus must enter the built-in model search');
  const nativeBefore = await manager.locator('[name="model_id"] option').evaluateAll(options => options.map(option => option.value));
  const selectedBefore = await manager.locator('[name="model_id"]').inputValue();
  await filter.fill(modelName.slice(0, 9));
  const filtered = await modelSearch.popup.locator('.choice-option').evaluateAll(nodes => nodes.map(node => node.dataset.value));
  assert.deepEqual(filtered, [modelName], 'keyword must filter to the discovered model only');
  await filter.fill('does-not-exist');
  assert.equal(await modelSearch.popup.locator('.choice-option').count(), 0, 'unknown query must not create a model option');
  assert.equal(await modelSearch.popup.locator('.choice-empty').isVisible(), true, 'empty state must be exposed');
  const nativeDuringEmpty = await manager.locator('[name="model_id"] option').evaluateAll(options => options.map(option => option.value));
  assert.deepEqual(nativeDuringEmpty, nativeBefore, 'filtering must never delete real native options');
  await page.keyboard.press('Enter');
  assert.equal(await manager.locator('[name="model_id"]').inputValue(), selectedBefore, 'Enter with no matching row must not change the binding');
  assert.equal(await modelSearch.popup.isVisible(), true, 'Enter with no match must leave the popup open');
  await filter.fill('');
  assert.equal(await modelSearch.popup.locator('.choice-option').count(), nativeBefore.length, 'clearing the query restores every discovered option');
  await page.keyboard.press('Escape');
  evidence.modelSearch.states = { before: nativeBefore, filtered, emptyNative: nativeDuringEmpty, restoredCount: nativeBefore.length, filteredOptionCount: 1 };

  // Keyboard-only selection after clearing the binding, then persist via save.
  await selectChoice(manager, '[name="model_id"]', '');
  assert.equal(await manager.locator('[name="model_id"]').inputValue(), '');
  const keyboardSearch = await openChoice(manager, '[name="model_id"]');
  assert.equal(await keyboardSearch.popup.locator('[data-model-filter]').evaluate(el => document.activeElement === el), true);
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('Enter');
  assert.equal(await keyboardSearch.popup.count(), 0);
  assert.equal(await manager.locator('[name="model_id"]').inputValue(), modelName, 'ArrowDown + Enter must select the discovered model');
  assert.equal((await choiceTrigger(manager, '[name="model_id"]').locator('[data-choice-value]').textContent()).trim(), modelName);
  const savedRole = page.waitForResponse(response => response.url().endsWith('/api/agent-roles/manager') && response.request().method() === 'PUT');
  await manager.locator('button[type="submit"]').click();
  assert.equal((await savedRole).status(), 200);
  await page.locator('.loading-line').waitFor({ state: 'hidden' });
  await page.reload();
  await page.locator('.role-model-form[data-role-id="manager"]').waitFor();
  await page.locator('.loading-line').waitFor({ state: 'hidden' });
  assert.equal(await page.locator('.role-model-form[data-role-id="manager"] [name="model_id"]').inputValue(), modelName, 'saved model must survive reload');
  evidence.checks.push('provider switch disables then resyncs model catalog; search focuses, filters, shows empty state, never mutates native options or creates model names; ArrowDown+Enter selection saves and survives reload');

  // Visual evidence with the saved binding: searchable model popup filtered.
  await go('roles');
  const savedManager = page.locator('.role-model-form[data-role-id="manager"]');
  await savedManager.waitFor();
  const savedSearch = await openChoice(savedManager, '[name="model_id"]');
  await savedSearch.popup.locator('[data-model-filter]').fill(modelName.slice(0, 9));
  await shot('roles-model-search-popup');
  await page.keyboard.press('Escape');

  // Mobile layout: same three-column controls stack without horizontal overflow.
  await page.setViewportSize({ width: 390, height: 844 });
  await go('roles');
  await page.locator('.role-card').first().waitFor();
  await page.locator('.role-card').first().scrollIntoViewIfNeeded();
  await page.locator('.loading-line').waitFor({ state: 'hidden' });
  const overflow = await page.evaluate(() => ({
    documentWidth: document.documentElement.scrollWidth,
    bodyWidth: document.body.scrollWidth,
    viewport: innerWidth,
  }));
  assert.ok(overflow.documentWidth <= overflow.viewport + 1, `mobile page must not overflow horizontally: ${JSON.stringify(overflow)}`);
  // F-06: measure the actual footer box, not only document scrollWidth. The
  // full-width footer must sit on the card content box (inside the 1px border).
  const mobileFooter = await page.locator('.role-card').first().evaluate(card => {
    const style = getComputedStyle(card);
    const box = card.getBoundingClientRect();
    const actions = card.querySelector('.role-model-form > .form-actions');
    const footer = actions.getBoundingClientRect();
    const borderLeft = parseFloat(style.borderLeftWidth) || 0;
    const borderRight = parseFloat(style.borderRightWidth) || 0;
    const borderBottom = parseFloat(style.borderBottomWidth) || 0;
    return {
      viewport: innerWidth,
      card: { left: box.left, right: box.right, bottom: box.bottom, width: box.width },
      footer: { left: footer.left, right: footer.right, bottom: footer.bottom, width: footer.width },
      border: { left: borderLeft, right: borderRight, bottom: borderBottom },
      delta: {
        left: Math.abs(footer.left - (box.left + borderLeft)),
        right: Math.abs((box.right - borderRight) - footer.right),
        bottom: Math.abs((box.bottom - borderBottom) - footer.bottom),
      },
      overflow: {
        left: box.left - footer.left,
        right: footer.right - box.right,
        bottom: footer.bottom - box.bottom,
      },
    };
  });
  assert.ok(mobileFooter.delta.left <= 1, `mobile role footer left edge must hug the card content box: ${JSON.stringify(mobileFooter)}`);
  assert.ok(mobileFooter.delta.right <= 1, `mobile role footer right edge must hug the card content box: ${JSON.stringify(mobileFooter)}`);
  assert.ok(mobileFooter.delta.bottom <= 1, `mobile role footer bottom edge must hug the card content box: ${JSON.stringify(mobileFooter)}`);
  assert.ok(mobileFooter.overflow.left <= 1 && mobileFooter.overflow.right <= 1 && mobileFooter.overflow.bottom <= 1, `mobile role footer must stay inside the card: ${JSON.stringify(mobileFooter)}`);
  assert.ok(Math.abs(mobileFooter.footer.width - (mobileFooter.card.width - mobileFooter.border.left - mobileFooter.border.right)) <= 1, `mobile role footer must span the card content width: ${JSON.stringify(mobileFooter)}`);
  await shot('roles-mobile-390');
  evidence.mobile = { ...overflow, footer: mobileFooter };
  evidence.checks.push('F-06: at 390px the role-card .form-actions box matches the card content box on left/right/bottom within 1px and never leaves the card');
  await page.setViewportSize({ width: 1360, height: 900 });

  // Popup/trigger horizontal containment and the modal protocol choice.
  await go('roles');
  const geometry = await page.locator('.role-card').first().evaluate(card => {
    const trigger = card.querySelector('select[name="model_id"]').closest('[data-choice-field]').querySelector('[data-choice-trigger]');
    const box = trigger.getBoundingClientRect();
    return { triggerLeft: box.left, triggerRight: box.right, triggerWidth: box.width, viewport: innerWidth };
  });
  await openChoice(page.locator('.role-card').first(), '[name="model_id"]');
  const popupBox = await page.locator('.choice-popup').evaluate(el => {
    const box = el.getBoundingClientRect();
    return { left: box.left, right: box.right, width: box.width, viewport: innerWidth };
  });
  assert.ok(popupBox.left >= 0 && popupBox.right <= popupBox.viewport + 1, `popup must stay inside the viewport: ${JSON.stringify(popupBox)}`);
  assert.ok(geometry.triggerRight <= geometry.viewport + 1, 'trigger must not overflow horizontally');
  await page.keyboard.press('Escape');
  evidence.modelSearch.geometry = { trigger: geometry, popup: popupBox };

  await go('providers');
  await page.locator(`[data-sixth-action="provider-edit"][data-id="${provider.id}"]`).click();
  const editDialog = page.locator('#dialog');
  await editDialog.locator('#provider-connection-form').waitFor();
  const otherProtocol = provider.protocol === 'responses' ? 'chat_completions' : 'responses';
  await selectChoice(editDialog, '[name="protocol"]', otherProtocol);
  assert.equal(await editDialog.locator('[name="protocol"]').inputValue(), otherProtocol);
  assert.ok((await choiceTrigger(editDialog, '[name="protocol"]').locator('[data-choice-value]').textContent()).trim());
  await editDialog.locator('.dialog-header button').click();
  await page.locator('#dialog').waitFor({ state: 'hidden' });
  evidence.checks.push('popup and trigger remain inside the viewport; the custom protocol select also works inside #dialog');

  // 3. Topbar account switch and research material filter.
  await go('holdings');
  await page.locator('#account-meta-form').waitFor();
  await selectChoice(page, '#account-switch', fixture.other_id);
  await page.waitForFunction(id => document.querySelector('#account-meta-form')?.dataset.accountId === id, fixture.other_id);
  await page.locator('.loading-line').waitFor({ state: 'hidden' });
  assert.equal(await page.locator('#account-switch').inputValue(), fixture.other_id);
  assert.equal((await page.locator('#account-switch-control [data-choice-value]').textContent()).trim(), accounts.find(a => a.id === fixture.other_id).name);
  await selectChoice(page, '#account-switch', fixture.account_id);
  await page.waitForFunction(id => document.querySelector('#account-meta-form')?.dataset.accountId === id, fixture.account_id);
  await page.locator('.loading-line').waitFor({ state: 'hidden' });

  await go('research');
  await page.waitForFunction(() => !!document.querySelector('#material-filter option[value="official_disclosure"]'), null, { timeout: 12000 });
  await selectChoice(page, '#material-filter', 'official_disclosure');
  await page.getByText('明确工程材料 1', { exact: false }).first().waitFor();
  assert.equal(await page.getByText('明确工程材料 2', { exact: false }).count(), 0, 'official filter must hide the news material');
  await selectChoice(page, '#material-filter', 'news');
  await page.getByText('明确工程材料 2', { exact: false }).first().waitFor();
  assert.equal(await page.getByText('明确工程材料 1', { exact: false }).count(), 0, 'news filter must hide the official material');
  assert.equal(await page.locator('#material-filter').inputValue(), 'news');
  evidence.checks.push('topbar account switch updates the account surface; material classification filter actually filters the index and keeps the native value/text in sync');

  // 4. Unified <details> disclosure rows.
  const checkDetails = async (routeName, selector, label) => {
    await go(routeName);
    const summary = page.locator(selector).first().locator('summary').first();
    await summary.waitFor();
    const details = page.locator(selector).first();
    const style = await summary.evaluate(el => {
      const summaryStyle = getComputedStyle(el);
      return {
        listStyleType: summaryStyle.listStyleType,
        fontWeight: summaryStyle.fontWeight,
        padding: [summaryStyle.paddingTop, summaryStyle.paddingRight, summaryStyle.paddingBottom, summaryStyle.paddingLeft],
        markerAfter: getComputedStyle(el, '::after').content,
      };
    });
    assert.equal(style.listStyleType, 'none', `${label}: native marker must be suppressed`);
    assert.equal(await details.evaluate(el => el.open), false);
    await summary.click();
    assert.equal(await details.evaluate(el => el.open), true, `${label}: summary must expand`);
    await summary.click();
    assert.equal(await details.evaluate(el => el.open), false, `${label}: summary must collapse`);
    evidence.details[label] = style;
    return style;
  };
  const providerDetails = await checkDetails('providers', 'details[data-key^="catalog:"]', 'provider-catalog');
  const roleEditorDetails = await checkDetails('roles', 'details[data-key$=":editor"]', 'role-editor');
  const ledgerDetails = await checkDetails('holdings', 'details.ledger-detail', 'ledger-history');
  // The ledger-detail class is specialized: the generic compact box must not
  // overwrite its roomier 20px/24px title and 600 weight.
  assert.equal(ledgerDetails.fontWeight, '600', `ledger-detail title weight damaged: ${JSON.stringify(ledgerDetails)}`);
  assert.deepEqual(ledgerDetails.padding, ['20px', '24px', '20px', '24px'], `ledger-detail title padding damaged: ${JSON.stringify(ledgerDetails)}`);
  assert.equal(providerDetails.fontWeight, '500', `generic details title weight must stay 500: ${JSON.stringify(providerDetails)}`);
  assert.notEqual(providerDetails.markerAfter, 'none', `generic details must render the unified chevron: ${JSON.stringify(providerDetails)}`);
  assert.notEqual(roleEditorDetails.markerAfter, 'none', `role editor must render the unified chevron: ${JSON.stringify(roleEditorDetails)}`);
  assert.equal(roleEditorDetails.listStyleType, 'none');

  // Second specialized surface: the research report body keeps its own clean
  // 11px/15px title and 10px card, not the generic bare-details treatment.
  await go('research');
  const reportBody = page.locator('.report-body-details').first();
  await reportBody.waitFor();
  const reportStyle = await reportBody.evaluate(el => {
    const summary = el.querySelector('summary');
    const summaryStyle = getComputedStyle(summary);
    const boxStyle = getComputedStyle(el);
    return {
      summaryPadding: [summaryStyle.paddingTop, summaryStyle.paddingRight, summaryStyle.paddingBottom, summaryStyle.paddingLeft],
      summaryWeight: summaryStyle.fontWeight,
      summaryBackground: summaryStyle.backgroundColor,
      summaryListStyle: summaryStyle.listStyleType,
      borderRadius: boxStyle.borderRadius,
      background: boxStyle.backgroundColor,
    };
  });
  assert.deepEqual(reportStyle.summaryPadding, ['11px', '15px', '11px', '15px'], `report-body-details title padding changed: ${JSON.stringify(reportStyle)}`);
  assert.equal(reportStyle.summaryWeight, '600');
  assert.equal(reportStyle.summaryBackground, 'rgb(244, 247, 244)');
  assert.equal(reportStyle.borderRadius, '10px');
  evidence.details['report-body'] = reportStyle;
  evidence.checks.push('F-09: generic bare details suppress the native marker and render the chevron, while .ledger-detail (20px/24px, weight 600) and .report-body-details (11px/15px, own 10px card) keep their specialized titles');

  // 5. Environment import confirmation: cancel then confirm sends exactly one POST.
  await go('providers');
  const envButton = page.locator('[data-sixth-action="environment-import"]');
  await envButton.waitFor();
  await envButton.click();
  await page.locator('#dialog[open]').waitFor();
  assert.ok((await page.locator('#dialog').textContent()).includes('确认导入环境连接'));
  await page.locator('#dialog [data-confirm-cancel]').click();
  await page.locator('#dialog').waitFor({ state: 'hidden' });
  await sleep(150);
  assert.equal(importRequests, 0, 'cancelling environment import must not POST');
  await envButton.click();
  await page.locator('#dialog[open]').waitFor();
  const importResponse = page.waitForResponse(response => response.url().endsWith('/api/environment-connection/import') && response.request().method() === 'POST');
  await page.locator('#dialog [data-confirm-accept]').click();
  assert.equal((await importResponse).status(), 200);
  await page.locator('.loading-line').waitFor({ state: 'hidden' });
  assert.equal(importRequests, 1);
  evidence.dialogs.environmentImport = { cancelPosts: 0, confirmPosts: importRequests, stubbed: true, requests: importRequests };

  // Provider clear: danger focus, cancel, close-button focus return, route
  // cancellation, Escape, then one real DELETE under a same-key duplicate click.
  await go('providers');
  const clearButton = page.locator(`[data-sixth-action="provider-clear"][data-id="${provider.id}"]`);
  await clearButton.click();
  await page.locator('#dialog[open]').waitFor();
  assert.ok((await page.locator('#dialog').textContent()).includes('清除供应商凭据'));
  assert.equal(await page.locator('#dialog [data-confirm-accept]').evaluate(el => el.classList.contains('danger')), true);
  // F-08: danger confirmations must focus the safe cancel action, not confirm.
  assert.equal(await page.locator('#dialog [data-confirm-cancel]').evaluate(el => document.activeElement === el), true, 'danger dialog initial focus must be the cancel button');
  await shot('providers-confirm-dialog');
  await page.locator('#dialog [data-confirm-cancel]').click();
  await page.locator('#dialog').waitFor({ state: 'hidden' });
  await sleep(150);
  assert.equal(deleteRequests, 0, 'cancel must not DELETE credentials');
  assert.equal(await page.locator('.loading-line').isVisible(), false, 'cancelled action must not strand the loading line');

  // Close button is a third cancel path and must restore focus to the trigger.
  await clearButton.click();
  await page.locator('#dialog[open]').waitFor();
  await page.locator('#dialog [data-action="close-dialog"]').click();
  await page.locator('#dialog').waitFor({ state: 'hidden' });
  await sleep(150);
  assert.equal(deleteRequests, 0, 'close button must not DELETE credentials');
  assert.equal(await clearButton.evaluate(el => document.activeElement === el), true, 'closing the dialog must return focus to the clear trigger');

  // F-01: hash navigation with a pending confirmation must cancel it and send
  // no request. No cancel/confirm button is pressed in this case.
  await clearButton.click();
  await page.locator('#dialog[open]').waitFor();
  await page.evaluate(() => { window.location.hash = '#roles'; });
  await page.locator('#dialog').waitFor({ state: 'hidden' });
  await page.locator('nav a[href="#roles"][aria-current="page"]').waitFor();
  await sleep(150);
  assert.equal(deleteRequests, 0, 'route change must cancel the pending provider clear without DELETE');
  await go('providers');

  await clearButton.click();
  await page.locator('#dialog[open]').waitFor();
  await page.keyboard.press('Escape');
  await page.locator('#dialog').waitFor({ state: 'hidden' });
  await sleep(150);
  assert.equal(deleteRequests, 0, 'Escape must not DELETE credentials');

  // Same-key concurrency lock: keep the first DELETE in flight, click the same
  // clear action again, and prove it neither opens a dialog nor issues a request.
  await clearButton.click();
  await page.locator('#dialog[open]').waitFor();
  let releaseClear, observeClear;
  const clearHeld = new Promise(resolve => { observeClear = resolve; });
  const clearGate = new Promise(resolve => { releaseClear = resolve; });
  const credentialPattern = `**/api/providers/${provider.id}/credential`;
  await page.route(credentialPattern, async route => {
    if (route.request().method() === 'DELETE') { observeClear(); await clearGate; }
    return route.continue();
  });
  const cleared = page.waitForResponse(response => response.request().method() === 'DELETE' && /\/api\/providers\/[^/]+\/credential$/.test(new URL(response.url()).pathname));
  await page.locator('#dialog [data-confirm-accept]').click();
  await clearHeld;
  await sleep(50);
  assert.equal(deleteRequests, 1, 'the confirmed clear must issue exactly one DELETE');
  await clearButton.click();
  await page.waitForFunction(() => document.querySelector('#toast-region')?.textContent.includes('仍在处理中'));
  assert.equal(await page.locator('#dialog').evaluate(el => el.open), false, 'a duplicate clear while DELETE is in flight must not open another confirmation');
  assert.equal(deleteRequests, 1, 'a duplicate clear while DELETE is in flight must not issue a second request');
  releaseClear();
  assert.equal((await cleared).status(), 200);
  await page.unroute(credentialPattern);
  await page.locator('.loading-line').waitFor({ state: 'hidden' });
  assert.equal(deleteRequests, 1);
  evidence.dialogs.providerClear = { dangerFocusCancel: true, cancelDeletes: 0, closeDeletes: 0, routeDeletes: 0, escapeDeletes: 0, duplicateDeletes: 0, confirmedDeletes: deleteRequests };
  evidence.checks.push('F-01/F-08: danger clear focuses cancel, close-button cancels with focus return, hash navigation cancels without DELETE, and a same-key duplicate click during the in-flight DELETE opens no second dialog and sends no second request');
  await assertNoNativeDialogs('provider clear');

  // Provider archive: cancel does nothing, confirm archives.
  await go('providers');
  const archiveButton = page.locator(`[data-sixth-action="provider-archive"][data-id="${provider.id}"]`);
  await archiveButton.click();
  await page.locator('#dialog[open]').waitFor();
  assert.ok((await page.locator('#dialog').textContent()).includes('归档供应商连接'));
  await page.locator('#dialog [data-confirm-cancel]').click();
  await page.locator('#dialog').waitFor({ state: 'hidden' });
  await sleep(150);
  assert.equal(archiveRequests, 0, 'cancel must not archive');
  await archiveButton.click();
  await page.locator('#dialog[open]').waitFor();
  const archived = page.waitForResponse(response => response.request().method() === 'POST' && /\/archive$/.test(new URL(response.url()).pathname));
  await page.locator('#dialog [data-confirm-accept]').click();
  assert.equal((await archived).status(), 200);
  await page.locator('.loading-line').waitFor({ state: 'hidden' });
  assert.equal(archiveRequests, 1);
  evidence.dialogs.providerArchive = { cancelArchives: 0, confirmedArchives: archiveRequests };
  evidence.checks.push('environment import, provider clear and archive use #dialog; cancel/Escape send no request, confirm performs exactly the single real action, loading line never sticks');

  // 6. Reverse prompt: trade first, then empty/required, Escape and confirmed reverse.
  await go('holdings');
  await page.locator('#trade-form').waitFor();
  await page.locator('#trade-symbol').fill('518880');
  await page.locator('#trade-quantity').fill('100');
  await page.locator('#trade-price').fill('7.000');
  await page.locator('#trade-time').fill('2026-10-30T10:00');
  await page.locator('#trade-form [name="price_confirmed"]').check();
  await page.locator('#trade-form [data-ledger-action="preview"]').click();
  await page.locator('.loading-line').waitFor({ state: 'hidden' });
  const submitTrade = page.locator('#trade-form [type="submit"]');
  assert.equal(await submitTrade.isDisabled(), false, await page.locator('#toast-region').textContent());
  await submitTrade.click();
  await page.locator('.loading-line').waitFor({ state: 'hidden' });
  const reverseButton = page.locator('[data-ledger-action="reverse"]').first();
  await reverseButton.waitFor();
  await reverseButton.click();
  await page.locator('#prompt-dialog-form').waitFor();
  await shot('holdings-reverse-prompt');
  const promptInput = page.locator('#dialog [data-prompt-input]');
  assert.equal(await promptInput.evaluate(el => document.activeElement === el), true);
  assert.equal(await promptInput.inputValue(), '', 'promptDialog initialValue contract must start empty for the ledger reverse prompt');
  assert.equal(await page.locator('#prompt-dialog-form').getAttribute('data-required'), 'true');
  await page.locator('#dialog [data-confirm-accept]').click();
  await page.locator('#dialog [data-prompt-error]').waitFor({ state: 'visible' });
  assert.ok((await page.locator('#dialog [data-prompt-error]').textContent()).includes('请填写有效内容'));
  assert.equal(await page.locator('#dialog').evaluate(el => el.open), true, 'empty required prompt must not close');
  assert.equal(reverseRequests, 0, 'empty prompt must not POST reverse');
  await page.keyboard.press('Escape');
  await page.locator('#dialog').waitFor({ state: 'hidden' });
  await sleep(150);
  assert.equal(reverseRequests, 0, 'Escape must not POST reverse');
  // F-01: leaving the route while the reverse prompt is pending must close it
  // and settle it as a cancellation, with no reverse POST.
  await reverseButton.click();
  await page.locator('#prompt-dialog-form').waitFor();
  await page.evaluate(() => { window.location.hash = '#settings'; });
  await page.locator('#dialog').waitFor({ state: 'hidden' });
  await page.locator('nav a[href="#settings"][aria-current="page"]').waitFor();
  await sleep(150);
  assert.equal(reverseRequests, 0, 'route change must cancel the pending reverse prompt without POST');
  await go('holdings');
  await page.locator('#trade-form').waitFor();
  await reverseButton.waitFor();
  await reverseButton.click();
  await page.locator('#prompt-dialog-form').waitFor();
  await promptInput.fill('受控测试：录入数量有误');
  const reversed = page.waitForResponse(response => response.request().method() === 'POST' && /\/ledger\/[^/]+\/reverse$/.test(new URL(response.url()).pathname));
  await page.locator('#dialog [data-confirm-accept]').click();
  assert.equal((await reversed).status(), 201);
  await page.locator('.loading-line').waitFor({ state: 'hidden' });
  assert.equal(reverseRequests, 1);
  const ledger = await api(`/api/accounts/${fixture.account_id}/ledger`, undefined, 'GET');
  assert.equal(ledger.entries[0].kind, 'reversal');
  evidence.dialogs.reverse = { initialValue: '', requiredBlocked: true, escapePosts: 0, routePosts: 0, confirmedPosts: reverseRequests, latestKind: ledger.entries[0].kind };
  evidence.checks.push('F-08/F-01: reverse prompt starts empty with required semantics, blocks blank submit, Escape and route navigation cancel without POST, valid reason reverses exactly once');

  // F-09 table row: a details element inside a table cell must not add its own
  // border/padding or shift the summary outside the cell content box.
  await go('holdings');
  const tableDetail = page.locator('table details[data-key^="ledger-record:"]').first();
  await tableDetail.waitFor();
  const tableStyle = await tableDetail.evaluate(el => {
    const boxStyle = getComputedStyle(el);
    const summary = el.querySelector('summary');
    const summaryStyle = getComputedStyle(summary);
    const cell = el.closest('td');
    const cellStyle = getComputedStyle(cell);
    const cellBox = cell.getBoundingClientRect();
    const summaryBox = summary.getBoundingClientRect();
    return {
      border: [boxStyle.borderTopWidth, boxStyle.borderRightWidth, boxStyle.borderBottomWidth, boxStyle.borderLeftWidth],
      padding: [boxStyle.paddingTop, boxStyle.paddingRight, boxStyle.paddingBottom, boxStyle.paddingLeft],
      background: boxStyle.backgroundColor,
      summaryPadding: [summaryStyle.paddingTop, summaryStyle.paddingRight, summaryStyle.paddingBottom, summaryStyle.paddingLeft],
      cellPadding: [cellStyle.paddingTop, cellStyle.paddingRight, cellStyle.paddingBottom, cellStyle.paddingLeft],
      delta: {
        left: Math.abs(summaryBox.left - (cellBox.left + parseFloat(cellStyle.paddingLeft))),
        right: Math.abs((cellBox.right - parseFloat(cellStyle.paddingRight)) - summaryBox.right),
      },
    };
  });
  assert.deepEqual(tableStyle.border, ['0px', '0px', '0px', '0px'], `table details must not add borders: ${JSON.stringify(tableStyle)}`);
  assert.deepEqual(tableStyle.padding, ['0px', '0px', '0px', '0px'], `table details must not add padding: ${JSON.stringify(tableStyle)}`);
  assert.equal(tableStyle.background, 'rgba(0, 0, 0, 0)', `table details must stay transparent: ${JSON.stringify(tableStyle)}`);
  assert.deepEqual(tableStyle.summaryPadding, ['4px', '0px', '4px', '0px'], `table summary compact padding changed: ${JSON.stringify(tableStyle)}`);
  assert.ok(tableStyle.delta.left <= 1 && tableStyle.delta.right <= 1, `table summary must stay aligned to the cell content box: ${JSON.stringify(tableStyle)}`);
  evidence.details['table-row'] = tableStyle;
  evidence.checks.push('F-09 table: the nested ledger-record details has no border/padding/background and its summary stays aligned to the cell content box');

  // 7. Settings screenshot after the destructive provider actions.
  await go('settings');
  await page.locator('#settings-form').waitFor();
  await shot('settings-desktop');

  await assertNoNativeDialogs('full nineteenth flow');
  assert.deepEqual(errors, [], errors.join('\n'));
  evidence.errors = errors;
  evidence.environment = {
    envImportStubbed: true,
    envImportRequests: importRequests,
    note: 'The environment-connection GET/POST are Playwright route stubs; no real environment credential import or model call occurred.',
  };
  evidence.network = {
    providerCredentialDeletes: deleteRequests,
    providerArchivePosts: archiveRequests,
    ledgerReversePosts: reverseRequests,
    modelCalls: 'none: fixtures use published synthetic reports, no worker or external model request',
  };
  evidence.passed = true;
  await writeFile('test-results/nineteenth/controls-evidence.json', JSON.stringify(evidence, null, 2));
  console.log(JSON.stringify({ passed: true, screenshots: evidence.screenshots.length, routes: evidence.routes.length, checks: evidence.checks }, null, 2));
} catch (error) {
  await mkdir('test-results/nineteenth', { recursive: true }).catch(() => {});
  if (page) await page.screenshot({ path: 'test-results/nineteenth/failure.png', fullPage: true }).catch(() => {});
  console.error(diagnostic);
  throw error;
} finally {
  if (browser) await browser.close();
  child.kill('SIGTERM');
  if (child.exitCode === null) await new Promise(resolve => child.once('exit', resolve));
  await rm(directory, { recursive: true, force: true });
}
