"""JavaScript evaluated inside the page to read the live UI.

Three scripts: collect interactive elements with stable selectors, check the
layout at the current viewport, and read a cheap page-state signature used to
compare before and after a click.
"""

COLLECT_INTERACTIVE_JS = r"""
(maxElements) => {
  const IMPLICIT = {a: 'link', button: 'button', select: 'combobox', textarea: 'textbox', summary: 'button'};
  const INPUT_ROLE = {checkbox: 'checkbox', radio: 'radio', submit: 'button', button: 'button', reset: 'button', range: 'slider', number: 'spinbutton', search: 'searchbox', file: 'file'};
  const SEL = 'a[href], button, input:not([type=hidden]), select, textarea, summary, [role=button], [role=link], [role=menuitem], [role=menuitemcheckbox], [role=tab], [role=switch], [role=checkbox], [role=radio], [role=combobox], [role=textbox], [onclick], [tabindex]:not([tabindex="-1"])';
  const esc = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : s.replace(/([^\w-])/g, '\\$1');
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim().slice(0, 80);

  function roleOf(el) {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit.split(' ')[0];
    const tag = el.tagName.toLowerCase();
    if (tag === 'input') return INPUT_ROLE[(el.type || 'text').toLowerCase()] || 'textbox';
    if (tag === 'a') return el.hasAttribute('href') ? 'link' : 'generic';
    return IMPLICIT[tag] || 'generic';
  }
  function nameOf(el) {
    const aria = el.getAttribute('aria-label');
    if (aria) return clean(aria);
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      const t = by.split(/\s+/).map((id) => document.getElementById(id)).filter(Boolean).map((n) => n.textContent).join(' ');
      if (clean(t)) return clean(t);
    }
    const tag = el.tagName.toLowerCase();
    if (tag === 'input' || tag === 'select' || tag === 'textarea') {
      if (el.labels && el.labels.length) { const t = clean(el.labels[0].textContent); if (t) return t; }
      if (el.placeholder) return clean(el.placeholder);
      if (tag === 'input' && (el.type === 'submit' || el.type === 'button') && el.value) return clean(el.value);
    }
    const text = clean(el.innerText || el.textContent);
    if (text) return text;
    const img = el.querySelector('img[alt]');
    if (img && img.alt) return clean(img.alt);
    const t = el.getAttribute('title');
    if (t) return clean(t);
    const svgTitle = el.querySelector('svg title');
    if (svgTitle) return clean(svgTitle.textContent);
    return '';
  }
  function isVisible(el) {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || parseFloat(cs.opacity) === 0) return false;
    let p = el.parentElement;
    while (p) {
      const pcs = getComputedStyle(p);
      if (pcs.display === 'none' || pcs.visibility === 'hidden') return false;
      p = p.parentElement;
    }
    return true;
  }
  function selectorFor(el) {
    const tid = el.getAttribute('data-testid');
    if (tid) return `[data-testid="${tid}"]`;
    if (el.id && document.querySelectorAll(`[id="${el.id.replace(/"/g, '\\"')}"]`).length === 1) return `[id="${el.id.replace(/"/g, '\\"')}"]`;
    // Always anchored: at a unique id or at <body>. An unanchored nth-of-type path
    // can match a different element elsewhere on the page.
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && cur !== document.body && cur !== document.documentElement) {
      const tag = cur.tagName.toLowerCase();
      if (cur.id && document.querySelectorAll(`[id="${cur.id.replace(/"/g, '\\"')}"]`).length === 1) {
        parts.unshift(`[id="${cur.id.replace(/"/g, '\\"')}"]`);
        return parts.join(' > ');
      }
      let part = tag;
      const parent = cur.parentElement;
      if (parent) {
        const same = Array.from(parent.children).filter((c) => c.tagName === cur.tagName);
        if (same.length > 1) part += `:nth-of-type(${same.indexOf(cur) + 1})`;
      }
      parts.unshift(part);
      cur = parent;
    }
    return 'body > ' + parts.join(' > ');
  }
  function pathHint(el) {
    const out = [];
    let cur = el.parentElement;
    while (cur && cur !== document.body && out.length < 4) {
      const tag = cur.tagName.toLowerCase();
      const role = cur.getAttribute('role');
      if (['main', 'nav', 'aside', 'header', 'footer', 'form', 'table', 'dialog'].includes(tag) || role) out.unshift(role ? `${tag}[role=${role}]` : tag);
      cur = cur.parentElement;
    }
    return out.join(' > ');
  }

  const nodes = Array.from(document.querySelectorAll(SEL));
  const seen = new Set();
  const out = [];
  for (const el of nodes) {
    if (seen.has(el)) continue;
    seen.add(el);
    if (el.closest('[aria-hidden="true"]')) continue;
    const r = el.getBoundingClientRect();
    const role = roleOf(el);
    const tag = el.tagName.toLowerCase();
    let href = null;
    if (tag === 'a' && el.hasAttribute('href')) { try { href = new URL(el.getAttribute('href'), location.href).href; } catch (e) { href = el.getAttribute('href'); } }
    out.push({
      selector: selectorFor(el),
      role,
      name: nameOf(el),
      tag,
      type: el.getAttribute('type') || null,
      href,
      id: el.id || null,
      testid: el.getAttribute('data-testid'),
      visible: isVisible(el),
      enabled: !(el.disabled || el.getAttribute('aria-disabled') === 'true'),
      in_dialog: !!el.closest('[role=dialog], [role=alertdialog], [role=menu], [aria-modal="true"]'),
      checked: (role === 'switch' || role === 'checkbox' || role === 'radio') ? (el.getAttribute('aria-checked') === 'true' || !!el.checked) : null,
      bbox: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
      path_hint: pathHint(el),
      target: el.getAttribute('target'),
      download: el.hasAttribute('download'),
      haspopup: el.getAttribute('aria-haspopup'),
      expanded: el.getAttribute('aria-expanded'),
    });
  }
  out.sort((a, b) => (b.visible - a.visible) || (a.bbox[1] - b.bbox[1]) || (a.bbox[0] - b.bbox[0]));
  return out.slice(0, maxElements);
}
"""

LAYOUT_CHECK_JS = r"""
(opts) => {
  const docW = document.documentElement.clientWidth;
  const scrollW = Math.max(document.documentElement.scrollWidth, document.body ? document.body.scrollWidth : 0);
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim().slice(0, 60);
  const desc = (el) => {
    const r = el.getBoundingClientRect();
    return { tag: el.tagName.toLowerCase(), id: el.id || null, cls: (el.className && typeof el.className === 'string') ? el.className.split(/\s+/).slice(0, 3).join('.') : '', text: clean(el.innerText || el.textContent), bbox: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)] };
  };
  const inScrollContainer = (el) => {
    let p = el.parentElement;
    while (p && p !== document.body) {
      const cs = getComputedStyle(p);
      if (/(auto|scroll)/.test(cs.overflowX) || /(auto|scroll)/.test(cs.overflow)) return true;
      p = p.parentElement;
    }
    return false;
  };
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== 'hidden' && cs.display !== 'none' && parseFloat(cs.opacity) !== 0;
  };

  const all = Array.from(document.body.querySelectorAll('*'));
  const overflowing = [];
  for (const el of all) {
    if (overflowing.length >= 10) break;
    const r = el.getBoundingClientRect();
    if (r.width <= 0) continue;
    const cs = getComputedStyle(el);
    if (cs.position === 'fixed') continue;
    if (r.right > docW + 2 || r.left < -2) {
      if (inScrollContainer(el)) continue;
      overflowing.push(desc(el));
    }
  }

  const interactive = Array.from(document.querySelectorAll('a[href], button, input:not([type=hidden]), select, textarea, [role=button], [role=link], [role=tab], [role=menuitem], [role=switch]')).filter(visible).slice(0, 150);
  const overlaps = [];
  for (let i = 0; i < interactive.length && overlaps.length < 10; i++) {
    const a = interactive[i].getBoundingClientRect();
    for (let j = i + 1; j < interactive.length; j++) {
      const A = interactive[i], B = interactive[j];
      if (A.contains(B) || B.contains(A)) continue;
      const b = B.getBoundingClientRect();
      const ix = Math.max(0, Math.min(a.right, b.right) - Math.max(a.left, b.left));
      const iy = Math.max(0, Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top));
      const inter = ix * iy;
      if (inter <= 0) continue;
      // An icon button placed inside a text field (show/hide password, clear, search) is a pattern, not a defect.
      const isField = (el) => ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName);
      const inside = (inner, outer) => inner.left >= outer.left - 1 && inner.right <= outer.right + 1 && inner.top >= outer.top - 1 && inner.bottom <= outer.bottom + 1;
      if ((isField(A) && !isField(B) && inside(b, a)) || (isField(B) && !isField(A) && inside(a, b))) continue;
      const smaller = Math.min(a.width * a.height, b.width * b.height);
      if (smaller > 0 && inter / smaller > 0.3) {
        overlaps.push({ a: desc(A), b: desc(B) });
        break;
      }
    }
  }

  const smallTargets = [];
  if (opts && opts.checkTargets) {
    for (const el of interactive) {
      if (smallTargets.length >= 10) break;
      const r = el.getBoundingClientRect();
      if (el.tagName === 'A' && el.closest('p, li, td, span')) continue;
      // WCAG 2.5.8 exempts controls whose size the user agent sets (native checkbox / radio).
      if (el.tagName === 'INPUT' && ['checkbox', 'radio'].includes((el.type || '').toLowerCase())) continue;
      if (r.width < 24 || r.height < 24) smallTargets.push(desc(el));
    }
  }

  const clipped = [];
  for (const el of all) {
    if (clipped.length >= 10) break;
    const cs = getComputedStyle(el);
    if (cs.overflow !== 'hidden' && cs.overflowX !== 'hidden') continue;
    if (cs.textOverflow === 'ellipsis') continue;
    if (el.scrollWidth > el.clientWidth + 2 && el.clientWidth > 0 && (el.innerText || '').trim().length > 0 && el.children.length === 0) {
      clipped.push(desc(el));
    }
  }

  return { docW, scrollW, horizontalOverflow: scrollW > docW + 2, overflowing, overlaps, smallTargets, clipped };
}
"""

PAGE_STATE_JS = r"""
() => {
  const root = document.getElementById('root') || document.getElementById('app') || document.body;
  const text = (document.body && document.body.innerText) ? document.body.innerText : '';
  const alerts = Array.from(document.querySelectorAll('[role=alert]')).map((n) => (n.innerText || '').trim().slice(0, 200)).filter(Boolean);
  let hash = 0;
  const html = root ? root.innerHTML : '';
  for (let i = 0; i < html.length; i += 7) hash = (hash * 31 + html.charCodeAt(i)) | 0;
  let overlays = 0;
  const vw = window.innerWidth, vh = window.innerHeight;
  for (const el of document.body.querySelectorAll('*')) {
    const cs = getComputedStyle(el);
    if (cs.position !== 'fixed' || cs.pointerEvents === 'none' || cs.visibility === 'hidden' || cs.display === 'none' || parseFloat(cs.opacity) === 0) continue;
    const r = el.getBoundingClientRect();
    if (r.width * r.height >= 0.4 * vw * vh) overlays += 1;
  }
  return {
    url: location.href,
    path: location.pathname,
    title: document.title,
    bodyTextLength: text.trim().length,
    rootEmpty: !root || root.children.length === 0,
    overlays,
    dialogs: document.querySelectorAll('[role=dialog], [role=alertdialog], [aria-modal="true"]').length,
    menus: document.querySelectorAll('[role=menu], [role=listbox]').length,
    alerts,
    domHash: hash,
    elementCount: document.getElementsByTagName('*').length,
    hasPasswordField: !!document.querySelector('input[type=password]'),
  };
}
"""


_VIS = r"""
  const vis = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    let p = el;
    while (p && p !== document.documentElement) {
      const cs = getComputedStyle(p);
      if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) === 0) return false;
      p = p.parentElement;
    }
    return true;
  };
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const selectorFor = (el) => {
    if (el.id && document.querySelectorAll(`[id="${el.id.replace(/"/g, '\\"')}"]`).length === 1) return `[id="${el.id.replace(/"/g, '\\"')}"]`;
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && cur !== document.body && cur !== document.documentElement) {
      if (cur.id && document.querySelectorAll(`[id="${cur.id.replace(/"/g, '\\"')}"]`).length === 1) { parts.unshift(`[id="${cur.id.replace(/"/g, '\\"')}"]`); return parts.join(' > '); }
      let part = cur.tagName.toLowerCase();
      const parent = cur.parentElement;
      if (parent) {
        const same = Array.from(parent.children).filter((c) => c.tagName === cur.tagName);
        if (same.length > 1) part += `:nth-of-type(${same.indexOf(cur) + 1})`;
      }
      parts.unshift(part);
      cur = parent;
    }
    return 'body > ' + parts.join(' > ');
  };
  const labelOf = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria) return clean(aria);
    if (el.labels && el.labels.length) { const t = clean(el.labels[0].textContent); if (t) return t; }
    if (el.placeholder) return clean(el.placeholder);
    return clean(el.getAttribute('name') || el.id || el.innerText || el.value || '');
  };
"""

FEEDBACK_JS = "() => {" + _VIS + r"""
  const ERR = /(error|went wrong|could not|couldn't|cannot|can't|unable|failed|failure|unavailable|try again|problem|not reach|offline|timed out|timeout)/i;
  const OK = /\b(saved|success|successfully|created|updated|added|removed|deleted|changes applied)\b/i;
  const LOAD = /^(loading|loading\.\.\.|loading…|please wait|fetching|saving…|saving\.\.\.)$/i;
  const EMPTY = /(\bno [a-z ]{0,30}(yet|found|available|to show|recorded|data|results|entries|records|members|projects|screenshots|items|activity|logs)\b|nothing (to show|here|yet)|is empty|\b0 results\b)/i;
  const leaves = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const t = clean(walker.currentNode.textContent);
    if (!t || t.length > 300) continue;
    const el = walker.currentNode.parentElement;
    if (!el || el.closest('script,style,noscript,option,select') || !vis(el)) continue;
    leaves.push(t);
  }
  const alerts = Array.from(document.querySelectorAll('[role=alert],[role=status],[aria-live=assertive],[aria-live=polite]')).filter(vis).map((n) => clean(n.innerText)).filter(Boolean);
  const toasts = [];
  for (const el of document.body.querySelectorAll('*')) {
    const cs = getComputedStyle(el);
    if (cs.position !== 'fixed' || !vis(el)) continue;
    if (el.querySelector('a,button,input,select,textarea')) continue;
    const t = clean(el.innerText);
    if (t && t.length < 300) toasts.push(t);
  }
  const loading = !!Array.from(document.querySelectorAll('[aria-busy="true"],[role=progressbar]')).find(vis)
    || leaves.some((t) => LOAD.test(t))
    || Array.from(document.querySelectorAll('[class*="spinner"],[class*="skeleton"],[class*="loader"],[class*="animate-spin"]')).some(vis);
  const retry = Array.from(document.querySelectorAll('button,[role=button],a')).filter(vis).map((b) => clean(b.innerText || b.getAttribute('aria-label'))).filter((t) => /^(retry|try again|reload|refresh)$/i.test(t));
  return {
    alerts: alerts.map((t) => t.slice(0, 200)),
    toasts: toasts.map((t) => t.slice(0, 200)),
    errors: leaves.filter((t) => ERR.test(t)).map((t) => t.slice(0, 160)),
    successes: [...alerts, ...toasts].filter((t) => OK.test(t) && !ERR.test(t)).map((t) => t.slice(0, 160)),
    loading,
    empties: leaves.filter((t) => EMPTY.test(t)).map((t) => t.slice(0, 160)),
    retry,
    leaves: leaves.slice(0, 400).map((t) => t.slice(0, 120)),
    textLength: clean(document.body.innerText).length,
    path: location.pathname,
  };
}"""

FORMS_JS = "() => {" + _VIS + r"""
  const nameOf = (el) => clean(el.getAttribute('aria-label') || el.innerText || el.value || el.getAttribute('title') || '');
  const inFixed = (el) => { let p = el; while (p && p !== document.body) { if (getComputedStyle(p).position === 'fixed') return true; p = p.parentElement; } return false; };
  return Array.from(document.forms).filter(vis).map((f, index) => {
    const fields = Array.from(f.elements).filter((el) => ['INPUT', 'SELECT', 'TEXTAREA'].includes(el.tagName)
      && !['hidden', 'submit', 'button', 'reset', 'image'].includes((el.type || '').toLowerCase())
      && !el.disabled && !el.readOnly && vis(el)).map((el) => ({
        selector: selectorFor(el),
        tag: el.tagName.toLowerCase(),
        type: (el.type || '').toLowerCase(),
        label: labelOf(el),
        required: !!el.required,
        min: el.getAttribute('min'),
        max: el.getAttribute('max'),
        minLength: el.minLength > 0 ? el.minLength : null,
        maxLength: el.maxLength > 0 ? el.maxLength : null,
        pattern: el.getAttribute('pattern'),
        value: el.type === 'password' ? '' : (el.value || '').slice(0, 80),
        options: el.tagName === 'SELECT' ? Array.from(el.options).map((o) => ({ value: o.value, text: clean(o.text) })) : null,
      }));
    const submits = Array.from(f.querySelectorAll('button:not([type=button]):not([type=reset]), input[type=submit]')).filter(vis);
    const submit = submits[submits.length - 1];
    return {
      index,
      selector: selectorFor(f),
      noValidate: !!f.noValidate,
      inLayer: inFixed(f) || !!f.closest('[role=dialog],[role=alertdialog],[aria-modal="true"]'),
      fields,
      submit: submit ? { selector: selectorFor(submit), name: nameOf(submit), disabled: !!submit.disabled } : null,
      text: clean(f.innerText).slice(0, 200),
    };
  });
}"""

FIELD_VALIDITY_JS = r"""(sel) => {
  const el = document.querySelector(sel);
  if (!el) return null;
  return { valid: el.validity ? el.validity.valid : true, message: el.validationMessage || '', value: el.type === 'password' ? null : (el.value || '').slice(0, 200), length: (el.value || '').length };
}"""

FORM_VALID_JS = r"""(sel) => { const f = document.querySelector(sel); if (!f) return null; return f.noValidate ? null : f.checkValidity(); }"""

LAYERS_JS = "() => {" + _VIS + r"""
  const out = [];
  for (const el of document.body.querySelectorAll('*')) {
    const cs = getComputedStyle(el);
    if (cs.position !== 'fixed' || !vis(el)) continue;
    let p = el.parentElement, nested = false;
    while (p && p !== document.body) { if (getComputedStyle(p).position === 'fixed') { nested = true; break; } p = p.parentElement; }
    if (nested) continue;
    const controls = Array.from(el.querySelectorAll('a[href],button,input:not([type=hidden]),select,textarea,[role=button],[role=menuitem],[tabindex]:not([tabindex="-1"])')).filter(vis);
    if (!controls.length) continue;
    const r = el.getBoundingClientRect();
    const dialogEl = el.matches('[role=dialog],[role=alertdialog],[aria-modal="true"]') ? el : (el.querySelector('[role=dialog],[role=alertdialog],[aria-modal="true"]') || el.closest('[role=dialog],[role=alertdialog],[aria-modal="true"]'));
    const menu = el.matches('[role=menu],[role=listbox],[role=tooltip]') || !!el.querySelector('[role=menu],[role=listbox]');
    const cls = (typeof el.className === 'string' ? el.className : '').split(/\s+/).filter(Boolean);
    out.push({
      signature: el.tagName.toLowerCase() + '.' + cls.filter((c) => !/^(translate|opacity|pointer-events|transition|duration)/.test(c)).slice(0, 6).join('.'),
      selector: selectorFor(el),
      area: Math.round(r.width * r.height),
      viewportShare: Math.round(100 * (r.width * r.height) / (window.innerWidth * window.innerHeight)),
      controls: controls.length,
      hasDialogRole: !!dialogEl,
      ariaModal: !!(dialogEl && dialogEl.getAttribute('aria-modal') === 'true'),
      labelled: !!(dialogEl && (dialogEl.getAttribute('aria-label') || dialogEl.getAttribute('aria-labelledby'))),
      isMenu: menu,
      focusInside: el.contains(document.activeElement) && document.activeElement !== document.body,
      text: clean(el.innerText).slice(0, 100),
    });
  }
  return out;
}"""

CANCEL_IN_LAYER_JS = "(layerSel) => {" + _VIS + r"""
  const layer = document.querySelector(layerSel);
  if (!layer) return null;
  const btns = Array.from(layer.querySelectorAll('button,[role=button]')).filter(vis);
  const pick = btns.find((b) => /^(cancel|no|keep|go back|close|dismiss)$/i.test(clean(b.innerText || b.getAttribute('aria-label'))))
    || btns.find((b) => /^(✕|×|x)$/i.test(clean(b.innerText || b.getAttribute('aria-label'))));
  return pick ? { selector: selectorFor(pick), name: clean(pick.innerText || pick.getAttribute('aria-label')) } : null;
}"""

STORAGE_KEYS_JS = r"""() => { const out = {}; try { for (let i = 0; i < localStorage.length; i++) { const k = localStorage.key(i); out[k] = (localStorage.getItem(k) || '').length; } } catch (e) {} return out; }"""
