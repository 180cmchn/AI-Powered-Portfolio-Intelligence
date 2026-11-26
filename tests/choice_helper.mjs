// Shared interaction helpers for the app's custom choice control.
// The native <select> stays the only successful form field but is rendered
// hidden, so tests must drive the visible trigger and popup instead.
import assert from 'node:assert/strict';

function pageOf(container) {
  return typeof container.page === 'function' ? container.page() : container;
}

function resolveArgs(a, b, c, d) {
  // Accept both (container, selectSelector, value) and
  // (page, container, selectSelector, value) so older call shapes keep working.
  if (d !== undefined) return { container: b, selectSelector: c, value: d };
  return { container: a, selectSelector: b, value: c };
}

export function choiceField(container, selectSelector) {
  // CSS :has() is evaluated inside the container's scope, unlike filter({has})
  // where a form-rooted inner locator does not match its own descendants.
  return container.locator(`[data-choice-field]:has(${selectSelector})`).first();
}

export function choiceTrigger(container, selectSelector) {
  if (selectSelector.includes('data-choice-trigger')) {
    return container.locator(selectSelector).first();
  }
  return choiceField(container, selectSelector).locator('[data-choice-trigger]').first();
}

async function waitEnabled(trigger, page, timeout) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await trigger.isEnabled()) return;
    await page.waitForTimeout(50);
  }
  throw new Error(`choice trigger stayed disabled for ${timeout}ms: ${await trigger.evaluate(el => el.id || el.outerHTML.slice(0, 120)).catch(() => 'detached')}`);
}

// Replaces waiting for the now-hidden native <select>: wait for its visible
// trigger instead, including the brief disabled period while a model catalog
// is being reloaded after a provider change.
export async function waitChoiceReady(container, selectSelector, { timeout = 15000 } = {}) {
  const trigger = choiceTrigger(container, selectSelector);
  await trigger.waitFor({ state: 'visible', timeout });
  await waitEnabled(trigger, pageOf(container), timeout);
  return trigger;
}

export async function openChoice(container, selectSelector, { timeout = 15000 } = {}) {
  const page = pageOf(container);
  const trigger = await waitChoiceReady(container, selectSelector, { timeout });
  if ((await trigger.getAttribute('aria-expanded')) !== 'true') {
    await trigger.click();
  }
  const popup = page.locator('.choice-popup').first();
  await popup.waitFor({ state: 'visible', timeout });
  assert.equal(await trigger.getAttribute('aria-expanded'), 'true', 'trigger must report an expanded popup');
  return { trigger, popup };
}

// Searchable variant: drive the built-in search box, prove the visible rows
// were filtered, then commit the requested option through a real click.
export async function selectSearchableChoice(root, selectSelector, queryOrValue, optionValue) {
  const container = root;
  const page = pageOf(container);
  const query = String(queryOrValue ?? '');
  const target = String(optionValue ?? query);
  const { trigger, popup } = await openChoice(container, selectSelector);
  assert.equal(await popup.evaluate(el => el.classList.contains('is-searchable')), true, 'popup must expose the built-in search box');
  const search = popup.locator('[data-choice-search]').first();
  await search.waitFor({ state: 'visible' });
  if (query !== '') await search.fill(query);
  await page.waitForFunction(() => document.querySelectorAll('.choice-popup .choice-option').length > 0);

  const options = popup.locator('.choice-option');
  const rows = await options.evaluateAll(nodes => nodes.map(node => ({
    value: node.dataset.value,
    text: (node.textContent || '').trim(),
    disabled: node.getAttribute('aria-disabled') === 'true',
  })));
  assert.ok(rows.length > 0, `search "${query}" must leave at least one visible option`);
  if (query !== '') {
    const needle = query.toLowerCase();
    for (const row of rows) {
      assert.ok(
        String(row.value).toLowerCase().includes(needle) || row.text.toLowerCase().includes(needle),
        `filtered row "${row.value}" does not match query "${query}": ${JSON.stringify(rows)}`,
      );
    }
  }
  const index = rows.findIndex(row => row.value === target);
  assert.ok(index >= 0, `searchable option "${target}" not found after filtering "${query}"; visible: ${JSON.stringify(rows.map(row => row.value))}`);
  assert.equal(rows[index].disabled, false, `searchable option "${target}" is disabled`);
  await options.nth(index).click();
  await popup.waitFor({ state: 'detached' });

  const native = container.locator(selectSelector).first();
  assert.equal(await native.inputValue(), target, 'native select must hold the chosen searchable value');
  const optionText = await native.evaluate((select, chosen) => {
    const option = [...select.options].find(item => item.value === chosen);
    return option?.textContent ?? null;
  }, target);
  const visibleText = (await trigger.locator('[data-choice-value]').textContent())?.trim();
  assert.ok(visibleText, 'visible searchable trigger text must not be empty');
  assert.equal(visibleText, optionText?.trim(), 'visible trigger text must match the selected searchable option text');
  return { trigger, text: visibleText, filtered: rows.map(row => row.value) };
}

export async function selectChoice(a, b, c, d) {
  const { container: target, selectSelector, value } = resolveArgs(a, b, c, d);
  const { trigger, popup } = await openChoice(target, selectSelector);
  const options = popup.locator('.choice-option');
  const count = await options.count();
  let index = -1;
  for (let i = 0; i < count; i += 1) {
    if ((await options.nth(i).getAttribute('data-value')) === value) { index = i; break; }
  }
  if (index < 0) {
    const available = await options.evaluateAll(nodes => nodes.map(node => node.dataset.value));
    throw new Error(`choice option "${value}" not found; available: ${JSON.stringify(available)}`);
  }
  assert.notEqual(await options.nth(index).getAttribute('aria-disabled'), 'true', `choice option "${value}" is disabled`);
  await options.nth(index).click();
  await popup.waitFor({ state: 'detached' });

  const native = target.locator(selectSelector).first();
  assert.equal(await native.inputValue(), value, 'native select must hold the chosen value');
  const optionText = await native.evaluate((select, chosen) => {
    const option = [...select.options].find(item => item.value === chosen);
    return option?.textContent ?? null;
  }, value);
  const visibleText = (await trigger.locator('[data-choice-value]').textContent())?.trim();
  assert.ok(visibleText, 'visible trigger text must not be empty');
  assert.equal(visibleText, optionText?.trim(), 'visible trigger text must match the selected option text');
  return { trigger, text: visibleText };
}
