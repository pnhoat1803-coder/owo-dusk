/* ==========================================================================
   OwO-Dusk phone dashboard (/m) - vanilla JavaScript, no build step, no CDN. Every visible text goes through
   window.DuskI18n (i18n.js). Data comes from /api/v2/m/home (every 5 s, only while the page is visible), and from
   /api/v2/m/accounts, /api/v2/events and /api/v2/console, which are read only while their tab is open. Requests carry
   the page's CSRF token (X-Dusk-CSRF) when they change something. Nothing here stores account data in the browser.
   ========================================================================== */
(function () {
  "use strict";

  var EM = "—";
  var PAGE = 25;
  var BACKOFF = [5000, 10000, 20000, 30000];
  var TABS = ["home", "accounts", "events", "logs", "more"];
  var BOT_DONE = { start: "bot.done.start", stop: "bot.done.stop", restart: "bot.done.restart" };

  var S = { tab: "home", home: null, attention: [], detail: null, sheet: null, opener: null, botPending: null, accId: null };
  var A = { q: "", state: "", offset: 0, total: 0, rows: [], loading: false, done: false, seq: 0, sig: "" };
  var L = { last: 0, lines: [], stick: true };
  var E = { sig: "" };
  var cache = {};
  var pollers = [];

  function $(id) { return document.getElementById(id); }
  function T(key, vars) { return window.DuskI18n ? window.DuskI18n.t(key, vars) : key; }
  function lab(prefix, name) {
    var key = prefix + name;
    return window.DuskI18n && window.DuskI18n.has(key) ? window.DuskI18n.t(key) : String(name || "");
  }
  function isNum(v) { return typeof v === "number" && isFinite(v); }
  function noop() {}
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }
  function setText(node, text) { if (node && node.textContent !== text) node.textContent = text; }
  function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }
  function setTitle(node, text) { if (node && node.getAttribute("title") !== text) node.setAttribute("title", text); }
  function csrf() {
    var meta = document.querySelector("meta[name='csrf-token']");
    return meta ? meta.getAttribute("content") || "" : "";
  }

  /* ---------- numbers and times ---------- */
  function fmtInt(n) { return isNum(n) ? Math.round(n).toLocaleString("en-US") : EM; }
  function fmt1(n) { return isNum(n) ? (Math.round(n * 10) / 10).toLocaleString("en-US", { maximumFractionDigits: 1 }) : EM; }
  function fmtFull(n, opts) {
    var signed = !!(opts && opts.signed), keep = !!(opts && opts.decimals), a = Math.abs(n);
    var sign = n < 0 ? "-" : (signed && n > 0 ? "+" : "");
    return sign + (keep ? (Math.round(a * 10) / 10).toLocaleString("en-US", { maximumFractionDigits: 1 })
      : Math.round(a).toLocaleString("en-US"));
  }
  /* Compact numbers as the desktop writes them: 1,234 - 12.3K - 156.6M - 1.2B. The exact value goes in the title */
  function fmtCompact(n, opts) {
    if (!isNum(n)) return EM;
    var signed = !!(opts && opts.signed), keep = !!(opts && opts.decimals), a = Math.abs(n);
    var sign = n < 0 ? "-" : (signed && n > 0 ? "+" : "");
    if (a < 10000) {
      return sign + (keep ? (Math.round(a * 10) / 10).toLocaleString("en-US", { maximumFractionDigits: 1 })
        : Math.round(a).toLocaleString("en-US"));
    }
    var steps = [[1e3, "K"], [1e6, "M"], [1e9, "B"]], idx = 0, i, v;
    for (i = 0; i < steps.length; i++) if (a >= steps[i][0]) idx = i;
    v = Math.round(a / steps[idx][0] * 10) / 10;
    if (v >= 1000 && idx < steps.length - 1) { idx++; v = Math.round(a / steps[idx][0] * 10) / 10; }
    return sign + v.toFixed(1).replace(/\.0$/, "") + steps[idx][1];
  }
  function setValue(node, n, opts) {
    setText(node, fmtCompact(n, opts));
    if (node) setTitle(node, isNum(n) ? fmtFull(n, opts) : "");
  }
  function fmtAgo(sec) {
    if (!isNum(sec)) return EM;
    if (sec < 0) sec = 0;
    if (sec < 60) return T("ago.sec", { n: Math.floor(sec) });
    if (sec < 3600) return T("ago.min", { n: Math.floor(sec / 60) });
    if (sec < 86400) return T("ago.hm", { h: Math.floor(sec / 3600), m: Math.floor((sec % 3600) / 60) });
    return T("ago.day", { n: Math.floor(sec / 86400) });
  }
  function fmtClock(ts) {
    if (!isNum(ts)) return EM;
    var d = new Date(ts * 1000);
    return (d.getHours() < 10 ? "0" : "") + d.getHours() + ":" + (d.getMinutes() < 10 ? "0" : "") + d.getMinutes();
  }
  function fmtUptime(minutes) {
    if (!isNum(minutes)) return EM;
    var h = Math.floor(minutes / 60), m = Math.floor(minutes % 60);
    return h >= 1 ? h + " h " + (m < 10 ? "0" : "") + m + " m" : m + " m";
  }
  function shortId(id) { return T("acc.short", { id: String(id).slice(-4) }); }
  function nameOf(name, id) { return name || shortId(id); }

  /* ---------- network: one request, with the ETag of the last answer ---------- */
  function goLogin() { window.location.href = "/login?next=" + encodeURIComponent("/m"); }
  function getJson(path) {
    var entry = cache[path], headers = { Accept: "application/json" };
    if (entry && entry.etag) headers["If-None-Match"] = entry.etag;
    return window.fetch(path, { credentials: "same-origin", cache: "no-store", headers: headers }).then(function (res) {
      if (res.status === 401) { goLogin(); throw new Error("auth"); }
      if (res.status === 304 && entry) return entry.data;
      if (!res.ok) throw new Error("HTTP " + res.status);
      return res.json().then(function (data) {
        cache[path] = { etag: res.headers.get("ETag"), data: data };
        return data;
      });
    });
  }
  function postJson(path, body) {
    return window.fetch(path, {
      method: "POST", credentials: "same-origin", cache: "no-store",
      headers: { Accept: "application/json", "Content-Type": "application/json", "X-Dusk-CSRF": csrf() },
      body: JSON.stringify(body || {})
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (res.status === 401) { goLogin(); throw new Error("auth"); }
        if (!res.ok) {
          var err = new Error((data && (data.message || data.error)) || ("HTTP " + res.status));
          err.status = res.status;
          err.body = data || {};
          throw err;
        }
        return data;
      });
    });
  }

  /* ---------- pollers: back-off after failures, paused while the page is hidden or the tab is not open ---------- */
  function setOnline(ok) {
    var pill = $("m-pill");
    if (pill) pill.hidden = !!ok;
  }
  function makePoller(fn, every, active) {
    var p = { fails: 0, busy: false, timer: null };
    p.run = function () {
      p.timer = null;
      if (p.busy || document.hidden || !active()) return;
      p.busy = true;
      Promise.resolve().then(fn).then(function () {
        p.fails = 0;
        setOnline(true);
      }, function (err) {
        if (!err || err.message !== "auth") { p.fails += 1; setOnline(false); }
      }).then(function () {
        p.busy = false;
        p.schedule();
      });
    };
    p.schedule = function () {
      if (p.timer || document.hidden || !active()) return;
      var wait = p.fails ? Math.max(every, BACKOFF[Math.min(p.fails, BACKOFF.length) - 1]) : every;
      p.timer = window.setTimeout(p.run, wait);
    };
    p.kick = function () {
      if (p.timer) { window.clearTimeout(p.timer); p.timer = null; }
      p.run();
    };
    p.pause = function () {
      if (p.timer) { window.clearTimeout(p.timer); p.timer = null; }
    };
    pollers.push(p);
    return p;
  }

  /* ---------- Home ---------- */
  var homePoll = null;
  function pollHome() {
    return getJson("/api/v2/m/home").then(function (d) {
      S.home = d;
      S.attention = d.attention || [];
      renderHome();
      syncMore();
    });
  }
  function botTone(state) {
    if (state === "running") return "ok";
    if (state === "starting" || state === "stopping") return "warn";
    if (state === "crashed") return "off";
    return "muted";
  }
  function botLabel(bot) {
    var st = bot.state;
    if (st === "running") return T("bot.state.running", { dur: fmtUptime(bot.uptime_min) });
    if (st === "starting") return T("bot.state.starting");
    if (st === "stopping") return T("bot.state.stopping");
    if (st === "crashed") return isNum(bot.exit_code) ? T("bot.state.crashed", { code: bot.exit_code }) : T("bot.state.crashedNoCode");
    return T("bot.state.stopped");
  }
  function renderBot(bot) {
    var st = bot.state || "", state = $("m-bot-state");
    if (state) {
      state.className = "m-state " + botTone(st);
      setText(state, botLabel(bot));
    }
    var idle = !S.botPending && st !== "stopping";
    var canStart = idle && (st === "stopped" || st === "crashed" || !st);
    var canStop = idle && (st === "running" || st === "starting");
    var start = $("m-btn-start"), stop = $("m-btn-stop"), restart = $("m-btn-restart");
    if (start) start.disabled = !canStart;
    if (stop) stop.disabled = !canStop;
    if (restart) restart.disabled = !canStop;
    var note = $("m-bot-note");
    if (note) {
      var cli = bot.supervised === false && (st === "running" || st === "starting");
      note.hidden = !cli;
      setText(note, cli ? T("bot.cli") : "");
    }
  }
  function toneFor(value, bad) { return bad ? "m-tone-bad" : ""; }
  function renderHome() {
    var d = S.home;
    if (!d) return;
    var s = d.summary || {};
    renderBot(d.bot || {});
    setValue($("m-h-online"), s.accounts_online);
    setText($("m-h-total"), isNum(s.accounts_total) ? T("m.hero.of", { total: fmtCompact(s.accounts_total) }) : "");
    setValue($("m-h-hpm"), s.hunts_per_min_total, { decimals: true });
    setValue($("m-h-cow"), s.cowoncy_today_total, { signed: true });
    var cap = $("m-c-cap"), err = $("m-c-err"), net = $("m-c-net");
    setValue(cap, s.captcha_today);
    setValue(err, s.errors_1h);
    setText(net, isNum(s.inbound_avg_kb_min) ? fmt1(s.inbound_avg_kb_min) : EM);
    if (net) net.className = "m-chip-val " + ((s.inbound_over_bad || 0) > 0 ? "m-tone-bad" : (s.inbound_over_warn || 0) > 0 ? "m-tone-warn" : "");
    renderAttention(S.attention);
    renderAutostop(s.autostop);
  }
  function renderAttention(list) {
    var ul = $("m-attention");
    if (!ul) return;
    clear(ul);
    if (!list.length) {
      ul.appendChild(el("li", "m-empty", T("att.none")));
      return;
    }
    list.forEach(function (item) {
      var li = el("li"), btn = el("button", "m-att");
      btn.type = "button";
      btn.appendChild(el("span", "who", nameOf(item.name, item.id)));
      btn.appendChild(el("span", "m-tone-" + (item.health === "bad" ? "bad" : item.health === "warn" ? "warn" : "muted"), lab("health.", item.health)));
      var reason = item.reasons && item.reasons.length ? item.reasons[0] : lab("state.", item.state);
      btn.appendChild(el("span", "why", reason));
      btn.addEventListener("click", function () { openAccount(item.id, item.name); });
      li.appendChild(btn);
      ul.appendChild(li);
    });
  }
  function renderAutostop(a) {
    var line = $("m-autostop");
    if (!line) return;
    var text = T("auto.off");
    if (a && a.enabled) {
      if (a.stopping) text = T("auto.stopping");
      else if (isNum(a.stops_at)) text = T("auto.at", { clock: fmtClock(a.stops_at), dur: fmtUptime(Math.max(0, (a.stops_at - Date.now() / 1000) / 60)) });
      else text = T("auto.on");
    }
    setText(line, T("m.autostop", { state: text }));
  }

  /* ---------- Accounts: 25 at a time, the next page when the end of the list comes into view ---------- */
  function rowObj(r) {
    return { id: r[0], name: r[1], state: r[2], health: r[3], hpm: r[4], ago: r[5], cow: r[6], cap: r[7] };
  }
  function filterQuery() {
    return (A.q ? "&q=" + encodeURIComponent(A.q) : "") + (A.state ? "&state=" + encodeURIComponent(A.state) : "");
  }
  function loadAccounts(reset) {
    if (reset) { A.offset = 0; A.rows = []; A.done = false; A.seq += 1; A.sig = ""; }
    if (A.loading || A.done) return Promise.resolve();
    var seq = A.seq;
    A.loading = true;
    var url = "/api/v2/m/accounts?offset=" + A.offset + "&limit=" + PAGE + filterQuery();
    return getJson(url).then(function (d) {
      if (seq !== A.seq) return;
      var more = (d.rows || []).map(rowObj);
      A.total = d.total || 0;
      A.rows = A.rows.concat(more);
      A.offset += more.length;
      A.done = !more.length || A.offset >= A.total;
      renderAccounts();
    }).then(function () { A.loading = false; }, function (err) { A.loading = false; throw err; });
  }
  /* The rows already on the screen, read again (the numbers move), without losing the scroll position */
  function refreshAccounts() {
    if (!A.rows.length) return Promise.resolve();
    var n = Math.min(100, Math.max(PAGE, A.rows.length));
    var seq = A.seq;
    return getJson("/api/v2/m/accounts?offset=0&limit=" + n + filterQuery()).then(function (d) {
      if (seq !== A.seq) return;
      A.rows = (d.rows || []).map(rowObj);
      A.total = d.total || 0;
      A.offset = A.rows.length;
      A.done = A.offset >= A.total;
      renderAccounts();
    });
  }
  function stateTone(state) {
    if (state === "hunting") return "ok";
    if (state === "captcha" || state === "stopped" || state === "offline") return "off";
    if (state === "sleeping") return "muted";
    return "warn";
  }
  function accountRow(r) {
    var btn = el("button", "m-row");
    btn.type = "button";
    btn.setAttribute("data-id", r.id);
    btn.appendChild(el("span", "nm", nameOf(r.name, r.id)));
    btn.appendChild(el("span", "st m-tone-" + (stateTone(r.state) === "ok" ? "good" : stateTone(r.state) === "off" ? "bad" : "muted"), lab("state.", r.state)));
    btn.appendChild(el("span", "meta", T("m.row.meta", { hpm: fmt1(r.hpm), ago: fmtAgo(r.ago) })));
    btn.addEventListener("click", function () { openAccount(r.id, r.name); });
    return btn;
  }
  function renderAccounts() {
    var sig = A.rows.map(function (r) { return [r.id, r.state, r.health, r.hpm, r.ago, r.cow, r.cap].join(","); }).join(";");
    if (sig !== A.sig) {
      A.sig = sig;
      var list = $("m-acc-list");
      clear(list);
      A.rows.forEach(function (r) {
        var li = el("li");
        li.appendChild(accountRow(r));
        list.appendChild(li);
      });
    }
    setText($("m-acc-info"), A.total ? T("m.acc.count", { shown: A.rows.length, total: A.total }) : "");
    var none = $("m-acc-empty");
    if (none) none.hidden = A.total > 0 || A.loading;
    maybeMore();
  }
  function maybeMore() {
    var sentinel = $("m-acc-sentinel");
    if (!sentinel || S.tab !== "accounts" || A.done || A.loading) return;
    if (sentinel.getBoundingClientRect().top < window.innerHeight + 200) loadAccounts(false).catch(noop);
  }

  /* ---------- Events ---------- */
  var eventsPoll = null;
  function loadEvents() {
    return getJson("/api/v2/events?after=0&limit=50").then(function (d) {
      var list = (d.events || []).slice().reverse();
      var empty = $("m-events-empty");
      if (empty) empty.hidden = list.length > 0;
      var sig = list.map(function (e) { return e.id; }).join(",");
      if (sig === E.sig) return;
      E.sig = sig;
      var ul = $("m-events");
      clear(ul);
      list.forEach(function (e) {
        var li = el("li", "m-ev" + (e.type === "captcha" ? " captcha" : e.sev === "warn" ? " warn" : ""));
        li.appendChild(el("span", "tm", fmtClock(e.ts)));
        li.appendChild(el("span", "txt", e.text || lab("ev.type.", e.type)));
        li.appendChild(el("span", "who", (e.name || (e.acct ? shortId(e.acct) : T("ev.all"))) + " · " + lab("ev.type.", e.type)));
        ul.appendChild(li);
      });
    });
  }

  /* ---------- Logs: the last 200 lines, read only while the tab is open ---------- */
  var logsPoll = null;
  function loadLogs() {
    return getJson("/api/v2/console?after=" + L.last + "&limit=200").then(function (d) {
      var lines = d.lines || [];
      if (lines.length) {
        L.last = lines[lines.length - 1].id;
        L.lines = L.lines.concat(lines).slice(-200);
        var pre = $("m-console");
        var stick = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40 || L.stick;
        clear(pre);
        L.lines.forEach(function (l) {
          var span = el("span", l.level === "warn" ? "warn" : l.level === "error" ? "error" : "", (l.t ? "[" + l.t + "] " : "") + l.text);
          pre.appendChild(span);
          pre.appendChild(document.createTextNode("\n"));
        });
        if (stick) pre.scrollTop = pre.scrollHeight;
      }
      var none = $("m-console-empty");
      if (none) none.hidden = L.lines.length > 0;
    });
  }

  /* ---------- Sheets: one at a time ---------- */
  function openSheet(id, focusNode) {
    closeSheet();
    var sheet = $(id);
    if (!sheet) return;
    S.sheet = sheet;
    S.opener = document.activeElement;
    $("m-scrim").hidden = false;
    sheet.hidden = false;
    if (focusNode && focusNode.focus) focusNode.focus({ preventScroll: true });
  }
  function closeSheet() {
    if (!S.sheet) return;
    S.sheet.hidden = true;
    S.sheet = null;
    $("m-scrim").hidden = true;
    if (S.opener && S.opener.focus && document.body.contains(S.opener)) S.opener.focus({ preventScroll: true });
    S.opener = null;
  }
  function toast(text, isErr) {
    var box = $("m-toast");
    if (!box) return;
    setText(box, text);
    box.className = "m-toast lg glass-lite lg-strong" + (isErr ? " err" : "");   // liquid.css material, see m.css
    box.hidden = false;
    window.clearTimeout(box._timer);
    box._timer = window.setTimeout(function () { box.hidden = true; }, isErr ? 7000 : 4000);
  }

  /* ---------- account sheet ---------- */
  function openAccount(id, name) {
    S.accId = id;
    var row = null;
    A.rows.forEach(function (r) { if (r.id === id) row = r; });
    var state = row ? row.state : "";
    S.attention.forEach(function (item) { if (item.id === id && !state) state = item.state; });
    setText($("m-acc-name"), nameOf(name || (row && row.name), id));
    var sleep = $("m-acc-sleep");
    setText(sleep, state === "sleeping" ? T("drawer.wake") : T("drawer.sleepAccount"));
    sleep.setAttribute("data-action", state === "sleeping" ? "wake" : "sleep");
    fillAccount(row, null, state);
    openSheet("m-sheet-account", $("m-acc-close"));
    getJson("/api/v2/accounts/" + encodeURIComponent(id)).then(function (d) {
      if (S.accId !== id || !d || !d.account) return;
      fillAccount(row, d.account, d.account.state);
      var acc = d.account, sleepNow = $("m-acc-sleep");
      setText(sleepNow, acc.state === "sleeping" ? T("drawer.wake") : T("drawer.sleepAccount"));
      sleepNow.setAttribute("data-action", acc.state === "sleeping" ? "wake" : "sleep");
    }, noop);
  }
  function fillAccount(row, full, state) {
    var chips = $("m-acc-chips");
    clear(chips);
    var health = full ? full.health : (S.attention.filter(function (i) { return i.id === S.accId; })[0] || {}).health;
    if (state) chips.appendChild(el("span", "m-state " + stateTone(state), lab("state.", state)));
    if (health) chips.appendChild(el("span", "m-state " + (health === "bad" ? "off" : health === "warn" ? "warn" : health === "good" ? "ok" : "muted"), lab("health.", health)));
    var dl = $("m-acc-fields");
    clear(dl);
    var f = full || {};
    var rowsOut = [
      [T("m.row.hpm"), isNum(f.hunts_per_min_15m) ? fmt1(f.hunts_per_min_15m) : (row ? fmt1(row.hpm) : EM)],
      [T("m.row.huntsTotal"), fmtInt(f.hunts_total)],
      [T("m.row.lastHunt"), isNum(f.last_hunt_ago_s) ? fmtAgo(f.last_hunt_ago_s) : (row ? fmtAgo(row.ago) : EM)],
      [T("m.row.cowoncy"), fmtInt(full ? f.cowoncy : (row ? row.cow : null))],
      [T("m.row.captchaToday"), fmtInt(full ? f.captcha_today : (row ? row.cap : null))],
      [T("m.row.inbound"), isNum(f.inbound_kb_min) ? fmt1(f.inbound_kb_min) + " " + T("unit.kbMin") : EM],
      [T("m.row.errors"), fmtInt(f.errors_1h)]
    ];
    rowsOut.forEach(function (pair) {
      dl.appendChild(el("dt", "", pair[0]));
      dl.appendChild(el("dd", "", pair[1]));
    });
    var why = $("m-acc-why");
    clear(why);
    var reasons = full && full.health_reasons ? full.health_reasons.slice(0, 3) : [];
    if (!reasons.length && full && full.health === "good") reasons = [T("drawer.healthy")];
    reasons.forEach(function (r) { why.appendChild(el("li", "", r)); });
    S.detail = full;
  }
  function accountAction() {
    var id = S.accId, btn = $("m-acc-sleep"), action = btn ? btn.getAttribute("data-action") || "sleep" : "sleep";
    if (!id) return;
    postJson("/api/v2/control", { target: id, action: action }).then(function () {
      toast(T("fleet.queued", { action: lab("act.", action), target: nameOf(null, id) }), false);
      closeSheet();
      if (homePoll) homePoll.kick();
    }, function (err) {
      toast(T("fleet.error", { msg: err.message }), true);
    });
  }

  /* ---------- bot control: Start asks for the optional auto-stop, Stop and Restart ask first ---------- */
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
  function botAction(name, body) {
    if (S.botPending) return;
    S.botPending = name;
    renderBot((S.home && S.home.bot) || {});
    postJson("/api/v2/bot/" + name, body).then(function () {
      toast(T(BOT_DONE[name]), false);
    }, function (err) {
      if (err && err.message === "auth") return;
      toast(botErrorText(err), true);
    }).then(function () {
      S.botPending = null;
      renderBot((S.home && S.home.bot) || {});
      if (homePoll) homePoll.kick();
    });
  }
  function openStart() {
    var input = $("m-start-min"), err = $("m-start-error");
    input.value = "";
    err.hidden = true;
    setText(err, "");
    openSheet("m-sheet-start", input);
  }
  function submitStart() {
    var input = $("m-start-min"), err = $("m-start-error"), raw = input.value.trim(), minutes = null;
    if (raw !== "") {
      var n = parseInt(raw, 10);
      if (!/^\d+$/.test(raw) || n < 1 || n > 1440) {
        setText(err, T("bot.dialog.invalid"));
        err.hidden = false;
        input.focus();
        return;
      }
      minutes = n;
    }
    closeSheet();
    botAction("start", { maxRuntimeMinutes: minutes });
  }
  function openConfirm(name) {
    var n = S.home && S.home.summary ? S.home.summary.accounts_total : null;
    setText($("m-confirm-msg"), name === "stop" ? T("bot.confirm.stop", { n: fmtInt(n) }) : T("bot.confirm.restart", { n: fmtInt(n) }));
    $("m-confirm-ok").onclick = function () { closeSheet(); botAction(name, {}); };
    openSheet("m-sheet-confirm", $("m-confirm-cancel"));
  }

  /* ---------- More: theme, language, addresses, the desktop version ---------- */
  function syncMore() {
    var nav = window.duskNav ? window.duskNav.read() : null;
    document.querySelectorAll("[data-theme-pick]").forEach(function (b) {
      b.setAttribute("aria-pressed", nav && b.getAttribute("data-theme-pick") === nav.theme ? "true" : "false");
    });
    var lang = window.DuskI18n ? window.DuskI18n.lang() : "en";
    document.querySelectorAll("[data-lang-pick]").forEach(function (b) {
      b.setAttribute("aria-pressed", b.getAttribute("data-lang-pick") === lang ? "true" : "false");
    });
    var lan = (S.home && S.home.lan) || {}, urls = lan.lan_urls || [];
    var list = $("m-lan");
    if (list) {
      clear(list);
      var local = el("li");
      local.appendChild(el("span", "", T("lan.this") + ": " + (lan.local_url || "")));
      list.appendChild(local);
      urls.forEach(function (url) {
        var li = el("li"), code = el("code", "", url), btn = el("button", "m-btn", T("lan.copy"));
        btn.type = "button";
        btn.addEventListener("click", function () { copyText(url, btn); });
        li.appendChild(code);
        li.appendChild(btn);
        list.appendChild(li);
      });
    }
    $("m-lan-none").hidden = urls.length > 0;
    $("m-lan-hint").hidden = urls.length > 0;
    var banner = $("m-banner");
    if (banner && S.home) banner.hidden = !(S.home.no_password || S.home.password_banner);
    setText($("m-version"), S.home && S.home.bot_version ? T("brand.version") + " v" + S.home.bot_version : "");
  }
  function copyText(text, btn) {
    var done = function () {
      setText(btn, T("lan.copied"));
      window.setTimeout(function () { setText(btn, T("lan.copy")); }, 1800);
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
      if (ok) done(); else toast(T("lan.copyManual"), true);
    };
    if (window.navigator && window.navigator.clipboard && window.navigator.clipboard.writeText) {
      window.navigator.clipboard.writeText(text).then(done, fallback);
    } else {
      fallback();
    }
  }
  function useDesktop() {
    try { window.localStorage.setItem("dusk.view", "desktop"); } catch (err) { /* blocked: the link still works */ }
    window.location.href = "/";
  }

  /* ---------- tabs ---------- */
  function tabFromHash() {
    var name = (window.location.hash || "").replace(/^#/, "");
    return TABS.indexOf(name) >= 0 ? name : "home";
  }
  function setTab(name) {
    if (TABS.indexOf(name) < 0) name = "home";
    S.tab = name;
    document.body.setAttribute("data-tab", name);
    TABS.forEach(function (t) { var view = $("v-" + t); if (view) view.hidden = t !== name; });
    document.querySelectorAll(".m-tab").forEach(function (b) {
      if (b.getAttribute("data-tab") === name) b.setAttribute("aria-current", "page");
      else b.removeAttribute("aria-current");
    });
    setText($("m-title"), T("m.tab." + name));
    window.scrollTo(0, 0);
    if (name === "accounts" && !A.rows.length && !A.loading) loadAccounts(true).catch(noop);
    if (name === "events" && eventsPoll) eventsPoll.kick();
    if (name === "logs" && logsPoll) logsPoll.kick();
    if (name !== "events" && eventsPoll) eventsPoll.pause();
    if (name !== "logs" && logsPoll) logsPoll.pause();
    if (name === "more") syncMore();
    closeSheet();
  }

  /* ---------- start ---------- */
  function init() {
    document.querySelectorAll(".m-tab").forEach(function (b) {
      b.addEventListener("click", function () {
        var name = b.getAttribute("data-tab");
        if ((window.location.hash || "").replace(/^#/, "") === name) setTab(name);
        else window.location.hash = "#" + name;
      });
    });
    window.addEventListener("hashchange", function () { setTab(tabFromHash()); });
    $("m-refresh").addEventListener("click", function () {
      if (homePoll) homePoll.kick();
      if (S.tab === "accounts" && accPoll) accPoll.kick();
      if (S.tab === "events" && eventsPoll) eventsPoll.kick();
      if (S.tab === "logs" && logsPoll) logsPoll.kick();
    });
    $("m-btn-start").addEventListener("click", openStart);
    $("m-btn-stop").addEventListener("click", function () { openConfirm("stop"); });
    $("m-btn-restart").addEventListener("click", function () { openConfirm("restart"); });
    $("m-start-ok").addEventListener("click", submitStart);
    $("m-start-cancel").addEventListener("click", closeSheet);
    $("m-start-min").addEventListener("keydown", function (ev) { if (ev.key === "Enter") { ev.preventDefault(); submitStart(); } });
    $("m-confirm-cancel").addEventListener("click", closeSheet);
    $("m-acc-sleep").addEventListener("click", accountAction);
    $("m-acc-open").addEventListener("click", function () {
      if (S.accId) window.location.href = "/#account/" + encodeURIComponent(S.accId);
    });
    $("m-acc-close").addEventListener("click", closeSheet);
    $("m-scrim").addEventListener("click", closeSheet);
    document.addEventListener("keydown", function (ev) { if (ev.key === "Escape") closeSheet(); });
    $("m-desktop").addEventListener("click", useDesktop);
    document.querySelectorAll("[data-theme-pick]").forEach(function (b) {
      b.addEventListener("click", function () { if (window.duskNav) window.duskNav.setTheme(b.getAttribute("data-theme-pick")); syncMore(); });
    });
    document.querySelectorAll("[data-lang-pick]").forEach(function (b) {
      b.addEventListener("click", function () { if (window.DuskI18n) window.DuskI18n.setLang(b.getAttribute("data-lang-pick")); syncMore(); });
    });

    var search = $("m-q"), timer = null;
    search.addEventListener("input", function () {
      window.clearTimeout(timer);
      timer = window.setTimeout(function () {
        A.q = search.value.trim();
        loadAccounts(true).catch(noop);
      }, 250);
    });
    document.querySelectorAll(".m-fchip").forEach(function (b) {
      b.addEventListener("click", function () {
        A.state = b.getAttribute("data-state") || "";
        document.querySelectorAll(".m-fchip").forEach(function (other) { other.classList.toggle("on", other === b); });
        loadAccounts(true).catch(noop);
      });
    });
    if ("IntersectionObserver" in window) {
      var io = new window.IntersectionObserver(function (entries) {
        entries.forEach(function (entry) { if (entry.isIntersecting) maybeMore(); });
      }, { rootMargin: "200px 0px" });
      io.observe($("m-acc-sentinel"));
    }
    window.addEventListener("scroll", maybeMore, { passive: true });

    window.addEventListener("dusk:langchange", function () {
      setText($("m-title"), T("m.tab." + S.tab));
      renderHome();
      renderAccounts();
      syncMore();
      if (S.accId) fillAccount(null, S.detail, S.detail ? S.detail.state : "");
      loadEvents().catch(noop);
    });
    window.addEventListener("dusk-nav", syncMore);

    document.addEventListener("visibilitychange", function () {
      pollers.forEach(function (p) {
        if (document.hidden) p.pause();
        else p.kick();
      });
    });

    homePoll = makePoller(pollHome, 5000, function () { return true; });
    var accPoll = makePoller(refreshAccounts, 10000, function () { return S.tab === "accounts" && A.rows.length > 0; });
    eventsPoll = makePoller(loadEvents, 10000, function () { return S.tab === "events"; });
    logsPoll = makePoller(loadLogs, 3000, function () { return S.tab === "logs"; });
    setTab(tabFromHash());
    homePoll.kick();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
