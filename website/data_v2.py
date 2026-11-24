"""Read-only data layer of the v2 dashboard.

One background thread (SnapshotStore.start) rebuilds a snapshot every refresh interval. HTTP requests only read
the cached snapshot, they never open a file or a database. Nothing here writes anything: SQLite files are opened
with mode=ro and closed after each refresh, JSON and log files are only read.

Sources
  live      utils/data/live_status.json         written by core/live_status.py (bot process), every 5 s
  db        utils/data/db.sqlite                user_stats, cowoncy_earnings, commands, meta_data
  gateway   logs/gateway_stats_<stamp>.txt      STATS line per account per minute (inbound bytes)
  runlog    logs/run_<stamp>[_partN].txt        read incrementally: captcha, error, gem lines
  console   utils/data/web_console.sqlite       mirrored into memory for /api/v2/console
  runtime   utils/data/weekly_runtime.json
  levels    utils/data/levels.sqlite            written by core/level_store.py (bot process), read here with mode=ro
When a source cannot be read the last good data stays and the source is reported ok=false with a growing age.
"""

import glob
import html as htmllib
import json
import os
import re
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

from . import health_rules

HIST_MINUTES = 70  # minute buckets kept per account (15 min rate, 60 min sparkline)
HIST_HOURS = 26  # hour buckets kept per account
LEVEL_HISTORY_DAYS = 40  # level_history rows read from levels.sqlite
LEVEL_DETAIL_POINTS = 30  # points of level_history in the account detail
LEVEL_GAIN_DAYS = 7
VERSION_LOG = 60  # versions /changes can answer
EVENT_RING = 3000
CONSOLE_RING = 1500
FIRST_READ_BYTES = 8 * 1024 * 1024  # a tail pass reads at most this much of a log file
LIVE_STALE_SECONDS = 20.0
GATEWAY_STALE_SECONDS = 200.0
CONSOLE_MAX_LIMIT = 500
EVENT_MAX_LIMIT = 500

SORT_KEYS = (
    "name", "state", "health", "id", "hunts_total", "hunts_per_min_15m", "battles_total", "last_hunt_ago_s",
    "cowoncy", "cowoncy_delta_today", "captcha_today", "inbound_kb_min", "errors_1h", "reconnects_1h", "level",
)
HEALTH_RANK = {"bad": 3, "warn": 2, "unknown": 1, "good": 0}
EVENT_TYPES = ("captcha", "daily", "error", "reconnect")

RUN_FILE = re.compile(r"^run_(\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d)(?:_part(\d+))?\.txt$")
STATS_LINE = re.compile(r"^STATS time=(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d) account=(\S+) msgs=(\d+) bytes=(\d+)")
CONSOLE_ROW = re.compile(r"<span class='timestamp'>\[(.*?)\]</span><span class='text'>(.*)</span></div>", re.S)
ERROR_RE = re.compile(r"(?i)traceback|exception|\berror\b(?!\s*=\s*None)|\bfailed\b")
IGNORE_RE = re.compile(r"Level quote fetch failed|error=None|status=ready|FAILED to solve|\[levelCheck\]")
# Defence in depth: the run log is already redacted, but nothing that looks like a secret may leave the process
TOKEN_RE = re.compile(r"[\w-]{24,}\.[\w-]{6}\.[\w-]{27,}")
WEBHOOK_RE = re.compile(r"(?:https?://)?(?:[\w-]+\.)*discord(?:app)?\.com/api/(?:v\d+/)?webhooks/[^\s\"'<>)\]]+", re.I)
CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _now():
    return time.time()


def _day_start(now):
    return datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _pst_day_start(now):
    """Start of the current day in America/Los_Angeles, the day the bot's daily and boss timers use"""
    try:
        import pytz

        zone = pytz.timezone("America/Los_Angeles")
        local = datetime.fromtimestamp(now, pytz.utc).astimezone(zone)
        midnight = zone.localize(datetime(local.year, local.month, local.day))
        return midnight.timestamp()
    except Exception:
        shifted = now - 8 * 3600
        return shifted - (shifted % 86400) + 8 * 3600


def _stamp_ts(raw):
    """Time of a run log line (the first 19 bytes), or None"""
    try:
        s = raw[:19].decode("ascii")
        return datetime(int(s[0:4]), int(s[5:7]), int(s[8:10]), int(s[11:13]), int(s[14:16]), int(s[17:19])).timestamp()
    except (ValueError, UnicodeDecodeError):
        return None


def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pct(cur, need):
    """XP progress in percent with one decimal, None when the numbers are not there"""
    if isinstance(cur, (int, float)) and isinstance(need, (int, float)) and need > 0:
        return round(min(100.0, max(0.0, cur / need * 100.0)), 1)
    return None


def _empty_levels():
    return {"rows": {}, "hist": {}, "active": None, "active_at": None}


class Source:
    """Health of one data source: ok, when it last worked and (for live data) the time of the data itself"""

    def __init__(self, name):
        self.name = name
        self.ok = False
        self.last_ok = None
        self.data_ts = None
        self.error = None

    def good(self, data_ts=None):
        self.ok = True
        self.last_ok = _now()
        self.data_ts = data_ts if data_ts is not None else self.last_ok
        self.error = None

    def bad(self, error):
        self.ok = False
        self.error = type(error).__name__ if isinstance(error, BaseException) else str(error)

    def view(self, now):
        stamp = self.data_ts
        return {
            "ok": bool(self.ok),
            "age_s": round(max(0.0, now - stamp), 1) if stamp else None,
            "last_ok_at": round(stamp, 1) if stamp else None,
            "error": self.error,
        }


class AccountHistory:
    """Everything the dashboard remembers about one account between two refreshes"""

    def __init__(self, key):
        self.key = key
        self.name = None
        self.first_seen = _now()
        self.hunt_prev = None
        self.mins = {}  # minute index -> hunts
        self.hours = {}  # hour index -> hunts
        self.rc_prev = None
        self.reconnect_ts = deque(maxlen=200)
        self.error_ts = deque(maxlen=500)
        self.captcha_ts = deque(maxlen=200)
        self.gem_used = 0
        self.gem_last = None
        self.gem_none = None  # reason of the last "no usable gems" line
        self.daily_prev = None
        self.live = None  # last live record of this account


class Snapshot:
    def __init__(self):
        self.version = 0
        self.generated_at = 0.0
        self.entries = {}
        self.order = []
        self.summary = {}
        self.sources = {}
        self.events_last_id = 0
        self.gateway = {}


def _clean(text, limit=200, extra_secrets=()):
    text = CTRL_RE.sub("", str(text))
    text = WEBHOOK_RE.sub("***", TOKEN_RE.sub("***", text))
    for word in extra_secrets:
        if word and len(word) >= 4:
            text = text.replace(word, "***")
    return text[:limit]


class SnapshotStore:
    def __init__(self, root=".", secrets=()):
        self.root = Path(root)
        self.secrets = [s for s in secrets if s]
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.sources = {name: Source(name) for name in ("live", "db", "gateway", "runlog", "console", "runtime", "levels", "quests")}
        self.cfg = dict(health_rules.DEFAULTS)
        self._cfg_mtime = None
        self.cfg_path = self.root / "config" / "dashboard_view.json"
        self.snapshot = Snapshot()
        self.acct = {}  # key -> AccountHistory
        self.names = {}  # key -> last known name
        self.name_to_key = {}
        # caches of the last good data of each source
        self._live = None
        # From the live status file: the number of accounts loaded from tokens.txt, and the id of the run that wrote it.
        # Kept when the file goes stale or the bot stops, so the total does not change while the bot is down
        self.configured_total = None
        self.live_run_id = None
        self._db = {"stats": {}, "cow": {}, "cow_date": None, "commands": {}}
        self._runtime = {"days": []}
        self._levels = _empty_levels()
        self._levels_sig = None
        self._quests = {}  # D7: key -> the saved quest state of an account (quests.sqlite), read-only
        self._quests_sig = None
        self.autostop = {"enabled": None}
        # incremental readers
        self._rl = {"stamp": None, "off": {}}
        self._gw = {"path": None, "off": 0}
        self.gw_names = {}  # account name -> {"ts", "kb", "hours": {hour: [sum, n]}}
        self.gw_last_line_ts = None
        self.events = deque(maxlen=EVENT_RING)
        self._event_id = 0
        self.console = deque(maxlen=CONSOLE_RING)
        self._console_last = 0
        # versions
        self._version = 0
        self._sigs = {}
        self._summary_sig = None
        self._src_sig = None
        self.ver_log = deque(maxlen=VERSION_LOG)
        self.run_started_at = None
        self.refresh_s = 3.0
        self.fallback_refresh = 3.0  # website.refreshInterval, used when dashboard_view.json has no refreshSeconds
        self.cache = {}  # (key, version) -> bytes, filled by the API layer
        self.last_refresh_ms = None

    # ---- lifecycle -------------------------------------------------------
    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="dashboard-snapshot", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.refresh()
            except Exception:  # the thread must live on, the next round tries again
                pass
            self.last_refresh_ms = round((time.monotonic() - started) * 1000, 1)
            self._stop.wait(max(0.2, self.refresh_s - (time.monotonic() - started)))

    # ---- one refresh -----------------------------------------------------
    def refresh(self, now=None):
        now = now or _now()
        self._load_cfg()
        live = self._read_live(now)
        db = self._read_db(now)
        self._tail_gateway(now)
        new_events = self._tail_runlog(now)
        self._mirror_console()
        self._read_runtime(now)
        self._read_levels(now)
        self._read_quests(now)
        with self._lock:
            self._apply(now, live, db, new_events)

    def _load_cfg(self):
        try:
            mtime = os.stat(self.cfg_path).st_mtime
        except OSError:
            mtime = None
        if mtime != self._cfg_mtime:
            self._cfg_mtime = mtime
            self.cfg = health_rules.load_thresholds(self.cfg_path)
        self.refresh_s = max(2.0, float(self.cfg.get("refreshSeconds") or self.fallback_refresh))

    # ---- sources ---------------------------------------------------------
    def _read_live(self, now):
        src = self.sources["live"]
        path = self.root / "utils" / "data" / "live_status.json"
        try:
            data = None
            for attempt in range(3):
                try:
                    with open(path, "rb") as handle:
                        data = json.loads(handle.read().decode("utf-8"))
                    break
                except (PermissionError, ValueError):
                    # The bot is replacing the file at this moment (Windows), look again in a moment
                    if attempt == 2:
                        raise
                    time.sleep(0.03)
            if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
                raise ValueError("bad live status")
            written = _num(data.get("written_at")) or now
            fresh = (now - written) <= max(LIVE_STALE_SECONDS, self.refresh_s * 3 + 5) and data.get("running", True)
            self._live = data
            total = data.get("configured_total")
            if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
                self.configured_total = total
            run_id = data.get("run_id")
            self.live_run_id = str(run_id)[:64] if run_id else None
            src.data_ts = written
            src.last_ok = now
            src.ok = bool(fresh)
            src.error = None if fresh else "stale"
        except Exception as error:
            src.bad(error)
        return self._live

    def _connect(self, path):
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        return sqlite3.connect(uri, uri=True, timeout=2)

    def _read_db(self, now):
        src = self.sources["db"]
        path = self.root / "utils" / "data" / "db.sqlite"
        conn = None
        try:
            conn = self._connect(path)
            stats = {}
            for row in conn.execute(
                "SELECT user_id, daily, lottery, cookie, giveaways, captchas, cowoncy, boss, boss_ticket FROM user_stats"
            ):
                stats[str(row[0])] = row[1:]
            cow = {}
            for user_id, hour, earnings in conn.execute("SELECT user_id, hour, earnings FROM cowoncy_earnings"):
                cow.setdefault(str(user_id), {})[hour] = earnings
            commands = dict(conn.execute("SELECT name, count FROM commands WHERE count > 0").fetchall())
            row = conn.execute("SELECT value FROM meta_data WHERE key = 'cowoncy_earnings_last_checked'").fetchone()
            self._db = {"stats": stats, "cow": cow, "cow_date": row[0] if row else None, "commands": commands}
            src.good(now)
        except Exception as error:
            src.bad(error)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        return self._db

    def _tail_file(self, path, offset):
        """Complete lines of path after offset: (list of bytes lines, new offset). Truncation restarts at 0"""
        size = os.stat(path).st_size
        if size < offset:
            offset = 0
        if size == offset:
            return [], offset
        with open(path, "rb") as handle:
            handle.seek(offset)
            chunk = handle.read(min(size - offset, FIRST_READ_BYTES))
        end = chunk.rfind(b"\n")
        if end < 0:
            return [], offset
        return chunk[: end + 1].split(b"\n")[:-1], offset + end + 1

    def _tail_gateway(self, now):
        src = self.sources["gateway"]
        folder = self.root / "logs"
        try:
            files = sorted(glob.glob(str(folder / "gateway_stats_*.txt")))
            if not files:
                raise FileNotFoundError("no gateway stats file")
            path = files[-1]
            if path != self._gw["path"]:
                self._gw = {"path": path, "off": 0}
            lines, self._gw["off"] = self._tail_file(path, self._gw["off"])
            for raw in lines:
                found = STATS_LINE.match(raw.decode("utf-8", "replace"))
                if not found:
                    continue
                y, mo, d, h, mi, s = (int(found.group(i)) for i in range(1, 7))
                ts = datetime(y, mo, d, h, mi, s).timestamp()
                name = found.group(7)
                kb = int(found.group(9)) / 1024.0
                entry = self.gw_names.get(name)
                if entry is None:
                    entry = self.gw_names[name] = {"ts": 0.0, "kb": None, "hours": {}}
                if ts >= entry["ts"]:
                    entry["ts"], entry["kb"] = ts, kb
                bucket = entry["hours"].setdefault(int(ts // 3600), [0.0, 0])
                bucket[0] += kb
                bucket[1] += 1
                if len(entry["hours"]) > HIST_HOURS + 2:
                    for old in sorted(entry["hours"])[: len(entry["hours"]) - HIST_HOURS]:
                        del entry["hours"][old]
                if self.gw_last_line_ts is None or ts > self.gw_last_line_ts:
                    self.gw_last_line_ts = ts
            src.good(self.gw_last_line_ts or now)
        except Exception as error:
            src.bad(error)

    def _tail_runlog(self, now):
        """Reads new run log lines. Returns a list of events (dicts without id) found in them"""
        src = self.sources["runlog"]
        found_events = []
        try:
            groups = {}
            for path in glob.glob(str(self.root / "logs" / "run_*.txt")):
                match = RUN_FILE.match(os.path.basename(path))
                if match:
                    groups.setdefault(match.group(1), []).append((int(match.group(2) or 1), path))
            if not groups:
                raise FileNotFoundError("no run log")
            stamp = max(groups)
            if stamp != self._rl["stamp"]:
                self._rl = {"stamp": stamp, "off": {}}
            last_ts = None
            for _, path in sorted(groups[stamp]):
                lines, self._rl["off"][path] = self._tail_file(path, self._rl["off"].get(path, 0))
                for raw in lines:
                    self._parse_run_line(raw, found_events)
                if lines:
                    last_ts = _stamp_ts(lines[-1]) or last_ts
            src.good(last_ts or src.data_ts or now)
        except Exception as error:
            src.bad(error)
        return found_events

    def _parse_run_line(self, raw, out):
        if b"| Ran: " in raw or b"| sleeping" in raw:
            return None
        parts = raw.decode("utf-8", "replace").rstrip("\r").split(" | ", 2)
        if len(parts) < 3 or len(parts[0]) != 19:
            return None
        stamp, name, text = parts
        try:
            ts = datetime(int(stamp[0:4]), int(stamp[5:7]), int(stamp[8:10]),
                          int(stamp[11:13]), int(stamp[14:16]), int(stamp[17:19])).timestamp()
        except ValueError:
            return None
        if name == "-":
            return ts
        kind = None
        sev = "info"
        if "Captcha detected" in text or "Ban detected" in text:
            kind, sev = "captcha", "bad"
        elif "Captcha solved" in text:
            kind, sev = "captcha", "good"
        elif "FAILED to solve" in text:
            kind, sev = "captcha", "bad"
        elif "used gems:" in text:
            out.append({"ts": ts, "type": "_gem_used", "name": name})
            return ts
        elif "no usable gems" in text:
            out.append({"ts": ts, "type": "_gem_none", "name": name, "text": text.split("no usable gems:", 1)[-1].strip()[:80]})
            return ts
        elif text.startswith("Traceback") or (ERROR_RE.search(text) and not IGNORE_RE.search(text)):
            kind, sev = "error", "warn"
        if kind:
            out.append({"ts": ts, "type": kind, "name": name, "sev": sev, "text": _clean(text, 200, self.secrets)})
        return ts

    def _mirror_console(self):
        src = self.sources["console"]
        path = self.root / "utils" / "data" / "web_console.sqlite"
        conn = None
        try:
            conn = self._connect(path)
            top = conn.execute("SELECT MAX(id) FROM logs").fetchone()[0] or 0
            reset = top < self._console_last
            last = 0 if reset else self._console_last
            if last == 0:
                rows = conn.execute("SELECT id, ts, html FROM logs ORDER BY id DESC LIMIT 1000").fetchall()[::-1]
            else:
                rows = conn.execute(
                    "SELECT id, ts, html FROM logs WHERE id > ? ORDER BY id LIMIT 1000", (last,)
                ).fetchall()
            parsed = []
            for row_id, ts, markup in rows:
                found = CONSOLE_ROW.search(markup or "")
                if found:
                    clock, body = found.group(1), htmllib.unescape(found.group(2))
                else:
                    clock, body = "", htmllib.unescape(re.sub(r"<[^>]+>", "", markup or ""))
                body = _clean(body, 400, self.secrets)
                parsed.append({"id": row_id, "ts": round(ts or 0, 1), "t": clock, "text": body, "level": _level(body)})
            with self._lock:
                if reset:
                    self.console.clear()
                self.console.extend(parsed)
                if parsed:
                    self._console_last = parsed[-1]["id"]
                elif reset:
                    self._console_last = 0
            src.good()
        except Exception as error:
            src.bad(error)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _read_runtime(self, now):
        src = self.sources["runtime"]
        try:
            with open(self.root / "utils" / "data" / "weekly_runtime.json", encoding="utf-8") as handle:
                data = json.load(handle)
            stored = data.get("days", {}) if isinstance(data, dict) else {}
            today = datetime.fromtimestamp(now).date()
            days = []
            for offset in range(6, -1, -1):
                day = today - timedelta(days=offset)
                seconds = _num(stored.get(day.isoformat())) or 0.0
                days.append({"date": day.isoformat(), "label": day.strftime("%a %d/%m"), "seconds": round(max(0.0, seconds), 1)})
            self._runtime = {"days": days}
            src.good(now)
        except Exception as error:
            src.bad(error)

    def _read_levels(self, now):
        """levels.sqlite, read-only. The file is only read again when it changed (its size or time, or its WAL file's).
        A missing file is not an error: the source is ok=false with "not created yet" and every level field is null"""
        src = self.sources["levels"]
        path = self.root / "utils" / "data" / "levels.sqlite"
        conn = None
        try:
            try:
                stat = os.stat(path)
            except FileNotFoundError:
                self._levels, self._levels_sig = _empty_levels(), None
                src.ok, src.error, src.data_ts = False, "not created yet", None
                return
            sig = [(stat.st_mtime_ns, stat.st_size)]
            try:
                wal = os.stat(str(path) + "-wal")
                sig.append((wal.st_mtime_ns, wal.st_size))
            except OSError:
                sig.append(None)
            sig = tuple(sig)
            if sig == self._levels_sig:
                src.good(now)
                return
            conn = self._connect(path)
            rows = {}
            for acc, level, cur, need, rank, checked, tried, last_ok, error in conn.execute(
                "SELECT account_id, level, xp_cur, xp_need, rank, checked_at, last_try_at, last_ok, last_error FROM account_level"
            ):
                rows[str(acc)] = {"level": level, "xp_cur": cur, "xp_need": need, "rank": rank, "checked_at": checked,
                                  "last_try_at": tried, "last_ok": last_ok, "last_error": error}
            hist = {}
            for acc, ts, level, cur, need in conn.execute(
                "SELECT account_id, ts, level, xp_cur, xp_need FROM level_history WHERE ts >= ? ORDER BY ts",
                (now - LEVEL_HISTORY_DAYS * 86400,),
            ):
                hist.setdefault(str(acc), []).append((ts, level, _pct(cur, need)))
            meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
            active = meta.get("check_active")
            self._levels = {"rows": rows, "hist": hist,
                            "active": None if active is None else active == "1",
                            "active_at": _float(meta.get("check_active_at"))}
            self._levels_sig = sig
            src.good(now)
        except Exception as error:
            src.bad(error)  # the last good data stays
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _read_quests(self, now):
        """utils/data/quests.sqlite (D7), read-only: the last quest state of each account. It is used when the live status has
        no quest block for an account (the bot is stopped). A missing file is not an error: "not created yet"."""
        src = self.sources["quests"]
        path = self.root / "utils" / "data" / "quests.sqlite"
        conn = None
        try:
            try:
                stat = os.stat(path)
            except FileNotFoundError:
                self._quests, self._quests_sig = {}, None
                src.ok, src.error, src.data_ts = False, "not created yet", None
                return
            sig = [(stat.st_mtime_ns, stat.st_size)]
            try:
                wal = os.stat(str(path) + "-wal")
                sig.append((wal.st_mtime_ns, wal.st_size))
            except OSError:
                sig.append(None)
            sig = tuple(sig)
            if sig == self._quests_sig:
                src.good(now)
                return
            conn = self._connect(path)
            rows = {}
            for acc, next_at, all_done, seals, quests_json, last_log, last_ok, last_claim, claims in conn.execute(
                "SELECT user_id, next_quest_at, all_done, seals, quests_json, last_log_at, last_ocr_ok_at, "
                "last_claim_at, claims_total FROM quest_state"
            ):
                try:
                    listed = json.loads(quests_json or "[]")
                except (TypeError, ValueError):
                    listed = []
                open_quests = [
                    {"id": q.get("quest_id"), "current": q.get("current"), "total": q.get("total")}
                    for q in listed if isinstance(q, dict) and not q.get("complete")
                ]
                rows[str(acc)] = {
                    "all_done": bool(all_done), "next_quest_at": next_at, "seals": seals,
                    "open_quests": open_quests, "last_log_at": last_log, "last_ocr_ok_at": last_ok,
                    "last_claim_at": last_claim, "claims_total": claims,
                }
            self._quests, self._quests_sig = rows, sig
            src.good(now)
        except Exception as error:
            src.bad(error)  # the last good data stays
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _quest_for(self, key, rec):
        """D7: the quest block of one account entry. The live status is the source while it has a quest key for the account
        (null when the quest cog is off). Without live data (the bot is stopped) the saved state is shown, marked saved"""
        if rec is not None and "quest" in rec:
            return rec["quest"]
        saved = self._quests.get(key)
        if saved is None:
            return None
        return dict(saved, enabled=None, saved=True)

    def _level_fields(self, key, now):
        """Level fields of one account entry. A field that is not known is left out (a missing key means "unknown"), so an
        account without a level costs only `"level":null`: level, xp_cur, xp_need, xp_pct, rank, level_checked_at,
        level_checked_ago_s, level_gain_7d, level_last_error"""
        out = {"level": None}  # `level` was always in the entry (null): it stays, the other fields appear when known
        row = self._levels["rows"].get(key)
        if row is None:
            return out
        if row["level"] is not None:
            out.update(level=row["level"], xp_cur=row["xp_cur"], xp_need=row["xp_need"],
                       xp_pct=_pct(row["xp_cur"], row["xp_need"]))
            if row["rank"] is not None:
                out["rank"] = row["rank"]
        if row["checked_at"]:
            out["level_checked_at"] = round(row["checked_at"], 1)
            out["level_checked_ago_s"] = round(max(0.0, now - row["checked_at"]), 1)
        if row["last_ok"] == 0 and row["last_error"]:
            out["level_last_error"] = str(row["last_error"])[:40]
        points = self._levels["hist"].get(key)
        if points and row["level"] is not None:
            cutoff = now - LEVEL_GAIN_DAYS * 86400
            base = None
            for ts, level, _pct_value in points:
                if ts <= cutoff:
                    base = level
                else:
                    if base is None:
                        base = level  # no row that old: the first level known is the start
                    break
            if base is not None:
                out["level_gain_7d"] = max(0, row["level"] - base)
        return {k: v for k, v in out.items() if v is not None or k == "level"}

    # ---- build -----------------------------------------------------------
    def _next_event(self, ev):
        self._event_id += 1
        ev["id"] = self._event_id
        ev.setdefault("sev", "info")
        ev.setdefault("text", "")
        ev.setdefault("acct", None)
        self.events.append(ev)
        return ev

    def _apply(self, now, live, db, new_events):
        started = time.monotonic()
        live_src = self.sources["live"]
        live_ok = bool(live is not None and live_src.ok)
        records = {}
        if live is not None:
            self.run_started_at = _num(live.get("started_at")) or self.run_started_at
            for rec in live.get("accounts", []):
                if not isinstance(rec, dict):
                    continue
                key = str(rec["id"]) if rec.get("id") else f"idx{rec.get('i')}"
                records[key] = rec
        for key in [k for k in self.acct if k.startswith("idx") and k not in records]:
            del self.acct[key]  # a pending account that has its user id now (or is gone)
        # The accounts of the run are the keys of the last live file (records). Anything else that the database or the
        # history knows is an old account: it stays listed but marked removed, and is left out of every total
        keys = list(dict.fromkeys(list(records) + list(db["stats"]) + list(self.acct)))
        for key in keys:
            hist = self.acct.get(key)
            if hist is None:
                hist = self.acct[key] = AccountHistory(key)
            rec = records.get(key)
            if rec and rec.get("name"):
                self.names[key] = rec["name"]
                self.name_to_key[rec["name"]] = key
            hist.name = self.names.get(key)
        # gateway names that were never seen live can still be mapped when unique, so skip them

        # events found in the run log
        for ev in new_events:
            key = self.name_to_key.get(ev["name"])
            hist = self.acct.get(key) if key else None
            kind = ev["type"]
            if kind == "_gem_used":
                if hist:
                    hist.gem_used += 1
                    hist.gem_last = max(hist.gem_last or 0, ev["ts"])
                    hist.gem_none = None
                continue
            if kind == "_gem_none":
                if hist:
                    hist.gem_none = ev["text"]
                continue
            ev["acct"] = key
            self._next_event(ev)
            if hist is not None:
                if kind == "error":
                    hist.error_ts.append(ev["ts"])
                if kind == "captcha" and "Captcha solved" not in ev["text"] and ev["sev"] == "bad" and "FAILED" not in ev["text"]:
                    hist.captcha_ts.append(ev["ts"])

        day0 = _day_start(now)
        pst0 = _pst_day_start(now)
        minute, hour = int(now // 60), int(now // 3600)
        cow_today_ok = (db.get("cow_date") == datetime.fromtimestamp(now).date().isoformat())
        entries = {}
        indexes = {}
        for key in keys:
            hist = self.acct[key]
            rec = records.get(key) if live_ok else None
            if rec is not None:
                hist.live = rec
            rec_use = rec
            stats = db["stats"].get(key)
            counts = (rec_use or {}).get("c") or {}
            lr = (rec_use or {}).get("lr") or {}
            # hunt buckets from the counter of the bot
            cur = _num(counts.get("hunt")) if rec_use else None
            if rec_use is not None and cur is not None:
                if hist.hunt_prev is None:
                    delta = 0
                elif cur < hist.hunt_prev:
                    delta = cur
                else:
                    delta = cur - hist.hunt_prev
                hist.hunt_prev = cur
                if delta:
                    hist.mins[minute] = hist.mins.get(minute, 0) + delta
                    hist.hours[hour] = hist.hours.get(hour, 0) + delta
                for old in [m for m in hist.mins if m < minute - HIST_MINUTES]:
                    del hist.mins[old]
                for old in [h for h in hist.hours if h < hour - HIST_HOURS]:
                    del hist.hours[old]
            # reconnect events from the counter
            rc = _num((rec_use or {}).get("rc")) if rec_use else None
            if rc is not None:
                if hist.rc_prev is not None and rc > hist.rc_prev:
                    ts = _num(rec_use.get("gwb")) or now
                    for _ in range(int(rc - hist.rc_prev)):
                        hist.reconnect_ts.append(ts)
                        self._next_event({"ts": ts, "type": "reconnect", "name": hist.name, "acct": key,
                                          "sev": "warn", "text": "Gateway disconnected and reconnected"})
                hist.rc_prev = rc
            # daily claimed
            daily_at = None
            if stats is not None:
                daily_at = stats[0] or None
                if hist.daily_prev is not None and daily_at and daily_at > hist.daily_prev:
                    self._next_event({"ts": daily_at, "type": "daily", "name": hist.name, "acct": key,
                                      "sev": "good", "text": "Daily claimed"})
                hist.daily_prev = daily_at or hist.daily_prev or 0
            entries[key] = self._entry(key, hist, rec_use, stats, counts, lr, now, day0, pst0, minute, hour, cow_today_ok,
                                       live_ok, db)
            entries[key]["removed"] = key not in records
            indexes[key] = (rec_use or hist.live or {}).get("i")
        self._finish(now, entries, indexes, live_ok, live, db, started)

    def _entry(self, key, hist, rec, stats, counts, lr, now, day0, pst0, minute, hour, cow_today_ok, live_ok, db):
        state = _state(rec) if rec else "offline"
        name = hist.name
        hunts_total = _num(counts.get("hunt")) if rec else None
        last_hunt_at = _num(lr.get("hunt")) if rec else None
        elapsed = max(1.0, min(15.0, (now - hist.first_seen) / 60.0))
        rate = None
        if rec is not None and hunts_total is not None:
            total15 = sum(v for m, v in hist.mins.items() if m > minute - 15)
            rate = round(total15 / elapsed, 2)
        spark = None
        if rec is not None and hunts_total is not None:
            spark = [sum(v for m, v in hist.mins.items() if minute - 59 + 5 * k <= m <= minute - 55 + 5 * k) for k in range(12)]
        cowoncy = delta = None
        if stats is not None:
            cowoncy = stats[5]
            hours = db["cow"].get(key)
            if cow_today_ok and hours:
                delta = 0
                for h in range(23, -1, -1):
                    if hours.get(h):
                        delta = hours[h]
                        break
        elif rec is not None and rec.get("bal") is not None:
            cowoncy = rec["bal"]  # no row in user_stats yet: the balance the account holds in memory
        daily_done = None
        if stats is not None:
            daily_done = bool(stats[0] and stats[0] >= pst0)
        captcha_today = sum(1 for t in hist.captcha_ts if t >= day0) if self.sources["runlog"].last_ok else None
        captcha_last = max(hist.captcha_ts) if hist.captcha_ts else None
        gw = self.gw_names.get(name) if name else None
        inbound = None
        if gw and gw["kb"] is not None and now - gw["ts"] <= GATEWAY_STALE_SECONDS:
            inbound = round(gw["kb"], 1)
        errors_1h = sum(1 for t in hist.error_ts if t >= now - 3600) if self.sources["runlog"].last_ok else None
        rc_1h = sum(1 for t in hist.reconnect_ts if t >= now - 3600) if rec is not None else None
        entry = {
            "id": key,
            "name": name,
            "state": state,
            "phase": _phase_of(rec, live_ok),
            "health": "unknown",
            "health_reasons": [],
            "hunts_total": hunts_total,
            "hunts_per_min_15m": rate,
            "battles_total": _num(counts.get("battle")) if rec else None,
            "last_hunt_at": last_hunt_at,
            "last_hunt_ago_s": round(now - last_hunt_at, 1) if last_hunt_at else None,
            "cowoncy": cowoncy,
            "cowoncy_delta_today": delta,
            "daily_done_today": daily_done,
            "gems": {"used": hist.gem_used if self.sources["runlog"].last_ok else None,
                     "last_at": hist.gem_last, "none": bool(hist.gem_none)},
            "captcha_today": captcha_today,
            "captcha_flag": bool(rec.get("cap_flag")) if rec else False,
            "captcha_last_at": captcha_last,
            "captcha_last_ago_s": round(now - captcha_last, 1) if captcha_last else None,
            "inbound_kb_min": inbound,
            "errors_1h": errors_1h,
            "reconnects_1h": rc_1h,
            **self._level_fields(key, now),
            "quest": self._quest_for(key, rec),
            "spark": spark,
        }
        if rec is not None and state == "stopped" and rec.get("re"):
            entry["stopped_reason"] = rec["re"]
        noisy = _noisy_for(rec) if live_ok else None
        if noisy is not None:
            entry["noisy_guild"] = noisy
        probe = dict(entry)
        probe["hunt_on"] = rec.get("hunt_on", True) if rec else True
        run_age = (now - self.run_started_at) if self.run_started_at and live_ok else None
        health, reasons = health_rules.evaluate(probe, self.cfg, run_age)
        entry["health"], entry["health_reasons"] = health, reasons
        # An info line only: it never changes the health
        info = health_rules.level_info(entry.get("level_checked_ago_s"), self._levels.get("active"))
        if info:
            entry["info_reasons"] = info
        return entry

    def _finish(self, now, entries, indexes, live_ok, live, db, started):
        # signature of every entry without the "x ago" fields, which change on every refresh
        sigs = {}
        for key, entry in entries.items():
            stable = {k: v for k, v in entry.items() if not k.endswith("_ago_s")}
            sigs[key] = hash(json.dumps(stable, sort_keys=True, separators=(",", ":")))
        changed = {k for k, s in sigs.items() if self._sigs.get(k) != s}
        removed = [k for k in self._sigs if k not in sigs]
        summary = self._summary(now, entries, live_ok, live, db)
        stable_summary = {k: v for k, v in summary.items() if k not in ("generated_at", "uptime_today_s")}
        stable_summary["autostop"] = {k: v for k, v in summary["autostop"].items() if k != "remaining_s"}
        summary_sig = hash(json.dumps(stable_summary, sort_keys=True, separators=(",", ":"), default=str))
        src_sig = tuple((n, s.ok) for n, s in sorted(self.sources.items()))
        events_last = self._event_id
        prior = self.snapshot
        content_changed = bool(changed or removed or summary_sig != self._summary_sig or src_sig != self._src_sig
                               or events_last != prior.events_last_id)
        if content_changed or prior.version == 0:
            self._version += 1
            self.ver_log.append((self._version, frozenset(changed), tuple(removed)))
            self.cache.clear()
        self._sigs, self._summary_sig, self._src_sig = sigs, summary_sig, src_sig
        snap = Snapshot()
        snap.version = self._version
        snap.generated_at = now
        snap.entries = entries
        snap.order = sorted(entries, key=lambda k: ((indexes[k] if indexes[k] is not None else 10 ** 6), k))
        snap.summary = summary
        snap.sources = {n: s.view(now) for n, s in self.sources.items()}
        snap.events_last_id = events_last
        self.snapshot = snap

    def _total_accounts(self, active):
        """The accounts of the run: configured_total from tokens.txt, as the bot wrote it in its live status file. None
        until a file of this run has been read (the dashboard never reads tokens.txt): the page then shows a dash, not 0"""
        if isinstance(self.configured_total, int) and self.configured_total >= 0:
            return self.configured_total
        return None

    def _summary(self, now, entries, live_ok, live, db):
        # Removed accounts (old ones in the database, not in this run) are left out of every number below
        active = {k: e for k, e in entries.items() if not e.get("removed")}
        counts = {"good": 0, "warn": 0, "bad": 0, "unknown": 0}
        ring = {"good": 0, "warn": 0, "bad": 0, "sleeping": 0, "unknown": 0}  # every account in exactly one slice
        states = {}
        rate = hunts_today = cow_today = cap_today = err_1h = 0
        inbound_values = []
        cowoncy_total = 0
        known_levels = []
        best = None
        gained = 0
        quest_known = False  # D7: any account has a quest block (live or saved)
        quest_on = quest_done = quest_seals = 0
        for key, entry in active.items():
            if entry.get("level") is not None:
                known_levels.append(entry["level"])
                if best is None or entry["level"] > best["level"]:
                    best = entry
            gained += entry.get("level_gain_7d") or 0
            quest = entry.get("quest")
            if quest is not None:
                quest_known = True
                quest_on += 1 if quest.get("enabled") is True else 0
                quest_done += 1 if quest.get("all_done") is True else 0
                quest_seals += quest.get("seals") or 0
            counts[entry["health"]] = counts.get(entry["health"], 0) + 1
            states[entry["state"]] = states.get(entry["state"], 0) + 1
            slice_ = "bad" if entry["health"] == "bad" else "sleeping" if entry["state"] == "sleeping" else entry["health"]
            ring[slice_] += 1
            rate += entry["hunts_per_min_15m"] or 0
            cow_today += entry["cowoncy_delta_today"] or 0
            cap_today += entry["captcha_today"] or 0
            err_1h += entry["errors_1h"] or 0
            cowoncy_total += entry["cowoncy"] or 0
            if entry["inbound_kb_min"] is not None:
                inbound_values.append(entry["inbound_kb_min"])
        day_hour0 = int(_day_start(now) // 3600)
        for key in active:
            hist = self.acct.get(key)
            if hist is not None:
                hunts_today += sum(v for h, v in hist.hours.items() if h >= day_hour0)
        today = self._runtime["days"][-1]["seconds"] if self._runtime["days"] else None
        # The countdown belongs to a bot that runs now: a live file left by a bot that was killed still says "enabled"
        auto = (live or {}).get("autostop") if live_ok and isinstance(live, dict) else None
        autostop = {"enabled": None, "stops_at": None, "remaining_s": None, "stopping": False}
        if isinstance(auto, dict):
            autostop["enabled"] = bool(auto.get("enabled"))
            autostop["stopping"] = bool(auto.get("stopping"))
            stops_at = _num(auto.get("stops_at"))
            if autostop["enabled"] and stops_at:
                autostop["stops_at"] = stops_at
                autostop["remaining_s"] = round(max(0.0, stops_at - now), 1)
                autostop["minutes"] = auto.get("minutes")
        total_n = self._total_accounts(active)
        online_n = sum(1 for e in active.values() if e.get("phase") == "online")
        return {
            "bot_online": live_ok,
            "accounts_total": total_n,
            # Online is the online phase only (logged in). Never more than the total, while the total is known
            "accounts_online": online_n if total_n is None else min(online_n, total_n),
            "health": counts,
            "ring": ring,
            "states": states,
            "hunts_per_min_total": round(rate, 2),
            "hunts_today": hunts_today,
            "cowoncy_today_total": cow_today,
            "cowoncy_total": cowoncy_total,
            "captcha_today": cap_today,
            "errors_1h": err_1h,
            "inbound_avg_kb_min": round(sum(inbound_values) / len(inbound_values), 1) if inbound_values else None,
            "level_avg": round(sum(known_levels) / len(known_levels), 1) if known_levels else None,
            "level_max": best["level"] if best else None,
            "level_max_name": best["name"] if best else None,
            "levels_known": len(known_levels),
            "levels_gained_7d": gained if known_levels else None,
            "quest_on": quest_on if quest_known else None,
            "quest_done": quest_done if quest_known else None,
            "quest_seals": quest_seals if quest_known else None,
            "inbound_over_warn": sum(1 for v in inbound_values if v >= self.cfg["inboundWarnKbMin"]),
            "inbound_over_bad": sum(1 for v in inbound_values if v >= self.cfg["inboundBadKbMin"]),
            "noisy_servers": _noisy_servers(live, live_ok),
            "quotes": _quotes_view(live, live_ok),
            "uptime_today_s": today,
            "run_started_at": self.run_started_at,
            "autostop": autostop,
            "commands": dict(sorted(db["commands"].items(), key=lambda kv: -kv[1])[:8]),
            "refresh_s": self.refresh_s,
            "attention": [
                {"id": k, "name": active[k]["name"], "health": active[k]["health"], "state": active[k]["state"],
                 "reasons": active[k]["health_reasons"]}
                for k in sorted(active, key=lambda k: (-HEALTH_RANK[active[k]["health"]], k))
                if active[k]["health"] in ("bad", "warn")][:10],
        }

    # ---- queries used by the API layer ----------------------------------
    def accounts_page(self, page, size, sort, order, q, state, health, show_removed=False):
        snap = self.snapshot
        # Removed accounts are listed only when asked for (the "Show removed accounts" filter), greyed out there
        items = [e for e in snap.entries.values() if show_removed or not e.get("removed")]
        if q:
            needle = q.lower()
            items = [e for e in items if needle in (e["name"] or "").lower() or needle in e["id"].lower()]
        if state:
            wanted = set(state.split(","))
            items = [e for e in items if e["state"] in wanted]
        if health:
            wanted = set(health.split(","))
            items = [e for e in items if e["health"] in wanted]
        reverse = order == "desc"
        if sort == "health":
            items.sort(key=lambda e: (HEALTH_RANK[e["health"]], e["name"] or ""), reverse=reverse)
        else:
            present = [e for e in items if e.get(sort) is not None]
            absent = [e for e in items if e.get(sort) is None]
            present.sort(key=lambda e: (str(e[sort]).lower() if isinstance(e[sort], str) else e[sort], e["id"]), reverse=reverse)
            items = present + absent
        total = len(items)
        pages = max(1, -(-total // size))
        page = min(max(1, page), pages)
        window = items[(page - 1) * size: page * size]
        return {"version": snap.version, "generated_at": snap.generated_at, "total": total, "page": page, "pages": pages,
                "size": size, "items": window}

    def account_detail(self, key):
        with self._lock:
            snap = self.snapshot
            entry = snap.entries.get(key)
            if entry is None:
                return None
            hist = self.acct.get(key)
            now = snap.generated_at or _now()
            hour = int(now // 3600)
            hunts = [hist.hours.get(h, 0) if hist else 0 for h in range(hour - 23, hour + 1)]
            gw = self.gw_names.get(hist.name) if hist and hist.name else None
            inbound = []
            for h in range(hour - 23, hour + 1):
                bucket = gw["hours"].get(h) if gw else None
                inbound.append(round(bucket[0] / bucket[1], 1) if bucket and bucket[1] else None)
            cow = self._db["cow"].get(key)
            cow_series = [None] * 24
            if cow and self._db.get("cow_date") == datetime.fromtimestamp(now).date().isoformat():
                current_hour = datetime.fromtimestamp(now).hour
                for h in range(current_hour + 1):
                    cow_series[h] = cow.get(h, 0)
            stats = self._db["stats"].get(key)
            extra = {}
            if stats is not None:
                extra = {"daily_at": stats[0] or None, "lottery_at": stats[1] or None, "cookie_at": stats[2] or None,
                         "giveaways_at": stats[3] or None, "boss_at": stats[6] or None, "boss_tickets": stats[7],
                         "captchas_total": stats[4]}
            if hist is not None:
                extra["gems_problem"] = hist.gem_none
            live = hist.live if hist and hist.live else {}
            commands = dict(live.get("c") or {})
            events = [e for e in self.events if e.get("acct") == key][-50:]
            points = self._levels["hist"].get(key) or []
            level_history = [{"ts": round(ts, 1), "level": level, "xp_pct": pct}
                             for ts, level, pct in points[-LEVEL_DETAIL_POINTS:]]
            return {
                "version": snap.version,
                "generated_at": snap.generated_at,
                "account": entry,
                "level_history": level_history,
                "extra": extra,
                "commands": commands,
                "series": {
                    "hours": [(hour - 23 + i) * 3600 for i in range(24)],
                    "hunts": hunts,
                    "inbound_kb_min": inbound,
                    "cowoncy_by_hour_of_day": cow_series,
                },
                "events": events,
            }

    def events_after(self, after, limit, kind=None, acct=None):
        with self._lock:
            items = [e for e in self.events if (not kind or e["type"] == kind) and (not acct or e.get("acct") == acct)]
            last = self._event_id
        if after <= 0:
            window = items[-limit:]
        else:
            window = [e for e in items if e["id"] > after][:limit]
        return {"version": self.snapshot.version, "last_id": last, "events": window}

    def console_after(self, after, limit):
        with self._lock:
            rows = list(self.console)
        if after <= 0:
            window = rows[-limit:]
        else:
            window = [r for r in rows if r["id"] > after][:limit]
        return {"last_id": rows[-1]["id"] if rows else 0, "lines": window}

    def console_last_id(self):
        return self.console[-1]["id"] if self.console else 0

    def gateway_view(self):
        snap = self.snapshot
        rows = [(e["inbound_kb_min"], e) for e in snap.entries.values()
                if e["inbound_kb_min"] is not None and not e.get("removed")]
        rows.sort(key=lambda r: -r[0])
        top = [{"id": e["id"], "name": e["name"], "kb_min": v,
                "level": "bad" if v >= self.cfg["inboundBadKbMin"] else "warn" if v >= self.cfg["inboundWarnKbMin"] else "good"}
               for v, e in rows[:20]]
        values = [v for v, _ in rows]
        return {
            "version": snap.version,
            "top": top,
            "totals": {"accounts": len(values), "sum_kb_min": round(sum(values), 1),
                       "avg_kb_min": round(sum(values) / len(values), 1) if values else None,
                       "max_kb_min": values[0] if values else None,
                       "over_warn": sum(1 for v in values if v >= self.cfg["inboundWarnKbMin"]),
                       "over_bad": sum(1 for v in values if v >= self.cfg["inboundBadKbMin"])},
            "thresholds": {"warn": self.cfg["inboundWarnKbMin"], "bad": self.cfg["inboundBadKbMin"]},
            "noisy_servers": snap.summary.get("noisy_servers") or [],
        }

    def sources_view(self, now=None):
        now = now or _now()
        return {n: s.view(now) for n, s in self.sources.items()}

    def runtime_view(self):
        return {"version": self.snapshot.version, "days": self._runtime["days"]}

    def changes_since(self, since):
        snap = self.snapshot
        if since > snap.version or not self.ver_log:
            return {"reset": True, "version": snap.version}
        if since == snap.version:
            return {"version": snap.version, "generated_at": snap.generated_at, "changed": [], "removed": [], "summary": snap.summary,
                    "sources": snap.sources}
        oldest = self.ver_log[0][0]
        if since + 1 < oldest:
            return {"reset": True, "version": snap.version}
        ids = set()
        removed = set()
        for version, changed, gone in self.ver_log:
            if version > since:
                ids |= set(changed)
                removed |= set(gone)
        return {
            "version": snap.version,
            "generated_at": snap.generated_at,
            "changed": [snap.entries[k] for k in snap.order if k in ids and k in snap.entries],
            "removed": sorted(removed - set(snap.entries)),
            "summary": snap.summary,
            "sources": snap.sources,
        }


def _phase_of(rec, live_ok):
    """starting, connecting, online or offline: what the live record says about the account. Offline without a live
    record, or when the live file is stale or the bot is not running"""
    if not live_ok or not rec:
        return "offline"
    phase = rec.get("phase")
    if phase in ("starting", "connecting", "online", "offline"):
        return phase
    if rec.get("rs") == "ready":
        return "online"
    if rec.get("rs") in ("failed", "stopped"):
        return "offline"
    return "connecting"


def _state(rec):
    status = rec.get("rs")
    if status in ("failed", "stopped") or rec.get("stp"):
        return "stopped"
    if status == "pending":
        return "connecting"  # not logged in yet: "Connecting…" in the dashboard, never "Offline"
    if rec.get("cap"):
        return "captcha"
    if rec.get("gwd"):
        return "reconnecting"
    if rec.get("sl"):
        return "sleeping"
    if rec.get("hold") or rec.get("on") is False:
        return "paused"
    return "hunting"


def _noisy_for(rec):
    """The busiest server of this account that is not its farm server, from its live record: {guild_id, kb_min} or
    None. None while the farm server is not known, so a farm server is never called noisy by mistake"""
    if not isinstance(rec, dict) or not rec.get("fg"):
        return None
    farm = str(rec["fg"])
    best = None
    for row in rec.get("gi") or []:
        if not isinstance(row, dict):
            continue
        guild_id = str(row.get("guild_id") or "")
        kb = _float(row.get("kb_min"))
        if not guild_id or kb is None or guild_id == farm:
            continue
        if best is None or kb > best["kb_min"]:
            best = {"guild_id": guild_id, "kb_min": round(kb, 1)}
    return best


def _noisy_servers(live, live_ok, limit=10):
    """Servers ranked by the inbound they cost across the accounts of this run, from the live records (ids and numbers):
    guild_id, kb_min (sum), accounts, worst (the account with the most) and farm (an account's spam channel is in it)"""
    if not live_ok or not isinstance(live, dict):
        return []
    farm = set()
    rows = {}
    for rec in live.get("accounts") or []:
        if not isinstance(rec, dict):
            continue
        if rec.get("fg"):
            farm.add(str(rec["fg"]))
        for row in rec.get("gi") or []:
            if not isinstance(row, dict):
                continue
            guild_id = str(row.get("guild_id") or "")
            kb = _float(row.get("kb_min"))
            if not guild_id or kb is None:
                continue
            item = rows.setdefault(guild_id, {"guild_id": guild_id, "kb_min": 0.0, "accounts": 0, "worst": None})
            item["kb_min"] += kb
            item["accounts"] += 1
            if item["worst"] is None or kb > item["worst"]["kb_min"]:
                item["worst"] = {"id": str(rec.get("id") or ""), "name": rec.get("name"), "kb_min": round(kb, 1)}
    ranked = sorted(rows.values(), key=lambda item: -item["kb_min"])[:limit]
    for item in ranked:
        item["kb_min"] = round(item["kb_min"], 1)
        item["farm"] = item["guild_id"] in farm
    return ranked


def _quotes_view(live, live_ok):
    """Level quote pool, cache and last fetch, as the bot wrote them (see utils/quote_source.py). None when unknown"""
    if not live_ok or not isinstance(live, dict) or not isinstance(live.get("quotes"), dict):
        return None
    quotes = live["quotes"]
    return {
        "pool": _num(quotes.get("pool")) or 0,
        "cache": _num(quotes.get("cache")) or 0,
        "last_ok_at": _num(quotes.get("last_ok_at")),
        "last_error": _clean(quotes["last_error"], 120) if quotes.get("last_error") else None,
    }


def _level(text):
    low = text.lower()
    if "traceback" in low or "error" in low or "exception" in low or "failed" in low or "banned" in low:
        return "error"
    if "captcha" in low or "warn" in low or "stall" in low or "stopp" in low:
        return "warn"
    return "info"
