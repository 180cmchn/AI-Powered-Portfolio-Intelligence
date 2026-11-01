// Server data is replaceable; connected reading nodes and form drafts belong to
// the reader. Keys are entity IDs or named panels, scoped by their keyed parents.
const element = node => node?.nodeType === Node.ELEMENT_NODE;
export function nodeKey(node) {
  if (!element(node)) return null;
  for (const name of ['data-read-key','data-draft-key','data-key','id','data-report-id','data-task-id','data-recommendation-id','data-role-id']) {
    if (node.hasAttribute(name)) return `${node.localName}:${name}:${node.getAttribute(name)}`;
  }
  return null;
}
function compatible(a,b) {
  return a && a.nodeType === b.nodeType && a.nodeName === b.nodeName && nodeKey(a) === nodeKey(b) && (nodeKey(a) || !element(a) || a.classList[0] === b.classList[0]);
}
export function reconcile(existing, incoming, {retain = () => false} = {}) {
  if (!compatible(existing,incoming)) { existing.replaceWith(incoming); return incoming; }
  if (!element(existing)) {
    if (existing.nodeValue !== incoming.nodeValue) existing.nodeValue = incoming.nodeValue;
    return existing;
  }
  if (retain(existing,incoming)) return existing.parentNode ? existing : incoming;
  // Moving a textarea's text or a select's options consumes the incoming
  // control's live state. Capture it before touching attributes or children.
  // Defaults remain markup-owned; live values may differ from those defaults.
  const control = incoming instanceof HTMLSelectElement
    ? {selected: [...incoming.options].map(option => option.selected), index: incoming.selectedIndex}
    : incoming instanceof HTMLInputElement
      ? {value: incoming.value, checked: incoming.checked}
      : incoming instanceof HTMLTextAreaElement ? {value: incoming.value} : null;
  // Details open is user state; rendering an existing entity never sets it.
  for (const attr of [...existing.attributes]) {
    if (attr.name === 'open' && existing.localName === 'details') continue;
    if (!incoming.hasAttribute(attr.name)) existing.removeAttribute(attr.name);
  }
  for (const attr of incoming.attributes) {
    if (attr.name === 'open' && existing.localName === 'details') continue;
    if (existing.getAttribute(attr.name) !== attr.value) existing.setAttribute(attr.name,attr.value);
  }
  const oldChildren = [...existing.childNodes], used = new Set();
  const keyed = new Map(oldChildren.filter(nodeKey).map(node => [nodeKey(node),node]));
  let cursor = existing.firstChild;
  for (const next of [...incoming.childNodes]) {
    const key = nodeKey(next);
    let old = key ? keyed.get(key) : (cursor && !used.has(cursor) && compatible(cursor,next) ? cursor : null);
    // Unkeyed markup is fixed structure, never used as the identity of a record.
    if (!old && !key) old = oldChildren.find(node=>!used.has(node) && !nodeKey(node) && compatible(node,next));
    if (old) {
      used.add(old);
      if (old !== cursor) existing.insertBefore(old,cursor);
      const retained = reconcile(old,next,{retain});
      cursor = retained.nextSibling;
    } else {
      existing.insertBefore(next,cursor);
    }
  }
  for (const old of oldChildren) if (!used.has(old)) old.remove();
  if (existing instanceof HTMLInputElement) {
    if (existing.value !== control.value) existing.value = control.value;
    if (existing.checked !== control.checked) existing.checked = control.checked;
  } else if (existing instanceof HTMLTextAreaElement) {
    if (existing.value !== control.value) existing.value = control.value;
  } else if (existing instanceof HTMLSelectElement) {
    // Index-based selection preserves duplicate values and every selected item
    // in a multi-select. Assigning .value would silently collapse both cases.
    if (existing.multiple) [...existing.options].forEach((option,index) => {
      if (option.selected !== control.selected[index]) option.selected = control.selected[index];
    });
    else if (existing.selectedIndex !== control.index) existing.selectedIndex = control.index;
  }
  return existing;
}
export function patchHTML(root, html, options) {
  const template = document.createElement('template'); template.innerHTML = html;
  const incoming = root.cloneNode(false); incoming.append(template.content);
  reconcile(root,incoming,options);
}
function scrollOwner(anchor) {
  for(let node=anchor?.parentElement;node;node=node.parentElement) {
    if(/auto|scroll/.test(getComputedStyle(node).overflowY) && (node.scrollHeight>node.clientHeight || node.matches('.dialog-content')))return node;
  }
  return document.scrollingElement;
}
const isWindowOwner = owner => owner === document.scrollingElement;
const viewportHeight = owner => isWindowOwner(owner) ? innerHeight : owner.clientHeight;
// A shrinking page can clamp scrollTop before the anchor compensation runs.
// Keep only the missing reading space, not old records or old response bodies.
function keepAnchorSpace(root, owner, anchor, top) {
  const host=isWindowOwner(owner) ? root : owner;
  let tail=[...host.children].find(node=>node.hasAttribute('data-reading-tail'));
  const oldHeight=tail?.getBoundingClientRect().height || 0;
  const position=anchor.getBoundingClientRect().top;
  const target=owner.scrollTop + position-top;
  const contentHeight=owner.scrollHeight-oldHeight;
  const needed=Math.max(0,Math.ceil(target+viewportHeight(owner)-contentHeight));
  if(needed && !tail) {
    tail=document.createElement('div');tail.dataset.readingTail='';tail.setAttribute('aria-hidden','true');
    tail.style.cssText='pointer-events:none;overflow-anchor:none;flex:none;';host.append(tail);
  }
  if(tail) {
    tail.readingOwner=owner;
    if(needed)tail.style.height=`${needed}px`;else tail.remove();
  }
  const delta=anchor.getBoundingClientRect().top-top;
  if(Math.abs(delta)>.5) {
    if(isWindowOwner(owner))window.scrollBy({top:delta,behavior:'instant'});
    else owner.scrollTop+=delta;
  }
}
document.addEventListener('scroll',()=>{
  // Reclaim padding as soon as the reader leaves the constrained bottom range.
  // Removing it here cannot clamp the current scroll position.
  for(const tail of document.querySelectorAll('[data-reading-tail]')) {
    const owner=tail.readingOwner;
    if(!owner || !owner.isConnected || owner.scrollTop+viewportHeight(owner)<=owner.scrollHeight-tail.getBoundingClientRect().height+.5)tail.remove();
  }
},true);
// Capture a visible record/text anchor rather than restoring an absolute window
// offset. Only compensate measured movement; unchanged updates do no scrolling.
export function readingTransaction(root, update) {
  const focused = document.activeElement;
  const selection = window.getSelection();
  const selected = selection?.rangeCount ? selection.getRangeAt(0).cloneRange() : null;
  const endpoints = selected && root.contains(selection.anchorNode) ? {anchor:selection.anchorNode, start:selection.anchorOffset, focus:selection.focusNode, end:selection.focusOffset, anchorText:selection.anchorNode.nodeValue, focusText:selection.focusNode.nodeValue} : null;
  let anchor = selected && root.contains(selected.startContainer) ? selected.startContainer.parentElement : null;
  const visible = node => {const r=node.getBoundingClientRect();return r.bottom>0 && r.top<innerHeight && r.width>0 && r.height>0;};
  if (!anchor || !visible(anchor)) {
    const hit = document.elementFromPoint(Math.min(innerWidth-20, Math.max(340,innerWidth*.55)), Math.min(180,innerHeight/3));
    anchor = hit && root.contains(hit) ? hit.closest('pre,p,summary,tr,[data-read-key],article') : null;
  }
  const top = anchor?.getBoundingClientRect().top;
  const scrollers = [...root.querySelectorAll('*')].filter(n=>n.scrollTop || n.scrollLeft).map(node=>({node,top:node.scrollTop,left:node.scrollLeft}));
  const inputSelection = focused && 'selectionStart' in focused && focused.selectionStart !== null ? [focused.selectionStart,focused.selectionEnd,focused.selectionDirection] : null;
  update();
  if (focused?.isConnected && document.activeElement !== focused) {
    focused.focus({preventScroll:true});
    if(inputSelection) focused.setSelectionRange(...inputSelection);
  }
  if(endpoints?.anchor.isConnected && endpoints.focus?.isConnected && endpoints.anchor.nodeValue === endpoints.anchorText && endpoints.focus.nodeValue === endpoints.focusText &&
    (selection.anchorNode!==endpoints.anchor || selection.focusNode!==endpoints.focus || selection.anchorOffset!==endpoints.start || selection.focusOffset!==endpoints.end)) {
    selection.setBaseAndExtent(endpoints.anchor,endpoints.start,endpoints.focus,endpoints.end);
  }
  for(const item of scrollers) if(item.node.isConnected) {
    if(item.node.scrollTop!==item.top)item.node.scrollTop=item.top;
    if(item.node.scrollLeft!==item.left)item.node.scrollLeft=item.left;
  }
  if(anchor?.isConnected && Number.isFinite(top)) {
    const delta=anchor.getBoundingClientRect().top-top;
    if(Math.abs(delta)>.5 || root.querySelector('[data-reading-tail]'))keepAnchorSpace(root,scrollOwner(anchor),anchor,top);
  }
}
