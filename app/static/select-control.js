// A fixed-choice form control. The native select is the sole successful field;
// the button and portal are its presentation, never a second submitted value.
// One delegated listener set and one transient popup serve the whole document.
export function choiceField({e, label, name, value, options, id, scope = ''}) {
  const labelId = `${id}-label`, popupId = `${id}-options`;
  return `<div class="field choice-field" data-choice-field data-read-key="choice:${e(scope)}:${e(name)}"><label id="${e(labelId)}" for="${e(id)}-control">${e(label)}</label><select id="${e(id)}" name="${e(name)}" data-choice-native hidden tabindex="-1" aria-hidden="true">${options.map(([key,text])=>`<option value="${e(key)}" ${key===value?'selected':''}>${e(text)}</option>`).join('')}</select><button type="button" id="${e(id)}-control" class="choice-trigger" role="combobox" aria-haspopup="listbox" aria-expanded="false" aria-controls="${e(popupId)}" aria-labelledby="${e(labelId)} ${e(id)}-value" data-choice-trigger><span id="${e(id)}-value" data-choice-value>${e(options.find(([key])=>key===value)?.[1] || '')}</span><span class="choice-chevron" aria-hidden="true">⌄</span></button></div>`;
}

let opened = null, scheduled = 0;
const fieldOf = node => node?.closest?.('[data-choice-field]');
const nativeOf = field => field?.querySelector('[data-choice-native]');
const triggerOf = field => field?.querySelector('[data-choice-trigger]');

function closeChoice({focus = false} = {}) {
  if (!opened) return;
  const {trigger, popup} = opened;
  opened = null;
  trigger.setAttribute('aria-expanded', 'false');
  trigger.removeAttribute('aria-activedescendant');
  popup.remove();
  if (focus && trigger.isConnected && !trigger.disabled) trigger.focus({preventScroll:true});
}

function paintChoice() {
  if (!opened) return;
  const {select, trigger, popup, active} = opened;
  for (const option of popup.children) {
    option.setAttribute('aria-selected', String(option.dataset.value === select.value));
    option.classList.toggle('is-active', Number(option.dataset.index) === active);
  }
  trigger.setAttribute('aria-activedescendant', `${popup.id}-${active}`);
  popup.children[active]?.scrollIntoView({block:'nearest',inline:'nearest'});
}

function positionChoice() {
  scheduled = 0;
  if (!opened) return;
  const {trigger,select,popup} = opened;
  if (!trigger.isConnected || select.matches(':disabled') || trigger.disabled) {closeChoice();return;}
  const rect = trigger.getBoundingClientRect(), viewport = window.visualViewport;
  const leftEdge = viewport?.offsetLeft || 0, topEdge = viewport?.offsetTop || 0;
  const width = viewport?.width || innerWidth, height = viewport?.height || innerHeight;
  if (rect.bottom <= topEdge || rect.top >= topEdge+height || rect.right <= leftEdge || rect.left >= leftEdge+width) {closeChoice();return;}
  const gap = 4, inset = 8;
  const popupWidth = Math.min(rect.width, width-inset*2);
  popup.style.width = `${popupWidth}px`;
  popup.style.left = `${Math.max(leftEdge+inset,Math.min(rect.left,leftEdge+width-inset-popupWidth))}px`;
  const below = topEdge+height-inset-rect.bottom-gap;
  const labelTop = fieldOf(trigger).querySelector('label').getBoundingClientRect().top;
  const above = labelTop-topEdge-inset-gap;
  const naturalHeight = popup.scrollHeight+2;
  // Flip above the label, not on top of it. If neither side fits, scroll inside
  // the larger space; the trigger and its label remain visible.
  const upwards = below < naturalHeight && above > below;
  const available = Math.max(1,upwards?above:below);
  popup.style.maxHeight = `${Math.min(320,available)}px`;
  popup.style.top = `${upwards?labelTop-gap-Math.min(naturalHeight,available):rect.bottom+gap}px`;
  popup.dataset.placement = upwards?'above':'below';
}
function schedulePosition() {
  if (opened && !scheduled) scheduled = requestAnimationFrame(positionChoice);
}

export function syncChoiceControls(root = document) {
  for (const field of root.querySelectorAll('[data-choice-field]')) {
    const select = nativeOf(field), trigger = triggerOf(field);
    if (!select || !trigger) continue;
    const value = trigger.querySelector('[data-choice-value]');
    const text = select.selectedOptions[0]?.textContent || '';
    if (value.textContent !== text) value.textContent = text;
    const disabled = select.matches(':disabled');
    if (trigger.disabled !== disabled) trigger.disabled = disabled;
  }
  if (opened) {
    if (!opened.trigger.isConnected || opened.select.matches(':disabled')) closeChoice();
    else {paintChoice();schedulePosition();}
  }
}

function openChoice(field, initial) {
  const select = nativeOf(field), trigger = triggerOf(field);
  if (!select || select.matches(':disabled') || trigger.disabled) return;
  closeChoice();
  const popup = document.createElement('div');
  popup.id = trigger.getAttribute('aria-controls');
  popup.className = 'choice-popup';
  popup.setAttribute('role','listbox');
  popup.setAttribute('aria-labelledby',field.querySelector('label').id);
  popup.setAttribute('popover','manual');
  for (const [index,option] of [...select.options].entries()) {
    const row = document.createElement('div');
    row.id = `${popup.id}-${index}`;
    row.className = 'choice-option';
    row.setAttribute('role','option');
    row.setAttribute('aria-disabled',String(option.disabled));
    row.dataset.value = option.value;
    row.dataset.index = String(index);
    const text = document.createElement('span');text.textContent = option.textContent;row.append(text);
    const mark = document.createElement('span');mark.className='choice-check';mark.setAttribute('aria-hidden','true');mark.textContent='✓';row.append(mark);
    popup.append(row);
  }
  // Popover enters the top layer, escaping .card overflow and modal stacking.
  // Older browsers use a fixed portal in the current modal or body.
  (trigger.closest('dialog') || document.body).append(popup);
  if (typeof popup.showPopover === 'function') popup.showPopover();
  else popup.removeAttribute('popover');
  opened = {select,trigger,popup,active:initial??Math.max(0,select.selectedIndex)};
  trigger.setAttribute('aria-expanded','true');
  trigger.focus({preventScroll:true});
  positionChoice();paintChoice();
}

function commitChoice(index) {
  if (!opened) return;
  const {select} = opened, option = select.options[index];
  if (!option || option.disabled || select.matches(':disabled')) return;
  const changed = select.value !== option.value;
  select.value = option.value;
  closeChoice({focus:true});
  syncChoiceControls(select.form || document);
  if (changed) {
    select.dispatchEvent(new Event('input',{bubbles:true}));
    select.dispatchEvent(new Event('change',{bubbles:true}));
  }
}

document.addEventListener('pointerdown',event=>{
  if (!opened) return;
  if (opened.popup.contains(event.target)) {event.preventDefault();return;}
  if (!opened.trigger.contains(event.target)) closeChoice();
});
document.addEventListener('click',event=>{
  const row = event.target.closest?.('.choice-option');
  if (row && opened?.popup.contains(row)) {commitChoice(Number(row.dataset.index));return;}
  const trigger = event.target.closest?.('[data-choice-trigger]');
  if (trigger) {if (opened?.trigger===trigger) closeChoice();else openChoice(fieldOf(trigger));}
});
document.addEventListener('keydown',event=>{
  const trigger = event.target.closest?.('[data-choice-trigger]');
  if (!trigger) return;
  const field = fieldOf(trigger), select = nativeOf(field);
  if (select.matches(':disabled')) return;
  const isOpen = opened?.trigger===trigger, key = event.key;
  if (key==='Tab') {if(isOpen)closeChoice();return;}
  if (key==='Escape') {if(isOpen){event.preventDefault();event.stopPropagation();closeChoice({focus:true});}return;}
  if (['Enter',' ','ArrowDown','ArrowUp','Home','End'].includes(key)) {
    event.preventDefault();event.stopPropagation();
    if (!isOpen) {openChoice(field,key==='Home'?0:key==='End'?select.options.length-1:undefined);return;}
    if (key==='Enter'||key===' ') {commitChoice(opened.active);return;}
    const step = key==='ArrowUp'?-1:1;
    let candidate = key==='Home'?0:key==='End'?select.options.length-1:Math.max(0,Math.min(select.options.length-1,opened.active+step));
    while (select.options[candidate]?.disabled && candidate>=0 && candidate<select.options.length) candidate += step;
    if (select.options[candidate]) opened.active = candidate;
    paintChoice();return;
  }
  if (key.length===1&&!event.ctrlKey&&!event.metaKey&&!event.altKey) {
    const match = [...select.options].findIndex(option=>!option.disabled&&option.textContent.startsWith(key));
    if(match>=0){event.preventDefault();if(!isOpen)openChoice(field,match);else{opened.active=match;paintChoice();}}
  }
});
document.addEventListener('focusin',event=>{if(opened && !opened.trigger.contains(event.target) && !opened.popup.contains(event.target))closeChoice();});
document.addEventListener('change',event=>{if(event.target.matches?.('[data-choice-native]'))syncChoiceControls(event.target.form || document);});
document.addEventListener('invalid',event=>{if(event.target.matches?.('[data-choice-native]')){event.preventDefault();triggerOf(fieldOf(event.target))?.focus();}},true);
document.addEventListener('reset',event=>{
  if(!event.target.querySelector?.('[data-choice-field]'))return;
  if(opened?.select.form===event.target)closeChoice();
  queueMicrotask(()=>{syncChoiceControls(event.target);event.target.dispatchEvent(new Event('input',{bubbles:true}));});
});
window.addEventListener('resize',schedulePosition);
document.addEventListener('scroll',schedulePosition,true);
window.visualViewport?.addEventListener('resize',schedulePosition);
window.visualViewport?.addEventListener('scroll',schedulePosition);
new MutationObserver(records=>{
  // Only inspect newly mounted controls or disabled state. Do not re-read every
  // form on each event append, or retain detached forms in an auxiliary cache.
  if(opened&&!opened.trigger.isConnected)closeChoice();
  for(const record of records){
    if(record.type==='attributes'){syncChoiceControls(record.target.form||record.target.parentElement||document);continue;}
    for(const node of record.addedNodes)if(node.nodeType===1){
      if(node.matches('[data-choice-field]'))syncChoiceControls(node.parentElement);
      else if(node.querySelector('[data-choice-field]'))syncChoiceControls(node);
    }
  }
  schedulePosition();
}).observe(document.documentElement,{subtree:true,childList:true,attributes:true,attributeFilter:['disabled']});
