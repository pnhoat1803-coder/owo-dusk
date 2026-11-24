/* ==========================================================================
   OwO-Dusk Fleet Command Center v2.6 - the Layout popover (position, pinned, theme, language), the only control in the
   navigation footer besides Sign out. Reads and writes through window.duskNav (nav-boot.js) and window.DuskI18n (i18n.js).
   Esc, a click outside, a second click on the button, or Tab out of the popover closes it. When the theme is Auto this
   file follows the operating system's light or dark setting. The fade between two themes is nav-boot.js.
   ========================================================================== */
(function () {
  "use strict";
  var btn = document.getElementById("nav-layout");
  var pop = document.getElementById("nav-layout-pop");
  var pin = document.getElementById("nav-pin");
  var note = document.getElementById("nav-forced-note");
  if (!btn || !pop || !pin || !window.duskNav) return;

  var nav = window.duskNav;
  var posBtns = Array.prototype.slice.call(pop.querySelectorAll("[data-nav-pos]"));
  var themeBtns = Array.prototype.slice.call(pop.querySelectorAll("[data-theme-pick]"));
  var langBtns = Array.prototype.slice.call(pop.querySelectorAll("[data-lang-pick]"));
  var open = false;

  function lang() { return window.DuskI18n ? window.DuskI18n.lang() : "en"; }

  function mark(list, attr, value, disabled) {
    list.forEach(function (b) {
      b.setAttribute("aria-pressed", b.getAttribute(attr) === value ? "true" : "false");
      if (disabled !== undefined) b.disabled = disabled;
    });
  }

  function sync() {
    var cur = nav.read();
    mark(posBtns, "data-nav-pos", cur.position, cur.forced);
    mark(themeBtns, "data-theme-pick", cur.theme);
    mark(langBtns, "data-lang-pick", lang());
    pin.setAttribute("aria-checked", cur.pinned ? "true" : "false");
    pin.classList.toggle("on", cur.pinned);
    pin.disabled = cur.forced;
    note.hidden = !cur.forced;
  }

  /* The popover is position: fixed and placed next to the button, on the side away from the screen edge */
  function place() {
    var r = btn.getBoundingClientRect(), pos = nav.read().position;
    var w = pop.offsetWidth || 280, h = pop.offsetHeight || 320, gap = 10;
    var vw = window.innerWidth, vh = window.innerHeight, left, top;
    if (pos === "left") { left = r.right + gap; top = r.bottom - h; }
    else if (pos === "right") { left = r.left - w - gap; top = r.bottom - h; }
    else if (pos === "top") { left = r.right - w; top = r.bottom + gap; }
    else { left = r.right - w; top = r.top - h - gap; }
    left = Math.max(8, Math.min(vw - w - 8, left));
    top = Math.max(8, Math.min(vh - h - 8, top));
    pop.style.left = Math.round(left) + "px";
    pop.style.top = Math.round(top) + "px";
  }

  function setOpen(v, returnFocus) {
    open = v;
    btn.setAttribute("aria-expanded", v ? "true" : "false");
    if (v) {
      pop.hidden = false;
      sync();
      place();
      var current = posBtns.filter(function (b) { return b.getAttribute("aria-pressed") === "true"; })[0];
      (current || posBtns[0]).focus();
    } else {
      pop.hidden = true;
      if (returnFocus) btn.focus();
    }
  }

  function setLang(next) {
    if (window.DuskI18n) window.DuskI18n.setLang(next);
    sync();
  }

  btn.addEventListener("click", function () { setOpen(!open, false); });
  posBtns.forEach(function (b) {
    b.addEventListener("click", function () { nav.write(b.getAttribute("data-nav-pos"), null); });
  });
  pin.addEventListener("click", function () { nav.write(null, nav.read().pinned ? "0" : "1"); });
  themeBtns.forEach(function (b) {
    b.addEventListener("click", function () { nav.setTheme(b.getAttribute("data-theme-pick")); });
  });
  langBtns.forEach(function (b) {
    b.addEventListener("click", function () { setLang(b.getAttribute("data-lang-pick")); });
  });

  window.addEventListener("dusk-nav", function () { sync(); if (open) place(); });
  window.addEventListener("dusk:langchange", function () { sync(); if (open) place(); });
  window.addEventListener("resize", function () { if (open) place(); });
  window.addEventListener("scroll", function () { if (open) place(); }, { passive: true });
  document.addEventListener("pointerdown", function (ev) {
    if (open && !pop.contains(ev.target) && !btn.contains(ev.target)) setOpen(false, false);
  });
  document.addEventListener("keydown", function (ev) {
    if (open && ev.key === "Escape") { ev.preventDefault(); setOpen(false, true); }
  });
  pop.addEventListener("focusout", function (ev) {
    var to = ev.relatedTarget;
    if (open && to && !pop.contains(to) && to !== btn) setOpen(false, false);
  });

  /* Auto theme follows the operating system (the popover, the page and the charts all update) */
  if (window.matchMedia) {
    var osLight = window.matchMedia("(prefers-color-scheme: light)");
    var follow = function () { nav.apply(); };
    if (osLight.addEventListener) osLight.addEventListener("change", follow);
    else if (osLight.addListener) osLight.addListener(follow);
  }
  /* Phone version: the link opens /m, and the choice of the desktop page (dusk.view) is cleared first */
  var phone = document.getElementById("nav-phone");
  if (phone) {
    phone.addEventListener("click", function () {
      try { window.localStorage.removeItem("dusk.view"); } catch (err) { /* blocked: the link still opens /m */ }
    });
  }
  sync();
})();
