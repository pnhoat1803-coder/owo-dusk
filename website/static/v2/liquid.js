/* ==========================================================================
   liquid.js  -  behaviour for the flat OwO-Dusk dashboard (v1, flat)
   The file keeps its name. What stays alive:
   - Sliding thumb for .lg-seg segmented controls.
   - Scroll-edge fade state for .lg-fade scroll containers.
   - ResizeObserver / MutationObserver scan that keeps the two above in step with the page.
   What is switched off (the code is still here, but nothing calls it):
   - Edge lens: refreshLens() returns at once, and <html data-fx> is always "solid", so lensWanted() is false.
   - Pointer glint: onPointerMove() returns at once and is not attached.
   - buildMap() and makeFilter() return at once, so no canvas or SVG filter is ever built.
   No dependencies, no network, no console output. Load with `defer`.
   Public API: window.DuskLiquid.refresh(), .lensActive(), .lensCount()
   ========================================================================== */
(function () {
  'use strict';
  if (window.DuskLiquid) return;

  var doc = document;
  var root = doc.documentElement;
  var NS = 'http://www.w3.org/2000/svg';
  var MAX_LENS = 8;
  var MAX_MAPS = 24;
  var ON_SEL = '[aria-pressed="true"],[aria-checked="true"],[aria-selected="true"],[aria-current="page"],.active,.is-active,[data-active="true"]';

  function mq(q) {
    try { return window.matchMedia(q); } catch (e) { return { matches: false, addEventListener: function () {} }; }
  }
  var mqReduceT = mq('(prefers-reduced-transparency: reduce)');
  var mqReduceM = mq('(prefers-reduced-motion: reduce)');
  var mqFine = mq('(hover: hover) and (pointer: fine)');

  function isChromium() {
    try {
      var b = navigator.userAgentData && navigator.userAgentData.brands;
      return !!(b && b.some(function (x) { return /Chromium/i.test(x.brand); }));
    } catch (e) { return false; }
  }
  var LENS_OK = isChromium();

  function each(list, fn) { Array.prototype.forEach.call(list, fn); }
  function px(v, d) { var n = parseFloat(v); return isFinite(n) ? n : d; }
  function shown(el) {
    if (!el.isConnected || el.offsetWidth < 8 || el.offsetHeight < 8) return false;
    return el.getClientRects().length > 0;
  }

  /* ---------- 1. Edge lens ------------------------------------------------ */
  var defs = null;
  var seq = 0;
  var maps = {};
  var mapKeys = [];
  var lensed = new Map(); /* element -> { id, key, filter } */

  function ensureDefs() {
    if (defs && defs.isConnected) return defs;
    var svg = doc.createElementNS(NS, 'svg');
    svg.setAttribute('id', 'lg-defs');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    svg.setAttribute('width', '0');
    svg.setAttribute('height', '0');
    svg.style.position = 'absolute';
    svg.style.width = '0';
    svg.style.height = '0';
    svg.style.overflow = 'hidden';
    svg.style.pointerEvents = 'none';
    defs = doc.createElementNS(NS, 'defs');
    svg.appendChild(defs);
    doc.body.appendChild(svg);
    return defs;
  }

  /* Displacement map for a rounded rectangle: neutral (128,128) inside,
     pointing inward inside a bezel band along the edge. R = x, G = y. */
  function buildMap(w, h, r, bezel) {
    return ''; /* Flat design: no displacement map is built */
    var key = w + 'x' + h + 'r' + r + 'b' + bezel;
    if (maps[key]) return maps[key];
    var c = doc.createElement('canvas');
    c.width = w;
    c.height = h;
    var ctx = c.getContext('2d');
    var img = ctx.createImageData(w, h);
    var d = img.data;
    var hw = w / 2, hh = h / 2;
    var ir = Math.min(r, hw, hh);
    for (var j = 0; j < h; j++) {
      var py = j + 0.5 - hh;
      var qy = Math.abs(py) - (hh - ir);
      for (var i = 0; i < w; i++) {
        var k = (j * w + i) * 4;
        var px0 = i + 0.5 - hw;
        var qx = Math.abs(px0) - (hw - ir);
        var ox = qx > 0 ? qx : 0, oy = qy > 0 ? qy : 0;
        var sd = Math.sqrt(ox * ox + oy * oy) + Math.min(Math.max(qx, qy), 0) - ir;
        var dist = -sd;
        var dx = 0, dy = 0;
        if (dist < bezel) {
          var t = 1 - (dist > 0 ? dist : 0) / bezel;
          t = t * t;
          var nx, ny;
          if (qx > 0 && qy > 0) {
            var l = Math.sqrt(qx * qx + qy * qy) || 1;
            nx = qx / l; ny = qy / l;
          } else if (qx > qy) { nx = 1; ny = 0; } else { nx = 0; ny = 1; }
          dx = -(px0 < 0 ? -nx : nx) * t;
          dy = -(py < 0 ? -ny : ny) * t;
        }
        d[k] = 128 + Math.round(127 * dx);
        d[k + 1] = 128 + Math.round(127 * dy);
        d[k + 2] = 128;
        d[k + 3] = 255;
      }
    }
    ctx.putImageData(img, 0, 0);
    var url = c.toDataURL('image/png');
    maps[key] = url;
    mapKeys.push(key);
    if (mapKeys.length > MAX_MAPS) delete maps[mapKeys.shift()];
    return url;
  }

  function makeFilter(id, w, h, url, scale) {
    return null; /* Flat design: no SVG filter is built */
    var f = doc.createElementNS(NS, 'filter');
    f.setAttribute('id', id);
    f.setAttribute('filterUnits', 'userSpaceOnUse');
    f.setAttribute('primitiveUnits', 'userSpaceOnUse');
    f.setAttribute('x', '0');
    f.setAttribute('y', '0');
    f.setAttribute('width', String(w));
    f.setAttribute('height', String(h));
    f.setAttribute('color-interpolation-filters', 'sRGB');
    var fi = doc.createElementNS(NS, 'feImage');
    fi.setAttribute('href', url);
    fi.setAttribute('x', '0');
    fi.setAttribute('y', '0');
    fi.setAttribute('width', String(w));
    fi.setAttribute('height', String(h));
    fi.setAttribute('preserveAspectRatio', 'none');
    fi.setAttribute('result', 'lgmap');
    var dm = doc.createElementNS(NS, 'feDisplacementMap');
    dm.setAttribute('in', 'SourceGraphic');
    dm.setAttribute('in2', 'lgmap');
    dm.setAttribute('scale', String(scale));
    dm.setAttribute('xChannelSelector', 'R');
    dm.setAttribute('yChannelSelector', 'G');
    f.appendChild(fi);
    f.appendChild(dm);
    return f;
  }

  function clearLens(el) {
    var st = lensed.get(el);
    if (!st) return;
    el.style.removeProperty('backdrop-filter');
    el.style.removeProperty('-webkit-backdrop-filter');
    el.removeAttribute('data-lg-lens');
    if (st.filter && st.filter.parentNode) st.filter.parentNode.removeChild(st.filter);
    lensed.delete(el);
  }

  function applyLens(el) {
    var w = Math.round(el.offsetWidth);
    var h = Math.round(el.offsetHeight);
    var cs = getComputedStyle(el);
    var r = Math.round(Math.min(px(cs.borderTopLeftRadius, 0), w / 2, h / 2));
    var bezel = Math.round(Math.min(px(cs.getPropertyValue('--lg-bezel'), 20), w / 2, h / 2));
    var scale = Math.round(px(cs.getPropertyValue('--lg-refract'), 48));
    var key = w + 'x' + h + 'r' + r + 'b' + bezel + 's' + scale;
    var st = lensed.get(el);
    if (!st) { st = { id: 'lg-lens-' + (++seq), key: '', filter: null }; lensed.set(el, st); }
    if (st.key !== key) {
      var f = makeFilter(st.id, w, h, buildMap(w, h, r, bezel), scale);
      if (st.filter && st.filter.parentNode) st.filter.parentNode.replaceChild(f, st.filter);
      else ensureDefs().appendChild(f);
      st.filter = f;
      st.key = key;
    }
    el.style.setProperty('backdrop-filter',
      'blur(var(--lg-b, 10px)) url(#' + st.id + ') saturate(var(--lg-sat, 180%)) brightness(var(--lg-bright, 1))');
    el.setAttribute('data-lg-lens', 'on');
  }

  function lensWanted() {
    return LENS_OK && root.getAttribute('data-fx') === 'liquid' && !mqReduceT.matches;
  }

  function refreshLens() {
    return; /* Flat design: lens disabled */
    var on = lensWanted();
    root.setAttribute('data-lens', on ? 'on' : 'off');
    var want = [];
    if (on) {
      each(doc.querySelectorAll('.lg-lens'), function (el) {
        if (want.length < MAX_LENS && shown(el)) want.push(el);
      });
    }
    lensed.forEach(function (st, el) { if (want.indexOf(el) < 0) clearLens(el); });
    want.forEach(applyLens);
    want.forEach(function (el) { observeSize(el); });
  }

  /* ---------- 2. Pointer glint ---------------------------------------------- */
  var glintEl = null, gx = 0, gy = 0, gRaf = 0;
  function dropGlint() {
    if (glintEl) {
      glintEl.style.removeProperty('--lg-mx');
      glintEl.style.removeProperty('--lg-my');
    }
    glintEl = null;
  }
  function paintGlint() {
    gRaf = 0;
    if (!glintEl || !glintEl.isConnected) return;
    var r = glintEl.getBoundingClientRect();
    glintEl.style.setProperty('--lg-mx', Math.round(gx - r.left) + 'px');
    glintEl.style.setProperty('--lg-my', Math.round(gy - r.top) + 'px');
  }
  function onPointerMove(e) {
    return; /* Flat design: glint disabled */
    if (!mqFine.matches || (e.pointerType && e.pointerType !== 'mouse')) return;
    var t = e.target && e.target.closest ? e.target.closest('.btn, .lg-btn, .lg, .glass') : null;
    if (t && t.hasAttribute('data-lg-noglint')) t = null;
    if (t !== glintEl) { dropGlint(); glintEl = t; }
    if (!t) return;
    gx = e.clientX;
    gy = e.clientY;
    if (!gRaf) gRaf = requestAnimationFrame(paintGlint);
  }

  /* ---------- 3. Segmented controls --------------------------------------- */
  function isOn(c) {
    if (c.matches(ON_SEL)) return true;
    var inp = c.matches('input') ? c : c.querySelector('input[type="radio"], input[type="checkbox"]');
    return !!(inp && inp.checked);
  }
  function syncSeg(seg) {
    var on = null;
    each(seg.children, function (c) {
      var hit = !c.hidden && isOn(c);
      if (hit && !on) on = c;
      if (hit) { if (!c.hasAttribute('data-lg-on')) c.setAttribute('data-lg-on', ''); }
      else if (c.hasAttribute('data-lg-on')) c.removeAttribute('data-lg-on');
    });
    if (!on || !shown(seg)) { seg.removeAttribute('data-lg-ready'); return; }
    var x = on.offsetLeft + 'px';
    var w = on.offsetWidth + 'px';
    var prev = seg.style.getPropertyValue('--seg-x');
    if (prev === x && seg.style.getPropertyValue('--seg-w') === w && seg.hasAttribute('data-lg-ready')) return;
    seg.style.setProperty('--seg-x', x);
    seg.style.setProperty('--seg-w', w);
    if (prev && prev !== x && !mqReduceM.matches && seg.hasAttribute('data-lg-ready')) {
      seg.classList.remove('lg-seg--moving');
      void seg.offsetWidth;
      seg.classList.add('lg-seg--moving');
      clearTimeout(seg._lgT);
      seg._lgT = setTimeout(function () { seg.classList.remove('lg-seg--moving'); }, 520);
    }
    seg.setAttribute('data-lg-ready', '');
  }
  function onSegEvent(e) {
    var seg = e.target && e.target.closest ? e.target.closest('.lg-seg') : null;
    if (seg) requestAnimationFrame(function () { syncSeg(seg); });
  }

  /* ---------- 4. Scroll-edge fade ------------------------------------------ */
  function syncFade(el) {
    var max = el.scrollHeight - el.clientHeight;
    var top = el.scrollTop > 2;
    var bottom = max > 2 && el.scrollTop < max - 2;
    var v = top && bottom ? 'both' : top ? 'top' : bottom ? 'bottom' : 'none';
    if (el.getAttribute('data-lg-fade') !== v) el.setAttribute('data-lg-fade', v);
  }
  var fadeQueue = new Set(), fadeRaf = 0;
  function queueFade(el) {
    fadeQueue.add(el);
    if (!fadeRaf) fadeRaf = requestAnimationFrame(function () {
      fadeRaf = 0;
      fadeQueue.forEach(syncFade);
      fadeQueue.clear();
    });
  }
  function onScroll(e) {
    var t = e.target;
    if (t && t.classList && t.classList.contains('lg-fade')) queueFade(t);
  }

  /* ---------- 5. Observers and scan ---------------------------------------- */
  var ro = null;
  var sized = new WeakSet();
  function observeSize(el) {
    if (!ro || sized.has(el)) return;
    sized.add(el);
    ro.observe(el);
  }
  var scanTimer = 0;
  function scheduleScan(delay) {
    clearTimeout(scanTimer);
    scanTimer = setTimeout(scan, delay == null ? 120 : delay);
  }
  function scan() {
    scanTimer = 0;
    each(doc.querySelectorAll('.lg-seg'), function (seg) {
      syncSeg(seg);
      observeSize(seg);
    });
    each(doc.querySelectorAll('.lg-fade'), function (el) {
      syncFade(el);
      observeSize(el);
    });
    lensed.forEach(function (st, el) { if (!el.isConnected) clearLens(el); });
    refreshLens();
  }

  function init() {
    if ('ResizeObserver' in window) {
      ro = new ResizeObserver(function () { scheduleScan(120); });
    }
    var mo = new MutationObserver(function (records) {
      for (var i = 0; i < records.length; i++) {
        var r = records[i];
        if (r.type === 'childList' || r.attributeName !== 'class' || !r.target.classList.contains('lg-seg')) {
          scheduleScan(140);
          return;
        }
      }
    });
    mo.observe(doc.body, {
      subtree: true,
      childList: true,
      attributes: true,
      attributeFilter: ['class', 'hidden', 'open', 'aria-pressed', 'aria-checked', 'aria-selected', 'aria-current', 'aria-expanded', 'data-state', 'data-active']
    });
    new MutationObserver(function () { scheduleScan(0); }).observe(root, {
      attributes: true,
      attributeFilter: ['data-fx', 'data-theme', 'lang']
    });
    new MutationObserver(function () { scheduleScan(0); }).observe(doc.body, {
      attributes: true,
      attributeFilter: ['data-nav', 'data-nav-pinned']
    });
    if (mqReduceT.addEventListener) mqReduceT.addEventListener('change', function () { scheduleScan(0); });
    /* Flat design: the glint listeners (pointermove, pointerleave) are not attached, so a moving mouse costs nothing here */
    doc.addEventListener('click', onSegEvent, true);
    doc.addEventListener('change', onSegEvent, true);
    doc.addEventListener('scroll', onScroll, { capture: true, passive: true });
    window.addEventListener('resize', function () { scheduleScan(150); }, { passive: true });
    if (doc.fonts && doc.fonts.ready) doc.fonts.ready.then(function () { scheduleScan(0); });
    scan();
  }

  window.DuskLiquid = {
    version: 1,
    lensSupported: LENS_OK,
    refresh: function () { scan(); },
    lensActive: function () { return lensWanted(); },
    lensCount: function () { return lensed.size; }
  };

  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', init);
  else init();
})();
