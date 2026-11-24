"""Routes of the v2 dashboard: pages, login, /api/v2/*, security headers and the global auth check.

GET routes read the snapshot that data_v2.SnapshotStore keeps in memory, they never open a database.
Two POST routes write files that the bot reads, and three start, stop or restart the bot process (website/bot_supervisor.py):
  POST /api/v2/control  appends one command to utils/data/web_commands.json; the bot runs it (core/live_status.py)
  POST /api/v2/config   saves one settings file after type and range checks, atomically (no .bak copy is kept)
  POST /api/v2/bot/start|stop|restart  bot control: one action at a time, at most one per 5 seconds per session
Every POST under /api/v2/ must send the X-Dusk-CSRF header with the token of the session (see _csrf_problem).
"""

import gzip
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import shutil
from datetime import datetime, timedelta
from pathlib import Path
import threading
import time
from urllib.parse import quote, urlsplit

from flask import Response, jsonify, make_response, redirect, render_template, request, session
from flask.sessions import SecureCookieSessionInterface

from utils import lan, log_paths
from . import bot_supervisor, data_v2, health_rules

GZIP_MIN_BYTES = 2048
MAX_CACHE = 300
# Five wrong passwords within LOGIN_WINDOW_S refuse logins from that address for LOGIN_LOCK_S (429). In memory only
LOGIN_WINDOW_S = 600
LOGIN_MAX_FAILS = 5
LOGIN_LOCK_S = 300
# One start, stop or restart per session every BOT_ACTION_GAP_S seconds (429 otherwise)
BOT_ACTION_GAP_S = 5.0
CSRF_HEADER = "X-Dusk-CSRF"
ACTIONS_LOG = Path("logs") / "dashboard_actions.log"
ACTIONS_LOG_ROTATE_BYTES = 5 * 1024 * 1024
KNOWN_STATES = {"hunting", "sleeping", "paused", "captcha", "stopped", "offline", "starting", "connecting", "reconnecting"}
KNOWN_HEALTH = {"good", "warn", "bad", "unknown"}
ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
# A Discord user ID used as a scope. Per-account settings were removed: such a scope answers 404 (_per_account_removed)
USER_SCOPE_RE = re.compile(r"^\d{4,25}$")
# The thresholds the Overview chips colour by (summary "thresholds"), read from config/dashboard_view.json
CHIP_THRESHOLDS = ("captchaWarnPerDay", "captchaBadPerDay", "errorsWarnPerHour", "errorsBadPerHour")
CHANNEL_ID_RE = re.compile(r"^\d{15,22}$")

# Commands the bot knows how to run (core/live_status.py). Anything else is refused here
CONTROL_ACTIONS = ("sleep", "wake", "restart_captcha", "switch_channel")
COMMAND_TTL_S = 60  # the bot ignores commands older than this
# Methods each /api/v2 path accepts. Any other path accepts GET and HEAD only
API_METHODS = {
    "/api/v2/control": ("POST",),
    "/api/v2/config": ("GET", "HEAD", "POST"),
    "/api/v2/bot/start": ("POST",),
    "/api/v2/bot/stop": ("POST",),
    "/api/v2/bot/restart": ("POST",),
}

# Allowed range of every dashboard threshold (config/dashboard_view.json)
DASHBOARD_BOUNDS = {
    "refreshSeconds": (2, 60),
    "lastHuntWarnSeconds": (30, 86400),
    "lastHuntBadSeconds": (60, 86400),
    "inboundWarnKbMin": (10, 10000),
    "inboundBadKbMin": (50, 20000),
    "captchaWarnPerDay": (0, 100),
    "captchaBadPerDay": (1, 100),
    "errorsWarnPerHour": (0, 1000),
    "errorsBadPerHour": (1, 1000),
    "huntsSinceStartWarn": (1, 100000),
    "huntsSinceStartWarnUntil": (1, 100000),
}
# (warn, bad) pairs: the warn mark can never be above the bad mark
DASHBOARD_PAIRS = (
    ("lastHuntWarnSeconds", "lastHuntBadSeconds"),
    ("inboundWarnKbMin", "inboundBadKbMin"),
    ("captchaWarnPerDay", "captchaBadPerDay"),
    ("errorsWarnPerHour", "errorsBadPerHour"),
    ("huntsSinceStartWarn", "huntsSinceStartWarnUntil"),
)

CSP ="default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'"
# The old page keeps its inline script and the CDN libraries it was written with
CSP_LEGACY = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline' cdn.jsdelivr.net cdnjs.cloudflare.com; connect-src 'self'"
)

store = None
_cfg = {"password": "", "version": "", "bind": "127.0.0.1", "port": None, "lan_urls": [], "password_banner": False}
_fails = {}  # client address -> monotonic times of its recent wrong passwords
_locked_until = {}  # client address -> monotonic time until which its logins are refused
_fails_lock = threading.Lock()
_bot_last = {}  # session key -> monotonic time of its last bot action
_bot_lock = threading.Lock()
_actions_lock = threading.Lock()
# One writer at a time for the command queue and for settings files (Flask runs requests in threads)
_write_lock = threading.Lock()
_configured = False


class _SessionInterface(SecureCookieSessionInterface):
    """The session cookie is Secure only when the request came over HTTPS: the LAN is plain HTTP"""

    def get_cookie_secure(self, app):
        return bool(request.is_secure)


def configure(password, version="", refresh_secs=3, port=None, host="127.0.0.1", app=None, root=".",
              lan_urls=None, password_banner=False):
    """Sets the password, the session key and starts the snapshot thread. Runs once per process. host: the address the
    server listens on. lan_urls: the URLs other devices can open. password_banner: True when the dashboard fell back to
    this machine because there was no password (utils/lan.py)"""
    global store, _configured
    _cfg["password"] = password or ""
    _cfg["version"] = version
    _cfg["host"] = host
    _cfg["bind"] = host
    _cfg["port"] = port
    _cfg["lan_urls"] = list(lan_urls or [])
    _cfg["password_banner"] = bool(password_banner)
    if _configured:
        return store
    _configured = True
    if app is not None:
        app.secret_key = secrets.token_hex(32)  # kept in memory only: a restart of this process signs everybody out
        app.session_interface = _SessionInterface()
        app.config.update(
            SESSION_COOKIE_NAME=f"owo_dash_{port or 0}",
            SESSION_COOKIE_HTTPONLY=True,
            SESSION_COOKIE_SAMESITE="Strict",
            PERMANENT_SESSION_LIFETIME=timedelta(hours=24),
        )
    store = data_v2.SnapshotStore(root=root, secrets=[password])
    store.fallback_refresh = max(2.0, float(refresh_secs or 3))
    store.refresh_s = store.fallback_refresh
    store.start()
    return store


# ---- auth ----------------------------------------------------------------
def _is_local():
    return request.remote_addr in ("127.0.0.1", "::1", "::ffff:127.0.0.1")


def _matches(supplied):
    expected = _cfg["password"]
    return bool(expected) and hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def authorized():
    if not _cfg["password"]:
        return _is_local()
    if session.get("ok") is True:
        return True
    header = request.headers.get("Authorization", "")
    if header[:7].lower() == "bearer " and _matches(header[7:].strip()):
        return True
    # The old page sends the password in this header
    legacy = request.headers.get("password")
    return legacy is not None and _matches(legacy)


def _safe_next(target):
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/"


def _locked(ip):
    """True while logins from this address are refused: LOGIN_LOCK_S after LOGIN_MAX_FAILS wrong passwords"""
    now = time.monotonic()
    with _fails_lock:
        until = _locked_until.get(ip)
        if until is None:
            return False
        if now < until:
            return True
        _locked_until.pop(ip, None)  # the lock is over: the count starts again
        _fails.pop(ip, None)
        return False


def _record_fail(ip):
    now = time.monotonic()
    with _fails_lock:
        recent = [t for t in _fails.get(ip, []) if now - t < LOGIN_WINDOW_S]
        recent.append(now)
        if len(recent) >= LOGIN_MAX_FAILS:
            _locked_until[ip] = now + LOGIN_LOCK_S
            recent = []
        _fails[ip] = recent


def _csrf_problem():
    """None when the request may change something: it carries the CSRF token of this session, and when the browser
    sends an Origin header, that Origin is this dashboard. Otherwise the reason, for the 403 answer"""
    expected = session.get("csrf")
    if not isinstance(expected, str) or not expected:
        return "CSRF token missing: reload the dashboard and try again"
    supplied = request.headers.get(CSRF_HEADER, "")
    if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        return "CSRF token missing or wrong"
    origin = request.headers.get("Origin")
    if origin is not None and urlsplit(origin).netloc != request.host:
        return "Origin does not match this dashboard"
    return None


def gate():
    path = request.path
    if path.startswith("/static/") or path in ("/login", "/logout"):
        return None

    ip = request.remote_addr or "?"

    auth_header = request.headers.get("Authorization", "")
    legacy_pw = request.headers.get("password")
    header_provided = (auth_header[:7].lower() == "bearer " and bool(auth_header[7:].strip())) or (legacy_pw is not None)

    if header_provided and _locked(ip):
        res = jsonify({"error": "Too many failed attempts. Wait 5 minutes.", "code": "locked", "retry_after": LOGIN_LOCK_S})
        res.status_code = 429
        res.headers["Retry-After"] = str(LOGIN_LOCK_S)
        return res

    if path.startswith("/api/v2/"):
        allowed = API_METHODS.get(path, ("GET", "HEAD"))
        if request.method not in allowed:
            res = jsonify({"error": "method not allowed", "allowed": list(allowed)})
            res.status_code = 405
            res.headers["Allow"] = ", ".join(allowed)
            return res

    if authorized():
        if path.startswith("/api/v2/") and request.method == "POST":
            # Every POST under /api/v2/ must carry the CSRF token of this session (see _csrf_problem)
            problem = _csrf_problem()
            if problem is not None:
                return _error(problem, 403)
        return None

    if header_provided:
        _record_fail(ip)

    if path.startswith("/api/"):
        return jsonify({"error": "unauthorized"}), 401

    return redirect("/login?next=" + quote(path))


def login():
    if not _cfg["password"]:
        return redirect("/")
    error = False
    next_url = _safe_next(request.values.get("next", "/"))
    if request.method == "POST":
        ip = request.remote_addr or "?"
        if _locked(ip):
            response = make_response(render_template("login.html", error="locked", next=next_url), 429)
            response.headers["Retry-After"] = str(LOGIN_LOCK_S)
            return response
        if _matches(request.form.get("password", "")):
            session.clear()
            session.permanent = True
            session["ok"] = True
            session["csrf"] = secrets.token_urlsafe(32)  # the token the page sends with every POST (X-Dusk-CSRF)
            return redirect(next_url)
        _record_fail(ip)
        time.sleep(0.3)
        error = "Wrong password."
        return render_template("login.html", error=error, next=next_url), 401
    return render_template("login.html", error=error, next=next_url)


def logout():
    # Drops every key of the session (the login flag included); the cookie is then empty
    session.clear()
    session.permanent = False
    session.modified = True
    return redirect("/login")


def _page(template):
    token = session.get("csrf")
    if not isinstance(token, str) or not token:
        # Served without a login (no password, this machine): the page still gets a token of its own
        session["csrf"] = secrets.token_urlsafe(32)
        session.permanent = True
    return render_template(template, version=_cfg["version"], csrf=session["csrf"])


def home():
    return _page("v2.html")


def mobile_home():
    """The phone dashboard (website/templates/m.html). The gate sends a request without a login to /login first"""
    return _page("m.html")


# ---- responses -----------------------------------------------------------
def _etag_matches(etag):
    header = request.headers.get("If-None-Match", "")
    if not header:
        return False
    wanted = f'"{etag}"'
    return any(part.strip().removeprefix("W/") == wanted for part in header.split(","))


def _respond(payload, etag, gz=None):
    headers = {"Cache-Control": "no-cache", "ETag": f'"{etag}"', "Vary": "Accept-Encoding"}
    if _etag_matches(etag):
        return Response(status=304, headers=headers)
    headers["Content-Type"] = "application/json"
    if len(payload) > GZIP_MIN_BYTES and "gzip" in request.headers.get("Accept-Encoding", ""):
        body = gz if gz is not None else gzip.compress(payload, compresslevel=5)
        headers["Content-Encoding"] = "gzip"
        return Response(body, status=200, headers=headers)
    return Response(payload, status=200, headers=headers)


def _encode(obj):
    return json.dumps(obj, separators=(",", ":"), default=str).encode("utf-8")


def _memo(key, build):
    """Encoded response of the current version, built once per version"""
    version = store.snapshot.version
    full = (key, version)
    hit = store.cache.get(full)
    if hit is None:
        payload = _encode(build())
        hit = (payload, gzip.compress(payload, compresslevel=5) if len(payload) > GZIP_MIN_BYTES else None)
        if len(store.cache) > MAX_CACHE:
            store.cache.clear()
        store.cache[full] = hit
    return _respond(hit[0], f"v{version}", hit[1])


def _error(message, status):
    response = jsonify({"error": message})
    response.status_code = status
    return response


def _bad(message):
    return _error(message, 400)


def _per_account_removed():
    """Per-account settings were removed: every account uses config/settings.json. Requests for them get 404"""
    return _error("per-account settings were removed: every account uses the default settings", 404)


def _int_arg(name, default, low, high):
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number")
    if value < low or value > high:
        raise ValueError(f"{name} must be from {low} to {high}")
    return value


def _stamp(obj):
    obj["server_time"] = round(time.time(), 2)
    return obj


# ---- API -----------------------------------------------------------------
def api_summary():
    # Built for each request (it is small): the age of a source that stopped working must grow
    snap = store.snapshot
    now = time.time()
    sources = store.sources_view(now)
    cfg = store.cfg if isinstance(getattr(store, "cfg", None), dict) else {}
    out = {"version": snap.version, "generated_at": snap.generated_at, "summary": snap.summary, "sources": sources,
           "no_password": not _cfg["password"], "bot_version": _cfg["version"],
           "thresholds": {key: cfg.get(key) for key in CHIP_THRESHOLDS},
           "lan": {"bind": _cfg["bind"], "port": _cfg["port"],
                   "local_url": f"http://127.0.0.1:{_cfg['port']}",
                   "lan_urls": _cfg["lan_urls"], "password_banner": _cfg["password_banner"]}}
    etag = f"v{snap.version}"
    if not all(s["ok"] or s.get("error") == "not created yet" for s in sources.values()):
        etag += f".{int(now // 2)}"  # ages change while a source is down
    return _respond(_encode(_stamp(out)), etag)


def api_accounts():
    try:
        page = _int_arg("page", 1, 1, 100000)
        size = _int_arg("size", 50, 1, 500)
    except ValueError as error:
        return _bad(str(error))
    sort = request.args.get("sort", "health")
    if sort not in data_v2.SORT_KEYS:
        return _bad("sort must be one of: " + ", ".join(data_v2.SORT_KEYS))
    order = request.args.get("order", "desc")
    if order not in ("asc", "desc"):
        return _bad("order must be asc or desc")
    q = request.args.get("q", "")[:64]
    state = request.args.get("state", "")
    health = request.args.get("health", "")
    if state and not set(state.split(",")) <= KNOWN_STATES:
        return _bad("unknown state")
    if health and not set(health.split(",")) <= KNOWN_HEALTH:
        return _bad("unknown health")
    show_removed = request.args.get("removed", "0") == "1"
    key = ("accounts", page, size, sort, order, q, state, health, show_removed)
    return _memo(key, lambda: _stamp(store.accounts_page(page, size, sort, order, q, state, health,
                                                         show_removed=show_removed)))


def api_account(account_id):
    if not ID_RE.match(account_id):
        return jsonify({"error": "not found"}), 404
    version = store.snapshot.version
    key = ("account", account_id)
    hit = store.cache.get((key, version))
    if hit is None:
        detail = store.account_detail(account_id)
        if detail is None:
            return jsonify({"error": "not found"}), 404
        payload = _encode(_stamp(detail))
        hit = (payload, gzip.compress(payload, compresslevel=5) if len(payload) > GZIP_MIN_BYTES else None)
        store.cache[(key, version)] = hit
    return _respond(hit[0], f"v{version}", hit[1])


def api_changes():
    try:
        since = _int_arg("since", 0, 0, 10 ** 12)
    except ValueError as error:
        return _bad(str(error))
    return _memo(("changes", since), lambda: _stamp(store.changes_since(since)))


def api_events():
    try:
        after = _int_arg("after", 0, 0, 10 ** 12)
        limit = _int_arg("limit", 100, 1, data_v2.EVENT_MAX_LIMIT)
    except ValueError as error:
        return _bad(str(error))
    kind = request.args.get("type", "")
    if kind and kind not in data_v2.EVENT_TYPES:
        return _bad("type must be one of: " + ", ".join(data_v2.EVENT_TYPES))
    acct = request.args.get("acct", "")
    if acct and not ID_RE.match(acct):
        return _bad("bad acct")
    return _memo(("events", after, limit, kind, acct), lambda: _stamp(store.events_after(after, limit, kind or None, acct or None)))


def api_gateway():
    return _memo("gateway", lambda: _stamp(store.gateway_view()))


# ---- phone dashboard API (website/templates/m.html) ----------------------------------------
# The home screen takes one answer with everything it shows. Its ETag is a digest of the body, without timestamps, so
# an unchanged answer is a 304. Account rows are arrays in the order of M_ROW_FIELDS, to keep each row small
M_SUMMARY_KEYS = (
    "bot_online", "accounts_total", "accounts_online", "hunts_per_min_total", "cowoncy_today_total", "captcha_today",
    "errors_1h", "inbound_avg_kb_min", "inbound_over_warn", "inbound_over_bad", "health", "autostop",
)
M_ROW_FIELDS = ["id", "name", "state", "health", "hunts_per_min", "last_hunt_ago_s", "cowoncy", "captcha_today"]


def _rounded(value, digits):
    return round(value, digits) if isinstance(value, float) else value


def _m_bot():
    status = _supervisor().status()
    uptime = status.get("uptime_s")
    last_exit = status.get("last_exit")
    return {
        "state": status.get("state"),
        "supervised": bool(status.get("supervised")),
        "uptime_min": int(uptime // 60) if isinstance(uptime, (int, float)) else None,
        "exit_code": last_exit.get("code") if isinstance(last_exit, dict) else None,
    }


def _m_summary_value(key, value):
    """The rates that move every refresh are rounded: a steady fleet then gives the same answer, and a 304"""
    if key in ("hunts_per_min_total", "inbound_avg_kb_min") and isinstance(value, float):
        return round(value) if abs(value) >= 10 else round(value, 1)
    return value


def _m_home_payload():
    snap = store.snapshot
    summary = snap.summary or {}
    picked = {key: _m_summary_value(key, summary.get(key)) for key in M_SUMMARY_KEYS}
    attention = [
        {"id": item.get("id"), "name": item.get("name"), "health": item.get("health"), "state": item.get("state"),
         "reasons": list(item.get("reasons") or [])[:2]}
        for item in (summary.get("attention") or [])[:10]
    ]
    sources = snap.sources or {}
    return {
        "bot": _m_bot(),
        "summary": picked,
        "attention": attention,
        "sources_ok": bool(sources) and all(s.get("ok") or s.get("error") == "not created yet" for s in sources.values()),
        "no_password": not _cfg["password"],
        "password_banner": bool(_cfg["password_banner"]),
        "bot_version": _cfg["version"],
        "lan": {"local_url": f"http://127.0.0.1:{_cfg['port']}", "lan_urls": _cfg["lan_urls"]},
    }


def api_m_home():
    payload = _encode(_m_home_payload())
    etag = hashlib.sha1(payload).hexdigest()[:16]
    gz = gzip.compress(payload, compresslevel=5) if len(payload) > GZIP_MIN_BYTES else None
    return _respond(payload, etag, gz)


def _m_accounts_page(offset, limit, q, state):
    items = [e for e in store.snapshot.entries.values() if not e.get("removed")]
    if q:
        items = [e for e in items if q in (e.get("name") or "").lower() or q in e["id"].lower()]
    if state:
        wanted = set(state.split(","))
        items = [e for e in items if e.get("state") in wanted]
    items.sort(key=lambda e: (-data_v2.HEALTH_RANK.get(e.get("health"), 0), (e.get("name") or "").lower(), e["id"]))
    rows = [
        [e["id"], e.get("name"), e.get("state"), e.get("health"), _rounded(e.get("hunts_per_min_15m"), 1),
         _rounded(e.get("last_hunt_ago_s"), 0), e.get("cowoncy"), e.get("captcha_today")]
        for e in items[offset: offset + limit]
    ]
    return {"fields": M_ROW_FIELDS, "total": len(items), "offset": offset, "limit": limit, "rows": rows}


def api_m_accounts():
    try:
        offset = _int_arg("offset", 0, 0, 100000)
        limit = _int_arg("limit", 25, 1, 100)
    except ValueError as error:
        return _bad(str(error))
    q = request.args.get("q", "")[:64].strip().lower()
    state = request.args.get("state", "")
    if state and not set(state.split(",")) <= KNOWN_STATES:
        return _bad("unknown state")
    return _memo(("m_accounts", offset, limit, q, state), lambda: _m_accounts_page(offset, limit, q, state))


def api_runtime():
    return _memo("runtime", lambda: _stamp(store.runtime_view()))


def api_console():
    try:
        after = _int_arg("after", 0, 0, 10 ** 12)
        limit = _int_arg("limit", 200, 1, data_v2.CONSOLE_MAX_LIMIT)
    except ValueError as error:
        return _bad(str(error))
    last = store.console_last_id()
    payload = _encode(_stamp(store.console_after(after, limit)))
    return _respond(payload, f"c{last}-{after}-{limit}")


# ---- settings write API ----------------------------------------------------------
# ---- bot control (website/bot_supervisor.py) -----------------------------------------
def _supervisor():
    return bot_supervisor.get_supervisor(store.root)


def _log_action(name, result):
    """One line per bot action in logs/dashboard_actions.log: time, client address, action, result. Never a secret"""
    try:
        path = log_paths.log_file(ACTIONS_LOG.name, root=store.root)
        with _actions_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > ACTIONS_LOG_ROTATE_BYTES:
                log_paths.rotate_to_part(path, path.parent)  # one old part is kept; only <name>.1 is ever replaced
            stamp = datetime.now().isoformat(timespec="seconds")
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(f"{stamp} | {request.remote_addr or '?'} | bot {name} | {result}\n")
    except OSError:
        pass  # a log that cannot be written must not stop the bot action


def _bot_wait(key):
    """Seconds this session must still wait before its next bot action, or 0 (the attempt is then recorded)"""
    now = time.monotonic()
    with _bot_lock:
        last = _bot_last.get(key)
        if last is not None and now - last < BOT_ACTION_GAP_S:
            return int(BOT_ACTION_GAP_S - (now - last)) + 1
        _bot_last[key] = now
        if len(_bot_last) > 200:
            for old_key in [k for k, t in _bot_last.items() if now - t > 3600]:
                _bot_last.pop(old_key, None)
        return 0


def _bot_action(name, run):
    key = session.get("csrf") or request.remote_addr or "?"
    wait = _bot_wait(key)
    if wait:
        _log_action(name, "rate limited")
        response = jsonify({"error": "rate_limited", "message": f"Wait {wait} seconds between bot actions.",
                            "retry_after": wait})
        response.status_code = 429
        response.headers["Retry-After"] = str(wait)
        return response
    try:
        status = run(_supervisor())
    except bot_supervisor.BotBusy:
        result, code = "busy", 409
        body = {"error": "busy", "message": "Another bot action is running. Wait for it to finish."}
    except bot_supervisor.BotRefused as error:
        result, code = error.code, 409
        body = {"error": error.code, "message": str(error)}
    except ValueError as error:
        result, code = "bad request", 400
        body = {"error": "bad_request", "message": str(error)}
    except Exception as error:  # an unexpected message may hold paths: only the error type is logged and returned
        result, code = f"error ({type(error).__name__})", 500
        body = {"error": "internal", "message": "The bot action failed. See logs/bot_supervised.log."}
    else:
        result, code = "ok", 200
        body = {"ok": True, "action": name, "status": status}
    _log_action(name, result)
    return jsonify(body), code


def _optional_json_body():
    """The JSON object sent with a POST whose body may be empty: {} when there is none"""
    if not request.data:
        return {}, None
    return _json_body()


def api_bot_status():
    response = jsonify(_supervisor().status())
    response.headers["Cache-Control"] = "no-store"
    return response


def api_bot_start():
    body, problem = _optional_json_body()
    if problem is not None:
        return problem
    minutes = body.get("maxRuntimeMinutes")
    return _bot_action("start", lambda sup: sup.start(minutes))


def api_bot_stop():
    return _bot_action("stop", lambda sup: sup.stop())


def api_bot_restart():
    return _bot_action("restart", lambda sup: sup.restart())


REDACTED_STR = "***REDACTED***"


# Keys that can run programs on this PC. They can only be changed by editing
# config/global_settings.json by hand, never through the dashboard.
LOCKED_GLOBAL_KEYS = (
    ("console", "commandToRunOnCaptcha"),
    ("console", "commandToRunOnBan"),
    ("console", "runConsoleCommandOnCaptcha"),
    ("console", "runConsoleCommandOnBan"),
    # The program that the level check starts (tesseract): same risk, so it is locked too
    ("levelCheck", "tesseractPath"),
)


def _redact_global_config(cfg):
    """Deep-copies and redacts secret keys from global_settings.json"""
    c = json.loads(json.dumps(cfg))
    if "website" in c and isinstance(c["website"], dict):
        if "password" in c["website"]:
            c["website"]["password"] = REDACTED_STR
    if "ocrApi" in c:
        c["ocrApi"] = REDACTED_STR
    if "webhook" in c and isinstance(c["webhook"], dict):
        for sec in ("webhookUrl", "webhookCaptchaUrl", "webhookUserIdToPingOnCaptcha"):
            if sec in c["webhook"]:
                c["webhook"][sec] = REDACTED_STR
    return c


def _restore_global_secrets(new_cfg, original_cfg):
    """Restores secret keys if new_cfg has redacted, empty or omitted them"""
    merged = dict(new_cfg)
    if "website" in original_cfg and isinstance(original_cfg["website"], dict):
        orig_pw = original_cfg["website"].get("password")
        if "website" in merged and isinstance(merged["website"], dict):
            if merged["website"].get("password") in (REDACTED_STR, None, ""):
                merged["website"]["password"] = orig_pw
        else:
            merged["website"] = {"password": orig_pw}

    if "ocrApi" in original_cfg:
        if merged.get("ocrApi") in (REDACTED_STR, None, ""):
            merged["ocrApi"] = original_cfg["ocrApi"]

    if "webhook" in original_cfg and isinstance(original_cfg["webhook"], dict):
        orig_wh = original_cfg["webhook"]
        if "webhook" not in merged or not isinstance(merged["webhook"], dict):
            merged["webhook"] = dict(orig_wh)
        else:
            for sec_key in ("webhookUrl", "webhookCaptchaUrl", "webhookUserIdToPingOnCaptcha"):
                if sec_key in orig_wh:
                    if merged["webhook"].get(sec_key) in (REDACTED_STR, None, ""):
                        merged["webhook"][sec_key] = orig_wh[sec_key]

    return merged


def _read_json_dict(path):
    """The object stored in a JSON file, or None when the file is missing, unreadable or not an object"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _strip_console_marker(incoming):
    """The GET response marks the console block with "locked": true. That is a marker, not a setting, so it is dropped"""
    console = incoming.get("console")
    if isinstance(console, dict) and "locked" in console:
        incoming = dict(incoming)
        console = dict(console)
        console.pop("locked", None)
        incoming["console"] = console
    return incoming


def _locked_key_changes(incoming, original):
    """Dotted names of the locked keys this request really changes.
    Absent, blank, the redaction marker and the value already saved are not changes: the dashboard sends back what it was shown"""
    changed = []
    for section, key in LOCKED_GLOBAL_KEYS:
        part = incoming.get(section)
        if not isinstance(part, dict) or key not in part:
            continue
        value = part[key]
        if value is None or value == "" or value == REDACTED_STR:
            continue
        saved_part = original.get(section)
        saved = saved_part.get(key) if isinstance(saved_part, dict) else None
        if value == saved:
            continue
        changed.append(f"{section}.{key}")
    return changed


def _keep_locked_keys(incoming, original):
    """Locked keys always keep the value that is saved, whatever the request sent"""
    for section, key in LOCKED_GLOBAL_KEYS:
        saved_part = original.get(section)
        if isinstance(saved_part, dict) and key in saved_part:
            part = incoming.get(section)
            if isinstance(part, dict):
                part[key] = saved_part[key]
    return incoming


def _mask_locked_console(cfg):
    """The command text never leaves the PC: the dashboard sees "" and a locked flag"""
    console = cfg.get("console")
    if isinstance(console, dict):
        for key in ("commandToRunOnCaptcha", "commandToRunOnBan"):
            if key in console:
                console[key] = ""
        console["locked"] = True
    return cfg


def _deep_merge(base, extra):
    """New dict: base with extra laid over it. Dicts merge key by key, every other value replaces the old one"""
    out = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _atomic_write_json(target_path, data):
    """Writes a JSON file through a temporary file and os.replace. no .bak copy is kept"""
    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{secrets.token_hex(4)}.tmp")
    text = json.dumps(data, indent=2, default=str)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    for attempt in range(4):
        try:
            os.replace(tmp, target)
            return True
        except PermissionError:
            time.sleep(0.03)
    try:
        tmp.unlink(missing_ok=True)
    except Exception:
        pass
    return False


def _json_body():
    """The JSON object sent with a POST, or the error response to send back.
    Only application/json is accepted: a browser cannot send that type across sites without a preflight."""
    if not request.is_json:
        return None, _error("send the body as JSON (Content-Type: application/json)", 415)
    try:
        body = request.get_json(silent=False)
    except Exception:
        return None, _bad("invalid JSON payload")
    if not isinstance(body, dict):
        return None, _bad("the JSON body must be an object")
    return body, None


def _checked_number(key, raw, low, high):
    """(value, None) for a JSON number inside the range, (None, message) otherwise. Booleans and strings are refused"""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
        return None, f"'{key}' must be a number"
    if raw < low or raw > high:
        return None, f"'{key}' must be from {low} to {high}"
    return (int(raw) if float(raw).is_integer() else float(raw)), None


def _shape_problems(reference, value, path, out):
    """Collects every place where value does not have the type that reference has (keys missing from value included).
    Keys that reference does not have are not checked, so a new key can be added."""
    if reference is None:
        return
    if isinstance(reference, bool):
        if not isinstance(value, bool):
            out.append(f"{path} must be true or false")
    elif isinstance(reference, (int, float)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            out.append(f"{path} must be a number")
        elif isinstance(reference, int) and not isinstance(value, bool) and not float(value).is_integer():
            out.append(f"{path} must be a whole number")
    elif isinstance(reference, str):
        if not isinstance(value, str):
            out.append(f"{path} must be text")
    elif isinstance(reference, list):
        if not isinstance(value, list):
            out.append(f"{path} must be a list")
        elif reference and value:
            for index, item in enumerate(value):
                _shape_problems(reference[0], item, f"{path}[{index}]", out)
    elif isinstance(reference, dict):
        if not isinstance(value, dict):
            out.append(f"{path} must be an object")
            return
        for key, ref_item in reference.items():
            if key not in value:
                out.append(f"{path}.{key} is missing")
            else:
                _shape_problems(ref_item, value[key], f"{path}.{key}", out)


def _type_problem(reference, value):
    out = []
    _shape_problems(reference, value, "settings", out)
    return "; ".join(out[:3]) if out else None


def _sleep_problem(cfg):
    """Ranges the bot relies on for the sleep block (utils/config_models/settings.py, Sleep)"""
    sleep = cfg.get("sleep")
    if not isinstance(sleep, dict):
        return None
    freq = sleep.get("frequencyPercentage")
    if isinstance(freq, (int, float)) and not isinstance(freq, bool) and not 0 <= freq <= 100:
        return "sleep.frequencyPercentage must be from 0 to 100"
    for key, strict in (("checkTime", False), ("sleepTime", True)):
        pair = sleep.get(key)
        if pair is None:
            continue
        if not isinstance(pair, list) or len(pair) != 2 or not all(
            isinstance(x, (int, float)) and not isinstance(x, bool) and x >= 0 for x in pair
        ):
            return f"sleep.{key} must be two seconds values: min, max"
        if pair[0] > pair[1] or (strict and pair[0] == pair[1]):
            return f"sleep.{key}: min must be below max"
    return None


def _dashboard_problem(current, updates):
    """Pairs of thresholds where the warn mark is above the bad mark"""
    merged = dict(health_rules.DEFAULTS)
    merged.update({
        k: v for k, v in current.items()
        if k in DASHBOARD_BOUNDS and isinstance(v, (int, float)) and not isinstance(v, bool)
    })
    merged.update(updates)
    for warn_key, bad_key in DASHBOARD_PAIRS:
        if merged[warn_key] > merged[bad_key]:
            return f"'{warn_key}' must not be above '{bad_key}'"
    return None


def _save_dashboard(root, data):
    target = root / "config" / "dashboard_view.json"
    current = _read_json_dict(target)
    if current is None:
        current = {}
    updates = {}
    for key, raw in data.items():
        if key not in DASHBOARD_BOUNDS:
            return _bad(f"unknown dashboard setting '{key}'")
        low, high = DASHBOARD_BOUNDS[key]
        value, problem = _checked_number(key, raw, low, high)
        if problem:
            return _bad(problem)
        updates[key] = value
    problem = _dashboard_problem(current, updates)
    if problem:
        return _bad(problem)
    # Keys this form does not show (huntsSinceStart*) keep their saved values
    merged = dict(current)
    merged.update(updates)
    if not _atomic_write_json(target, merged):
        return _error("failed to write config file", 500)
    try:
        store.refresh()
    except Exception:
        pass
    return jsonify({
        "ok": True,
        "scope": "dashboard",
        "hot_reloaded": True,
        "applies": "immediate",
        "message": "Dashboard thresholds saved. The live views use them on the next refresh.",
    })


def _website_problem(cfg):
    """Checks the website options the dashboard writes: the host (IPv4) and the three bot options (true or false)"""
    site = cfg.get("website")
    if not isinstance(site, dict):
        return None
    if "host" in site and not lan.is_ipv4(site["host"]):
        return "website.host must be an IPv4 address: 0.0.0.0 for every device on the network, 127.0.0.1 for this machine"
    for key in ("restartOnCrash", "startBotOnLaunch", "stopBotWithDashboard"):
        if key in site and not isinstance(site[key], bool):
            return f"website.{key} must be true or false"
    return None


def _save_global(root, data):
    target = root / "config" / "global_settings.json"
    if not target.exists():
        return _error("global_settings.json not found", 404)
    original = _read_json_dict(target)
    if original is None:
        # Refuse to write: without the saved file the secrets could not be put back
        return _error("global_settings.json could not be read, nothing was saved", 500)
    data = _strip_console_marker(data)
    locked = _locked_key_changes(data, original)
    if locked:
        # The dashboard cannot change a key that runs programs on this PC (LOCKED_GLOBAL_KEYS)
        refused = jsonify({"ok": False, "error": "locked_key", "keys": locked})
        refused.status_code = 400
        return refused
    data = _keep_locked_keys(data, original)
    merged = _deep_merge(original, _restore_global_secrets(data, original))
    problem = _type_problem(original, merged) or _website_problem(merged)
    if problem:
        return _bad(problem)
    if not _atomic_write_json(target, merged):
        return _error("failed to write config file", 500)
    return jsonify({
        "ok": True,
        "scope": "global",
        "hot_reloaded": False,
        "applies": "restart",
        "message": "Global settings saved. Bot options apply on the next bot start; host and port apply when the dashboard restarts.",
    })


def _save_bot_settings(root, data):
    """Writes config/settings.json, the one profile every account uses. The posted keys are laid over the saved file"""
    default_path = root / "config" / "settings.json"
    default_cfg = _read_json_dict(default_path)
    if default_cfg is None:
        return _error("settings.json not found or unreadable, nothing was saved", 500)
    merged = _deep_merge(default_cfg, data)
    problem = _type_problem(default_cfg, merged) or _sleep_problem(merged)
    if problem:
        return _bad(problem)
    if not _atomic_write_json(default_path, merged):
        return _error("failed to write config file", 500)
    return jsonify({
        "ok": True,
        "scope": "default",
        "hot_reloaded": False,
        "applies": "next_login",
        "message": "Default bot settings saved. They apply when an account next logs in, or after a bot restart.",
    })


def api_control():
    body, problem = _json_body()
    if problem is not None:
        return problem

    action = body.get("action")
    target = body.get("target")
    value = body.get("value")

    if action not in CONTROL_ACTIONS:
        return _bad("action must be one of: " + ", ".join(CONTROL_ACTIONS))

    if not isinstance(target, str) or not target.strip():
        return _bad("target must be 'all' or an account ID string")
    target = target.strip()
    if target != "all" and not ID_RE.match(target):
        return _bad("invalid target account ID")

    if action == "switch_channel":
        channel = "" if value is None else str(value).strip()
        if not CHANNEL_ID_RE.match(channel):
            return _bad("value must be a Discord channel snowflake (15-22 digits)")
        value = channel
    else:
        value = None

    now = time.time()
    cmd_item = {
        "cmd_id": secrets.token_hex(8),
        "action": action,
        "target": target,
        "value": value,
        "created_at": round(now, 3),
    }

    cmd_file = store.root / "utils" / "data" / "web_commands.json"
    with _write_lock:
        commands = []
        existing = None
        if cmd_file.exists():
            try:
                with open(cmd_file, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except (OSError, ValueError):
                existing = None
        if isinstance(existing, list):
            commands = [
                c for c in existing
                if isinstance(c, dict) and isinstance(c.get("created_at"), (int, float))
                and now - c["created_at"] <= COMMAND_TTL_S
            ]
        commands.append(cmd_item)
        if not _atomic_write_json(cmd_file, commands):
            return _error("could not write command queue (locked)", 500)

    return jsonify({
        "ok": True,
        "cmd_id": cmd_item["cmd_id"],
        "action": action,
        "target": target,
        "message": f"Command {action} queued for {target}",
    })


def api_config():
    if request.method == "POST":
        return api_post_config()
    return api_get_config()


def api_get_config():
    scope = request.args.get("scope", "dashboard").strip()
    root = store.root
    if scope == "dashboard":
        cfg_file = root / "config" / "dashboard_view.json"
        data = _read_json_dict(cfg_file)
        if data is None:
            data = dict(health_rules.DEFAULTS)
        return jsonify({"scope": "dashboard", "hot_reloaded": True, "config": data})
    elif scope == "global":
        cfg_file = root / "config" / "global_settings.json"
        if not cfg_file.exists():
            return jsonify({"error": "global_settings.json not found"}), 404
        raw = _read_json_dict(cfg_file)
        if raw is None:
            return _error("global_settings.json could not be read", 500)
        safe = _mask_locked_console(_redact_global_config(raw))
        return jsonify({"scope": "global", "hot_reloaded": False, "config": safe})
    elif scope == "default":
        data = _read_json_dict(root / "config" / "settings.json")
        if data is None:
            return jsonify({"error": "settings.json not found or unreadable"}), 404
        return jsonify({"scope": "default", "hot_reloaded": False, "config": data})
    elif USER_SCOPE_RE.match(scope):
        return _per_account_removed()
    else:
        return _bad("scope must be dashboard, global or default")


def api_post_config():
    body, problem = _json_body()
    if problem is not None:
        return problem

    scope = str(body.get("scope", "dashboard")).strip()
    data = body.get("config")
    if data is None:
        data = body.get("data")
    if not isinstance(data, dict):
        return _bad("config must be a JSON object")

    root = store.root
    with _write_lock:
        if scope == "dashboard":
            return _save_dashboard(root, data)
        if scope == "global":
            return _save_global(root, data)
        if scope == "default":
            return _save_bot_settings(root, data)
        if USER_SCOPE_RE.match(scope):
            return _per_account_removed()
        return _bad("scope must be dashboard, global or default")


def api_not_found(_error=None):
    return jsonify({"error": "not found"}), 404


# ---- headers -------------------------------------------------------------
def add_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = CSP_LEGACY if request.path == "/legacy" else CSP
    if request.path.startswith("/api/") or request.path in ("/", "/login", "/legacy", "/m"):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


def init_app(app):
    app.before_request(gate)
    app.after_request(add_headers)
    app.add_url_rule("/", "v2_home", home, methods=["GET"])
    app.add_url_rule("/login", "v2_login", login, methods=["GET", "POST"])
    app.add_url_rule("/logout", "v2_logout", logout, methods=["POST"])
    app.add_url_rule("/m", "v2_mobile", mobile_home, methods=["GET"])
    routes = (
        ("/api/v2/summary", api_summary),
        ("/api/v2/accounts", api_accounts),
        ("/api/v2/accounts/<account_id>", api_account),
        ("/api/v2/changes", api_changes),
        ("/api/v2/events", api_events),
        ("/api/v2/gateway", api_gateway),
        ("/api/v2/runtime", api_runtime),
        ("/api/v2/console", api_console),
        ("/api/v2/m/home", api_m_home),
        ("/api/v2/m/accounts", api_m_accounts),
    )
    for rule, view in routes:
        app.add_url_rule(rule, "v2_" + view.__name__, view, methods=["GET"])
    app.add_url_rule("/api/v2/control", "v2_control", api_control, methods=["POST"])
    app.add_url_rule("/api/v2/config", "v2_config", api_config, methods=["GET", "POST"])
    app.add_url_rule("/api/v2/bot", "v2_bot", api_bot_status, methods=["GET"])
    app.add_url_rule("/api/v2/bot/start", "v2_bot_start", api_bot_start, methods=["POST"])
    app.add_url_rule("/api/v2/bot/stop", "v2_bot_stop", api_bot_stop, methods=["POST"])
    app.add_url_rule("/api/v2/bot/restart", "v2_bot_restart", api_bot_restart, methods=["POST"])

    @app.errorhandler(404)
    def _not_found(error):
        if request.path.startswith("/api/v2/"):
            return api_not_found()
        return error

    @app.errorhandler(405)
    def _not_allowed(error):
        if request.path.startswith("/api/v2/"):
            response = jsonify({"error": "method not allowed", "allowed": ["GET", "HEAD"]})
            response.status_code = 405
            return response
        return error

    @app.errorhandler(500)
    def _server_error(error):
        if request.path.startswith("/api/v2/"):
            return jsonify({"error": "internal error"}), 500
        return error
