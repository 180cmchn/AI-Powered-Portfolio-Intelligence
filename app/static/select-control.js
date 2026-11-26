// A fixed-choice form control. The native select is the sole successful field;
// the button and portal are its presentation, never a second submitted value.
// One delegated listener set and one transient popup serve the whole document.
function decodeHtmlEntitiesFallback(text) {
  const named = {lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ', amp: '&'};
  return String(text).replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (full, entity) => {
    const lower = entity.toLowerCase();
    if (lower in named) return named[lower];
    if (lower.startsWith('#x')) {
      const cp = parseInt(lower.slice(2), 16);
      return Number.isFinite(cp) && cp > 0 && cp <= 0x10ffff ? String.fromCodePoint(cp) : full;
    }
    if (lower.startsWith('#')) {
      const cp = parseInt(lower.slice(1), 10);
      return Number.isFinite(cp) && cp > 0 && cp <= 0x10ffff ? String.fromCodePoint(cp) : full;
    }
    return full;
  });
}

function hasSelectedAttr(attrs) {
  let i = 0;
  const s = String(attrs);
  while (i < s.length) {
    while (i < s.length && /\s/.test(s[i])) i++;
    if (i >= s.length) break;
    const nameStart = i;
    while (i < s.length && !/[\s=/>]/.test(s[i])) i++;
    const name = s.slice(nameStart, i).toLowerCase();
    while (i < s.length && /\s/.test(s[i])) i++;
    if (s[i] === '=') {
      i++;
      while (i < s.length && /\s/.test(s[i])) i++;
      if (s[i] === '"' || s[i] === "'") {
        const quote = s[i++];
        while (i < s.length && s[i] !== quote) i++;
        if (i < s.length) i++;
      } else {
        while (i < s.length && !/[\s>]/.test(s[i])) i++;
      }
    }
    if (name === 'selected') return true;
  }
  return false;
}

function appendSafeOptions(target, source) {
  for (const child of source.children) {
    if (child.tagName === 'OPTION') {
      const opt = document.createElement('option');
      opt.value = child.getAttribute('value') ?? child.textContent ?? '';
      opt.textContent = child.textContent ?? '';
      opt.disabled = child.hasAttribute('disabled');
      if (child.hasAttribute('selected')) opt.setAttribute('selected', '');
      target.append(opt);
    } else if (child.tagName === 'OPTGROUP') {
      const group = document.createElement('optgroup');
      group.label = child.getAttribute('label') ?? '';
      group.disabled = child.hasAttribute('disabled');
      appendSafeOptions(group, child);
      target.append(group);
    }
  }
}

function initialTextFromOptionsHtml(html) {
  const markup = String(html || '');
  if (!markup) return '';
  if (typeof document !== 'undefined') {
    const template = document.createElement('template');
    template.innerHTML = `<select>${markup}</select>`;
    const parsedSelect = template.content.querySelector('select');
    if (parsedSelect) {
      const safeSelect = document.createElement('select');
      appendSafeOptions(safeSelect, parsedSelect);
      return safeSelect.selectedOptions[0]?.textContent ?? safeSelect.options[0]?.textContent ?? '';
    }
  }
  const matches = [...markup.matchAll(/<option\b((?:[^>"']|"[^"]*"|'[^']*')*)>([\s\S]*?)<\/option>/gi)];
  if (!matches.length) return '';
  let chosen = matches[0];
  for (const match of matches) {
    if (hasSelectedAttr(match[1])) chosen = match;
  }
  return decodeHtmlEntitiesFallback(chosen[2].replace(/<[^>]*>/g, ''));
}

export function choiceField({
  e,
  label = '',
  name,
  value = '',
  options = [],
  id = name,
  scope = '',
  help = '',
  helpHtml = '',
  variant = 'field',
  ariaLabel = '',
  wrapperClass = '',
  triggerClass = '',
  selectClass = '',
  selectAttrs = '',
  disabled = false,
  searchable = false,
  searchPlaceholder = '搜索选项',
  searchAttr = '',
  emptyText = '未找到匹配项',
  optionsHtml = '',
  selectedText,
}) {
  const isInline = variant === 'inline';
  const hasVisibleLabel = !isInline && Boolean(label);
  const labelId = `${id}-label`, popupId = `${id}-options`;
  const wrapperClasses = [
    isInline ? 'choice-field choice-inline' : 'field choice-field',
    wrapperClass,
  ].filter(Boolean).join(' ');
  const triggerClasses = ['choice-trigger', triggerClass].filter(Boolean).join(' ');
  const effectiveAriaLabel = ariaLabel || (!hasVisibleLabel ? label : '');
  const attrsText = String(selectAttrs || '').trim();
  const isDisabled = Boolean(disabled) || /(?:^|\s)disabled(?:\s*=|\s|$)/i.test(attrsText);
  const hasOptionsHtml = typeof optionsHtml === 'string' && optionsHtml.length > 0;
  const optionMarkup = hasOptionsHtml
    ? optionsHtml
    : options.map(([key, text]) => `<option value="${e(key)}" ${key === value ? 'selected' : ''}>${e(text)}</option>`).join('');
  const resolvedText = selectedText !== undefined && selectedText !== null
    ? String(selectedText)
    : hasOptionsHtml
      ? initialTextFromOptionsHtml(optionsHtml)
      : (options.find(([key]) => key === value)?.[1] ?? options[0]?.[1] ?? '');

  const fieldAttrs = [
    `class="${e(wrapperClasses)}"`,
    'data-choice-field',
    `data-read-key="choice:${e(scope)}:${e(name)}"`,
    `data-choice-variant="${isInline ? 'inline' : 'field'}"`,
    searchable ? 'data-choice-searchable="true"' : '',
    searchable ? `data-choice-search-placeholder="${e(searchPlaceholder)}"` : '',
    searchable && searchAttr ? `data-choice-search-attr="${e(searchAttr)}"` : '',
    searchable ? `data-choice-empty-text="${e(emptyText)}"` : '',
  ].filter(Boolean).join(' ');

  const selectAttrList = [
    `id="${e(id)}"`,
    `name="${e(name)}"`,
    selectClass ? `class="${e(selectClass)}"` : '',
    !hasVisibleLabel && effectiveAriaLabel ? `aria-label="${e(effectiveAriaLabel)}"` : '',
    attrsText,
    disabled && !/(?:^|\s)disabled(?:\s*=|\s|$)/i.test(attrsText) ? 'disabled' : '',
    'data-choice-native',
    'hidden',
    'tabindex="-1"',
    'aria-hidden="true"',
  ].filter(Boolean).join(' ');

  const triggerLabelAttr = hasVisibleLabel
    ? `aria-labelledby="${e(labelId)} ${e(id)}-value"`
    : (effectiveAriaLabel ? `aria-label="${e(effectiveAriaLabel)}"` : '');

  const triggerAttrList = [
    'type="button"',
    `id="${e(id)}-control"`,
    `class="${e(triggerClasses)}"`,
    'role="combobox"',
    'aria-haspopup="listbox"',
    'aria-expanded="false"',
    `aria-controls="${e(popupId)}"`,
    triggerLabelAttr,
    isDisabled ? 'disabled' : '',
    'data-choice-trigger',
  ].filter(Boolean).join(' ');

  const labelMarkup = hasVisibleLabel ? `<label id="${e(labelId)}" for="${e(id)}-control">${e(label)}</label>` : '';
  const helpMarkup = helpHtml ? String(helpHtml) : (help ? `<small>${e(help)}</small>` : '');

  return `<div ${fieldAttrs}>${labelMarkup}<select ${selectAttrList}>${optionMarkup}</select><button ${triggerAttrList}><span id="${e(id)}-value" data-choice-value>${e(resolvedText)}</span><span class="choice-chevron" aria-hidden="true">⌄</span></button>${helpMarkup}</div>`;
}

let opened = null, scheduled = 0;
const fieldOf = node => node?.closest?.('[data-choice-field]');
const nativeOf = field => field?.querySelector('[data-choice-native]');
const triggerOf = field => field?.querySelector('[data-choice-trigger]');

function optionsSnapshot(select) {
  return [...select.options].map(o => `${o.value}\u0000${o.textContent}\u0000${o.disabled ? 1 : 0}`).join('\u0001');
}

function setActive(index) {
  if (!opened) return;
  const option = index >= 0 ? opened.select.options[index] : null;
  opened.active = option ? index : -1;
  opened.activeValue = option ? option.value : undefined;
}

function closeChoice({focus = false} = {}) {
  if (!opened) return;
  const {trigger, popup, searchInput, compositionTimer} = opened;
  if (compositionTimer) clearTimeout(compositionTimer);
  opened = null;
  trigger.setAttribute('aria-expanded', 'false');
  trigger.removeAttribute('aria-activedescendant');
  searchInput?.removeAttribute('aria-activedescendant');
  popup.remove();
  if (focus && trigger.isConnected && !trigger.disabled) trigger.focus({preventScroll: true});
}

function paintChoice({scroll = false} = {}) {
  if (!opened) return;
  const {select, trigger, list, searchInput, active} = opened;
  trigger.setAttribute('aria-expanded', 'true');
  let activeEl = null;
  for (const option of list.querySelectorAll('.choice-option')) {
    const idx = Number(option.dataset.index);
    option.setAttribute('aria-selected', String(idx === select.selectedIndex));
    const isActive = idx === active;
    option.classList.toggle('is-active', isActive);
    if (isActive) activeEl = option;
  }
  if (activeEl) {
    trigger.setAttribute('aria-activedescendant', activeEl.id);
    searchInput?.setAttribute('aria-activedescendant', activeEl.id);
    if (scroll) activeEl.scrollIntoView({block: 'nearest', inline: 'nearest'});
  } else {
    trigger.removeAttribute('aria-activedescendant');
    searchInput?.removeAttribute('aria-activedescendant');
  }
}

function positionChoice() {
  scheduled = 0;
  if (!opened) return;
  const {field, trigger, select, popup, list, searchable, searchBox} = opened;
  if (!trigger.isConnected || select.matches(':disabled') || trigger.disabled) { closeChoice(); return; }
  const rect = trigger.getBoundingClientRect(), viewport = window.visualViewport;
  const leftEdge = viewport?.offsetLeft || 0, topEdge = viewport?.offsetTop || 0;
  const width = viewport?.width || innerWidth, height = viewport?.height || innerHeight;
  if (rect.bottom <= topEdge || rect.top >= topEdge + height || rect.right <= leftEdge || rect.left >= leftEdge + width) {
    closeChoice();
    return;
  }

  const scroller = searchable ? list : popup;
  const prevScrollTop = scroller.scrollTop;

  const gap = 4, inset = 8;
  const isInline = field.dataset.choiceVariant === 'inline';
  if (isInline) {
    const minWidth = Math.min(rect.width, width - inset * 2);
    const maxWidth = Math.max(minWidth, Math.min(320, width - inset * 2));
    popup.style.minWidth = `${minWidth}px`;
    popup.style.width = 'max-content';
    popup.style.maxWidth = `${maxWidth}px`;
    const actualWidth = Math.min(maxWidth, Math.max(minWidth, popup.getBoundingClientRect().width));
    popup.style.left = `${Math.max(leftEdge + inset, Math.min(rect.left, leftEdge + width - inset - actualWidth))}px`;
  } else {
    const popupWidth = Math.min(rect.width, width - inset * 2);
    popup.style.minWidth = '';
    popup.style.maxWidth = '';
    popup.style.width = `${popupWidth}px`;
    popup.style.left = `${Math.max(leftEdge + inset, Math.min(rect.left, leftEdge + width - inset - popupWidth))}px`;
  }

  const below = topEdge + height - inset - rect.bottom - gap;
  const labelEl = field.querySelector('label');
  const labelRect = labelEl?.getBoundingClientRect();
  const upperAnchor = labelRect && labelRect.width > 0 && labelRect.height > 0 ? labelRect.top : rect.top;
  const above = upperAnchor - topEdge - inset - gap;

  const cs = getComputedStyle(popup);
  const borderY = (parseFloat(cs.borderTopWidth) || 0) + (parseFloat(cs.borderBottomWidth) || 0);
  const searchHeight = searchable && searchBox ? searchBox.getBoundingClientRect().height : 0;
  const naturalHeight = Math.ceil((searchable ? searchHeight + list.scrollHeight : popup.scrollHeight) + borderY);
  // Flip above the visible label (or trigger), not on top of it.
  const upwards = below < naturalHeight && above > below;
  const available = Math.max(1, upwards ? above : below);
  const maxPopupHeight = Math.min(searchable ? 340 : 320, available);
  popup.style.maxHeight = `${maxPopupHeight}px`;
  if (searchable && searchBox) {
    list.style.maxHeight = `${Math.max(0, maxPopupHeight - searchHeight - borderY)}px`;
  }
  popup.style.top = `${upwards ? upperAnchor - gap - Math.min(naturalHeight, maxPopupHeight) : rect.bottom + gap}px`;
  popup.dataset.placement = upwards ? 'above' : 'below';

  if (prevScrollTop > 0) {
    const maxScroll = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
    const restored = Math.min(prevScrollTop, maxScroll);
    if (Math.abs(scroller.scrollTop - restored) > 1) scroller.scrollTop = restored;
  }
}

function schedulePosition() {
  if (opened && !scheduled) scheduled = requestAnimationFrame(positionChoice);
}

function renderOpenedOptions({initial, preserveActive = false} = {}) {
  if (!opened) return;
  const {select, popup, list, searchable, searchInput, emptyText} = opened;
  const scroller = searchable ? list : popup;
  const prevScrollTop = scroller.scrollTop;
  const previousActiveValue = preserveActive
    ? (opened.activeValue !== undefined ? opened.activeValue : (opened.active >= 0 ? select.options[opened.active]?.value : undefined))
    : undefined;
  const query = searchable ? (searchInput?.value || '').trim().toLowerCase() : '';
  list.textContent = '';
  const enabledIndexes = [];
  const allIndexes = [];
  let hasSelectableNonEmpty = false;
  for (const [index, option] of [...select.options].entries()) {
    if (searchable && query !== '') {
      if (option.value === '') continue;
      const textMatch = option.textContent.toLowerCase().includes(query);
      const valueMatch = option.value.toLowerCase().includes(query);
      if (!textMatch && !valueMatch) continue;
    }
    const row = document.createElement('div');
    row.id = `${list.id}-${index}`;
    row.className = 'choice-option';
    row.setAttribute('role', 'option');
    row.setAttribute('aria-disabled', String(option.disabled));
    row.dataset.value = option.value;
    row.dataset.index = String(index);
    const text = document.createElement('span');
    text.textContent = option.textContent;
    row.append(text);
    const mark = document.createElement('span');
    mark.className = 'choice-check';
    mark.setAttribute('aria-hidden', 'true');
    mark.textContent = '✓';
    row.append(mark);
    list.append(row);
    allIndexes.push(index);
    if (!option.disabled) {
      enabledIndexes.push(index);
      if (option.value !== '') hasSelectableNonEmpty = true;
    }
  }
  if (searchable && (allIndexes.length === 0 || (query === '' && !hasSelectableNonEmpty))) {
    const empty = document.createElement('div');
    empty.className = 'choice-empty';
    empty.setAttribute('role', 'status');
    empty.textContent = emptyText;
    list.append(empty);
  }
  if (initial !== undefined) {
    if (enabledIndexes.includes(initial)) {
      setActive(initial);
    } else if (enabledIndexes.length > 0) {
      let nearest = enabledIndexes[0];
      let bestDist = Math.abs(nearest - initial);
      for (const idx of enabledIndexes) {
        const dist = Math.abs(idx - initial);
        if (dist < bestDist) {
          bestDist = dist;
          nearest = idx;
        }
      }
      setActive(nearest);
    } else {
      setActive(-1);
    }
  } else if (preserveActive && previousActiveValue !== undefined) {
    const matchByVal = enabledIndexes.find(idx => select.options[idx]?.value === previousActiveValue);
    if (matchByVal !== undefined) setActive(matchByVal);
    else if (enabledIndexes.includes(select.selectedIndex)) setActive(select.selectedIndex);
    else setActive(enabledIndexes[0] ?? -1);
  } else if (enabledIndexes.includes(select.selectedIndex)) {
    setActive(select.selectedIndex);
  } else if (enabledIndexes.length > 0) {
    setActive(enabledIndexes[0]);
  } else {
    setActive(-1);
  }

  if (preserveActive && prevScrollTop > 0 && opened.activeValue === previousActiveValue) {
    const maxScroll = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
    scroller.scrollTop = Math.min(prevScrollTop, maxScroll);
  }
}

export function syncChoiceControls(root = document) {
  const fields = root?.matches?.('[data-choice-field]')
    ? [root]
    : [...(root || document).querySelectorAll('[data-choice-field]')];
  for (const field of fields) {
    const select = nativeOf(field), trigger = triggerOf(field);
    if (!select || !trigger) continue;
    const valueEl = trigger.querySelector('[data-choice-value]');
    const text = select.selectedOptions[0]?.textContent ?? select.options[0]?.textContent ?? '';
    if (valueEl && valueEl.textContent !== text) valueEl.textContent = text;
    const disabled = select.matches(':disabled');
    if (trigger.disabled !== disabled) trigger.disabled = disabled;
  }
  if (opened) {
    if (!opened.trigger.isConnected || opened.select.matches(':disabled')) {
      closeChoice();
    } else {
      const inScope = !root || root === document || root === opened.field || root.contains?.(opened.field);
      if (inScope) {
        const nextSnapshot = optionsSnapshot(opened.select);
        if (nextSnapshot !== opened.snapshot) {
          opened.snapshot = nextSnapshot;
          renderOpenedOptions({preserveActive: true});
        }
        paintChoice({scroll: false});
        schedulePosition();
      }
    }
  }
}

function openChoice(field, initial, initialQuery = '') {
  const select = nativeOf(field), trigger = triggerOf(field);
  if (!select || select.matches(':disabled') || trigger.disabled) return;
  closeChoice();
  const searchable = field.dataset.choiceSearchable === 'true';
  const searchPlaceholder = field.dataset.choiceSearchPlaceholder || '搜索选项';
  const searchAttr = field.dataset.choiceSearchAttr || '';
  const emptyText = field.dataset.choiceEmptyText || '未找到匹配项';
  const listId = trigger.getAttribute('aria-controls') || `${select.id || 'choice'}-options`;

  const popup = document.createElement('div');
  popup.className = searchable ? 'choice-popup is-searchable' : 'choice-popup';
  popup.setAttribute('popover', 'manual');

  let list, searchBox = null, searchInput = null;
  if (searchable) {
    popup.id = `${listId}-popup`;
    searchBox = document.createElement('div');
    searchBox.className = 'choice-search';
    searchInput = document.createElement('input');
    searchInput.type = 'search';
    searchInput.className = 'choice-search-input';
    searchInput.dataset.choiceSearch = '';
    searchInput.autocomplete = 'off';
    searchInput.placeholder = searchPlaceholder;
    searchInput.setAttribute('aria-label', searchPlaceholder);
    searchInput.setAttribute('aria-controls', listId);
    searchInput.setAttribute('aria-autocomplete', 'list');
    if (searchAttr && /^[a-zA-Z_:][a-zA-Z0-9_.:-]*$/.test(searchAttr) && searchAttr !== 'name') {
      searchInput.setAttribute(searchAttr, '');
    }
    if (initialQuery) searchInput.value = initialQuery;
    searchBox.append(searchInput);

    list = document.createElement('div');
    list.id = listId;
    list.className = 'choice-options';
    list.setAttribute('role', 'listbox');
    popup.append(searchBox, list);
  } else {
    popup.id = listId;
    popup.setAttribute('role', 'listbox');
    list = popup;
  }

  const labelEl = field.querySelector('label');
  const ariaLabelText = trigger.getAttribute('aria-label') || select.getAttribute('aria-label') || '';
  if (labelEl?.id) {
    list.setAttribute('aria-labelledby', labelEl.id);
  } else if (ariaLabelText) {
    list.setAttribute('aria-label', ariaLabelText);
  }

  opened = {
    field,
    select,
    trigger,
    popup,
    list,
    searchable,
    searchBox,
    searchInput,
    emptyText,
    snapshot: optionsSnapshot(select),
    active: -1,
    activeValue: undefined,
    composing: false,
    justEndedComposition: false,
    compositionTimer: 0,
  };
  renderOpenedOptions({initial});

  // Popover enters the top layer, escaping .card overflow and modal stacking.
  // Older browsers use a fixed portal in the current modal or body.
  (trigger.closest('dialog') || document.body).append(popup);
  if (typeof popup.showPopover === 'function') popup.showPopover();
  else popup.removeAttribute('popover');

  trigger.setAttribute('aria-expanded', 'true');
  if (searchable && searchInput) {
    const syncSearchFilter = () => {
      if (!opened || opened.searchInput !== searchInput) return;
      const prevActive = opened.active;
      const prevActiveValue = opened.activeValue;
      renderOpenedOptions({preserveActive: true});
      paintChoice({scroll: opened.active !== prevActive || opened.activeValue !== prevActiveValue});
      positionChoice();
    };
    searchInput.addEventListener('compositionstart', () => {
      if (!opened || opened.searchInput !== searchInput) return;
      if (opened.compositionTimer) {
        clearTimeout(opened.compositionTimer);
        opened.compositionTimer = 0;
      }
      opened.composing = true;
      opened.justEndedComposition = false;
    });
    searchInput.addEventListener('compositionend', () => {
      if (!opened || opened.searchInput !== searchInput) return;
      opened.composing = false;
      opened.justEndedComposition = true;
      if (opened.compositionTimer) clearTimeout(opened.compositionTimer);
      opened.compositionTimer = setTimeout(() => {
        if (opened && opened.searchInput === searchInput) {
          opened.justEndedComposition = false;
          opened.compositionTimer = 0;
        }
      }, 0);
      syncSearchFilter();
    });
    searchInput.addEventListener('input', event => {
      event.stopPropagation();
      syncSearchFilter();
    });
    searchInput.focus({preventScroll: true});
    if (initialQuery) {
      const end = searchInput.value.length;
      searchInput.setSelectionRange(end, end);
    }
  } else {
    trigger.focus({preventScroll: true});
  }
  positionChoice();
  paintChoice({scroll: true});
}

function commitChoice(index) {
  if (!opened) return;
  const {select} = opened, option = select.options[index];
  if (!option || option.disabled || select.matches(':disabled')) return;
  const changed = select.selectedIndex !== index || select.value !== option.value;
  select.selectedIndex = index;
  closeChoice({focus: true});
  syncChoiceControls(select.form || document);
  if (changed) {
    select.dispatchEvent(new Event('input', {bubbles: true}));
    select.dispatchEvent(new Event('change', {bubbles: true}));
  }
}

if (typeof document !== 'undefined') {
  document.addEventListener('pointerdown', event => {
    if (!opened) return;
    if (opened.searchInput && (event.target === opened.searchInput || event.target.closest?.('.choice-search'))) {
      return;
    }
    if (opened.popup.contains(event.target)) {
      event.preventDefault();
      return;
    }
    if (!opened.trigger.contains(event.target)) closeChoice();
  });

  document.addEventListener('click', event => {
    const row = event.target.closest?.('.choice-option');
    if (row && opened?.list.contains(row)) {
      commitChoice(Number(row.dataset.index));
      return;
    }
    const trigger = event.target.closest?.('[data-choice-trigger]');
    if (trigger) {
      if (opened?.trigger === trigger) closeChoice();
      else openChoice(fieldOf(trigger));
    }
  });

  document.addEventListener('keydown', event => {
    const inSearch = opened?.searchInput && event.target === opened.searchInput;
    const trigger = event.target.closest?.('[data-choice-trigger]');
    if (!inSearch && !trigger) return;

    if (inSearch) {
      if (event.isComposing || event.keyCode === 229 || opened.composing || opened.justEndedComposition) {
        if (opened.justEndedComposition && !opened.composing) {
          opened.justEndedComposition = false;
        }
        return;
      }
      const {list} = opened;
      const key = event.key;
      if (key === 'Tab') {
        opened.trigger.focus({preventScroll: true});
        closeChoice();
        return;
      }
      if (key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        closeChoice({focus: true});
        return;
      }
      if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(key)) {
        event.preventDefault();
        event.stopPropagation();
        const enabledRows = [...list.querySelectorAll('.choice-option:not([aria-disabled="true"])')];
        if (!enabledRows.length) {
          setActive(-1);
          paintChoice({scroll: false});
          return;
        }
        const currentPos = enabledRows.findIndex(row => Number(row.dataset.index) === opened.active);
        let nextPos = 0;
        if (key === 'Home') nextPos = 0;
        else if (key === 'End') nextPos = enabledRows.length - 1;
        else if (key === 'ArrowDown') nextPos = currentPos < 0 ? 0 : Math.min(enabledRows.length - 1, currentPos + 1);
        else if (key === 'ArrowUp') nextPos = currentPos < 0 ? 0 : Math.max(0, currentPos - 1);
        setActive(Number(enabledRows[nextPos].dataset.index));
        paintChoice({scroll: true});
        return;
      }
      if (key === 'Enter') {
        event.preventDefault();
        event.stopPropagation();
        const activeRow = opened.active >= 0
          ? list.querySelector(`.choice-option[data-index="${opened.active}"]:not([aria-disabled="true"])`)
          : null;
        if (activeRow) commitChoice(opened.active);
        return;
      }
      return;
    }

    const field = fieldOf(trigger), select = nativeOf(field);
    if (!select || select.matches(':disabled')) return;
    const isOpen = opened?.trigger === trigger, key = event.key;
    if (key === 'Tab') {
      if (isOpen) closeChoice();
      return;
    }
    if (key === 'Escape') {
      if (isOpen) {
        event.preventDefault();
        event.stopPropagation();
        closeChoice({focus: true});
      }
      return;
    }
    if (['Enter', ' ', 'ArrowDown', 'ArrowUp', 'Home', 'End'].includes(key)) {
      event.preventDefault();
      event.stopPropagation();
      const enabledIndexes = [];
      for (let i = 0; i < select.options.length; i++) {
        if (!select.options[i].disabled) enabledIndexes.push(i);
      }
      if (!isOpen) {
        const initial = key === 'Home'
          ? enabledIndexes[0]
          : key === 'End'
            ? enabledIndexes.at(-1)
            : undefined;
        openChoice(field, initial);
        return;
      }
      if (key === 'Enter' || key === ' ') {
        commitChoice(opened.active);
        return;
      }
      if (!enabledIndexes.length) {
        setActive(-1);
        paintChoice({scroll: false});
        return;
      }
      const currentPos = enabledIndexes.indexOf(opened.active);
      let nextPos = 0;
      if (key === 'Home') nextPos = 0;
      else if (key === 'End') nextPos = enabledIndexes.length - 1;
      else if (key === 'ArrowDown') nextPos = currentPos < 0 ? 0 : Math.min(enabledIndexes.length - 1, currentPos + 1);
      else if (key === 'ArrowUp') nextPos = currentPos < 0 ? 0 : Math.max(0, currentPos - 1);
      setActive(enabledIndexes[nextPos]);
      paintChoice({scroll: true});
      return;
    }
    if (key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey && !event.isComposing && event.keyCode !== 229) {
      if (field.dataset.choiceSearchable === 'true') {
        event.preventDefault();
        openChoice(field, undefined, key);
        return;
      }
      const lower = key.toLowerCase();
      const match = [...select.options].findIndex(option => !option.disabled && option.textContent.toLowerCase().startsWith(lower));
      if (match >= 0) {
        event.preventDefault();
        if (!isOpen) openChoice(field, match);
        else { setActive(match); paintChoice({scroll: true}); }
      }
    }
  });

  document.addEventListener('focusin', event => {
    if (opened && !opened.trigger.contains(event.target) && !opened.popup.contains(event.target)) {
      closeChoice();
    }
  });
  document.addEventListener('change', event => {
    if (event.target.matches?.('[data-choice-native]')) {
      syncChoiceControls(fieldOf(event.target) || event.target.form || document);
    }
  });
  document.addEventListener('choice:sync', event => {
    const field = fieldOf(event.target);
    if (field) syncChoiceControls(field);
  });
  document.addEventListener('invalid', event => {
    if (event.target.matches?.('[data-choice-native]')) {
      event.preventDefault();
      triggerOf(fieldOf(event.target))?.focus();
    }
  }, true);
  document.addEventListener('reset', event => {
    if (!event.target.querySelector?.('[data-choice-field]')) return;
    if (opened?.select.form === event.target) closeChoice();
    queueMicrotask(() => {
      syncChoiceControls(event.target);
      event.target.dispatchEvent(new Event('input', {bubbles: true}));
    });
  });

  const handleDocumentScroll = event => {
    if (!opened) return;
    if (event?.target instanceof Node && opened.popup.contains(event.target)) return;
    schedulePosition();
  };

  window.addEventListener('resize', schedulePosition);
  document.addEventListener('scroll', handleDocumentScroll, true);
  window.visualViewport?.addEventListener('resize', schedulePosition);
  window.visualViewport?.addEventListener('scroll', schedulePosition);

  new MutationObserver(records => {
    // Only inspect newly mounted controls, disabled/selected state, or dynamic native options.
    if (opened && !opened.trigger.isConnected) closeChoice();
    const seen = new Set();
    let externalMutation = false;
    for (const record of records) {
      if (opened?.popup.contains(record.target)) continue;
      externalMutation = true;
      if (record.type === 'attributes') {
        const target = fieldOf(record.target) || record.target.form || record.target.parentElement || document;
        if (!seen.has(target)) { seen.add(target); syncChoiceControls(target); }
        continue;
      }
      if (record.type === 'childList' && record.target.matches?.('[data-choice-native]')) {
        const field = fieldOf(record.target);
        if (field && !seen.has(field)) { seen.add(field); syncChoiceControls(field); }
        continue;
      }
      for (const node of record.addedNodes) {
        if (node.nodeType !== 1) continue;
        if (node.matches('[data-choice-field]')) {
          if (!seen.has(node)) { seen.add(node); syncChoiceControls(node); }
        } else if (node.querySelector('[data-choice-field]')) {
          if (!seen.has(node)) { seen.add(node); syncChoiceControls(node); }
        }
      }
    }
    if (externalMutation) schedulePosition();
  }).observe(document.documentElement, {subtree: true, childList: true, attributes: true, attributeFilter: ['disabled', 'selected']});
}
