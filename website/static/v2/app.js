/* ==========================================================================
   OwO-Dusk Fleet Command Center v2.6 - Frontend Systems Engine
   Vanilla ES6+ JavaScript, Zero Build Steps, Zero External CDNs, Strict No-XSS
   Every visible text goes through T() (window.DuskI18n, see i18n.js). A language switch writes the text again from the
   data already on the page, so it sends no request.
   ========================================================================== */
(function () {
  "use strict";

  var SPRITE = "/static/v2/icons.svg#";
  var SVGNS = "http://www.w3.org/2000/svg";
  var VIEWS = ["overview", "accounts", "events", "network", "logs", "runtime", "config"];
  var BACKOFF = [3000, 6000, 12000, 30000];
  /* An IPv4 address, as the Server & bot tab accepts it (website.host) */
  var IPV4_RE = /^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/;
  var EM = "—";

  /* Text in the language in use (i18n.js). Without it the key itself is shown */
  function T(key, vars) { return window.DuskI18n ? window.DuskI18n.t(key, vars) : key; }
  /* A name that is a key only when the key exists (a status or an event type the code does not list keeps its raw name) */
  function lab(prefix, name) {
    var key = prefix + name;
    return window.DuskI18n && window.DuskI18n.has(key) ? window.DuskI18n.t(key) : String(name || "");
  }

  var IC = {
    hunting: "hunt", sleeping: "sleep", paused: "pause", captcha: "captcha", stopped: "stop", offline: "offline",
    starting: "clock", connecting: "reconnect", reconnecting: "reconnect", removed: "info",
    good: "check", warn: "error", bad: "close", unknown: "info",
    daily: "daily", error: "error", reconnect: "reconnect", config: "settings"
  };

  var S = {
    view: "", accountId: null, summary: null, sources: null, version: 0, skew: 0, refreshMs: 3000,
    map: new Map(), nodes: new Map(), known: new Map(), items: [],
    page: 1, pages: 1, total: 0, q: "", fstate: "", fhealth: "", sort: "health", order: "desc", size: 50, mode: "cards",
    lastPageFetch: 0, pageDirty: false, pageSeq: 0, failStep: 0, pollers: {},
    ev: { list: [], last: 0, type: "", q: "" }, con: { last: 0, stick: true, count: 0 },
    detailVersion: -1, gwVersion: -1, attSig: null, cmdSig: null, cmdMore: false, rtDays: null,
    thresholds: null, lastDetail: null, gwLast: null, rtLast: null, cfgRows: [],
    cfgTab: "dashboard", loadedConfigs: {},
    showRemoved: false, bot: null, botPending: null, lan: null, noPassword: false,
    quietRender: false   // the next renderList() plays no entrance (a language switch rebuilds every card)
  };

  /* ---------- DOM & String Helpers ---------- */
  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  function svgEl(tag, attrs) {
    var n = document.createElementNS(SVGNS, tag);
    for (var k in attrs) {
      if (Object.prototype.hasOwnProperty.call(attrs, k)) n.setAttribute(k, attrs[k]);
    }
    return n;
  }
  function icon(name, cls) {
    var s = svgEl("svg", { "class": "ic" + (cls ? " " + cls : ""), "aria-hidden": "true" });
    s.appendChild(svgEl("use", { href: SPRITE + "i-" + name }));
    return s;
  }
  function setTxt(node, text) { if (node && node.textContent !== text) node.textContent = text; }
  function setCls(node, cls) { if (node && node.className !== cls) node.className = cls; }
  /* Tips live in data-tip: the tooltip engine shows them on hover, focus and tap (see initTooltips) */
  function setTitle(node, text) { if (node && node.getAttribute("data-tip") !== text) node.setAttribute("data-tip", text); }
  function clear(node) { if (node) { while (node.firstChild) node.removeChild(node.firstChild); } }
  function nowS() { return (Date.now() + S.skew) / 1000; }
  function isNum(v) { return typeof v === "number" && isFinite(v); }
  function fmtInt(n) { return isNum(n) ? Math.round(n).toLocaleString("en-US") : EM; }
  function fmt1(n) { return isNum(n) ? (Math.round(n * 10) / 10).toLocaleString("en-US", { maximumFractionDigits: 1 }) : EM; }
  function fmt2(n) { return isNum(n) ? (Math.round(n * 100) / 100).toLocaleString("en-US", { maximumFractionDigits: 2 }) : EM; }
  function fmtSigned(n) { return isNum(n) ? (n > 0 ? "+" : "") + fmtInt(n) : EM; }
  function fmtAgo(sec) {
    if (!isNum(sec)) return EM;
    if (sec < 0) sec = 0;
    if (sec < 60) return T("ago.sec", { n: Math.floor(sec) });
    if (sec < 3600) return T("ago.min", { n: Math.floor(sec / 60) });
    if (sec < 86400) return T("ago.hm", { h: Math.floor(sec / 3600), m: Math.floor((sec % 3600) / 60) });
    return T("ago.day", { n: Math.floor(sec / 86400) });
  }
  function fmtDur(sec) {
    if (!isNum(sec)) return EM;
    sec = Math.max(0, Math.round(sec));
    var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
    if (h >= 1) return h + " h " + (m < 10 ? "0" : "") + m + " m";
    if (m >= 1) return m + " m " + (sec % 60) + " s";
    return sec + " s";
  }
  function fmtTime(ts) { return isNum(ts) ? new Date(ts * 1000).toLocaleString() : EM; }
  function fmtClock(ts) {
    if (!isNum(ts)) return EM;
    var d = new Date(ts * 1000);
    return (d.getHours() < 10 ? "0" : "") + d.getHours() + ":" + (d.getMinutes() < 10 ? "0" : "") + d.getMinutes();
  }
  function shortId(id) { return T("acc.short", { id: String(id).slice(-4) }); }
  function nameOf(e) { return e.name || shortId(e.id); }
  function store(key, value) { try { window.localStorage.setItem("owoDash." + key, value); } catch (err) {} }
  function recall(key, fallback) { try { var v = window.localStorage.getItem("owoDash." + key); return v === null ? fallback : v; } catch (err) { return fallback; } }
  /* Keys named as they are stored (dusk.*): the same try/catch, a blocked store gives the fallback */
  function storeKey(key, value) { try { window.localStorage.setItem(key, value); } catch (err) {} }
  function recallKey(key, fallback) { try { var v = window.localStorage.getItem(key); return v === null ? fallback : v; } catch (err) { return fallback; } }

  /* Compact numbers for the Overview and the account cards: 1,234 · 12.3K · 156.6M · 1.2B. opts.signed adds + to a
     positive delta, opts.decimals keeps one decimal below 10 000 (rates such as hunts per minute) */
  function fmtCompact(n, opts) {
    if (!isNum(n)) return EM;
    var signed = !!(opts && opts.signed), keep = !!(opts && opts.decimals), a = Math.abs(n);
    var sign = n < 0 ? "-" : (signed && n > 0 ? "+" : "");
    if (a < 10000) return sign + (keep ? (Math.round(a * 10) / 10).toLocaleString("en-US", { maximumFractionDigits: 1 }) : Math.round(a).toLocaleString("en-US"));
    var steps = [[1e3, "K"], [1e6, "M"], [1e9, "B"]], idx = 0, i, v;
    for (i = 0; i < steps.length; i++) if (a >= steps[i][0]) idx = i;
    v = Math.round(a / steps[idx][0] * 10) / 10;
    if (v >= 1000 && idx < steps.length - 1) { idx++; v = Math.round(a / steps[idx][0] * 10) / 10; }
    return sign + v.toFixed(1).replace(/\.0$/, "") + steps[idx][1];
  }
  /* The exact value with separators, for the title of a compact number: 156,644,139 */
  function fmtFull(n, opts) {
    var signed = !!(opts && opts.signed), keep = !!(opts && opts.decimals), a = Math.abs(n);
    var sign = n < 0 ? "-" : (signed && n > 0 ? "+" : "");
    return sign + (keep ? (Math.round(a * 10) / 10).toLocaleString("en-US", { maximumFractionDigits: 1 }) : Math.round(a).toLocaleString("en-US"));
  }
  /* Writes the compact number into node, and the exact one into its title (the browser shows it on hover) */
  function setValue(node, n, opts) {
    if (!node) return;
    setTxt(node, fmtCompact(n, opts));
    if (isNum(n)) {
      var full = fmtFull(n, opts);
      if (node.getAttribute("title") !== full) node.setAttribute("title", full);
    } else if (node.hasAttribute("title")) {
      node.removeAttribute("title");
    }
  }
  /* A status pill holds an icon (svg > use) and a text span: this sets the class, the icon and the text */
  function setStatusPill(pill, cls, iconName, text) {
    if (!pill) return;
    setCls(pill, cls);
    var use = pill.firstChild && pill.firstChild.firstChild, href = SPRITE + "i-" + iconName;
    if (use && use.getAttribute("href") !== href) use.setAttribute("href", href);
    setTxt(pill.lastChild, text);
  }

  /* Batch DOM writes in animation frame */
  var queue = [], queued = false;
  function W(fn) {
    queue.push(fn);
    if (!queued) { queued = true; window.requestAnimationFrame(flush); }
  }
  function flush() {
    queued = false;
    var jobs = queue; queue = [];
    for (var i = 0; i < jobs.length; i++) {
      try { jobs[i](); } catch (err) { if (window.console) console.warn(err); }
    }
  }

  /* ---------- Network Layer ---------- */
  /* The CSRF token the server put in the page (a meta tag). Every POST sends it back in X-Dusk-CSRF */
  function csrfToken() {
    var meta = document.querySelector("meta[name='csrf-token']");
    return meta ? meta.getAttribute("content") || "" : "";
  }
  function api(path, options) {
    var opts = options || {};
    opts.credentials = "same-origin";
    opts.headers = opts.headers || {};
    opts.headers.Accept = "application/json";
    if (String(opts.method || "GET").toUpperCase() === "POST") opts.headers["X-Dusk-CSRF"] = csrfToken();
    return window.fetch(path, opts).then(function (res) {
      if (res.status === 401) { window.location.href = "/login"; throw new Error("auth"); }
      if (!res.ok) {
        // The server says why (a refused setting, a rate limit); fall back to the status code
        return res.json().catch(function () { return {}; }).then(function (body) {
          var err = new Error((body && (body.message || body.error)) || ("HTTP " + res.status));
          err.status = res.status;
          err.code = body && body.error;
          err.body = body || {};
          throw err;
        });
      }
      var stamp = Date.parse(res.headers.get("Date") || "");
      if (isFinite(stamp)) S.skew = stamp + 500 - Date.now();
      return res.json();
    });
  }
  function netOk() {
    if (S.failStep) { S.failStep = 0; $("pill-conn").hidden = true; }
  }
  function netFail(err) {
    if (err && err.message === "auth") return;
    S.failStep = Math.min(S.failStep + 1, BACKOFF.length);
    $("pill-conn").hidden = false;
  }
  function delayFor(base) { return S.failStep ? Math.max(base, BACKOFF[S.failStep - 1]) : base; }

  function poller(name, fn, baseMs, delayFirst) {
    var p = { name: name, fn: fn, base: baseMs, timer: null, stopped: false, busy: false };
    p.run = function () {
      if (p.stopped || document.hidden || p.busy) return;
      p.busy = true;
      var done = function () {
        p.busy = false;
        if (!p.stopped && !document.hidden) p.timer = window.setTimeout(p.run, delayFor(p.base));
      };
      var result;
      try { result = Promise.resolve(fn()); } catch (err) { result = Promise.reject(err); }
      result.then(function () { netOk(); done(); }, function (err) { netFail(err); done(); });
    };
    p.kick = function () { window.clearTimeout(p.timer); p.timer = null; p.run(); };
    p.stop = function () { p.stopped = true; window.clearTimeout(p.timer); p.timer = null; };
    if (S.pollers[name]) S.pollers[name].stop();
    S.pollers[name] = p;
    if (delayFirst) p.timer = window.setTimeout(p.run, baseMs); else p.kick();
    return p;
  }
  function stopPoller(name) { if (S.pollers[name]) { S.pollers[name].stop(); delete S.pollers[name]; } }

  document.addEventListener("visibilitychange", function () {
    for (var k in S.pollers) {
      if (!Object.prototype.hasOwnProperty.call(S.pollers, k)) continue;
      if (document.hidden) { window.clearTimeout(S.pollers[k].timer); S.pollers[k].timer = null; }
      else S.pollers[k].kick();
    }
  });

  /* ---------- Remote IPC Control Client ---------- */
  function controlAccount(target, action, value, onDone) {
    var payload = { target: target, action: action };
    if (value !== undefined && value !== null) payload.value = value;
    return api("/api/v2/control", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function (res) {
      showFleetStatus(T("fleet.queued", { action: lab("act.", action), target: target === "all" ? T("target.all") : target }));
      if (onDone) onDone(null, res);
      if (S.pollers.changes) S.pollers.changes.kick();
      return res;
    }).catch(function (err) {
      showFleetStatus(T("fleet.error", { msg: err.message }), true);
      if (onDone) onDone(err);
      throw err;
    });
  }

  function showFleetStatus(msg, isErr) {
    var box = $("fleet-action-status");
    if (!box) return;
    setTxt(box, msg);
    box.style.color = isErr ? "var(--bad)" : "var(--accent-cyan)";
    box.classList.add("active");
    window.clearTimeout(box._timer);
    box._timer = window.setTimeout(function () { box.classList.remove("active"); }, 4000);
  }

  /* Dialogs: one at a time. Escape or a click on the backdrop closes it, Tab stays inside it, and focus goes back
     to the control that opened it. Errors are written inside the dialog, never with alert() */
  var modalState = null;
  function openModal(backdrop, focusTarget) {
    closeModal();
    modalState = { backdrop: backdrop, opener: document.activeElement };
    backdrop.hidden = false;
    if (focusTarget) focusTarget.focus();
  }
  function closeModal() {
    var state = modalState;
    if (!state) return;
    modalState = null;
    state.backdrop.hidden = true;
    if (state.opener && state.opener.focus && document.body.contains(state.opener)) state.opener.focus({ preventScroll: true });
  }
  function modalKey(ev) {
    if (ev.key === "Escape") { ev.preventDefault(); closeModal(); return; }
    if (ev.key !== "Tab") return;
    var box = modalState.backdrop;
    var items = Array.prototype.filter.call(box.querySelectorAll("input, button, select, textarea"), function (n) {
      return !n.disabled && n.offsetParent !== null;
    });
    if (!items.length) return;
    var first = items[0], last = items[items.length - 1], active = document.activeElement;
    if (ev.shiftKey && (active === first || !box.contains(active))) { ev.preventDefault(); last.focus(); }
    else if (!ev.shiftKey && (active === last || !box.contains(active))) { ev.preventDefault(); first.focus(); }
  }
  function openConfirmModal(msg, onConfirm) {
    setTxt($("modal-confirm-msg"), msg);
    $("modal-confirm-ok").onclick = function () { closeModal(); onConfirm(); };
    $("modal-confirm-cancel").onclick = closeModal;
    openModal($("modal-confirm"), $("modal-confirm-cancel"));  // Cancel first: the safe choice keeps focus
  }
  function openChannelModal(accountId, accountName, onConfirm) {
    var input = $("modal-channel-input"), err = $("modal-channel-error");
    setTxt($("modal-channel-account"), accountName + " (" + accountId + ")");
    input.value = ""; err.hidden = true; setTxt(err, "");
    function submit() {
      var val = input.value.trim();
      if (!/^\d{15,22}$/.test(val)) {
        setTxt(err, T("modal.channel.invalid"));
        err.hidden = false;
        input.focus();
        return;
      }
      closeModal();
      onConfirm(val);
    }
    $("modal-channel-confirm").onclick = submit;
    $("modal-channel-cancel").onclick = closeModal;
    input.onkeydown = function (ev) { if (ev.key === "Enter") { ev.preventDefault(); submit(); } };
    openModal($("modal-channel"), input);
  }
  function initModals() {
    document.querySelectorAll(".modal-backdrop").forEach(function (box) {
      box.addEventListener("mousedown", function (ev) { if (ev.target === box) closeModal(); });
    });
    document.addEventListener("keydown", function (ev) {
      if (modalState) { modalKey(ev); return; }
      if (ev.key === "Escape" && S.view === "account") window.location.hash = "#accounts";
    });
    $("drawer-scrim").addEventListener("click", function () { window.location.hash = "#accounts"; });
  }

  /* Sleep range: two seconds values, shortest first. The bot needs a pair (min < max) */
  function readRange(minName, maxName) {
    var minEl = document.querySelector("[name='" + minName + "']"), maxEl = document.querySelector("[name='" + maxName + "']");
    var lo = minEl ? parseFloat(minEl.value) : NaN, hi = maxEl ? parseFloat(maxEl.value) : NaN;
    if (!isNum(lo) || !isNum(hi) || lo < 0 || hi <= lo) {
      showConfigBanner(T("cfg.err.range"), false);
      return null;
    }
    return [lo, hi];
  }
  function readPercent(raw) {
    var n = parseFloat(raw);
    return isNum(n) && n >= 0 && n <= 100 ? Math.round(n) : null;
  }

  /* Sidebar collapse, kept per browser. It only applies to the left and right bars. Under 900 px those bars are always
     compact. Top and bottom bars have no collapse (the button is hidden there). Position changes come as dusk-nav */
  function initNav() {
    var layout = document.querySelector(".app-layout"), btn = $("nav-toggle");
    if (!layout || !btn) return;
    var collapsed = recall("nav", "open") === "collapsed";
    var narrow = window.matchMedia ? window.matchMedia("(max-width: 900px)") : null;
    function apply() {
      var pos = document.body.getAttribute("data-nav");
      var side = pos === "left" || pos === "right";
      layout.classList.toggle("nav-collapsed", side && (collapsed || !!(narrow && narrow.matches)));
      btn.setAttribute("aria-expanded", collapsed ? "false" : "true");
      btn.setAttribute("aria-label", T(collapsed ? "nav.expand" : "nav.collapse"));
      setTitle(btn, T(collapsed ? "nav.expandTip" : "nav.collapseTip"));
    }
    btn.addEventListener("click", function () { collapsed = !collapsed; store("nav", collapsed ? "collapsed" : "open"); apply(); });
    window.addEventListener("dusk-nav", apply);
    window.addEventListener("dusk:langchange", apply);
    if (narrow) {
      if (narrow.addEventListener) narrow.addEventListener("change", apply);
      else if (narrow.addListener) narrow.addListener(apply);
    }
    apply();
  }

  /* ---------- Summary and Header ---------- */
  function applySummary(sum, sources, extra) {
    S.summary = sum;
    if (sources) S.sources = sources;
    // The LAN block and the password flag come with the summary only (api_summary, website/api_v2.py)
    if (extra && extra.lan) { S.lan = extra.lan; W(renderLan); W(renderBanner); }
    if (extra && extra.no_password !== undefined) { S.noPassword = !!extra.no_password; W(renderBanner); }
    // The chip thresholds come with the summary only (not with the changes poll), so the last ones are kept between polls
    if (extra && extra.thresholds) S.thresholds = extra.thresholds;
    if (sum && isNum(sum.refresh_s)) S.refreshMs = Math.max(2000, sum.refresh_s * 1000);
    W(renderHeader);
    W(renderQuoteStatus);
    if (S.view === "overview") W(renderOverview);
  }
  /* ---------- Bot control: the state, the three buttons, the LAN addresses and the banner ---------- */
  function setDisabled(node, off) { if (node && node.disabled !== off) node.disabled = off; }
  function botState() { return S.bot ? S.bot.state : ""; }
  /* Starting, stopping, or an action in progress: the bot poll runs every second */
  function botBusy() { var st = botState(); return st === "starting" || st === "stopping" || !!S.botPending; }

  /* The text, colour and icon of the bot state from GET /api/v2/bot. A crash shows the exit code */
  function botPillFor(bot) {
    var st = bot ? bot.state : "";
    if (st === "running") return { cls: "pill ok", icon: "check", text: T("bot.state.running", { dur: fmtDur(bot.uptime_s) }) };
    if (st === "starting") return { cls: "pill warn", icon: "clock", text: T("bot.state.starting") };
    if (st === "stopping") return { cls: "pill warn", icon: "clock", text: T("bot.state.stopping") };
    if (st === "crashed") {
      var code = bot.last_exit ? bot.last_exit.code : null;
      return { cls: "pill off", icon: "error", text: isNum(code) ? T("bot.state.crashed", { code: code }) : T("bot.state.crashedNoCode") };
    }
    return { cls: "pill", icon: "offline", text: T("bot.state.stopped") };
  }

  /* The nav footer and the Overview show the state. Start is on only when the bot is stopped or crashed; Stop and Restart
     only when it runs or starts; none is on while an action or a stop is in progress */
  function renderHeader() {
    var bot = S.bot, pill = botPillFor(bot), st = botState();
    setStatusPill($("bot-state"), pill.cls, pill.icon, pill.text);
    setStatusPill($("ov-bot"), pill.cls, pill.icon, pill.text);
    var idle = !!bot && !S.botPending && st !== "stopping";
    var canStart = idle && (st === "stopped" || st === "crashed");
    var canStop = idle && (st === "running" || st === "starting");
    setDisabled($("bot-start"), !canStart); setDisabled($("ov-bot-start"), !canStart);
    setDisabled($("bot-stop"), !canStop); setDisabled($("ov-bot-stop"), !canStop);
    setDisabled($("bot-restart"), !canStop); setDisabled($("ov-bot-restart"), !canStop);
    var cli = $("bot-cli");
    if (cli) cli.hidden = !(!!bot && bot.supervised === false && (st === "running" || st === "starting"));
  }

  /* The yellow banner: no password is set (the page is for this machine only), or a LAN address was asked for without one */
  function renderBanner() {
    var lan = S.lan || {};
    $("banner").hidden = !(S.noPassword || lan.password_banner);
  }

  /* The addresses in the Layout popover: this machine first, then each LAN address with its copy button */
  function renderLan() {
    var lan = S.lan, list = $("lan-list");
    if (!lan || !list) return;
    var urls = lan.lan_urls || [];
    clear(list);
    var local = el("li", "lan-row");
    local.appendChild(el("span", "lan-tag", T("lan.this")));
    local.appendChild(el("span", "lan-url", lan.local_url || ""));
    list.appendChild(local);
    urls.forEach(function (url) {
      var row = el("li", "lan-row"), btn = el("button", "gel small lan-copy", T("lan.copy"));
      btn.type = "button";
      btn.addEventListener("click", function () { copyText(url, btn); });
      row.appendChild(el("span", "lan-url", url));
      row.appendChild(btn);
      list.appendChild(row);
    });
    $("lan-none").hidden = urls.length > 0;
    $("lan-hint").hidden = urls.length > 0;
  }

  /* Copies a text with the Clipboard API. Without it, or when it is refused, the text is selected in a hidden box and copied
     with execCommand; when that fails too, the page asks for a manual copy */
  function copyText(text, btn) {
    var done = function () {
      setTxt(btn, T("lan.copied"));
      btn.classList.add("copied");
      window.setTimeout(function () { setTxt(btn, T("lan.copy")); btn.classList.remove("copied"); }, 1800);
    };
    var fallback = function () {
      var area = document.createElement("textarea"), ok = false;
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.left = "-9999px";
      document.body.appendChild(area);
      area.select();
      try { ok = document.execCommand("copy"); } catch (err) { ok = false; }
      document.body.removeChild(area);
      if (ok) done(); else showFleetStatus(T("lan.copyManual"), true);
    };
    if (window.navigator && window.navigator.clipboard && window.navigator.clipboard.writeText) {
      window.navigator.clipboard.writeText(text).then(done, fallback);
    } else {
      fallback();
    }
  }

  /* One request for the bot state. While the bot starts or stops, and while an action runs, it polls every second */
  function pollBot() {
    return api("/api/v2/bot").then(function (d) {
      S.bot = d;
      var p = S.pollers.bot;
      if (p) p.base = botBusy() ? 1000 : S.refreshMs;
      W(renderHeader);
    });
  }

  /* The text of a failed bot request, in the language in use. The server sends a code (website/api_v2.py) */
  function botErrorText(err) {
    var body = err.body || {};
    if (err.status === 429) return T("bot.err.rate", { n: body.retry_after || 5 });
    if (err.status === 403) return T("bot.err.csrf");
    if (body.error === "busy") return T("bot.err.busy");
    if (body.error === "already_running") return T("bot.err.running");
    if (body.error === "not_stopped") return T("bot.err.notStopped");
    if (body.error === "start_failed") return T("bot.err.start");
    if (body.error === "bad_request" && body.message) return body.message;
    return T("bot.err.generic", { msg: body.message || err.message || "" });
  }

  /* The answer of a start, stop or restart is written in the nav footer and in the Overview */
  var BOT_DONE = { start: "bot.done.start", stop: "bot.done.stop", restart: "bot.done.restart" };
  function showBotMsg(text, isErr) {
    var box = $("bot-msg");
    if (box) {
      setTxt(box, text);
      box.className = "bot-msg" + (isErr ? " err" : "");
      box.hidden = false;
      window.clearTimeout(box._timer);
      box._timer = window.setTimeout(function () { box.hidden = true; }, isErr ? 9000 : 5000);
    }
    showFleetStatus(text, isErr);
  }
  /* A start, stop or restart. The buttons are off while it runs. The server answers when the action is done */
  function botAction(name, body) {
    if (S.botPending) return Promise.resolve();
    S.botPending = name;
    W(renderHeader);
    return api("/api/v2/bot/" + name, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (res) {
      if (res && res.status) S.bot = res.status;
      showBotMsg(T(BOT_DONE[name]), false);
    }, function (err) {
      showBotMsg(botErrorText(err), true);
    }).then(function () {
      S.botPending = null;
      W(renderHeader);
      if (S.pollers.bot) S.pollers.bot.kick();
    });
  }

  /* Start: an optional auto-stop in whole minutes, 1 to 1440. Empty means the autoStop of the config */
  function openBotStart() {
    var input = $("modal-start-min"), err = $("modal-start-error");
    input.value = "";
    err.hidden = true;
    setTxt(err, "");
    function submit() {
      var raw = input.value.trim(), minutes = null;
      if (raw !== "") {
        var n = parseInt(raw, 10);
        if (!/^\d+$/.test(raw) || n < 1 || n > 1440) {
          setTxt(err, T("bot.dialog.invalid"));
          err.hidden = false;
          input.focus();
          return;
        }
        minutes = n;
      }
      closeModal();
      botAction("start", { maxRuntimeMinutes: minutes });
    }
    $("modal-start-ok").onclick = submit;
    $("modal-start-cancel").onclick = closeModal;
    input.onkeydown = function (ev) { if (ev.key === "Enter") { ev.preventDefault(); submit(); } };
    openModal($("modal-start"), input);
  }
  /* Stop and Restart ask first, in the glass dialog, and say how many accounts disconnect */
  function openBotConfirm(name) {
    var n = S.summary ? S.summary.accounts_total : null;
    var msg = name === "stop" ? T("bot.confirm.stop", { n: fmtInt(n) }) : T("bot.confirm.restart", { n: fmtInt(n) });
    openConfirmModal(msg, function () { botAction(name); });
  }
  function initBot() {
    document.querySelectorAll("[data-bot-action]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var name = btn.getAttribute("data-bot-action");
        if (name === "start") openBotStart(); else openBotConfirm(name);
      });
    });
  }

  /* ---------- Overview View ---------- */
  /* Heroes and chips: the ids are in v2.html. A tone class (tone-good, tone-warn, tone-bad, tone-muted) colours a value */
  function renderHeroes(sum) {
    var bot = !!sum.bot_online, on = sum.accounts_online, total = sum.accounts_total;
    setValue($("kpi-online-n"), on);
    setCls($("kpi-online-n"), "tone-" + (!bot ? "muted" : isNum(on) && on > 0 ? "good" : "warn"));
    setTxt($("kpi-online-of"), "/ " + fmtCompact(total));
    setTxt($("kpi-online-ctx"), T("hero.onlineCtx", { total: fmtCompact(total) }));
    setValue($("kpi-hpm"), sum.hunts_per_min_total, { decimals: true });
    setValue($("kpi-hunts"), sum.hunts_today);
    var cow = sum.cowoncy_today_total;
    setValue($("kpi-cow"), cow, { signed: true });
    setCls($("kpi-cow"), "ov-val" + (isNum(cow) && cow > 0 ? " tone-good" : isNum(cow) && cow < 0 ? " tone-bad" : ""));
    setValue($("kpi-cow-total"), sum.cowoncy_total);
  }
  /* Captcha and Errors chips take the dashboard thresholds (summary "thresholds", from config/dashboard_view.json):
     zero is muted, at or above the bad mark is red, at or above the warn mark is amber, otherwise the normal colour */
  function chipTone(value, warn, bad) {
    if (!isNum(value) || value === 0) return " tone-muted";
    if (isNum(bad) && value >= bad) return " tone-bad";
    if (isNum(warn) && value >= warn) return " tone-warn";
    return "";
  }
  function thresholdTip(warn, bad) {
    return isNum(warn) && isNum(bad) ? " " + T("chip.thr", { warn: warn, bad: bad }) : "";
  }
  function renderChips(sum) {
    var th = S.thresholds || {};
    setValue($("kpi-cap"), sum.captcha_today);
    setCls($("kpi-cap"), "ov-chip-val" + chipTone(sum.captcha_today, th.captchaWarnPerDay, th.captchaBadPerDay));
    setTitle($("chip-cap"), T("chip.cap.tip") + thresholdTip(th.captchaWarnPerDay, th.captchaBadPerDay));
    setValue($("kpi-err"), sum.errors_1h);
    setCls($("kpi-err"), "ov-chip-val" + chipTone(sum.errors_1h, th.errorsWarnPerHour, th.errorsBadPerHour));
    setTitle($("chip-err"), T("chip.err.tip") + thresholdTip(th.errorsWarnPerHour, th.errorsBadPerHour));
    setValue($("kpi-net"), sum.inbound_avg_kb_min, { decimals: true });
    setCls($("kpi-net"), "ov-chip-val" + (sum.inbound_over_bad > 0 ? " tone-bad" : sum.inbound_over_warn > 0 ? " tone-warn" : ""));
    setTitle($("chip-net"), T("chip.net.tip") + " " +
      T("chip.net.detail", { warn: fmtInt(sum.inbound_over_warn), bad: fmtInt(sum.inbound_over_bad) }));
    setValue($("kpi-lvl"), sum.level_avg, { decimals: true });
    setTitle($("chip-lvl"), !isNum(sum.level_avg) ? T("chip.lvl.off") :
      T("chip.lvl.detail", { max: sum.level_max, who: sum.level_max_name ? " (" + sum.level_max_name + ")" : "",
        gain: fmtInt(sum.levels_gained_7d), known: fmtInt(sum.levels_known), total: fmtInt(sum.accounts_total) }));
    /* D7: autoquest on, done today and Quest Seals in total (read only) */
    var questVars = { on: fmtInt(sum.quest_on), done: fmtInt(sum.quest_done), seals: fmtInt(sum.quest_seals) };
    setTxt($("kpi-quest"), isNum(sum.quest_on) ? T("quest.summary", questVars) : EM);
    setTitle($("chip-quest"), isNum(sum.quest_on) ? T("chip.quest.detail", questVars) : T("chip.quest.off"));
    var h = sum.health || {}, g = h.good || 0, w = h.warn || 0, b = h.bad || 0;
    setTxt($("kpi-h-good"), fmtInt(g));
    setTxt($("kpi-h-warn"), fmtInt(w));
    setTxt($("kpi-h-bad"), fmtInt(b));
    setCls($("kpi-h-warn"), w > 0 ? "tone-warn" : "tone-muted");
    setCls($("kpi-h-bad"), b > 0 ? "tone-bad" : "tone-muted");
    setTitle($("chip-health"), T("chip.health.detail", { good: g, warn: w, bad: b, unknown: h.unknown || 0 }));
  }

  var RING_ORDER = ["good", "warn", "bad", "sleeping", "unknown"];
  function renderRing(ring) {
    var svg = $("ring"), total = 0, i;
    for (i = 0; i < RING_ORDER.length; i++) total += ring[RING_ORDER[i]] || 0;
    clear(svg);
    var r = 54, c = 2 * Math.PI * r, off = 0;
    var tr = svgEl("circle", { cx: 75, cy: 75, r: r, "class": "seg track" });
    svg.appendChild(tr);
    if (total > 0) {
      RING_ORDER.forEach(function (k) {
        var n = ring[k] || 0;
        if (!n) return;
        var len = c * n / total;
        var seg = svgEl("circle", { cx: 75, cy: 75, r: r, "class": "seg " + k, transform: "rotate(-90 75 75)",
          "stroke-dasharray": Math.max(0, len - 1.5).toFixed(2) + " " + (c - Math.max(0, len - 1.5)).toFixed(2), "stroke-dashoffset": (-off).toFixed(2) });
        setTitle(seg, T("ring.tip", { label: lab("ring.", k), n: n }));
        svg.appendChild(seg);
        off += len;
      });
    }
    var t1 = svgEl("text", { x: 75, y: 78, "text-anchor": "middle" }); t1.textContent = String(total);
    var t2 = svgEl("text", { x: 75, y: 96, "text-anchor": "middle", "class": "sub" }); t2.textContent = T("ring.total");
    svg.appendChild(t1); svg.appendChild(t2);
    var lg = $("legend"); clear(lg);
    RING_ORDER.forEach(function (k) {
      var li = el("li"); li.appendChild(el("span", "dot " + k));
      li.appendChild(el("span", "", lab("ring.", k) + ": " + (ring[k] || 0)));
      lg.appendChild(li);
    });
  }

  /* Segmented Fleet State Distribution Bar */
  var STATE_ORDER = ["hunting", "sleeping", "paused", "captcha", "reconnecting", "connecting", "starting", "stopped", "offline"];
  function renderFleetStateBar(states) {
    var bar = $("fleet-state-bar"), leg = $("fleet-state-legend");
    if (!bar || !leg) return;
    clear(bar); clear(leg);
    var total = 0;
    STATE_ORDER.forEach(function (s) { total += (states[s] || 0); });
    if (total === 0) return;

    /* The bar has every state. The legend names the three largest and counts the rest; its tip lists them all */
    var present = STATE_ORDER.filter(function (s) { return (states[s] || 0) > 0; });
    present.forEach(function (s) {
      var count = states[s];
      var pct = ((count / total) * 100).toFixed(1);
      var seg = el("div", "segment " + s);
      seg.style.width = pct + "%";
      setTitle(seg, T("fleet.bar.tip", { state: lab("state.", s), n: count, pct: pct }));
      bar.appendChild(seg);
    });
    var largest = present.slice().sort(function (a, b) { return states[b] - states[a]; });
    largest.slice(0, 3).forEach(function (s) {
      var li = el("span", "segmented-legend-item");
      li.appendChild(el("span", "legend-dot " + s));
      li.appendChild(el("span", "", lab("state.", s) + " " + states[s]));
      leg.appendChild(li);
    });
    if (largest.length > 3) {
      var more = el("span", "segmented-legend-item", T("fleet.more", { n: largest.length - 3 }));
      leg.appendChild(more);
    }
    setTitle(leg, present.map(function (s) { return lab("state.", s) + " " + states[s]; }).join(", "));
  }

  /* Command Breakdown Progress Bars */
  /* Top 8 commands (the server sends 8). A "+N more" button shows the rest inside the panel, which scrolls */
  function renderFleetCommandBreakdown(commands) {
    var box = $("fleet-command-breakdown");
    if (!box) return;
    var entries = [];
    for (var k in commands) {
      if (Object.prototype.hasOwnProperty.call(commands, k)) entries.push([k, commands[k]]);
    }
    entries.sort(function (a, b) { return b[1] - a[1]; });
    var sig = S.cmdMore + "|" + entries.map(function (e) { return e[0] + "=" + e[1]; }).join(",");
    if (sig === S.cmdSig) return;
    S.cmdSig = sig;
    clear(box);
    if (!entries.length) {
      box.appendChild(el("div", "muted", T("cmd.none")));
      return;
    }
    var max = 1;
    entries.forEach(function (e) { if (e[1] > max) max = e[1]; });
    (S.cmdMore ? entries : entries.slice(0, 8)).forEach(function (pair) {
      var name = pair[0], count = pair[1];
      var pct = Math.min(100, Math.max(2, (count / max) * 100)).toFixed(1);
      var row = el("div", "cmd-bar-row");
      row.appendChild(el("span", "cmd-name", name));
      var track = el("div", "cmd-track");
      var fill = el("div", "cmd-fill");
      fill.style.width = pct + "%";
      track.appendChild(fill);
      row.appendChild(track);
      var cnt = el("span", "cmd-count");
      setValue(cnt, count);
      row.appendChild(cnt);
      box.appendChild(row);
    });
    if (entries.length > 8) {
      var more = el("button", "btn btn-more", S.cmdMore ? T("cmd.showTop", { n: 8 }) : T("fleet.more", { n: entries.length - 8 }));
      more.type = "button";
      more.setAttribute("aria-expanded", S.cmdMore ? "true" : "false");
      more.addEventListener("click", function () {
        S.cmdMore = !S.cmdMore;
        S.cmdSig = "";
        renderFleetCommandBreakdown((S.summary && S.summary.commands) || {});
      });
      box.appendChild(more);
    }
  }

  /* Needs Attention: name, status chip, first reason. Rebuilt only when the list changes, so the scroll position stays */
  function renderAttention(list) {
    var ul = $("attention");
    if (!ul) return;
    var sig = (list || []).map(function (i) { return i.id + ":" + i.health + ":" + (i.reasons || []).join("|"); }).join(";");
    if (sig === S.attSig) return;
    S.attSig = sig;
    clear(ul);
    if (!list || !list.length) {
      var empty = el("li", "empty");
      empty.appendChild(icon("check"));
      empty.appendChild(el("span", "", T("att.none")));
      ul.appendChild(empty);
      return;
    }
    list.forEach(function (item) {
      var li = el("li", "ov-att");
      var a = el("a", "who", item.name || shortId(item.id));
      a.href = "#account/" + encodeURIComponent(item.id);
      li.appendChild(a);
      li.appendChild(chip(item.health, lab("health.", item.health)));
      var reason = item.reasons && item.reasons.length ? item.reasons[0] : lab("health.", item.health);
      li.appendChild(el("span", "why", reason));
      setTitle(li, (item.reasons || []).join(". ") || lab("health.", item.health));
      ul.appendChild(li);
    });
  }

  /* Data sources: one pill with the count (green when all report, amber when some do, red when none). The tip lists each */
  function renderSources() {
    var pill = $("sources");
    if (!pill) return;
    var s = S.sources || {}, names = Object.keys(s), ok = 0, lines = [];
    names.forEach(function (name) {
      var src = s[name] || {};
      if (src.ok) ok++;
      lines.push((src.ok ? T("src.ok", { name: name }) : T("src.off", { name: name }) + (src.error ? " (" + src.error + ")" : "")));
    });
    setCls(pill, !names.length ? "pill" : ok === names.length ? "pill ok" : ok === 0 ? "pill off" : "pill warn");
    setTxt(pill, T("src.pill", { ok: ok, total: names.length }));
    setTitle(pill, lines.length ? lines.join(". ") : T("src.none"));
  }

  /* Status strip: uptime today (the session start is in its tip) and the auto-stop time, e.g. 21:40 (in 1 h 12 m) */
  function renderUptime() {
    var sum = S.summary;
    if (!sum) return;
    var up = $("uptime"), stop = $("autostop");
    setTxt(up, fmtDur(sum.uptime_today_s));
    setTitle(up, isNum(sum.run_started_at)
      ? T("up.started", { clock: fmtClock(sum.run_started_at), ago: fmtAgo(nowS() - sum.run_started_at) })
      : T("up.none"));
    var a = sum.autostop || {}, text = T("auto.off"), tip = T("auto.offTip");
    if (a.enabled) {
      if (a.stopping) { text = T("auto.stopping"); tip = T("auto.stoppingTip"); }
      else if (isNum(a.stops_at)) { text = T("auto.at", { clock: fmtClock(a.stops_at), dur: fmtDur(a.stops_at - nowS()) }); tip = T("auto.leftTip"); }
      else { text = T("auto.on"); tip = T("auto.onTip"); }
    }
    setTxt(stop, text);
    setTitle(stop, tip);
  }

  function renderOverview() {
    var sum = S.summary;
    if (!sum) return;
    renderHeroes(sum);
    renderChips(sum);
    renderRing(sum.ring || {});
    renderFleetStateBar(sum.states || {});
    renderFleetCommandBreakdown(sum.commands || {});
    renderAttention(sum.attention);
    renderSources();
    renderUptime();
  }

  function chip(kind, label) {
    var c = el("span", "chip " + kind);
    c.appendChild(icon(IC[kind] || "info"));
    c.appendChild(el("b", "", label || kind));
    return c;
  }

  /* ---------- Accounts View (Keyed Non-Destructive Reconciliation) ---------- */
  /* The card metrics. unit and tip are keys, read in the language in use (makeCard) */
  var METRICS = [
    { id: "hpm", ic: "hunt", unit: "unit.huntsMin", tip: "metric.tip.hpm" },
    { id: "last", ic: "clock", unit: "", tip: "metric.tip.last" },
    { id: "cow", ic: "cowoncy", unit: "unit.cowoncy", tip: "metric.tip.cow" },
    { id: "dcow", ic: "cowoncy", unit: "unit.today", tip: "metric.tip.dcow" },
    { id: "cap", ic: "captcha", unit: "unit.captcha", tip: "metric.tip.cap" },
    { id: "net", ic: "network", unit: "unit.kbMin", tip: "metric.tip.net" },
    { id: "daily", ic: "daily", unit: "", tip: "metric.tip.daily" },
    { id: "health", ic: "", unit: "", tip: "metric.tip.health" }
  ];

  /* Action buttons (cards and table): an icon, a text span (hidden in a narrow table) and an aria-label */
  function actionButton(kind, label, iconName) {
    /* kind is btn-sleep or btn-primary; the matching lg-btn--* modifier is the flat button look (liquid.css, section 4) */
    var b = el("button", "btn " + kind + " " + kind.replace("btn-", "lg-btn--"));
    b.type = "button";
    b.appendChild(icon(iconName, "sm"));
    b.appendChild(el("span", "btn-text", label));
    b.setAttribute("aria-label", label);
    return b;
  }
  /* A sleeping account shows Wake with the sun icon. The click handler picks sleep or wake from the state (makeCard) */
  function setSleepButton(b, sleeping) {
    var label = sleeping ? T("btn.wake") : T("btn.sleep"), href = SPRITE + (sleeping ? "i-sun" : "i-sleep");
    var use = b.firstChild && b.firstChild.firstChild;
    if (use && use.getAttribute("href") !== href) use.setAttribute("href", href);
    setTxt(b.querySelector(".btn-text"), label);
    b.setAttribute("aria-label", label);
    setTitle(b, sleeping ? T("btn.wakeTip") : T("btn.sleepTip"));
  }

  function makeCard(id) {
    var root = el("article", "card glass-lite h-unknown");
    root.tabIndex = 0;
    root.setAttribute("role", "link");
    root.setAttribute("data-id", id);

    var hd = el("div", "hd"), nm = el("span", "nm"), state = el("span", "chip unknown");
    var stIcon = icon("info"), stLabel = el("b", "", "");
    state.appendChild(stIcon); state.appendChild(stLabel);
    hd.appendChild(nm); hd.appendChild(state); root.appendChild(hd);

    var lvl = el("div", "lvl"), badge = el("span", "lvl-badge"), lvlTxt = el("span", "", "LVL " + EM), bar = makeBar();
    badge.appendChild(icon("level")); badge.appendChild(lvlTxt); lvl.appendChild(badge); lvl.appendChild(bar); root.appendChild(lvl);

    var dl = el("div", "metrics"), refs = { root: root, nm: nm, state: state, stIcon: stIcon, stLabel: stLabel, m: {}, lvl: lvl, lvlTxt: lvlTxt, bar: bar };
    METRICS.forEach(function (m) {
      var d = el("div"), v = el("span", "v", EM), u = el("span", "u", m.unit ? T(m.unit) : "");
      if (m.id === "health") {
        var hc = el("span", "chip unknown"), hi = icon("info"), hl = el("b", "", "");
        hc.appendChild(hi); hc.appendChild(hl); d.appendChild(hc);
        refs.hChip = hc; refs.hIcon = hi; refs.hLabel = hl;
      } else {
        d.appendChild(icon(m.ic, "sm")); d.appendChild(v); if (m.unit) d.appendChild(u);
        refs.m[m.id] = v;
      }
      setTitle(d, T(m.tip));
      dl.appendChild(d);
    });
    root.appendChild(dl);

    var spark = svgEl("svg", { "class": "spark", viewBox: "0 0 120 30", preserveAspectRatio: "none", role: "img", "aria-label": T("spark.aria") });
    var area = svgEl("path", { "class": "a", d: "" }), line = svgEl("path", { "class": "l", d: "" });
    spark.appendChild(area); spark.appendChild(line);
    root.appendChild(spark);

    var why = el("div", "why-line"); root.appendChild(why);

    /* Per-Account Quick Action Buttons */
    var actRow = el("div", "card-actions-row");
    var btnSleep = actionButton("btn-sleep", T("btn.sleep"), "sleep");
    setTitle(btnSleep, T("btn.toggle.tip"));
    var btnInspect = actionButton("btn-primary", T("btn.inspect"), "eye");
    setTitle(btnInspect, T("btn.inspect.tip"));

    actRow.appendChild(btnSleep);
    actRow.appendChild(btnInspect);
    root.appendChild(actRow);

    refs.area = area; refs.line = line; refs.why = why; refs.id = id; refs.sparkSig = "";
    refs.btnSleep = btnSleep; refs.btnInspect = btnInspect;

    btnSleep.addEventListener("click", function (ev) {
      ev.stopPropagation();
      var cur = S.map.get(id);
      var action = (cur && cur.state === "sleeping") ? "wake" : "sleep";
      controlAccount(id, action);
    });

    btnInspect.addEventListener("click", function (ev) {
      ev.stopPropagation();
      window.location.hash = "#account/" + encodeURIComponent(id);
    });

    var open = function () { window.location.hash = "#account/" + encodeURIComponent(id); };
    root.addEventListener("click", open);
    root.addEventListener("keydown", function (ev) { if (ev.key === "Enter") open(); });
    return refs;
  }

  function setChipRefs(chipEl, iconEl, labelEl, kind, text) {
    setCls(chipEl, "chip " + kind);
    var href = SPRITE + "i-" + (IC[kind] || "info");
    if (iconEl.firstChild.getAttribute("href") !== href) iconEl.firstChild.setAttribute("href", href);
    setTxt(labelEl, text === undefined ? kind : text);
  }

  function sparkPath(values) {
    var n = values.length, max = 1, i;
    for (i = 0; i < n; i++) if (values[i] > max) max = values[i];
    var pts = [];
    for (i = 0; i < n; i++) pts.push([(i / (n - 1)) * 120, 28 - (values[i] / max) * 24]);
    var d = "M" + pts.map(function (p) { return p[0].toFixed(1) + " " + p[1].toFixed(1); }).join(" L");
    return { line: d, area: d + " L120 30 L0 30 Z" };
  }

  function makeBar() { var b = el("span", "xpbar empty"); b.appendChild(el("i")); return b; }
  function setBar(bar, pct) {
    var known = isNum(pct), width = known ? Math.max(0, Math.min(100, pct)).toFixed(1) + "%" : "0%";
    setCls(bar, known ? "xpbar" : "xpbar empty");
    if (bar.firstChild.style.width !== width) bar.firstChild.style.width = width;
  }
  function dailyText(e) { return e.daily_done_today === null || e.daily_done_today === undefined ? EM : (e.daily_done_today ? T("daily.done") : T("daily.pending")); }

  function patchCard(n, e) {
    setCls(n.root, "card glass-lite h-" + e.health + (e.removed ? " is-removed" : ""));
    setTxt(n.nm, nameOf(e)); setTitle(n.nm, nameOf(e) + " (ID: " + e.id + ")");
    setChipRefs(n.state, n.stIcon, n.stLabel, e.removed ? "removed" : e.state, e.removed ? T("state.removed") : lab("state.", e.state));
    setTxt(n.m.hpm, fmt1(e.hunts_per_min_15m));
    n.m.last.setAttribute("data-at", isNum(e.last_hunt_at) ? String(e.last_hunt_at) : ""); renderAgo(n.m.last);
    setValue(n.m.cow, e.cowoncy);
    setValue(n.m.dcow, e.cowoncy_delta_today, { signed: true });
    setTxt(n.m.cap, fmtInt(e.captcha_today));
    setTxt(n.m.net, fmt1(e.inbound_kb_min));
    setTxt(n.m.daily, dailyText(e));
    setTxt(n.lvlTxt, "LVL " + (isNum(e.level) ? fmtInt(e.level) : EM)); setBar(n.bar, e.xp_pct); setLevelTip(n.lvl, e);
    setChipRefs(n.hChip, n.hIcon, n.hLabel, e.health, lab("health.", e.health));
    var why = e.health_reasons && e.health_reasons.length ? e.health_reasons[0] : "";
    setTxt(n.why, why);
    setSleepButton(n.btnSleep, e.state === "sleeping");

    var sig = e.spark ? e.spark.join(",") : "";
    if (sig !== n.sparkSig) {
      n.sparkSig = sig;
      if (e.spark) { var p = sparkPath(e.spark); n.line.setAttribute("d", p.line); n.area.setAttribute("d", p.area); }
      else { n.line.setAttribute("d", ""); n.area.setAttribute("d", ""); }
    }
  }

  /* D7: the autoquest state of one account, read only. q is the entry's quest block: null when the quest cog is off, and
     saved (no live data: the bot is stopped) when the state comes from quests.sqlite */
  function questStatusText(q) {
    if (!q) return T("quest.off");
    var open = fmtInt((q.open_quests || []).length);
    var state = q.all_done ? T("quest.done") : T("quest.openN", { n: open });
    return q.saved ? T("quest.saved") + ": " + state : state;
  }
  function questOpenText(q) {
    var list = (q && q.open_quests) || [];
    if (!list.length) return T("quest.none");
    return list.map(function (x) { return x.id + " " + fmtInt(x.current) + "/" + fmtInt(x.total); }).join(", ");
  }
  function questNextText(q) {
    if (!q || !isNum(q.next_quest_at)) return EM;
    var left = q.next_quest_at - nowS();
    var when = left > 0 ? T("quest.inHours", { h: (Math.round(left / 360) / 10).toFixed(1) }) : T("quest.now");
    return fmtTime(q.next_quest_at) + " (" + when + ")";
  }

  /* Level of one account: the stored last good read (levels.sqlite). rank and the time of the read go in a tooltip */
  function levelText(e) {
    if (!isNum(e.level)) return EM;
    var xp = isNum(e.xp_cur) && isNum(e.xp_need)
      ? " · XP " + fmtInt(e.xp_cur) + " / " + fmtInt(e.xp_need) + (isNum(e.xp_pct) ? " (" + fmt1(e.xp_pct) + "%)" : "")
      : "";
    return "LVL " + fmtInt(e.level) + xp;
  }
  function setLevelTip(node, e) {
    var parts = [];
    if (isNum(e.level) && isNum(e.rank)) parts.push(T("level.rank", { rank: fmtInt(e.rank) }));
    if (isNum(e.level) && isNum(e.level_checked_at)) parts.push(T("level.checkedAt", { time: fmtTime(e.level_checked_at) }));
    if (parts.length) setTitle(node, parts.join(" · ")); else node.removeAttribute("data-tip");
  }

  function renderAgo(node) {
    var at = parseFloat(node.getAttribute("data-at"));
    if (!isNum(at)) { setTxt(node, EM); node.removeAttribute("data-tip"); return; }
    setTxt(node, fmtAgo(nowS() - at)); setTitle(node, fmtTime(at));
  }

  /* The column titles are keys (col.*), read in the language in use (buildHead) */
  var COLS = [
    { key: "name", label: "col.name", sort: "name", get: function (e) { return nameOf(e); } },
    { key: "state", label: "col.state", sort: "state", chip: "state" },
    { key: "health", label: "col.health", sort: "health", chip: "health" },
    { key: "hunts_per_min_15m", label: "col.hpm", sort: "hunts_per_min_15m", num: 1, get: function (e) { return fmt1(e.hunts_per_min_15m); } },
    { key: "hunts_total", label: "col.hunts", sort: "hunts_total", num: 1, get: function (e) { return fmtInt(e.hunts_total); } },
    { key: "battles_total", label: "col.battles", sort: "battles_total", num: 1, get: function (e) { return fmtInt(e.battles_total); } },
    { key: "level", label: "col.level", sort: "level", num: 1, lvl: 1 },
    { key: "last", label: "col.last", sort: "last_hunt_ago_s", num: 1, ago: "last_hunt_at" },
    { key: "cowoncy", label: "col.cowoncy", sort: "cowoncy", num: 1, get: function (e) { return fmtInt(e.cowoncy); } },
    { key: "dcow", label: "col.dcow", sort: "cowoncy_delta_today", num: 1, get: function (e) { return fmtSigned(e.cowoncy_delta_today); } },
    { key: "daily", label: "col.daily", get: dailyText },
    { key: "cap", label: "col.cap", sort: "captcha_today", num: 1, get: function (e) { return fmtInt(e.captcha_today); } },
    { key: "net", label: "col.net", sort: "inbound_kb_min", num: 1, get: function (e) { return fmt1(e.inbound_kb_min); } },
    { key: "err", label: "col.err", sort: "errors_1h", num: 1, get: function (e) { return fmtInt(e.errors_1h); } },
    { key: "rc", label: "col.rc", sort: "reconnects_1h", num: 1, get: function (e) { return fmtInt(e.reconnects_1h); } },
    { key: "actions", label: "col.actions", num: 1, act: 1 }
  ];

  function buildHead() {
    var tr = $("thead"); clear(tr);
    COLS.forEach(function (c) {
      var th = el("th", (c.num ? "num" : "") + (c.act ? " act" : "")); th.setAttribute("scope", "col");
      if (c.sort) {
        var b = el("button", "", T(c.label)); b.type = "button"; b.setAttribute("data-sort", c.sort);
        b.addEventListener("click", function () {
          if (S.sort === c.sort) S.order = S.order === "desc" ? "asc" : "desc";
          else { S.sort = c.sort; S.order = c.sort === "name" || c.sort === "state" ? "asc" : "desc"; }
          syncControls(); loadPage().catch(netFail);
        });
        th.appendChild(b);
      } else { th.textContent = T(c.label); }
      tr.appendChild(th);
    });
  }
  function markSorted() {
    var ths = $("thead").children;
    for (var i = 0; i < ths.length; i++) {
      var b = ths[i].firstChild, on = b && b.getAttribute && b.getAttribute("data-sort") === S.sort;
      ths[i].setAttribute("aria-sort", on ? (S.order === "asc" ? "ascending" : "descending") : "none");
    }
  }

  function makeRow(id) {
    var tr = el("tr"); tr.tabIndex = 0; tr.setAttribute("data-id", id);
    var refs = { root: tr, cells: {}, id: id };
    COLS.forEach(function (c) {
      var td = el("td", (c.num ? "num" : "") + (c.act ? " act" : ""));
      if (c.lvl) {
        var lt = el("span", "", EM), lb = makeBar();
        td.className = "num lvlcell"; td.appendChild(lt); td.appendChild(lb);
        refs.cells[c.key] = { td: td, txt: lt, bar: lb };
      } else if (c.chip) {
        var ch = el("span", "chip unknown"), ic = icon("info"), lb = el("b", "");
        ch.appendChild(ic); ch.appendChild(lb); td.appendChild(ch);
        refs.cells[c.key] = { chip: ch, ic: ic, lb: lb };
      } else if (c.act) {
        var rowSleep = actionButton("btn-sleep", T("btn.sleep"), "sleep"), rowInsp = actionButton("btn-primary", T("btn.inspect"), "eye");
        setTitle(rowSleep, T("btn.toggle.tip")); setTitle(rowInsp, T("btn.inspect.tip"));
        var rowActs = el("span", "row-actions");
        rowActs.appendChild(rowSleep); rowActs.appendChild(rowInsp); td.appendChild(rowActs);
        refs.cells[c.key] = { td: td, sleep: rowSleep, insp: rowInsp };

        rowSleep.addEventListener("click", function (ev) {
          ev.stopPropagation();
          var cur = S.map.get(id);
          var action = (cur && cur.state === "sleeping") ? "wake" : "sleep";
          controlAccount(id, action);
        });
        rowInsp.addEventListener("click", function (ev) {
          ev.stopPropagation();
          window.location.hash = "#account/" + encodeURIComponent(id);
        });
      } else {
        refs.cells[c.key] = td;
      }
      tr.appendChild(td);
    });

    var open = function () { window.location.hash = "#account/" + encodeURIComponent(id); };
    tr.addEventListener("click", open);
    tr.addEventListener("keydown", function (ev) { if (ev.key === "Enter") open(); });
    return refs;
  }

  function patchRow(n, e) {
    setCls(n.root, e.removed ? "is-removed" : "");
    COLS.forEach(function (c) {
      var cell = n.cells[c.key];
      if (c.lvl) { setTxt(cell.txt, isNum(e.level) ? fmtInt(e.level) : EM); setBar(cell.bar, e.xp_pct); setLevelTip(cell.td, e); }
      else if (c.chip) {
        var stateRemoved = c.chip === "state" && e.removed;
        setChipRefs(cell.chip, cell.ic, cell.lb, stateRemoved ? "removed" : e[c.chip], stateRemoved ? T("state.removed") : lab(c.chip + ".", e[c.chip]));
      }
      else if (c.ago) { cell.setAttribute("data-at", isNum(e[c.ago]) ? String(e[c.ago]) : ""); renderAgo(cell); }
      else if (c.act) { setSleepButton(cell.sleep, e.state === "sleeping"); }
      else { setTxt(cell, c.get(e)); }
    });
  }

  /* Keyed Non-Destructive DOM Patching */
  function renderList() {
    var tableMode = S.mode === "table", parent = tableMode ? $("rows") : $("cards");
    $("cards").hidden = tableMode; $("tablebox").hidden = !tableMode;

    // Moving a focused node can drop focus, so the focused card or button is remembered and given back
    var focusNode = document.activeElement, focusId = null, focusPart = -1;
    if (focusNode && parent.contains(focusNode)) {
      var host = focusNode.closest("[data-id]");
      if (host) {
        focusId = host.getAttribute("data-id");
        focusPart = host === focusNode ? -2 : Array.prototype.indexOf.call(host.querySelectorAll("button"), focusNode);
      }
    }

    var old = S.nodes, fresh = new Map(), born = [];

    S.items.forEach(function (e, idx) {
      var n = old.get(e.id);
      var needsCreate = !n || (tableMode && !n.cells) || (!tableMode && n.cells);
      if (needsCreate) {
        n = tableMode ? makeRow(e.id) : makeCard(e.id);
        born.push(n.root);
      }
      tableMode ? patchRow(n, e) : patchCard(n, e);
      fresh.set(e.id, n);

      var curChild = parent.children[idx];
      if (curChild !== n.root) {
        parent.insertBefore(n.root, curChild || null);
      }
    });

    var currentChildren = Array.prototype.slice.call(parent.children);
    for (var i = S.items.length; i < currentChildren.length; i++) {
      parent.removeChild(currentChildren[i]);
    }

    S.nodes = fresh;
    if (S.quietRender) S.quietRender = false; else staggerIn(born);   // only the new nodes enter, never the ones a poll patched
    restoreFocus(focusId, focusPart, focusNode, fresh);
    $("acc-empty").hidden = S.items.length > 0;
    renderPagers();
    markSorted();
  }

  function applyPage(d) {
    S.items = d.items; S.page = d.page; S.pages = d.pages; S.total = d.total; S.version = Math.max(S.version, d.version);
    S.map = new Map();
    d.items.forEach(function (e) {
      S.map.set(e.id, e);
      S.known.set(e.id, { h: e.health, s: e.state, v: e[S.sort] });
    });
    S.pageDirty = false; S.lastPageFetch = Date.now();
    W(renderList);
  }

  function pageUrl() {
    var p = "page=" + S.page + "&size=" + S.size + "&sort=" + encodeURIComponent(S.sort) + "&order=" + S.order;
    if (S.q) p += "&q=" + encodeURIComponent(S.q);
    if (S.fstate) p += "&state=" + encodeURIComponent(S.fstate);
    if (S.fhealth) p += "&health=" + encodeURIComponent(S.fhealth);
    if (S.showRemoved) p += "&removed=1";
    return "/api/v2/accounts?" + p;
  }
  function loadPage() {
    var seq = ++S.pageSeq;
    return api(pageUrl()).then(function (d) {
      if (seq !== S.pageSeq) return;
      applyPage(d);
    });
  }

  /* Pagers: one at the top of the toolbar and one under the list, both with the same buttons (renderPagers).
     The page numbers are real buttons; "…" stands for pages not shown */
  function pageList(page, pages) {
    var out = [], i;
    if (pages <= 7) { for (i = 1; i <= pages; i++) out.push(i); return out; }
    out.push(1);
    var lo = Math.max(2, page - 1), hi = Math.min(pages - 1, page + 1);
    if (lo > 2) out.push("…");
    for (i = lo; i <= hi; i++) out.push(i);
    if (hi < pages - 1) out.push("…");
    out.push(pages);
    return out;
  }
  function renderPagers() {
    var hide = S.size === 500 && S.pages <= 1;   // "All" shows every account on one page: no pager needed
    var info = S.total === 1 ? T("acc.one") : T("acc.many", { n: S.total });
    document.querySelectorAll(".pager").forEach(function (box) {
      box.hidden = hide;
      var prev = box.querySelector("[data-pg='prev']"), next = box.querySelector("[data-pg='next']");
      if (prev) prev.disabled = S.page <= 1;
      if (next) next.disabled = S.page >= S.pages;
      setTxt(box.querySelector(".pager-info"), info);
      var holder = box.querySelector(".pager-pages"), sig = S.page + "/" + S.pages;
      if (!holder || holder.getAttribute("data-sig") === sig) return;
      holder.setAttribute("data-sig", sig);
      clear(holder);
      pageList(S.page, S.pages).forEach(function (p) {
        if (p === "…") { holder.appendChild(el("span", "pager-gap", "…")); return; }
        var b = el("button", "btn btn-page" + (p === S.page ? " btn-primary lg-btn--primary" : ""), String(p));
        b.type = "button";
        b.setAttribute("data-page-to", String(p));
        b.setAttribute("aria-label", T("pager.page", { n: p }));
        if (p === S.page) b.setAttribute("aria-current", "page");
        holder.appendChild(b);
      });
    });
  }
  function goToPage(n) {
    if (!isNum(n) || n < 1 || n > S.pages || n === S.page) return;
    S.page = n;
    loadPage().then(scrollAccountsTop).catch(netFail);
  }
  /* After a page change the list starts at its top. The toolbar stays above it (scroll-margin-top in aero.css) */
  function scrollAccountsTop() {
    var sec = $("view-accounts");
    if (sec && sec.scrollIntoView) sec.scrollIntoView({ block: "start" });
  }
  function initPagers() {
    document.querySelectorAll(".pager").forEach(function (box) {
      box.addEventListener("click", function (ev) {
        var b = ev.target.closest ? ev.target.closest("button") : null;
        if (!b || b.disabled) return;
        if (b.hasAttribute("data-page-to")) goToPage(parseInt(b.getAttribute("data-page-to"), 10));
        else if (b.getAttribute("data-pg") === "prev") goToPage(S.page - 1);
        else if (b.getAttribute("data-pg") === "next") goToPage(S.page + 1);
      });
    });
    /* Left and right arrows change the page, unless the focus is in a field */
    document.addEventListener("keydown", function (ev) {
      if (S.view !== "accounts" || modalState) return;
      var t = ev.target, tag = t && t.tagName;
      if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA" || (t && t.isContentEditable)) return;
      if (ev.key === "ArrowLeft") { ev.preventDefault(); goToPage(S.page - 1); }
      else if (ev.key === "ArrowRight") { ev.preventDefault(); goToPage(S.page + 1); }
    });
  }

  var SORTS = [
    ["health", "sort.health"], ["name", "sort.name"], ["state", "sort.state"], ["hunts_per_min_15m", "sort.hpm"],
    ["hunts_total", "sort.hunts"], ["battles_total", "sort.battles"], ["last_hunt_ago_s", "sort.last"],
    ["cowoncy", "sort.cowoncy"], ["cowoncy_delta_today", "sort.dcow"], ["captcha_today", "sort.cap"],
    ["inbound_kb_min", "sort.net"], ["errors_1h", "sort.err"], ["reconnects_1h", "sort.rc"],
    ["level", "sort.level"]
  ];
  /* The sort menu. A language switch writes its options again and keeps the chosen one (syncControls) */
  function populateSorts() {
    var sel = $("f-sort");
    clear(sel);
    SORTS.forEach(function (s) { var o = el("option", "", T(s[1])); o.value = s[0]; sel.appendChild(o); });
  }
  function syncControls() {
    $("f-sort").value = S.sort; setTxt($("f-order"), S.order === "desc" ? T("acc.desc") : T("acc.asc"));
    $("f-size").value = String(S.size);
    $("f-removed").checked = !!S.showRemoved;
    var c = S.mode === "cards";
    setCls($("m-cards"), "gel small" + (c ? " on" : "")); setCls($("m-table"), "gel small" + (!c ? " on" : ""));
  }
  function initAccountControls() {
    var sel = $("f-sort");
    populateSorts();
    buildHead();
    var timer = null;
    $("q").addEventListener("input", function () {
      window.clearTimeout(timer);
      timer = window.setTimeout(function () { S.q = $("q").value.trim(); S.page = 1; loadPage().catch(netFail); }, 250);
    });
    $("f-state").addEventListener("change", function () { S.fstate = this.value; S.page = 1; loadPage().catch(netFail); });
    $("f-health").addEventListener("change", function () { S.fhealth = this.value; S.page = 1; loadPage().catch(netFail); });
    sel.addEventListener("change", function () {
      S.sort = this.value; S.order = this.value === "name" || this.value === "state" ? "asc" : "desc";
      syncControls(); S.page = 1; loadPage().catch(netFail);
    });
    $("f-order").addEventListener("click", function () { S.order = S.order === "desc" ? "asc" : "desc"; syncControls(); loadPage().catch(netFail); });
    $("f-removed").addEventListener("change", function () { S.showRemoved = this.checked; S.page = 1; loadPage().catch(netFail); });
    $("f-size").addEventListener("change", function () { S.size = parseInt(this.value, 10) || 50; storeKey("dusk.accounts.size", String(S.size)); S.page = 1; loadPage().catch(netFail); });
    $("m-cards").addEventListener("click", function () { S.mode = "cards"; store("mode", "cards"); syncControls(); S.nodes = new Map(); W(renderList); });
    $("m-table").addEventListener("click", function () { S.mode = "table"; store("mode", "table"); syncControls(); S.nodes = new Map(); W(renderList); });
    initPagers();
    syncControls();
  }

  /* ---------- Incremental Updates Polling ---------- */
  function pollChanges() {
    return api("/api/v2/changes?since=" + S.version).then(function (d) {
      if (d.reset) return reloadAll();
      var urgent = false, dirty = false;
      S.genAt = d.generated_at;
      W(function () {
        (d.changed || []).forEach(function (e) {
          var prev = S.known.get(e.id), key = S.sort;
          S.known.set(e.id, { h: e.health, s: e.state, v: e[key] });
          if (S.map.has(e.id)) {
            S.map.set(e.id, e);
            for (var i = 0; i < S.items.length; i++) { if (S.items[i].id === e.id) { S.items[i] = e; break; } }
            var n = S.nodes.get(e.id);
            if (n && S.view === "accounts") S.mode === "table" ? patchRow(n, e) : patchCard(n, e);
          }
          if (prev) {
            var hs = prev.h !== e.health || prev.s !== e.state;
            if (hs) { dirty = true; if (S.fstate || S.fhealth) urgent = true; }
            else if (key !== "name" && key !== "id" && prev.v !== e[key]) dirty = true;
          }
        });
        if (dirty) S.pageDirty = true;
        (d.removed || []).forEach(function (id) { S.known.delete(id); if (S.map.has(id)) S.pageDirty = true; });
        var age = Date.now() - S.lastPageFetch;
        if (S.view === "accounts" && ((S.pageDirty && (urgent || age > 10000)) || age > 60000)) loadPage().catch(netFail);
      });
      S.version = d.version;
      applySummary(d.summary, d.sources, d);
    });
  }
  function reloadAll() {
    return api("/api/v2/summary").then(function (d) {
      S.version = d.version; S.genAt = d.generated_at;
      applySummary(d.summary, d.sources, d);
      return loadPage();
    });
  }

  window.setInterval(function () {
    if (document.hidden) return;
    W(function () {
      if (S.view === "accounts") {
        S.nodes.forEach(function (n) {
          var node = n.m ? n.m.last : (n.cells ? n.cells.last : null);
          if (node) renderAgo(node);
        });
      }
      if (S.view === "overview") renderUptime();
      if (S.view === "account") tickDetail();
    });
  }, 1000);

  /* ---------- Lightweight SVG Charts with Crosshairs ---------- */
  var CW = 320, CH = 170, ML = 38, MR = 8, MT = 10, MB = 22;
  function chartBase(svg, ymin, ymax, fmtY) {
    clear(svg);
    svg.setAttribute("viewBox", "0 0 " + CW + " " + (svg.getAttribute("data-h") || CH));
    var H = parseFloat(svg.getAttribute("data-h") || CH);
    var plotH = H - MT - MB;
    [0, 0.5, 1].forEach(function (f) {
      var y = MT + plotH * (1 - f), v = ymin + (ymax - ymin) * f;
      svg.appendChild(svgEl("line", { x1: ML, x2: CW - MR, y1: y, y2: y, "class": "axis" }));
      var t = svgEl("text", { x: ML - 5, y: y + 3.5, "text-anchor": "end" }); t.textContent = fmtY(v); svg.appendChild(t);
    });
    return { H: H, plotH: plotH, plotW: CW - ML - MR };
  }

  /* Crosshair: a dashed line, a dot and a readout at the point nearest to the pointer. Works with mouse, pen and
     touch (a tap shows it for a few seconds). Every chart keeps its points in svg._series, see barChart */
  function bindCrosshair(svg) {
    if (svg._xhair) return;
    svg._xhair = true;
    var clearTimer = null;
    function nearest(ev) {
      var s = svg._series, rect = svg.getBoundingClientRect();
      if (!s || !s.xs.length || !rect.width) return -1;
      var vx = (ev.clientX - rect.left) * (CW / rect.width), best = -1, dist = Infinity;
      for (var i = 0; i < s.xs.length; i++) {
        if (!isNum(s.vals[i])) continue;
        var d = Math.abs(s.xs[i] - vx);
        if (d < dist) { dist = d; best = i; }
      }
      return best;
    }
    function draw(i) {
      var s = svg._series, layer = svg.querySelector("g.xlayer");
      if (!layer) { layer = svgEl("g", { "class": "xlayer", "aria-hidden": "true" }); svg.appendChild(layer); }
      clear(layer);
      if (!s || i < 0 || !isNum(s.vals[i])) return;
      var x = s.xs[i], y = s.ys[i];
      layer.appendChild(svgEl("line", { x1: x.toFixed(1), x2: x.toFixed(1), y1: MT, y2: s.bottom, "class": "xhair" }));
      layer.appendChild(svgEl("circle", { cx: x.toFixed(1), cy: y.toFixed(1), r: 3.5, "class": "xdot" }));
      var text = s.labels[i] + ": " + s.fmt(s.vals[i]) + (s.unit ? " " + s.unit : "");
      var w = Math.min(230, 18 + text.length * 6.2), left = x + 10 + w > CW - MR ? x - 10 - w : x + 10;
      var box = svgEl("g", { "class": "xtip" });
      box.appendChild(svgEl("rect", { x: left.toFixed(1), y: MT, width: w.toFixed(1), height: 18, rx: 4 }));
      var label = svgEl("text", { x: (left + 8).toFixed(1), y: MT + 12.5 });
      label.textContent = text; box.appendChild(label); layer.appendChild(box);
    }
    function hold(ev) {
      window.clearTimeout(clearTimer);
      draw(nearest(ev));
      if (ev.pointerType === "touch") clearTimer = window.setTimeout(function () { draw(-1); }, 2500);
    }
    svg.addEventListener("pointermove", function (ev) { if (ev.pointerType !== "touch") hold(ev); });
    svg.addEventListener("pointerdown", hold);
    svg.addEventListener("pointerleave", function (ev) { if (ev.pointerType !== "touch") draw(-1); });
  }

  function barChart(svg, values, labels, fmtY, unit) {
    var max = 1; values.forEach(function (v) { if (isNum(v) && v > max) max = v; });
    var g = chartBase(svg, 0, max, fmtY), n = values.length, bw = g.plotW / Math.max(1, n);
    var series = { xs: [], ys: [], vals: values, labels: labels, fmt: fmtY, unit: unit || "", bottom: MT + g.plotH };
    values.forEach(function (v, i2) {
      var h = isNum(v) ? (v / max) * g.plotH : 0, x = ML + i2 * bw + bw * 0.15;
      svg.appendChild(svgEl("rect", {
        x: x.toFixed(1), y: (MT + g.plotH - h).toFixed(1),
        width: Math.max(2, bw * 0.7).toFixed(1), height: Math.max(0, h).toFixed(1),
        rx: 2, "class": "bar"
      }));
      series.xs.push(x + bw * 0.35); series.ys.push(MT + g.plotH - h);
      if (i2 % Math.ceil(n / 6) === 0 || i2 === n - 1) {
        var last = i2 === n - 1;
        var tx = svgEl("text", { x: (last ? x + bw * 0.7 : x + bw * 0.35).toFixed(1), y: g.H - 5, "text-anchor": last ? "end" : "middle" });
        tx.textContent = labels[i2].split(" ")[0]; svg.appendChild(tx);
      }
    });
    svg._series = series;
    bindCrosshair(svg);
  }

  function lineChart(svg, values, labels, fmtY, unit, cls) {
    var vals = values.filter(isNum), min = 0, max = 1;
    if (vals.length) { min = Math.min(0, Math.min.apply(null, vals)); max = Math.max(1, Math.max.apply(null, vals)); }
    var g = chartBase(svg, min, max, fmtY), n = values.length, step = g.plotW / Math.max(1, n - 1);
    var d = "", started = false, firstX = 0, lastX = 0, areaD = "";
    function yOf(v) { return MT + g.plotH * (1 - (v - min) / (max - min || 1)); }
    var series = { xs: [], ys: [], vals: values, labels: labels, fmt: fmtY, unit: unit || "", bottom: MT + g.plotH };
    values.forEach(function (v, i) {
      series.xs.push(ML + i * step);
      series.ys.push(isNum(v) ? yOf(v) : null);
      if (!isNum(v)) { started = false; return; }
      var x = ML + i * step, y = yOf(v);
      d += (started ? " L" : " M") + x.toFixed(1) + " " + y.toFixed(1);
      if (!started) firstX = x; started = true; lastX = x;
    });
    if (d) {
      areaD = d + " L" + lastX.toFixed(1) + " " + yOf(Math.max(0, min)).toFixed(1) + " L" + firstX.toFixed(1) + " " + yOf(Math.max(0, min)).toFixed(1) + " Z";
      svg.appendChild(svgEl("path", { d: areaD, "class": "ar" }));
      svg.appendChild(svgEl("path", { d: d, "class": "ln " + (cls || "") }));
    }
    values.forEach(function (v, i) {
      if (i % Math.ceil(n / 6) === 0 || i === n - 1) {
        var tx = svgEl("text", { x: (ML + i * step).toFixed(1), y: g.H - 5, "text-anchor": i === n - 1 ? "end" : "middle" });
        tx.textContent = labels[i].split(" ")[0]; svg.appendChild(tx);
      }
    });
    if (!d) {
      var nt = svgEl("text", { x: CW / 2, y: g.H / 2, "text-anchor": "middle" }); nt.textContent = T("chart.nodata"); svg.appendChild(nt);
    }
    svg._series = series;
    bindCrosshair(svg);
  }

  /* ---------- Account Detail View & Quick Controls ---------- */
  function hourLabel(ts) { var d = new Date(ts * 1000); return (d.getHours() < 10 ? "0" : "") + d.getHours() + ":00"; }
  function rowDl(dl, label, value, tip, ago) {
    var dt = el("dt", "", label), dd = el("dd", "", value);
    if (tip) { setTitle(dt, tip); setTitle(dd, tip); }
    if (ago !== undefined) { dd.setAttribute("data-at", isNum(ago) ? String(ago) : ""); dd.setAttribute("data-ago", "1"); }
    dl.appendChild(dt); dl.appendChild(dd);
  }
  function group(title, ic) {
    var g = el("div", "panel glass group"), h = el("h2"); h.appendChild(icon(ic)); h.appendChild(document.createTextNode(title));
    var dl = el("dl"); g.appendChild(h); g.appendChild(dl);
    return { root: g, dl: dl };
  }
  function eventLi(e, linkName) {
    var li = el("li", "ev " + (e.sev || "info")), t = el("span", "t", fmtClock(e.ts));
    setTitle(t, fmtTime(e.ts));
    li.appendChild(icon(IC[e.type] || "info"));
    li.appendChild(t);
    var tx = el("span", "", e.text || e.type); li.appendChild(tx);
    li.appendChild(chip(e.sev === "bad" ? "bad" : e.sev === "warn" ? "warn" : e.sev === "good" ? "good" : "unknown", lab("ev.type.", e.type)));
    if (linkName) {
      var who = el("span", "who"), a = el("a", "", e.name || (e.acct ? shortId(e.acct) : T("ev.all")));
      if (e.acct) a.href = "#account/" + encodeURIComponent(e.acct);
      who.appendChild(a); li.appendChild(who);
    }
    return li;
  }

  function renderDetail(d) {
    var a = d.account, ex = d.extra || {}, cm = d.commands || {};
    S.lastDetail = d;
    setTxt($("d-title"), nameOf(a));
    var chips = $("d-chips"); clear(chips);
    chips.appendChild(chip(a.state, lab("state.", a.state))); chips.appendChild(document.createTextNode(" ")); chips.appendChild(chip(a.health, lab("health.", a.health)));

    /* Detail Drawer Quick Action Buttons */
    var btnSleep = $("d-btn-sleep");
    if (btnSleep) {
      setTxt($("d-lbl-sleep"), a.state === "sleeping" ? T("drawer.wake") : T("drawer.sleepAccount"));
      btnSleep.onclick = function () {
        var action = a.state === "sleeping" ? "wake" : "sleep";
        controlAccount(a.id, action, null, function () { loadDetail(); });
      };
    }
    var btnSwitch = $("d-btn-switch-channel");
    if (btnSwitch) {
      btnSwitch.onclick = function () {
        openChannelModal(a.id, nameOf(a), function (newCh) {
          controlAccount(a.id, "switch_channel", newCh, function () { loadDetail(); });
        });
      };
    }
    var why = $("d-why"); clear(why);
    if (a.health_reasons && a.health_reasons.length) {
      a.health_reasons.forEach(function (r) { why.appendChild(el("li", "", r)); });
    } else {
      why.appendChild(el("li", "empty", a.health === "good" ? T("drawer.healthy") : T("drawer.noDetails")));
    }

    var box = $("d-groups"); clear(box);
    var g1 = group(T("grp.farming"), "hunt");
    rowDl(g1.dl, T("row.state"), lab("state.", a.state));
    rowDl(g1.dl, T("row.huntsTotal"), fmtInt(a.hunts_total));
    rowDl(g1.dl, T("row.huntsMin"), fmt2(a.hunts_per_min_15m) + " " + T("unit.perMin"));
    rowDl(g1.dl, T("row.battles"), fmtInt(a.battles_total));
    rowDl(g1.dl, T("row.lastHunt"), fmtAgo(a.last_hunt_at ? nowS() - a.last_hunt_at : null), "", a.last_hunt_at);
    box.appendChild(g1.root);

    var gl = group(T("grp.level"), "level");
    rowDl(gl.dl, T("row.levelXp"), levelText(a));
    rowDl(gl.dl, T("row.rank"), isNum(a.rank) ? "#" + fmtInt(a.rank) : EM);
    rowDl(gl.dl, T("row.levelChecked"), isNum(a.level_checked_at) ? fmtAgo(nowS() - a.level_checked_at) : EM, "", a.level_checked_at);
    box.appendChild(gl.root);

    /* D7: the Quest group (read only) */
    var gq = group(T("grp.quest"), "check"), qq = a.quest;
    rowDl(gq.dl, T("row.questStatus"), questStatusText(qq));
    rowDl(gq.dl, T("row.questNext"), questNextText(qq));
    rowDl(gq.dl, T("row.questSeals"), qq && isNum(qq.seals) ? fmtInt(qq.seals) : EM);
    rowDl(gq.dl, T("row.questOpen"), questOpenText(qq));
    rowDl(gq.dl, T("row.questLastClaim"), qq && isNum(qq.last_claim_at) ? fmtAgo(nowS() - qq.last_claim_at) : EM, "", qq && qq.last_claim_at);
    box.appendChild(gq.root);

    var g2 = group(T("grp.economy"), "cowoncy");
    rowDl(g2.dl, T("row.cowoncy"), fmtInt(a.cowoncy));
    rowDl(g2.dl, T("row.todayDelta"), fmtSigned(a.cowoncy_delta_today));
    rowDl(g2.dl, T("row.dailyClaimed"), ex.daily_at ? fmtTime(ex.daily_at) : EM);
    box.appendChild(g2.root);

    var g3 = group(T("grp.captcha"), "captcha");
    rowDl(g3.dl, T("row.captchasToday"), fmtInt(a.captcha_today));
    rowDl(g3.dl, T("row.lastCaptcha"), a.captcha_last_at ? fmtAgo(nowS() - a.captcha_last_at) : EM, "", a.captcha_last_at);
    rowDl(g3.dl, T("row.inbound"), fmt1(a.inbound_kb_min) + " " + T("unit.kbMin"));
    rowDl(g3.dl, T("row.reconnects"), fmtInt(a.reconnects_1h));
    box.appendChild(g3.root);

    var s = d.series || {}, hours = (s.hours || []).map(hourLabel);
    barChart($("c-hunts"), s.hunts || [], hours, function (v) { return v >= 10 ? fmtInt(v) : fmt1(v); }, T("unit.hunts"));
    var dh = []; for (var i = 0; i < 24; i++) dh.push((i < 10 ? "0" : "") + i + ":00");
    lineChart($("c-cow"), s.cowoncy_by_hour_of_day || [], dh, fmtInt, T("unit.cowoncy"), "g");
    lineChart($("c-net"), s.inbound_kb_min || [], hours, fmtInt, T("unit.kbMin"), "");

    var ul = $("d-events"); clear(ul);
    var evs = (d.events || []).slice().reverse();
    if (!evs.length) ul.appendChild(el("li", "empty", T("drawer.noEvents")));
    evs.forEach(function (e) { ul.appendChild(eventLi(e, false)); });
  }

  function tickDetail() {
    var nodes = document.querySelectorAll("#d-groups [data-ago]");
    for (var i = 0; i < nodes.length; i++) renderAgo(nodes[i]);
  }
  function loadDetail() {
    var id = S.accountId;
    if (!id) return Promise.resolve();
    return api("/api/v2/accounts/" + encodeURIComponent(id)).then(function (d) {
      if (id !== S.accountId) return;
      if (d.version === S.detailVersion && $("d-title").textContent) return;
      S.detailVersion = d.version;
      W(function () { renderDetail(d); });
    }, function (err) {
      if (err.status === 404) {
        W(function () { setTxt($("d-title"), T("drawer.notFound")); clear($("d-groups")); clear($("d-chips")); });
        return;
      }
      throw err;
    });
  }

  /* ---------- Events View ---------- */
  function renderEvents() {
    var ul = $("e-list"), q = S.ev.q.toLowerCase();
    var list = S.ev.list.filter(function (e) { return !q || (e.name || "").toLowerCase().indexOf(q) >= 0; });
    clear(ul);
    setTxt($("e-info"), T("ev.count", { n: list.length }));
    if (!list.length) { ul.appendChild(el("li", "empty", T("ev.none"))); return; }
    list.slice(0, 300).forEach(function (e) { ul.appendChild(eventLi(e, true)); });
  }
  function loadEvents() {
    var first = S.ev.last === 0;
    var url = "/api/v2/events?after=" + S.ev.last + "&limit=500" + (S.ev.type ? "&type=" + S.ev.type : "");
    return api(url).then(function (d) {
      var evs = d.events || [];
      if (first) S.ev.list = evs; else if (evs.length) S.ev.list = S.ev.list.concat(evs).slice(-1500); else return;
      if (evs.length) S.ev.last = evs[evs.length - 1].id;
      W(renderEvents);
    });
  }

  /* ---------- Network View ---------- */
  function loadNetwork() {
    return api("/api/v2/gateway").then(function (d) {
      S.gwLast = d;
      if (d.version === S.gwVersion) return;
      S.gwVersion = d.version;
      W(function () { renderNetwork(d); });
    });
  }
  /* The gateway bars. A language switch draws them again from the last answer (onLangChange) */
  function renderNetwork(d) {
    var box = $("n-bars"); clear(box);
    var t = d.totals || {}, th = d.thresholds || {};
    setTxt($("n-info"), T("net.info", { n: t.accounts, total: fmtInt(t.sum_kb_min), avg: fmt1(t.avg_kb_min) }));
    var top = d.top || [], max = Math.max(th.bad * 1.15 || 1, top.length ? top[0].kb_min : 1);
    if (!top.length) box.appendChild(el("div", "empty", T("net.none")));
    top.forEach(function (r) {
      var row = el("div", "barrow"), a = el("a", "", r.name || shortId(r.id));
      a.href = "#account/" + encodeURIComponent(r.id);
      var track = el("div", "track"), fill = el("div", "fill " + (r.level === "good" ? "" : r.level));
      fill.style.width = Math.max(1, Math.min(100, (r.kb_min / max) * 100)).toFixed(1) + "%";
      track.appendChild(fill);
      var val = el("div", "val", fmt1(r.kb_min) + " " + T("unit.kbMin"));
      row.appendChild(a); row.appendChild(track); row.appendChild(val); box.appendChild(row);
    });
    renderNoisy(d.noisy_servers || []);
  }

  /* Noisy servers: the servers the accounts take the most inbound from, summed over the fleet (the gateway view). The
     farm server is tagged. Read only: nothing here leaves a server */
  function renderNoisy(list) {
    var ul = $("n-noisy"), info = $("n-noisy-info");
    if (!ul) return;
    clear(ul);
    setTxt(info, list && list.length ? T("net.noisy.info", { n: list.length }) : "");
    if (!list || !list.length) {
      ul.appendChild(el("li", "empty", T("net.noisy.none")));
      return;
    }
    list.forEach(function (g) {
      var li = el("li", "noisy-row"), head = el("div", "noisy-head");
      head.appendChild(el("b", "noisy-id", String(g.guild_id)));
      if (g.farm) head.appendChild(chip("good", T("net.noisy.farm")));
      head.appendChild(el("span", "noisy-kb", fmt1(g.kb_min) + " " + T("unit.kbMin")));
      li.appendChild(head);
      var worst = g.worst || {};
      li.appendChild(el("div", "noisy-meta", T("net.noisy.row", {
        accounts: fmtInt(g.accounts), worst: worst.name || shortId(worst.id || ""), kb: fmt1(worst.kb_min)
      })));
      ul.appendChild(li);
    });
  }

  /* ---------- Logs View ---------- */
  function lineVisible(div) {
    var q = $("l-q").value.trim().toLowerCase(), lv = $("l-level").value, level = div.getAttribute("data-level");
    if (lv === "error" && level !== "error") return false;
    if (lv === "warn" && level === "info") return false;
    return !q || div.textContent.toLowerCase().indexOf(q) >= 0;
  }
  function applyLogFilter() {
    var box = $("console"), kids = box.children;
    for (var i = 0; i < kids.length; i++) kids[i].hidden = !lineVisible(kids[i]);
    if (S.con.stick) box.scrollTop = box.scrollHeight;
  }
  function loadConsole() {
    return api("/api/v2/console?after=" + S.con.last + "&limit=200").then(function (d) {
      if (!d.lines.length) return;
      S.con.last = d.lines[d.lines.length - 1].id;
      W(function () {
        var box = $("console"), frag = document.createDocumentFragment();
        d.lines.forEach(function (l) {
          var div = el("div"); div.setAttribute("data-level", l.level);
          if (l.level !== "info") div.className = l.level;
          div.appendChild(el("span", "tm", l.t || "")); div.appendChild(document.createTextNode(l.text));
          div.hidden = !lineVisible(div);
          frag.appendChild(div);
        });
        box.appendChild(frag);
        while (box.children.length > 1500) box.removeChild(box.firstChild);
        if (S.con.stick) box.scrollTop = box.scrollHeight;
      });
    });
  }
  /* The Following / Paused pill of the console. A language switch writes it again (onLangChange) */
  function setFollowPill() {
    var p = $("l-follow");
    setCls(p, "pill " + (S.con.stick ? "ok" : "off"));
    setTxt(p, T(S.con.stick ? "log.following" : "log.paused"));
  }
  function initLogs() {
    var box = $("console");
    box.addEventListener("scroll", function () {
      var stick = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
      if (stick !== S.con.stick) {
        S.con.stick = stick;
        setFollowPill();
      }
    });
    $("l-q").addEventListener("input", applyLogFilter);
    $("l-level").addEventListener("change", applyLogFilter);
  }

  /* ---------- Runtime View ---------- */
  /* The 7-day chart is drawn in real pixels: the viewBox is the size of its box, so its labels stay at 12 px */
  function loadRuntime() {
    return api("/api/v2/runtime").then(function (d) {
      S.rtLast = d;
      W(function () { renderRuntime(d); });
    });
  }
  /* The totals and the chart. A language switch writes them again from the last answer (onLangChange) */
  function renderRuntime(d) {
    var days = d.days || [], n = days.length, total = 0;
    days.forEach(function (x) { total += x.seconds || 0; });
    S.rtDays = days;
    setTxt($("r-total"), T("rt.total", { total: fmt1(total / 3600), avg: fmt1(n ? total / 3600 / n : 0) }));
    var today = n ? days[n - 1].seconds : null;
    setTxt($("r-info"), T("rt.info", { today: fmtDur(today), total: fmtDur(total) }));
    drawRuntime();
  }
  function drawRuntime() {
    var box = $("rt-chart"), svg = $("c-runtime"), days = S.rtDays;
    if (!box || !svg || !days) return;
    var w = Math.floor(box.clientWidth), h = Math.floor(box.clientHeight);
    if (w < 160 || h < 120) return;   // the view is hidden: it is drawn again when it shows
    clear(svg);
    svg.setAttribute("viewBox", "0 0 " + w + " " + h);
    var n = days.length;
    if (!n) {
      var none = svgEl("text", { x: w / 2, y: h / 2, "text-anchor": "middle" });
      none.textContent = T("rt.none");
      svg.appendChild(none);
      return;
    }
    var padX = 10, top = 24, colW = (w - padX * 2) / n, narrow = colW < 84;
    var labelH = narrow ? 34 : 20, base = h - labelH - 4, plotH = base - top, max = 1, i;
    for (i = 0; i < n; i++) {
      var peak = (days[i].seconds || 0) / 3600;
      if (peak > max) max = peak;
    }
    svg.appendChild(svgEl("line", { x1: padX, x2: w - padX, y1: base, y2: base, "class": "rt-base" }));
    for (i = 0; i < n; i++) {
      var hours = (days[i].seconds || 0) / 3600, cx = padX + colW * (i + 0.5), barW = Math.min(64, colW * 0.6);
      var barH = Math.max(2, (hours / max) * plotH), y = base - barH;   // a 2 px stub keeps a zero day visible
      var bar = svgEl("rect", { x: (cx - barW / 2).toFixed(1), y: y.toFixed(1), width: barW.toFixed(1), height: barH.toFixed(1), rx: 3,
        "class": "rt-bar " + (i === n - 1 ? "today" : "soft") });
      var tip = svgEl("title", {});
      tip.textContent = days[i].label + ": " + fmt2(hours) + " h";
      bar.appendChild(tip);
      svg.appendChild(bar);
      var val = svgEl("text", { x: cx.toFixed(1), y: (y - 6).toFixed(1), "text-anchor": "middle", "class": "rt-val" });
      val.textContent = fmt1(hours) + " h";
      svg.appendChild(val);
      var parts = String(days[i].label || "").split(" ");
      if (narrow) {
        var l1 = svgEl("text", { x: cx.toFixed(1), y: (base + 14).toFixed(1), "text-anchor": "middle" });
        l1.textContent = parts[0] || "";
        var l2 = svgEl("text", { x: cx.toFixed(1), y: (base + 28).toFixed(1), "text-anchor": "middle" });
        l2.textContent = parts.slice(1).join(" ");
        svg.appendChild(l1); svg.appendChild(l2);
      } else {
        var lab = svgEl("text", { x: cx.toFixed(1), y: (base + 15).toFixed(1), "text-anchor": "middle" });
        lab.textContent = days[i].label || "";
        svg.appendChild(lab);
      }
    }
  }
  /* Redraws when the box changes size: the window, the sidebar, or the view showing for the first time (100 ms debounce) */
  function initRuntimeChart() {
    var box = $("rt-chart");
    if (!box) return;
    var timer = null;
    function again() { window.clearTimeout(timer); timer = window.setTimeout(drawRuntime, 100); }
    if (window.ResizeObserver) { var ro = new window.ResizeObserver(again); ro.observe(box); }
    else window.addEventListener("resize", again);
  }

  /* ---------- Configuration Center View ---------- */
  function showConfigBanner(msg, isOk) {
    var b = $("cfg-status-banner");
    if (!b) return;
    setTxt(b, msg);
    b.className = "cfg-banner " + (isOk ? "ok" : "err");
    b.hidden = false;
    window.clearTimeout(b._timer);
    b._timer = window.setTimeout(function () { b.hidden = true; }, 5000);
  }

  /* Dashboard & Health (dashboard_view.json) is live: the server uses a change at once. Default Bot Profile (settings.json)
     is the one profile every account uses: an account reads it when it next logs in, or after a restart. Each row has its
     key path, a mark for when it applies, and a description; the info button opens the full text. The search box shows the
     matching rows and hides the sections that have none */
  function switchConfigTab(tabName) {
    S.cfgTab = tabName === "default" || tabName === "server" ? tabName : "dashboard";
    var tabs = document.querySelectorAll("#cfg-tabs .gel");
    for (var i = 0; i < tabs.length; i++) {
      setCls(tabs[i], "gel small" + (tabs[i].getAttribute("data-cfg-tab") === S.cfgTab ? " on" : ""));
    }
    $("cfg-pane-dashboard").hidden = S.cfgTab !== "dashboard";
    $("cfg-pane-default").hidden = S.cfgTab !== "default";
    $("cfg-pane-server").hidden = S.cfgTab !== "server";
    if (S.cfgTab === "dashboard") loadDashboardConfig();
    else if (S.cfgTab === "default") loadDefaultConfig();
    else loadServerConfig();
    applyCfgFilter();
  }

  function loadDashboardConfig() {
    return api("/api/v2/config?scope=dashboard").then(function (res) {
      var cfg = res.config || {};
      for (var k in cfg) {
        var input = document.querySelector("#form-cfg-dashboard [name='" + k + "']");
        if (input) input.value = cfg[k];
      }
    }).catch(function (err) {
      showConfigBanner(T("cfg.err.loadDash", { msg: err.message }), false);
    });
  }

  function loadDefaultConfig() {
    return api("/api/v2/config?scope=default").then(function (res) {
      var cfg = res.config || {};
      S.loadedConfigs.default = cfg;
      renderDefaultConfigForm(cfg);
      applyCfgFilter();
    }).catch(function (err) {
      showConfigBanner(T("cfg.err.loadBot", { msg: err.message }), false);
    });
  }

  /* The Server & bot tab: the website block of global_settings.json, without the password. The host shows what the file has;
     when the file has none, the default it takes is written under the field (the same rule as utils/lan.py) */
  function loadServerConfig() {
    return api("/api/v2/config?scope=global").then(function (res) {
      renderServerConfigForm((res.config && res.config.website) || {});
      renderLockedForm((res.config && res.config.console) || {}, (res.config && res.config.levelCheck) || {});
      renderGatewayForm((res.config && res.config.gateway) || {});
      applyCfgFilter();
    }).catch(function (err) {
      showConfigBanner(T("cfg.err.loadServer", { msg: err.message }), false);
    });
  }
  function renderServerConfigForm(site) {
    var hasHost = typeof site.host === "string" && site.host !== "";
    var fallback = "0.0.0.0"; // website.host decides the bind; enableHost is ignored
    $("cfg-srv-host").value = hasHost ? site.host : "";
    $("cfg-srv-host").placeholder = fallback;
    setTxt($("cfg-srv-host-note"), hasHost ? "" : T("cfg.hostDefault", { host: fallback }));
    $("cfg-srv-port").value = isNum(site.port) ? String(site.port) : "";
    $("cfg-srv-restart").checked = site.restartOnCrash === true;
    $("cfg-srv-launch").checked = site.startBotOnLaunch === true;
    $("cfg-srv-stopwith").checked = site.stopBotWithDashboard === true;
  }

  /* The locked keys (LOCKED_GLOBAL_KEYS in website/api_v2.py), read only here. The server sends the two command texts
     blank; the change is made by hand in config/global_settings.json */
  function renderLockedForm(con, lc) {
    $("cfg-lock-cmd-captcha").value = typeof con.commandToRunOnCaptcha === "string" ? con.commandToRunOnCaptcha : "";
    $("cfg-lock-cmd-ban").value = typeof con.commandToRunOnBan === "string" ? con.commandToRunOnBan : "";
    $("cfg-lock-run-captcha").checked = con.runConsoleCommandOnCaptcha === true;
    $("cfg-lock-run-ban").checked = con.runConsoleCommandOnBan === true;
    $("cfg-lock-tesseract").value = typeof lc.tesseractPath === "string" ? lc.tesseractPath : "";
  }
  /* The gateway block of global_settings.json (Gateway traffic, Server & bot tab). A missing key shows its default */
  function renderGatewayForm(gw) {
    $("cfg-gw-lean").checked = gw.lean !== false;
    $("cfg-gw-chunk").checked = gw.chunkGuilds === true;
    $("cfg-gw-subs").checked = gw.guildSubscriptions === true;
    $("cfg-gw-max").value = String(isNum(gw.maxMessages) ? gw.maxMessages : 200);
    $("cfg-gw-request").checked = gw.requestGuilds !== false;
  }
  /* The gateway values from the form, or null when the message cache size is not a whole number from 0 to 1000 */
  function gatewayValues() {
    var raw = String($("cfg-gw-max").value).trim(), max = parseInt(raw, 10);
    if (!/^\d+$/.test(raw) || !isNum(max) || max < 0 || max > 1000) return null;
    return {
      lean: $("cfg-gw-lean").checked, chunkGuilds: $("cfg-gw-chunk").checked,
      guildSubscriptions: $("cfg-gw-subs").checked, maxMessages: max, requestGuilds: $("cfg-gw-request").checked
    };
  }

  /* Level quote status under the farming commands: pool, cache and the last good fetch, from the bot's live status */
  function renderQuoteStatus() {
    var box = $("cfg-quote-status"), q = S.summary ? S.summary.quotes : null;
    if (!box) return;
    if (!q) { box.hidden = true; return; }
    var when = !isNum(q.last_ok_at) ? T("cfg.quote.none") :
      fmtClock(q.last_ok_at) + " " + (q.last_error ? T("cfg.quote.err", { msg: q.last_error }) : T("cfg.quote.ok"));
    setTxt(box, T("cfg.quote.line", { pool: fmtInt(q.pool), cache: fmtInt(q.cache), when: when }));
    box.hidden = false;
  }

  /* The bot tab: one row per setting, rows built from the saved file. Command names come from the file, and each command
     has its own description (config.desc.default.commands.*). A key with no description says so (cfgDesc) */
  function renderDefaultConfigForm(cfg) {
    var cmdsBox = $("cfg-farm-cmds"), sleepBox = $("cfg-delays-sleep"), gemsBox = $("cfg-gems-autosell"), bossBox = $("cfg-boss-daily");
    var cmds = (cfg && cfg.commands) || {}, sleep = (cfg && cfg.sleep) || {}, autoUse = (cfg && cfg.autoUse) || {};
    var boss = (cfg && cfg.bossBattle) || {}, range = Array.isArray(sleep.sleepTime) && sleep.sleepTime.length === 2 ? sleep.sleepTime : [300, 600];
    S.cfgRows = [];
    clear(cmdsBox); clear(sleepBox); clear(gemsBox); clear(bossBox);
    Object.keys(cmds).forEach(function (cmd) {
      cmdsBox.appendChild(cfgRow({ key: "commands." + cmd + ".enabled", label: cmd, raw: true, desc: "config.desc.default.commands." + cmd,
        mark: "restart", control: checkbox("cmd_" + cmd, !!(cmds[cmd] && cmds[cmd].enabled)) }));
    });
    var lvl = cmds.lvlGrind || {};
    cmdsBox.appendChild(cfgRow({ key: "commands.lvlGrind.quoteSource", label: "cfg.label.quoteSource",
      desc: "config.desc.default.commands.lvlGrind.quoteSource", mark: "restart",
      control: selectInput("lvl_quote_source", [["api", "api"], ["file", "file"]], lvl.quoteSource === "file" ? "file" : "api") }));
    sleepBox.appendChild(cfgRow({ key: "sleep.enabled", label: "cfg.label.sleepEnabled", desc: "config.desc.default.sleep.enabled",
      mark: "restart", control: checkbox("sleep_enabled", !!sleep.enabled) }));
    sleepBox.appendChild(cfgRow({ key: "sleep.frequencyPercentage", label: "cfg.label.sleepFreq", desc: "config.desc.default.sleep.frequencyPercentage",
      mark: "restart", control: numberInput("sleep_freq", isNum(sleep.frequencyPercentage) ? sleep.frequencyPercentage : 10, 0, 100) }));
    sleepBox.appendChild(cfgRow({ key: "sleep.sleepTime[0]", label: "cfg.label.sleepMin", desc: "config.desc.default.sleep.sleepTime.min",
      mark: "restart", control: numberInput("sleep_min", range[0], 0, null) }));
    sleepBox.appendChild(cfgRow({ key: "sleep.sleepTime[1]", label: "cfg.label.sleepMax", desc: "config.desc.default.sleep.sleepTime.max",
      mark: "restart", control: numberInput("sleep_max", range[1], 0, null) }));
    gemsBox.appendChild(cfgRow({ key: "autoUse.gems.enabled", label: "cfg.label.gems", desc: "config.desc.default.gems.enabled",
      mark: "restart", control: checkbox("autouse_gems", !!(autoUse.gems && autoUse.gems.enabled)) }));
    gemsBox.appendChild(cfgRow({ key: "autoUse.autoLootbox", label: "cfg.label.lootbox", desc: "config.desc.default.gems.autoLootbox",
      mark: "restart", control: checkbox("autouse_lootbox", !!autoUse.autoLootbox) }));
    bossBox.appendChild(cfgRow({ key: "bossBattle.enabled", label: "cfg.label.boss", desc: "config.desc.default.boss.enabled",
      mark: "restart", control: checkbox("boss_enabled", !!boss.enabled) }));
    bossBox.appendChild(cfgRow({ key: "autoDaily", label: "cfg.label.daily", desc: "config.desc.default.daily.autoDaily",
      mark: "restart", control: checkbox("auto_daily", !!(cfg && cfg.autoDaily)) }));
  }

  /* The text of a setting: its description, or "Not used by the current code." when the dictionary does not list the key */
  function cfgDesc(key) {
    return window.DuskI18n && window.DuskI18n.has(key) ? window.DuskI18n.t(key) : T("cfg.unused");
  }
  function numberInput(name, value, min, max) {
    var input = el("input");
    input.type = "number"; input.name = name; input.step = "1";
    if (min !== null) input.min = String(min);
    if (max !== null) input.max = String(max);
    input.value = String(value);
    return input;
  }
  function selectInput(name, options, value) {
    var sel = el("select");
    sel.name = name;
    options.forEach(function (pair) {
      var o = el("option", "", pair[1]);
      o.value = pair[0];
      sel.appendChild(o);
    });
    sel.value = value;
    return sel;
  }
  function checkbox(name, checked) {
    var input = el("input");
    input.type = "checkbox"; input.name = name; input.checked = !!checked;
    return input;
  }
  /* One row of the bot tab. opts: key (the path shown under the label), label (an i18n key, or the raw name when raw is
     set), desc (a key under config.desc.*), mark ("now" or "restart"), control (the input). paintCfgRow writes the texts,
     so a language switch writes them again without rebuilding the row or losing what was typed */
  function cfgRow(opts) {
    var row = el("div", "cfg-row"), head = el("div", "cfg-row-head"), labelBox = el("div", "cfg-row-label");
    var name = el("span", ""), info = el("button", "cfg-info", "i"), keyLine = el("div", "cfg-row-key");
    var mark = el("span", "cfg-apply" + (opts.mark === "now" ? " now" : "")), desc = el("p", "cfg-desc");
    var input = el("div", "cfg-row-input");
    info.type = "button";
    info.setAttribute("aria-expanded", "false");
    labelBox.appendChild(name); labelBox.appendChild(info);
    keyLine.appendChild(document.createTextNode(opts.key + " ")); keyLine.appendChild(mark);
    head.appendChild(labelBox); head.appendChild(keyLine); head.appendChild(desc);
    input.appendChild(opts.control);
    row.setAttribute("data-cfg-row", "");
    row.appendChild(head); row.appendChild(input);
    var ref = { row: row, name: name, info: info, mark: mark, desc: desc, control: opts.control, label: opts.label,
      raw: !!opts.raw, descKey: opts.desc, markKey: opts.mark === "now" ? "cfg.apply.now" : "cfg.apply.restart" };
    S.cfgRows.push(ref);
    paintCfgRow(ref);
    return row;
  }
  function paintCfgRow(ref) {
    var text = ref.raw ? ref.label : T(ref.label);
    setTxt(ref.name, text);
    ref.control.setAttribute("aria-label", text);
    setTxt(ref.desc, cfgDesc(ref.descKey));
    setTxt(ref.mark, T(ref.markKey));
    ref.info.setAttribute("aria-label", T("cfg.info"));
    setTitle(ref.info, T("cfg.info"));
  }
  /* The search box: a row shows when its text (label, key path, description) holds the query. A section with no match is
     hidden. The section counts show the rows shown out of the rows in the section */
  function applyCfgFilter() {
    var box = $("cfg-search"), pane = $({ dashboard: "cfg-pane-dashboard", "default": "cfg-pane-default", server: "cfg-pane-server" }[S.cfgTab]);
    if (!box || !pane) return;
    var q = box.value.trim().toLowerCase(), shown = 0, total = 0;
    Array.prototype.forEach.call(pane.querySelectorAll(".cfg-section"), function (sec) {
      var rows = sec.querySelectorAll("[data-cfg-row]"), hits = 0;
      Array.prototype.forEach.call(rows, function (row) {
        var hit = !q || row.textContent.toLowerCase().indexOf(q) >= 0;
        row.hidden = !hit;
        if (hit) hits++;
      });
      sec.hidden = !!q && hits === 0;
      setTxt(sec.querySelector("[data-cfg-count]"), q ? hits + " / " + rows.length : String(rows.length));
      shown += hits;
      total += rows.length;
    });
    setTxt($("cfg-count"), q ? T("cfg.count.some", { shown: shown, total: total }) : T("cfg.count.all", { total: total }));
  }
  /* A language switch writes the bot rows again. The dashboard rows are written by DuskI18n.apply */
  function refreshCfgTexts() {
    S.cfgRows.forEach(function (ref) { paintCfgRow(ref); });
    applyCfgFilter();
    renderQuoteStatus();
  }
  /* The info button shows or hides the full description of its row */
  function toggleCfgInfo(btn) {
    var head = btn.closest(".cfg-row-head"), desc = head ? head.querySelector(".cfg-desc") : null;
    if (!desc) return;
    var open = !desc.classList.contains("open");
    desc.classList.toggle("open", open);
    btn.setAttribute("aria-expanded", open ? "true" : "false");
  }

  function initConfigForms() {
    var tabs = document.querySelectorAll("#cfg-tabs .gel");
    for (var i = 0; i < tabs.length; i++) {
      (function (btn) {
        btn.addEventListener("click", function () {
          switchConfigTab(btn.getAttribute("data-cfg-tab"));
        });
      })(tabs[i]);
    }
    $("cfg-search").addEventListener("input", applyCfgFilter);
    $("view-config").addEventListener("click", function (ev) {
      var btn = ev.target.closest ? ev.target.closest(".cfg-info") : null;
      if (btn) toggleCfgInfo(btn);
    });

    $("form-cfg-dashboard").addEventListener("submit", function (ev) {
      ev.preventDefault();
      var data = {};
      var inputs = document.querySelectorAll("#form-cfg-dashboard input");
      for (var j = 0; j < inputs.length; j++) {
        var num = parseFloat(inputs[j].value);
        data[inputs[j].name] = isNaN(num) ? inputs[j].value : num;
      }
      api("/api/v2/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scope: "dashboard", config: data })
      }).then(function () {
        showConfigBanner(T("cfg.saved.dash"), true);
        reloadAll().catch(netFail);   // the chips and the refresh rate take the new values at once
      }, function (err) {
        showConfigBanner(T("cfg.err.save", { msg: err.message }), false);
      });
    });

    $("form-cfg-default").addEventListener("submit", function (ev) {
      ev.preventDefault();
      var baseCfg = S.loadedConfigs.default || {};
      baseCfg.commands = baseCfg.commands || {};
      var cmdInputs = document.querySelectorAll("#cfg-farm-cmds input");
      for (var c = 0; c < cmdInputs.length; c++) {
        var name = cmdInputs[c].name.replace(/^cmd_/, "");
        baseCfg.commands[name] = baseCfg.commands[name] || {};
        baseCfg.commands[name].enabled = cmdInputs[c].checked;
      }
      var quoteSource = document.querySelector("[name='lvl_quote_source']");
      if (quoteSource) {
        baseCfg.commands.lvlGrind = baseCfg.commands.lvlGrind || {};
        baseCfg.commands.lvlGrind.quoteSource = quoteSource.value === "file" ? "file" : "api";
      }
      baseCfg.sleep = baseCfg.sleep || {};
      var sleepSwitch = document.querySelector("[name='sleep_enabled']");
      if (sleepSwitch) baseCfg.sleep.enabled = sleepSwitch.checked;
      var sf = document.querySelector("[name='sleep_freq']");
      var pct = sf ? readPercent(sf.value) : null;
      if (sf && pct === null) { showConfigBanner(T("cfg.err.percent"), false); return; }
      if (sf) baseCfg.sleep.frequencyPercentage = pct;
      var range = readRange("sleep_min", "sleep_max");
      if (!range) return;
      baseCfg.sleep.sleepTime = range;
      baseCfg.autoUse = baseCfg.autoUse || {};
      var ug = document.querySelector("[name='autouse_gems']");
      // gems is an object in settings.json ({enabled, order, tiers, ...}): only its enabled flag changes here
      if (ug) { baseCfg.autoUse.gems = baseCfg.autoUse.gems || {}; baseCfg.autoUse.gems.enabled = ug.checked; }
      var ul = document.querySelector("[name='autouse_lootbox']");
      if (ul) baseCfg.autoUse.autoLootbox = ul.checked;
      baseCfg.bossBattle = baseCfg.bossBattle || {};
      var bb = document.querySelector("[name='boss_enabled']");
      if (bb) baseCfg.bossBattle.enabled = bb.checked;
      var ad = document.querySelector("[name='auto_daily']");
      if (ad) baseCfg.autoDaily = ad.checked;

      api("/api/v2/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scope: "default", config: baseCfg })
      }).then(function () {
        showConfigBanner(T("cfg.saved.default"), true);
      }, function (err) {
        showConfigBanner(T("cfg.err.saveBot", { msg: err.message }), false);
      });
    });

    $("form-cfg-server").addEventListener("submit", function (ev) {
      ev.preventDefault();
      var hostEl = $("cfg-srv-host"), portEl = $("cfg-srv-port");
      var host = hostEl.value.trim(), port = parseInt(portEl.value, 10);
      if (!IPV4_RE.test(host)) { showConfigBanner(T("cfg.hostInvalid"), false); hostEl.focus(); return; }
      if (!isNum(port) || port < 1024 || port > 65535) { showConfigBanner(T("cfg.portInvalid"), false); portEl.focus(); return; }
      var gatewayPayload = gatewayValues();
      if (!gatewayPayload) { showConfigBanner(T("cfg.err.gwMax"), false); $("cfg-gw-max").focus(); return; }
      api("/api/v2/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scope: "global", config: { website: {
          host: host,
          port: port,
          restartOnCrash: $("cfg-srv-restart").checked,
          startBotOnLaunch: $("cfg-srv-launch").checked,
          stopBotWithDashboard: $("cfg-srv-stopwith").checked
        }, gateway: gatewayPayload } })
      }).then(function () {
        showConfigBanner(T("cfg.saved.server"), true);
      }, function (err) {
        showConfigBanner(T("cfg.err.saveServer", { msg: err.message }), false);
      });
    });
  }

  /* ---------- Global Fleet Controls Setup ---------- */
  function initFleetControls() {
    var btnSleep = $("btn-fleet-sleep");
    if (btnSleep) {
      btnSleep.addEventListener("click", function () {
        openConfirmModal(T("modal.fleetSleep"), function () {
          controlAccount("all", "sleep");
        });
      });
    }
    var btnWake = $("btn-fleet-wake");
    if (btnWake) {
      btnWake.addEventListener("click", function () {
        openConfirmModal(T("modal.fleetWake"), function () {
          controlAccount("all", "wake");
        });
      });
    }
    var btnReset = $("btn-fleet-reset-captcha");
    if (btnReset) {
      btnReset.addEventListener("click", function () {
        openConfirmModal(T("modal.fleetReset"), function () {
          controlAccount("all", "restart_captcha");
        });
      });
    }
  }

  /* ---------- Accessible Tooltip Engine ---------- */
  /* Native title tooltips do not show on touch screens, so every tip is a data-tip attribute and this engine
     shows it: on mouse hover, on keyboard focus, and on a tap (it stays for TIP_TAP_MS). Escape or a scroll hides it */
  var TIP_TAP_MS = 2600;
  function initTooltips() {
    var tipBox = $("obsidian-tooltip");
    if (!tipBox) return;
    document.querySelectorAll("[title]").forEach(function (node) {
      if (!node.hasAttribute("data-tip")) node.setAttribute("data-tip", node.getAttribute("title"));
      node.removeAttribute("title");
    });

    var active = null, hideTimer = null;
    function tipNode(node) { return node && node.closest ? node.closest("[data-tip]") : null; }
    function place(node) {
      var rect = node.getBoundingClientRect(), w = tipBox.offsetWidth || 220, h = tipBox.offsetHeight || 28;
      var left = Math.max(8, Math.min(window.innerWidth - w - 8, rect.left + rect.width / 2 - w / 2));
      var top = rect.top - h - 10;
      if (top < 8) top = rect.bottom + 10;
      tipBox.style.left = Math.round(left) + "px";
      tipBox.style.top = Math.round(top) + "px";
    }
    function hide() {
      window.clearTimeout(hideTimer); hideTimer = null;
      if (active) active.removeAttribute("aria-describedby");
      active = null;
      tipBox.classList.remove("visible");
      tipBox.setAttribute("aria-hidden", "true");
    }
    function show(node, timed) {
      var text = node.getAttribute("data-tip");
      if (!text) return;
      window.clearTimeout(hideTimer); hideTimer = null;
      if (active && active !== node) active.removeAttribute("aria-describedby");
      active = node;
      setTxt(tipBox, text);
      tipBox.classList.add("visible");
      tipBox.setAttribute("aria-hidden", "false");
      node.setAttribute("aria-describedby", "obsidian-tooltip");
      place(node);
      if (timed) hideTimer = window.setTimeout(hide, TIP_TAP_MS);
    }

    document.addEventListener("pointerover", function (ev) {
      if (ev.pointerType === "touch") return;
      var node = tipNode(ev.target);
      if (node && node !== active) show(node, false);
    });
    document.addEventListener("pointerout", function (ev) {
      if (ev.pointerType === "touch" || !active) return;
      var node = tipNode(ev.target);
      if (node === active && !(ev.relatedTarget && node.contains(ev.relatedTarget))) hide();
    });
    document.addEventListener("pointerup", function (ev) {
      if (ev.pointerType !== "touch") return;
      var node = tipNode(ev.target);
      if (!node || node === active) hide(); else show(node, true);
    });
    document.addEventListener("focusin", function (ev) {
      var node = tipNode(ev.target);
      if (node) show(node, false);
    });
    document.addEventListener("focusout", function (ev) {
      if (active && tipNode(ev.target) === active) hide();
    });
    document.addEventListener("keydown", function (ev) { if (ev.key === "Escape") hide(); });
    window.addEventListener("scroll", hide, { passive: true });
    // A tip whose element was re-rendered away must not stay on screen
    window.setInterval(function () { if (active && !active.isConnected) hide(); }, 1000);
  }

  /* Keeps the focused card or button when the list is re-ordered (see renderList) */
  function restoreFocus(focusId, focusPart, focusNode, fresh) {
    if (focusId === null || document.activeElement === focusNode) return;
    var keep = fresh.get(focusId);
    if (!keep) return;
    var target = focusPart === -2 ? keep.root : (focusPart >= 0 ? keep.root.querySelectorAll("button")[focusPart] : null);
    if (target && target.focus) target.focus({ preventScroll: true });
  }

  /* ---------- Router ---------- */
  function leaveView() {
    ["detail", "events", "network", "logs", "runtime"].forEach(stopPoller);
  }

  /* Plays the stagger entrance (liquid.css section 13) again on the panels of a view that was just shown */
  function replayEnter(page) {
    if (!page) return;
    var panels = page.querySelectorAll("[data-lg-enter]");
    for (var i = 0; i < panels.length; i++) {
      var p = panels[i];
      p.style.animation = "none";
      void p.offsetHeight;   // force a reflow, so the animation starts from the beginning
      p.style.animation = "";
    }
  }

  /* Page crossfade (liquid.css section 13): the view that was just shown fades in. The old view is hidden at once. Remove,
     reflow, add: the animation starts again even when the same view is shown twice in a row */
  function enterView(page) {
    if (!page) return;
    page.classList.remove("view-enter");
    void page.offsetWidth;   // force a reflow, so the animation starts from the beginning
    page.classList.add("view-enter");
  }

  /* Staggered entrance of account cards and table rows (liquid.css section 13). It plays for new nodes and for the nodes
     of the Accounts page when it is shown again, never for a poll: a poll only patches nodes that already exist. The first
     16 nodes get --lg-i 0..15 (30 ms apart, 450 ms at most); the others appear at once, they are far below the screen.
     data-lg-once marks these nodes: animationend takes the attribute off again (endEnter), so a card that is moved in the
     list later does not play the entrance a second time. The static panels of the pages keep theirs (replayEnter) */
  var ENTER_MAX = 15;
  function staggerIn(nodes) {
    for (var i = 0; i < nodes.length && i <= ENTER_MAX; i++) {
      var n = nodes[i];
      n.style.setProperty("--lg-i", String(i));
      n.setAttribute("data-lg-once", "");
      n.setAttribute("data-lg-enter", "");
    }
  }
  function endEnter(ev) {
    var n = ev.target;
    if (!n || !n.hasAttribute || !n.hasAttribute("data-lg-once") || (ev.animationName && ev.animationName !== "lg-enter")) return;
    n.removeAttribute("data-lg-enter");
    n.removeAttribute("data-lg-once");
    n.style.removeProperty("--lg-i");
  }
  /* The cards (or rows) that the Accounts page shows now */
  function enterList() {
    var parent = $(S.mode === "table" ? "rows" : "cards");
    if (parent) staggerIn(parent.children);
  }

  function show(view, arg) {
    var wasDrawer = S.view === "account";
    leaveView();
    S.view = view;
    document.body.setAttribute("data-view", view);   // aero.css keeps the Overview to one screen on desktop
    // The account drawer slides over the account list, which stays visible behind its scrim
    VIEWS.concat(["account"]).forEach(function (v) {
      var elView = $("view-" + v);
      if (elView) elView.hidden = v !== view && !(view === "account" && v === "accounts");
    });
    $("drawer-scrim").hidden = view !== "account";
    replayEnter($("view-" + view));
    // The drawer has its own slide, and the account list behind it (or under it, when it closes) must not flash
    if (view !== "account" && !wasDrawer) enterView($("view-" + view));
    if (view === "account" && !wasDrawer) S.opener = document.activeElement;
    if (wasDrawer && view !== "account" && S.opener && S.opener.focus && document.body.contains(S.opener)) {
      S.opener.focus({ preventScroll: true });
    }
    var tab = view === "account" ? "accounts" : view;
    var tabs = document.querySelectorAll("#tabs .gel");
    for (var i = 0; i < tabs.length; i++) {
      var on = tabs[i].getAttribute("data-view") === tab;
      tabs[i].setAttribute("aria-selected", on ? "true" : "false");
      if (on) tabs[i].setAttribute("aria-current", "page"); else tabs[i].removeAttribute("aria-current");
      setCls(tabs[i], "gel lg-nav-link" + (on ? " on" : ""));
    }
    if (view === "overview") W(renderOverview);
    else if (view === "accounts") {
      syncControls();
      if (S.items.length) W(renderList); else loadPage().catch(netFail);
      if (!wasDrawer) W(enterList);
    }
    else if (view === "account") {
      S.accountId = arg; S.detailVersion = -1; $("d-title").textContent = "";
      $("view-account").scrollTop = 0;
      if (!S.items.length) loadPage().catch(netFail);
      poller("detail", loadDetail, S.refreshMs);
      if (!wasDrawer) $("d-title").focus({ preventScroll: true });
    }
    else if (view === "events") { S.ev.last = 0; S.ev.list = []; poller("events", loadEvents, S.refreshMs); }
    else if (view === "network") { S.gwVersion = -1; poller("network", loadNetwork, S.refreshMs); }
    else if (view === "logs") { poller("logs", loadConsole, 2000); }
    else if (view === "runtime") { poller("runtime", loadRuntime, 60000); }
    else if (view === "config") switchConfigTab(S.cfgTab);
    if (view !== "account") window.scrollTo(0, 0);
  }

  function route() {
    var hash = decodeURIComponent((window.location.hash || "").replace(/^#/, "")) || "overview";
    var parts = hash.split("/"), name = parts[0];
    if (name === "account" && parts[1]) show("account", parts.slice(1).join("/"));
    else if (VIEWS.indexOf(name) >= 0) show(name);
    else show("overview");
  }

  /* A language switch writes the text again from the data already on the page. No request is sent for it */
  function onLangChange() {
    populateSorts();
    buildHead();
    syncControls();
    document.querySelectorAll(".pager-pages").forEach(function (holder) { holder.removeAttribute("data-sig"); });
    S.nodes = new Map(); S.cmdSig = null; S.attSig = null;
    setFollowPill();
    renderHeader();
    if (S.lan) renderLan();
    if (S.summary) W(renderOverview);
    if (S.view === "accounts") { S.quietRender = true; W(renderList); }   // the cards are rebuilt, not new: no entrance
    if (S.view === "account" && S.lastDetail) W(function () { renderDetail(S.lastDetail); });
    if (S.view === "events") W(renderEvents);
    if (S.view === "network" && S.gwLast) W(function () { renderNetwork(S.gwLast); });
    if (S.view === "runtime" && S.rtLast) W(function () { renderRuntime(S.rtLast); });
    if (S.view === "config") refreshCfgTexts();
  }

  /* ---------- Initialization ---------- */
  function init() {
    S.mode = recall("mode", "cards") === "table" ? "table" : "cards";
    var size = parseInt(recallKey("dusk.accounts.size", "50"), 10);
    S.size = [25, 50, 100, 500].indexOf(size) >= 0 ? size : 50;

    var tabs = document.querySelectorAll("#tabs .gel");
    for (var i = 0; i < tabs.length; i++) {
      (function (b) {
        var name = b.getAttribute("data-view");
        b.setAttribute("aria-label", b.getAttribute("data-tip") || name);
        b.addEventListener("click", function () {
          if (window.location.hash === "#" + name) route();
          else window.location.hash = "#" + name;
        });
      })(tabs[i]);
    }

    $("e-type").addEventListener("change", function () {
      S.ev.type = this.value; S.ev.last = 0; S.ev.list = [];
      if (S.pollers.events) S.pollers.events.kick();
    });
    $("e-q").addEventListener("input", function () {
      S.ev.q = this.value.trim(); W(renderEvents);
    });

    initAccountControls();
    initLogs();
    initFleetControls();
    initConfigForms();
    initTooltips();
    initNav();
    initModals();
    initRuntimeChart();
    initBot();
    poller("bot", pollBot, 3000, false);

    document.addEventListener("animationend", endEnter, true);
    document.addEventListener("animationcancel", endEnter, true);
    window.addEventListener("hashchange", route);
    window.addEventListener("dusk:langchange", onLangChange);
    reloadAll().then(function () { netOk(); }, netFail).then(function () {
      route();
      poller("changes", pollChanges, S.refreshMs, true);
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
