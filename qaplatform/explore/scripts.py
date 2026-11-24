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
    // Screen-reader-only controls (a 1px box clipped to nothing — the "skip to content" pattern) are
    // keyboard/AT-only, not mouse targets, so they are not "clickable" and must not be tested as such.
    if ((r.width <= 2 || r.height <= 2) && (/rect\(0(px)?,? 0(px)?,? 0(px)?,? 0(px)?\)/.test(cs.clip) || /inset\(50%\)/.test(cs.clipPath))) return false;
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
      placeholder: el.getAttribute('placeholder') || null,
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
  const ancestorClips = (el) => {
    // An ancestor with overflow hidden/clip/auto/scroll visually crops the child, so the child is
    // not actually spilling past the page — it is clipped by its container.
    let p = el.parentElement;
    while (p && p !== document.body) {
      const cs = getComputedStyle(p);
      if (/(hidden|clip|auto|scroll)/.test(cs.overflowX) || /(hidden|clip|auto|scroll)/.test(cs.overflow)) return true;
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
  // THE key precision gate: is `el` actually the painted (top, visible) element at a point inside
  // its own box? A geometric issue the eye cannot see — because the element is covered, transparent
  // behind something, or below the captured viewport — is not a bug a screenshot would show.
  const W = window.innerWidth, Hh = window.innerHeight;
  const topAt = (x, y) => (x >= 1 && y >= 1 && x < W - 1 && y < Hh - 1) ? document.elementFromPoint(x, y) : null;
  const hits = (el, t) => !!t && (t === el || el.contains(t) || t.contains(el));
  const painted = (el) => {
    const r = el.getBoundingClientRect();
    return hits(el, topAt(r.left + r.width / 2, r.top + r.height / 2))
      || hits(el, topAt(r.left + 3, r.top + 3)) || hits(el, topAt(r.right - 3, r.bottom - 3))
      || hits(el, topAt(r.right - 3, r.top + 3)) || hits(el, topAt(r.left + 3, r.bottom - 3));
  };
  const textWidth = (el) => { try { const rg = document.createRange(); rg.selectNodeContents(el); return rg.getBoundingClientRect().width; } catch (e) { return 0; } };

  const all = Array.from(document.body.querySelectorAll('*'));
  const overflowing = [];
  for (const el of all) {
    if (overflowing.length >= 10) break;
    const r = el.getBoundingClientRect();
    if (r.width <= 1 || r.height <= 1) continue;
    const cs = getComputedStyle(el);
    if (cs.position === 'fixed') continue;
    if (r.right > docW + 3 || r.left < -3) {
      if (ancestorClips(el)) continue;   // visually cropped by a container, not spilling off the page
      overflowing.push(desc(el));
    }
  }

  const interactive = Array.from(document.querySelectorAll('a[href], button, input:not([type=hidden]), select, textarea, [role=button], [role=link], [role=tab], [role=menuitem], [role=switch]')).filter(visible).slice(0, 150);
  const overlaps = [];
  for (let i = 0; i < interactive.length && overlaps.length < 10; i++) {
    const A = interactive[i], a = A.getBoundingClientRect();
    for (let j = i + 1; j < interactive.length; j++) {
      const B = interactive[j];
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
      // A visible overlap of >=6px on BOTH axes is a real collision (the paint check below confirms
      // it is actually visible); a large proportional overlap also counts even if one control is small.
      const smaller = Math.min(a.width * a.height, b.width * b.height);
      if (!((ix >= 6 && iy >= 6) || (smaller > 0 && inter / smaller > 0.3))) continue;
      // VISUAL CONFIRM: at the collision centre one of the two must be the painted element, and both
      // must be painted somewhere. If a container/overlay sits on top, or either is invisible, the
      // eye sees no overlap — so the screenshot would show nothing and we do not report it.
      const cx = (Math.max(a.left, b.left) + Math.min(a.right, b.right)) / 2;
      const cy = (Math.max(a.top, b.top) + Math.min(a.bottom, b.bottom)) / 2;
      const top = topAt(cx, cy);
      if (!(hits(A, top) || hits(B, top)) || !painted(A) || !painted(B)) continue;
      overlaps.push({ a: desc(A), b: desc(B) });
      break;
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
      if (r.width >= 24 && r.height >= 24) continue;
      if (!painted(el)) continue;   // covered or off the captured viewport — not a visible target
      // The real tap area may come from a wrapping control with padding around a small child.
      const host = el.closest('a[href], button, [role=button]') || el;
      const hr = host.getBoundingClientRect();
      if (hr.width >= 24 && hr.height >= 24) continue;
      smallTargets.push(desc(el));
    }
  }

  // Clipped TEXT: a single-line (white-space:nowrap/pre) text run cut off by an overflow:hidden box —
  // the clip may be on the text element itself OR on an ancestor (e.g. a fixed-width ".ticker" around
  // an <h1>). This deliberately ignores carousels/sliders/masks whose overflow crops BLOCKS, not a
  // forced single line of text: there the text wraps normally and is not visibly cut.
  const clipperOf = (el) => {
    let p = el;
    while (p && p !== document.body) {
      const c = getComputedStyle(p);
      if (/(hidden|clip)/.test(c.overflow) || /(hidden|clip)/.test(c.overflowX)) {
        return c.textOverflow === 'ellipsis' ? null : p;  // ellipsis = intentional truncation
      }
      p = p.parentElement;
    }
    return null;
  };
  const clipped = [];
  for (const el of all) {
    if (clipped.length >= 10) break;
    if (el.children.length !== 0) continue;                 // text leaves only
    if (!(el.innerText || '').trim()) continue;
    const cs = getComputedStyle(el);
    if (!/(nowrap|pre)/.test(cs.whiteSpace)) continue;       // wrapping text can never be clipped this way
    const clipper = clipperOf(el);
    if (!clipper) continue;
    const cr = clipper.getBoundingClientRect();
    if (cr.width <= 2 || cr.height <= 2) continue;           // sr-only clipper
    const r = el.getBoundingClientRect();
    // The rendered text's right edge = the element's left + its content width (scrollWidth). A block
    // heading is constrained to its parent's width, so its box does not grow — but its nowrap text
    // overflows and is cut by the clipper. Compare that text edge to the clip box.
    const textRight = r.left + el.scrollWidth;
    const cut = textRight > cr.right + 4 || r.left < cr.left - 4;
    if (!cut || !painted(el)) continue;
    clipped.push(desc(el));
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
  // A page can be visually full (images, canvas, icons, media) with little text, so text length
  // alone must never decide "empty". Count painted visual content inside the viewport.
  let visibleContent = 0;
  const vcW = window.innerWidth, vcH = window.innerHeight;
  for (const el of document.body ? document.body.querySelectorAll('img, svg, canvas, video, picture, [style*="background-image"], input, button, a[href], [role=img]') : []) {
    const rr = el.getBoundingClientRect();
    if (rr.width >= 8 && rr.height >= 8 && rr.top < vcH && rr.bottom > 0 && rr.left < vcW && rr.right > 0) { visibleContent += 1; if (visibleContent >= 3) break; }
  }
  if (visibleContent < 1) {
    // Content may live in Shadow DOM, a <canvas> or an iframe (not reachable by the query above).
    // Check what is actually PAINTED at a few viewport points — a real element there means not empty.
    for (const [x, y] of [[vcW / 2, vcH * 0.12], [vcW / 2, vcH / 2], [vcW / 2, vcH * 0.3], [vcW * 0.3, vcH * 0.5], [vcW * 0.7, vcH * 0.5], [vcW * 0.25, vcH * 0.12]]) {
      const t = document.elementFromPoint(x, y);
      if (t && t !== document.body && t !== document.documentElement) { visibleContent = 1; break; }
    }
  }
  return {
    url: location.href,
    path: location.pathname,
    title: document.title,
    bodyTextLength: text.trim().length,
    visibleContent,
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
  const ERR = /\b(error|went wrong|could not|couldn't|cannot|can't|unable|failed|failure|unavailable|try again|problem|not reach|offline|timed out|timeout)/i;
  // Words that are errors only in a message about the user's input ("Invalid email or password"),
  // not as data ("Invalid Date", a "Locked" status badge): counted only inside a form, dialog or alert.
  const INPUT_ERR = /\b(invalid|incorrect|wrong|denied|does ?n[o']t match|not valid|unauthori[sz]ed|forbidden|too many attempts|must be|is required)\b/i;
  const DATA_TEXT = /^(invalid date|nan|locked|denied|forbidden)$/i;
  const OK = /\b(saved|success|successfully|created|updated|added|removed|deleted|changes applied)\b/i;
  const LOAD = /^(loading|loading\.\.\.|loading…|please wait|fetching|saving…|saving\.\.\.)$/i;
  const EMPTY = /(\bno [a-z ]{0,30}(yet|found|available|to show|recorded|data|results|entries|records|members|projects|screenshots|items|activity|logs)\b|nothing (to show|here|yet)|is empty|\b0 results\b)/i;
  const leaves = [];
  const inputErrs = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const t = clean(walker.currentNode.textContent);
    if (!t || t.length > 300) continue;
    const el = walker.currentNode.parentElement;
    if (!el || el.closest('script,style,noscript,option,select') || !vis(el)) continue;
    leaves.push(t);
    if (!DATA_TEXT.test(t) && INPUT_ERR.test(t) && el.closest('form,[role=alert],[role=dialog],[aria-live],[class*="error" i],[class*="invalid" i],[class*="toast" i]')) inputErrs.push(t);
  }
  const alerts = Array.from(document.querySelectorAll('[role=alert],[role=status],[aria-live=assertive],[aria-live=polite],[class*="field-error" i],[class*="error-message" i],[class*="form-error" i],[class*="invalid-feedback" i],[class*="help-block" i].error')).filter(vis).map((n) => clean(n.innerText)).filter(Boolean);
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
    errors: Array.from(new Set(leaves.filter((t) => ERR.test(t)).concat(inputErrs))).map((t) => t.slice(0, 160)),
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
        placeholder: el.getAttribute('placeholder') || '',
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
      heading: clean((f.querySelector('h1, h2, h3, h4, legend, [role=heading]') || {}).innerText || ''),
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

# Nav/tab controls of a router-less SPA: visible items in a nav/sidebar/tablist that switch screens.
NAV_CONTROLS_JS = "() => {" + _VIS + r"""
  const SKIP = /^(log\s?out|sign\s?out|logout|dark|light|theme|toggle theme|profile|account|menu|notifications?|close|back)$/i;
  const containers = Array.from(document.querySelectorAll('nav, aside, header, [role=navigation], [role=tablist], [class*="sidebar" i], [class*="navbar" i], [class*="tabs" i]'));
  const seen = new Set();
  const out = [];
  const consider = (el) => {
    if (!vis(el) || seen.has(el)) return;
    seen.add(el);
    // a menu/listbox trigger (avatar, dropdown) opens a popup, not a screen
    const pop = (el.getAttribute('aria-haspopup') || '').toLowerCase();
    if (pop && pop !== 'false') return;
    const label = clean(el.innerText || el.getAttribute('aria-label') || el.getAttribute('title'));
    if (!label || label.length > 40 || SKIP.test(label)) return;
    out.push({ selector: selectorFor(el), label, role: el.getAttribute('role') || (el.tagName === 'A' ? 'link' : 'button') });
  };
  for (const c of containers)
    for (const el of c.querySelectorAll('button, a[href], [role=tab], [role=menuitem], [role=button]')) consider(el);
  for (const el of document.querySelectorAll('[role=tab]')) consider(el);
  return out.slice(0, 20);
}"""

# Find record containers whose visible text includes our unique marker, and the controls inside them.
# Used only by the provision module, which acts exclusively on records it created.
FIND_MARKED_JS = "(marker) => {" + _VIS + r"""
  const mk = (marker || '').toLowerCase();
  if (!mk) return [];
  const all = Array.from(document.querySelectorAll('tr, li, [role=row], [role=listitem], [class*="card" i], [class*="row" i], [class*="item" i], div'));
  const hits = all.filter((el) => vis(el) && (el.innerText || '').toLowerCase().includes(mk));
  hits.sort((a, b) => a.querySelectorAll('*').length - b.querySelectorAll('*').length);
  const chosen = [];
  for (const el of hits) {
    if (chosen.some((c) => c.contains(el) || el.contains(c))) continue;
    if (el.querySelectorAll('button, a[href], [role=button], [role=menuitem]').length) chosen.push(el);
  }
  return chosen.slice(0, 5).map((el) => ({
    selector: selectorFor(el),
    text: clean(el.innerText).slice(0, 100),
    controls: Array.from(el.querySelectorAll('button, a[href], [role=button], [role=menuitem]')).filter(vis).map((b) => ({ selector: selectorFor(b), label: clean(b.innerText || b.getAttribute('aria-label')) })),
  }));
}"""

# Single-record containers that carry the provision marker. A container qualifies only if it
# is one record (a row, list item or card that holds no other such record), never a wrapper
# around a whole table, so a control found inside it can only belong to the demo record.
_RECORD_SEL = "tr, li, [role=row], [role=listitem], [class*='card' i]"
MARKED_ROWS_JS = "(marker) => {" + _VIS + r"""
  const mk = (marker || '').toLowerCase();
  if (!mk) return [];
  const SEL = """ + repr(_RECORD_SEL) + r""";
  const rows = Array.from(document.querySelectorAll(SEL)).filter((el) => vis(el)
    && (el.innerText || '').toLowerCase().includes(mk) && !el.querySelector(SEL)
    && !el.matches('form') && !el.closest('form') && !el.querySelector('form, textarea, input[type=file], input[type=text], input:not([type])'));
  return rows.slice(0, 5).map((el) => ({
    text: clean(el.innerText).slice(0, 160),
    controls: Array.from(el.querySelectorAll('button, a[href], [role=button], [role=menuitem]')).filter(vis)
      .map((b) => ({ label: clean(b.innerText || b.getAttribute('aria-label')) })).filter((c) => c.label),
  }));
}"""

# Stamp the one control labelled `label` inside the one marked record, for an identity click.
# Returns the nonce, or a reason string starting with '!' when it is not unambiguous.
MARK_CONTROL_JS = "(args) => {" + _VIS + r"""
  const mk = (args.marker || '').toLowerCase(), want = (args.label || '').toLowerCase().trim();
  const SEL = """ + repr(_RECORD_SEL) + r""";
  const rows = Array.from(document.querySelectorAll(SEL)).filter((el) => vis(el)
    && (el.innerText || '').toLowerCase().includes(mk) && !el.querySelector(SEL)
    && !el.matches('form') && !el.closest('form') && !el.querySelector('form, textarea, input[type=file], input[type=text], input:not([type])'));
  if (rows.length !== 1) return '!' + rows.length + ' marked records';
  const row = rows[0];
  const hits = Array.from(row.querySelectorAll('button, a[href], [role=button], [role=menuitem]')).filter(vis)
    .filter((b) => clean(b.innerText || b.getAttribute('aria-label')).toLowerCase() === want);
  if (hits.length !== 1) return '!' + hits.length + " controls named '" + want + "' in the record";
  document.querySelectorAll('[data-qaprobe-target]').forEach((e) => e.removeAttribute('data-qaprobe-target'));
  const nonce = 'q' + Math.random().toString(36).slice(2, 10);
  hits[0].setAttribute('data-qaprobe-target', nonce);
  return nonce;
}"""

# Visible modal layers, to tell whether a click opened a confirmation.
LAYER_COUNT_JS = "() => {" + _VIS + r"""
  return Array.from(document.querySelectorAll('[role=dialog], [role=alertdialog], [aria-modal="true"], dialog[open], [class*="modal" i]')).filter(vis).length;
}"""

# Is a record list actually on screen (rendered, not loading, not an error state)? Absence of the
# demo record only proves deletion when the list it would be in really rendered.
LIST_READY_JS = "() => {" + _VIS + r"""
  const err = Array.from(document.querySelectorAll('.error-text, [role=alert], [class*="error" i]')).filter(vis)
    .find((e) => /error|failed|could not|unable|\b[45]\d\d\b/i.test(e.innerText || ''));
  if (err) return { ok: false, why: 'error state: ' + clean(err.innerText).slice(0, 80) };
  const busy = Array.from(document.querySelectorAll('[aria-busy="true"], [class*="spinner" i], [class*="skeleton" i]')).some(vis)
    || Array.from(document.querySelectorAll('td, p, div, span')).some((e) => e.children.length === 0 && vis(e) && /^loading/i.test((e.innerText || '').trim()));
  if (busy) return { ok: false, why: 'still loading' };
  const rows = Array.from(document.querySelectorAll('tbody tr, [role=row], li')).filter(vis).length;
  const empty = Array.from(document.querySelectorAll('[class*="empty" i], [class*="state" i]')).some(vis);
  return { ok: rows > 0 || empty, why: rows || empty ? '' : 'no record list on screen', rows };
}"""
