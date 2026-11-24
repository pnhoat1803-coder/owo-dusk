/* ==========================================================================
   OwO-Dusk Fleet Command Center v2.6 - applied before the first paint: the sidebar position and pinned state (on
   <body>), and the theme and the Effects attribute (on <html>). Loaded synchronously as the first script of <body>, so
   the attributes exist before anything is drawn. Browser storage keys:
     dusk.nav.position  left | right | top | bottom   (default left)
     dusk.nav.pinned    "1" | "0"                    (default "1")
     dusk.theme         auto | dark | light           (default auto: follows the operating system)
   There is one look now (flat), so <html data-fx> is always "solid": nothing reads or writes dusk.fx any more, and a
   value an older version left in the browser storage is ignored. The attribute stays for the rules and checks that read it.
   Under 768 px the bar is always at the bottom and pinned; the saved choice is left as it is.
   Blocked storage (private windows) falls back to the defaults.
   ========================================================================== */
(function () {
  "use strict";
  var POSITIONS = ["left", "right", "top", "bottom"];
  var THEMES = ["auto", "dark", "light"];
  var KEY_POS = "dusk.nav.position", KEY_PIN = "dusk.nav.pinned", KEY_THEME = "dusk.theme";
  var KEY_VIEW = "dusk.view";
  /* How long <html data-theme-transition> stays on after a change of theme (liquid.css, section 13 fades the colours) */
  var THEME_FADE_MS = 400;
  /* The colour fade makes the browser animate every element that is drawn, and its cost grows with their number: about 250
     elements (the Overview) fade with no trouble, but the account list (3 000+) froze the page for most of a second. Above
     this many drawn elements the browser crossfades a snapshot instead (View Transitions), whose cost does not depend on
     the size of the page */
  var FADE_CSS_MAX = 600;
  /* Desktop hashes that have a phone tab keep it; the others open the phone's home screen */
  var PHONE_TABS = { overview: "home", accounts: "accounts", events: "events", logs: "logs" };
  var narrow = window.matchMedia ? window.matchMedia("(max-width: 767px)") : null;
  var lightOs = window.matchMedia ? window.matchMedia("(prefers-color-scheme: light)") : null;
  var lastTheme = null;
  var themeFadeTimer = 0;

  function stored(key) {
    try { return window.localStorage.getItem(key); } catch (err) { return null; }
  }
  function store(key, value) {
    try { window.localStorage.setItem(key, value); } catch (err) { /* blocked: the choice lasts until the page closes */ }
  }

  function saved() {
    var position = POSITIONS.indexOf(stored(KEY_POS)) >= 0 ? stored(KEY_POS) : "left";
    var pin = stored(KEY_PIN);
    pin = pin === "0" || pin === "1" ? pin : "1";
    var theme = THEMES.indexOf(stored(KEY_THEME)) >= 0 ? stored(KEY_THEME) : "auto";
    /* Flat design: always solid, whatever an older version stored under dusk.fx */
    return { position: position, pinned: pin, theme: theme, fx: "solid" };
  }

  function resolvedTheme(pref) {
    if (pref === "dark" || pref === "light") return pref;
    return lightOs && lightOs.matches ? "light" : "dark";
  }

  /* Sets the attributes from storage. Returns the theme in use (dark or light) */
  function apply() {
    var choice = saved(), forced = !!(narrow && narrow.matches);
    var resolved = resolvedTheme(choice.theme), root = document.documentElement, body = document.body;
    root.setAttribute("data-theme", resolved);
    root.setAttribute("data-theme-pref", choice.theme);
    root.setAttribute("data-fx", choice.fx);
    if (body) {
      body.setAttribute("data-nav", forced ? "bottom" : choice.position);
      body.setAttribute("data-nav-pinned", forced ? "1" : choice.pinned);
      body.setAttribute("data-nav-forced", forced ? "1" : "0");
    }
    return resolved;
  }

  function reducedMotion() {
    return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }

  /* A cheap guess of how many elements the page draws: the elements inside the views that are shown (a hidden view draws
     none, although its elements stay in the page), plus about 80 for the navigation and the other fixed parts. Asking every
     element for its box would be exact, but it forces a layout: 110 ms on the account list. The guess takes under 1 ms */
  function drawnEstimate() {
    var views = document.querySelectorAll(".view:not([hidden]), .m-view:not([hidden])"), n = 80;
    if (!views.length) return document.body ? document.body.getElementsByTagName("*").length : 0;
    for (var i = 0; i < views.length; i++) n += views[i].getElementsByTagName("*").length;
    return n;
  }

  /* <html data-theme-transition> goes on first and comes off THEME_FADE_MS later, so liquid.css fades the colours instead
     of snapping them. The theme is applied at once, in the same call */
  function fadeColours() {
    var root = document.documentElement;
    root.setAttribute("data-theme-transition", "");
    window.clearTimeout(themeFadeTimer);
    themeFadeTimer = window.setTimeout(function () { root.removeAttribute("data-theme-transition"); }, THEME_FADE_MS);
  }

  /* Applies the attributes and tells the charts and the popover when the theme really changed. Every route to a new theme
     passes here: the Layout popover, the phone's More tab and the operating system when the theme is Auto. How it fades:
       small page      colour fade (fadeColours), the theme is on the page when the call returns
       big page        the browser crossfades a snapshot (View Transitions, liquid.css has the timing); the theme is on the
                       page one frame later, inside the update callback. The charts hear about it after that
       big page, and the browser has no View Transitions: no fade, the theme switches at once
       reduced motion, the first call of the page, a repeat: no fade */
  function applyAll() {
    var before = lastTheme, next = resolvedTheme(saved().theme);
    var changed = before !== null && before !== next;
    if (changed && !reducedMotion()) {
      if (drawnEstimate() <= FADE_CSS_MAX) {
        fadeColours();
      } else if (document.startViewTransition) {
        lastTheme = next;
        try {
          var shown = document.startViewTransition(function () {
            apply();
            window.dispatchEvent(new Event("dusk:themechange"));
          });
          shown.ready.catch(function () { /* the transition was skipped (hidden tab, a newer one): the update ran anyway */ });
          return;
        } catch (err) { /* no transition: switch at once, below */ }
      }
    }
    lastTheme = apply();
    if (changed) window.dispatchEvent(new Event("dusk:themechange"));
  }

  function writeNav(position, pinned) {
    if (position) store(KEY_POS, position);
    if (pinned) store(KEY_PIN, pinned);
    applyAll();
    window.dispatchEvent(new Event("dusk-nav"));
  }

  function setTheme(pref) {
    if (THEMES.indexOf(pref) >= 0) store(KEY_THEME, pref);
    applyAll();
    window.dispatchEvent(new Event("dusk-nav"));
  }

  /* A phone (under 768 px, or a touch screen under 1024 px) that opens / goes to /m. dusk.view "desktop" keeps the
     desktop page, set by the link on /m. A #account/<id> link stays on the desktop for that one visit */
  function phoneScreen() {
    var coarse = window.matchMedia ? window.matchMedia("(pointer: coarse)").matches : false;
    return window.innerWidth < 768 || (coarse && window.innerWidth < 1024);
  }
  function redirectIfPhone() {
    if (window.location.pathname !== "/" || stored(KEY_VIEW) === "desktop" || !phoneScreen()) return;
    var hash = window.location.hash || "";
    if (hash.indexOf("#account/") === 0) return;
    var tab = PHONE_TABS[hash.replace(/^#/, "")];
    window.location.replace("/m" + (tab ? "#" + tab : ""));
  }

  window.duskNav = {
    read: function () {
      var choice = saved();
      return {
        position: choice.position,
        pinned: choice.pinned === "1",
        forced: !!(narrow && narrow.matches),
        theme: choice.theme,
        resolved: resolvedTheme(choice.theme),
        fx: choice.fx
      };
    },
    write: writeNav,
    setTheme: setTheme,
    apply: applyAll
  };

  redirectIfPhone();
  applyAll();
  if (narrow) {
    var changed = function () { applyAll(); window.dispatchEvent(new Event("dusk-nav")); };
    if (narrow.addEventListener) narrow.addEventListener("change", changed);
    else if (narrow.addListener) narrow.addListener(changed);
  }
})();
