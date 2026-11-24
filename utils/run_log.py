"""A plain-text copy of what the bot shows on its console, kept in the logs folder.

start() is the first thing uwu.py does. From then on, everything written to stdout or
stderr (print, the rich console, tracebacks, discord.py errors) and everything the bot
logs with self.log is also written to logs/run_YYYY-MM-DD_HH-MM-SS.txt, one line at a
time, with the date, the time and the account name. The console does not change.

It is switched on by debug.logInTextFile in config/misc.json. debug.logFolder,
debug.logMaxMB and debug.logKeepFiles set the folder, the size at which a new part file
starts, and how many files are kept.

Each captcha episode of an account, from "Captcha detected!" to its result, is also written
to logs/captcha/ once it ends, see CaptchaLog. It needs the run log to be on.
"""

import atexit
import heapq
import itertools
import json
import os
import re
import sys
import threading
import time
import tomllib
from collections import deque
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from utils import log_paths

try:
    from rich.text import Text
except ImportError:  # without rich, the markup in the bot's text is left as it is
    Text = None

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MISC_PATH = PROJECT_ROOT / "config" / "misc.json"
DEFAULT_FOLDER = "logs"
DEFAULT_MAX_MB = 20
DEFAULT_KEEP = 20
REDACTED = "***"
CAPTCHA_FOLDER = "captcha"

# Escape codes that colour the console
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# Discord bot tokens: three dot-separated parts
TOKEN = re.compile(r"[\w-]{24,}\.[\w-]{6}\.[\w-]{27,}")
# Discord webhook URLs
WEBHOOK = re.compile(
    r"(?:https?://)?(?:[\w-]+\.)*discord(?:app)?\.com/api/(?:v\d+/)?webhooks/[^\s\"'<>)\]]+",
    re.IGNORECASE,
)
# Config keys whose values are secrets: ocrApi, api_key, the webhook URLs
SECRET_KEY = re.compile(r"(?i)^(ocr\w*|api_?key|token|secret\w*|webhook\w*url|webhook)$")
# Shapes a secret takes inside a log line, masked even when its value was never collected:
# `Authorization: Bearer <key>`, `apikey=<key>`, `"api_key": "<key>"`.
# SECRET_KEY stays a key-name matcher; this one is what makes a leaked header harmless.
SECRET_ASSIGNMENT = re.compile(
    r"(?i)(authorization\s*:\s*bearer\s+|apikey\s*=\s*|api_?key\s*[\"']?\s*[:=]\s*[\"']?)"
    r"([^\s\"',;}]+)"
)

_runlog = None
_tees = []
# Called with the exit code by exit_process, see add_exit_hook()
_exit_hooks = []
_accounts = {}
_accounts_lock = threading.Lock()
_local = threading.local()

# A caller only queues its lines and its console text. One writer thread per run writes them (RunLog._writer_loop),
# so a slow disk or console never holds up the thread that logged
MAX_QUEUED = 50000  # lines waiting for the file, and text chunks waiting for the console
FLUSH_EVERY_SECONDS = 1.0  # a written line reaches the disk at most this long later...
FLUSH_EVERY_LINES = 200  # ...or sooner, once this many lines wait for a flush
DRAIN_SECONDS = 5.0  # how long an exit waits for the writer to let go of the file or the console
# Error, warning, exception, captcha and stall lines are kept when the queue is full. Other lines go first
IMPORTANT = re.compile(r"error|warn|exception|traceback|captcha|stall|stop", re.IGNORECASE)

# The levels the two sinks are filtered with, lowest first
LEVEL_ORDER = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
DEFAULT_CONSOLE_LEVEL = "INFO"
DEFAULT_FILE_LEVEL = "DEBUG"
# Lines of these categories also belong to the per-episode captcha trace
CAPTCHA_CATEGORIES = ("captcha", "captcha_dm", "captcha_provider")
# Captchaly API keys have this shape; mask them even when they were not collected from the config
CAPTCHALY_KEY = re.compile(r"\bCP_[A-Za-z0-9]{8,}\b")
# The per-episode trace file, see CaptchaTrace
DEFAULT_TRACE_LEVEL = "DEBUG"
DEFAULT_TRACE_MAX_MB = 20
DEFAULT_TRACE_KEEP = 5


def _warn(message):
    # Straight to the real stderr: going through the console would loop back into the log
    try:
        if sys.__stderr__ is not None:
            sys.__stderr__.write(f"[run log] {message}\n")
    except (OSError, ValueError):  # the real stderr is closed too: there is nowhere left to say it
        pass


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _level_rank(level):
    """The rank of a level name; 0 when the name is not a known level"""
    return LEVEL_ORDER.get(str(level).strip().upper(), 0)


def _parse_level(value, default, label):
    """A valid level name from the config, or the default and one warning"""
    if isinstance(value, str) and value.strip().upper() in LEVEL_ORDER:
        return value.strip().upper()
    _warn(f"{label} {value!r} is not a log level, using {default}")
    return default


def read_settings(misc):
    """The log keys of misc["debug"]. Values that cannot be used are replaced by the defaults"""
    debug = misc.get("debug") if isinstance(misc.get("debug"), dict) else {}

    enabled = debug.get("logInTextFile", False)
    if not isinstance(enabled, bool):
        _warn("logInTextFile must be true or false, the run log stays off")
        enabled = False

    folder = debug.get("logFolder", DEFAULT_FOLDER)
    if not isinstance(folder, str) or not folder.strip():
        _warn(f"logFolder {folder!r} is not a folder name, using {DEFAULT_FOLDER!r}")
        folder = DEFAULT_FOLDER

    max_mb = debug.get("logMaxMB", DEFAULT_MAX_MB)
    if not _is_number(max_mb) or max_mb <= 0:
        _warn(f"logMaxMB {max_mb!r} must be a positive number, using {DEFAULT_MAX_MB}")
        max_mb = DEFAULT_MAX_MB

    keep = debug.get("logKeepFiles", DEFAULT_KEEP)
    if not isinstance(keep, int) or isinstance(keep, bool) or keep < 1:
        _warn(f"logKeepFiles {keep!r} must be a whole number of at least 1, using {DEFAULT_KEEP}")
        keep = DEFAULT_KEEP

    console_level = debug.get("consoleLevel")
    if console_level is None:
        console_level = DEFAULT_CONSOLE_LEVEL
    else:
        console_level = _parse_level(console_level, DEFAULT_CONSOLE_LEVEL, "consoleLevel")

    file_level = debug.get("fileLevel")
    if file_level is None:
        file_level = DEFAULT_FILE_LEVEL
    else:
        file_level = _parse_level(file_level, DEFAULT_FILE_LEVEL, "fileLevel")

    return {
        "enabled": enabled,
        "folder": folder.strip(),
        "max_bytes": int(max_mb * 1024 * 1024),
        "keep": keep,
        "console_level": console_level,
        "file_level": file_level,
        "trace": read_trace_settings(debug),
    }


def read_trace_settings(debug):
    """The captchaTrace keys of misc["debug"]. Missing or unusable values fall back to safe defaults"""
    node = debug.get("captchaTrace")
    if not isinstance(node, dict):
        node = {}

    enabled = node.get("enabled", False)
    if not isinstance(enabled, bool):
        _warn(f"captchaTrace.enabled {enabled!r} must be true or false, the trace stays off")
        enabled = False

    level = node.get("level")
    if level is None:
        level = DEFAULT_TRACE_LEVEL
    else:
        level = _parse_level(level, DEFAULT_TRACE_LEVEL, "captchaTrace.level")

    include_raw = node.get("includeDmRaw", False)
    if not isinstance(include_raw, bool):
        _warn(f"captchaTrace.includeDmRaw {include_raw!r} must be true or false, using false")
        include_raw = False

    max_mb = node.get("maxFileMB", DEFAULT_TRACE_MAX_MB)
    if not _is_number(max_mb) or max_mb <= 0:
        _warn(f"captchaTrace.maxFileMB {max_mb!r} must be a positive number, using {DEFAULT_TRACE_MAX_MB}")
        max_mb = DEFAULT_TRACE_MAX_MB

    max_files = node.get("maxFiles", DEFAULT_TRACE_KEEP)
    if not isinstance(max_files, int) or isinstance(max_files, bool) or max_files < 1:
        _warn(f"captchaTrace.maxFiles {max_files!r} must be a whole number of at least 1, using {DEFAULT_TRACE_KEEP}")
        max_files = DEFAULT_TRACE_KEEP

    return {
        "enabled": enabled,
        "level": level,
        "include_dm_raw": include_raw,
        "max_bytes": int(max_mb * 1024 * 1024),
        "max_files": max_files,
    }


def _collect_secrets(node, found):
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str) and SECRET_KEY.match(key):
                found.add(value.strip())
            else:
                _collect_secrets(value, found)
    elif isinstance(node, list):
        for item in node:
            _collect_secrets(item, found)


def load_secrets():
    """Values that must never reach the log: secret config values and the tokens in tokens.txt"""
    found = set()
    for path in sorted((PROJECT_ROOT / "config").glob("*.json")):
        try:
            _collect_secrets(json.loads(path.read_text(encoding="utf-8")), found)
        except (OSError, ValueError):
            pass
    for path in sorted((PROJECT_ROOT / "config").glob("*.toml")):
        try:
            _collect_secrets(tomllib.loads(path.read_text(encoding="utf-8")), found)
        except (OSError, ValueError):
            pass
    try:
        for line in (PROJECT_ROOT / "tokens.txt").read_text(encoding="utf-8").splitlines():
            if line.split():
                found.add(line.split()[0])
    except (OSError, ValueError):
        pass
    # Longest first, so a secret is never half-replaced by a shorter one inside it
    return sorted((value for value in found if len(value) >= 8), key=len, reverse=True)


def _redact(text, secrets):
    for secret in secrets:
        text = text.replace(secret, REDACTED)
    # Header and query forms are masked by shape too, so a key that was not
    # collected from the config (rotated at runtime, added to a new file) still
    # never reaches the run log
    text = SECRET_ASSIGNMENT.sub(lambda match: match.group(1) + REDACTED, text)
    text = CAPTCHALY_KEY.sub(REDACTED, text)
    return WEBHOOK.sub(REDACTED, TOKEN.sub(REDACTED, text))


class CaptchaLog:
    """Writes each account's captcha episode, from "Captcha detected!" to its result, as one
    block in logs/captcha/run_<stamp>_RESOLVED.txt or run_<stamp>_FAILED.txt. The stamp is the
    one of the run file. A file is created when the first episode with its result ends"""

    def __init__(self, folder, stamp, secrets):
        self.folder = Path(folder)
        self.stamp = stamp
        self.secrets = secrets
        self._lock = threading.Lock()
        # Thread of an account -> its episode, while it has no result yet
        self._episodes = {}
        # RESOLVED or FAILED -> the file it is written to
        self._files = {}

    def start(self, thread, name):
        with self._lock:
            if thread not in self._episodes:
                self._episodes[thread] = {
                    "name": name or "-",
                    "started": datetime.now(),
                    "lines": [],
                }

    def collect(self, thread, lines):
        with self._lock:
            episode = self._episodes.get(thread)
            if episode is not None:
                episode["lines"].extend(lines)

    def finish(self, thread, result, reason):
        with self._lock:
            episode = self._episodes.pop(thread, None)
        if episode is None:
            return
        header = (
            f"===== {episode['name']} | bắt đầu {episode['started']:%H:%M:%S}"
            f" | kết thúc {datetime.now():%H:%M:%S} | {result} | {reason} ====="
        )
        block = [_redact(header, self.secrets) + "\n", *episode["lines"], "\n"]
        self._write(result, block)

    def finish_unfinished(self, reason):
        with self._lock:
            threads = list(self._episodes)
        for thread in threads:
            self.finish(thread, "FAILED", reason)

    def _write(self, result, lines):
        try:
            with self._lock:
                if result not in self._files:
                    self.folder.mkdir(parents=True, exist_ok=True)
                    path = self.folder / f"run_{self.stamp}_{result}.txt"
                    self._files[result] = open(path, "ab")
                handle = self._files[result]
                for line in lines:
                    handle.write(line.encode("utf-8", errors="replace"))
                    handle.flush()  # every line reaches the disk, as in the run file
        except OSError as error:
            _warn(f"writing to the captcha log failed: {error}")

    def close(self):
        with self._lock:
            for handle in self._files.values():
                handle.close()
            self._files.clear()


def _trace_part(path, index):
    """The rotated name of a trace file: <name>_trace.txt -> <name>_trace.<index>.txt"""
    return path.with_name(f"{path.stem}.{index}{path.suffix}")


def _safe_name(account):
    """An account name that is safe to use as part of a file name"""
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(account or "")).strip("._-")
    return name[:40] or "account"


class CaptchaTrace:
    """One file per captcha episode: logs/captcha/<account>_<stamp>_trace.txt

    Only the captcha categories reach it, and only levels at or above its own level. The file is
    rotated in place (<name>_trace.1.txt, .2, ...) once it grows past max_bytes, keeping at most
    max_files files. A trace that cannot be written never stops the bot: the error is reported
    once and the episode simply has no trace.
    """

    def __init__(self, folder, settings, secrets):
        self.folder = Path(folder)
        self.settings = settings or {}
        self.enabled = bool(self.settings.get("enabled", False))
        self.level = self.settings.get("level", DEFAULT_TRACE_LEVEL)
        self.include_dm_raw = bool(self.settings.get("include_dm_raw", False))
        self.max_bytes = int(self.settings.get("max_bytes", DEFAULT_TRACE_MAX_MB * 1024 * 1024))
        self.keep = int(self.settings.get("max_files", DEFAULT_TRACE_KEEP))
        self.secrets = secrets
        self._lock = threading.Lock()
        self._episodes = {}  # thread -> {"handle", "path", "size"}

    def start(self, thread, account):
        if not self.enabled:
            return
        name = _safe_name(account)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.folder / f"{name}_{stamp}_trace.txt"
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            handle = open(path, "ab")
            size = os.path.getsize(path)
        except OSError as error:
            _warn(f"captcha trace off for this episode: {error}")
            return
        with self._lock:
            previous = self._episodes.pop(thread, None)
            if previous is not None:
                try:
                    previous["handle"].close()
                except OSError:
                    pass
            self._episodes[thread] = {"handle": handle, "path": path, "size": size}

    def write(self, thread, level, category, text):
        if not self.enabled or category not in CAPTCHA_CATEGORIES:
            return
        if _level_rank(level) < _level_rank(self.level):
            return
        stamp = datetime.now().isoformat(timespec="milliseconds")
        parts = [part for part in str(text).splitlines() if part.strip()]
        with self._lock:
            state = self._episodes.get(thread)
            if state is None or state["handle"] is None:
                return
            for part in parts:
                line = f"{stamp} | {level} | {category} | {_redact(part, self.secrets)}\n"
                data = line.encode("utf-8", errors="replace")
                try:
                    state["handle"].write(data)
                    state["handle"].flush()
                except OSError as error:
                    _warn(f"captcha trace write failed: {error}")
                    return
                state["size"] += len(data)
                if state["size"] > self.max_bytes:
                    self._rotate(state)

    def _rotate(self, state):
        path = state["path"]
        try:
            state["handle"].close()
        except OSError:
            pass
        state["handle"] = None
        if self.keep <= 1:
            try:
                open(path, "wb").close()
            except OSError as error:
                _warn(f"captcha trace rotate failed: {error}")
                return
        else:
            # Only this trace's own parts (<name>_trace.<N>.txt) are deleted or renamed, and only inside this folder
            pattern = f"{path.stem}.[0-9]*{path.suffix}"
            oldest = _trace_part(path, self.keep - 1)
            try:
                if oldest.exists():
                    log_paths.safe_unlink(oldest, self.folder, pattern)
                for index in range(self.keep - 2, 0, -1):
                    source = _trace_part(path, index)
                    if source.exists():
                        log_paths.safe_rename(source, _trace_part(path, index + 1), self.folder, pattern)
                log_paths.safe_rename(path, _trace_part(path, 1), self.folder, pattern)
            except OSError as error:
                _warn(f"captcha trace rotate failed: {error}")
        try:
            state["handle"] = open(path, "ab")
            state["size"] = 0
        except OSError as error:
            _warn(f"captcha trace reopen failed: {error}")

    def finish(self, thread):
        with self._lock:
            state = self._episodes.pop(thread, None)
        if state is not None:
            try:
                state["handle"].close()
            except OSError:
                pass

    def finish_all(self):
        with self._lock:
            states = list(self._episodes.values())
            self._episodes.clear()
        for state in states:
            try:
                state["handle"].close()
            except OSError:
                pass


class RunLog:
    """Writes lines to one run file, and to a new part file once the current one is full"""

    def __init__(self, folder, max_bytes, keep, secrets, console_level=DEFAULT_CONSOLE_LEVEL,
                 file_level=DEFAULT_FILE_LEVEL, trace_settings=None):
        self.folder = Path(folder)
        self.max_bytes = max_bytes
        self.keep = keep
        self.secrets = secrets
        self.console_level = console_level
        self.file_level = file_level
        self._stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self._part = 1
        self._io_lock = threading.Lock()  # the file: the writer thread, drain() and close() take turns
        self._handle = None
        self._size = 0
        self._broken = False
        self._unflushed = 0  # lines written since the last flush
        self._last_flush = time.monotonic()
        # Queued lines and console text. _queue_lock is held for a few microseconds, never while anything is written
        self._queue_lock = threading.Lock()
        self._console_lock = threading.Lock()  # the console: the writer thread, or drain()
        self._wake = threading.Event()  # set when something is queued
        self._seq = itertools.count()  # numbers the lines, so the file gets them in the order they were queued
        self._lines = deque()  # (number, line) of the lines that may be dropped, see _queue
        self._urgent = deque()  # (number, line) of the error, warning, captcha and stall lines
        self._console_text = deque(maxlen=MAX_QUEUED)  # (stream, text): a stalled console loses its oldest text
        self._dropped = 0  # lines dropped since the last "run_log: dropped" line
        self.captcha = CaptchaLog(self.folder / CAPTCHA_FOLDER, self._stamp, secrets)
        self.trace = CaptchaTrace(self.folder / CAPTCHA_FOLDER, trace_settings, secrets)
        self.folder.mkdir(parents=True, exist_ok=True)
        self._open()
        self._prune()
        threading.Thread(target=self._writer_loop, name="run-log-writer", daemon=True).start()

    @property
    def path(self):
        suffix = "" if self._part == 1 else f"_part{self._part}"
        return self.folder / f"run_{self._stamp}{suffix}.txt"

    def _open(self):
        self._handle = open(self.path, "ab")
        self._size = os.path.getsize(self.path)

    def _prune(self):
        # Oldest first. The file this run has just opened is the newest, so it stays. Only run_*.txt files directly
        # inside this folder are considered (utils/log_paths.py)
        log_paths.remove_old(self.folder, "run_*.txt", self.keep)

    def write(self, line, account="", level="INFO", category=None, markup=False, **extras):
        """Queues `line` for the file when it reaches the file level, and returns whether the console should
        show it. `level` and `category` decide the sinks; the caller prints to the console itself"""
        rank = _level_rank(level)
        visible = rank >= _level_rank(self.console_level)
        if self._broken:
            return visible
        plain = self._plain(line, markup)
        if category in CAPTCHA_CATEGORIES:
            # The per-episode trace has its own level; it gets the raw text (the file sink drops markup tags)
            self.trace.write(threading.get_ident(), level, category, _redact(str(line), self.secrets))
        if rank >= _level_rank(self.file_level):
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            name = account or "-"
            lines = [
                f"{stamp} | {name} | {part}\n"
                for part in plain.splitlines()
                if part.strip()
            ]
            self._queue(lines)
            self.captcha.collect(threading.get_ident(), lines)
        return visible

    def console_write(self, stream, text):
        """Queues `text` for `stream`, the real stdout or stderr. The writer thread prints it"""
        with self._queue_lock:
            self._console_text.append((stream, text))
        self._wake.set()

    def _queue(self, lines):
        with self._queue_lock:
            for line in lines:
                entry = (next(self._seq), line)
                if IMPORTANT.search(line):
                    self._urgent.append(entry)
                else:
                    self._lines.append(entry)
                if len(self._lines) + len(self._urgent) > MAX_QUEUED:
                    # The oldest line that may go is dropped; an important one only when nothing else is queued
                    queue = self._lines or self._urgent
                    queue.popleft()
                    self._dropped += 1
        self._wake.set()

    def _writer_loop(self):
        while True:
            # A line that is not flushed yet is flushed FLUSH_EVERY_SECONDS after it was written, even if no line follows
            wait = None
            if self._unflushed:
                wait = max(0.0, self._last_flush + FLUSH_EVERY_SECONDS - time.monotonic())
            self._wake.wait(wait)
            self._wake.clear()  # cleared before the queues are taken: a line queued after this sets it again
            try:
                with self._io_lock:
                    self._write_file_queued()
                with self._console_lock:
                    self._write_console_queued()
            except Exception as error:  # the writer must keep going, or nothing else is ever written
                _warn(f"run log writer failed: {type(error).__name__}: {error}")

    def _write_file_queued(self):
        """Writes the queued lines to the file. Called by the writer thread, and by drain() at exit"""
        with self._queue_lock:
            lines, self._lines = self._lines, deque()
            urgent, self._urgent = self._urgent, deque()
            dropped, self._dropped = self._dropped, 0
        if dropped:
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._append(f"{stamp} | - | run_log: dropped {dropped} lines\n".encode("utf-8"))
        for _, line in heapq.merge(lines, urgent):
            self._append(line.encode("utf-8", errors="replace"))
        self._flush_if_due()

    def _write_console_queued(self):
        """Prints the queued console text. Called by the writer thread, and by drain() at exit"""
        with self._queue_lock:
            pending, self._console_text = self._console_text, deque(maxlen=MAX_QUEUED)
        streams = {}
        for stream, text in pending:
            try:
                stream.write(text)
            except Exception:  # a console that cannot be written to is skipped; the run file still has the lines
                continue
            streams[id(stream)] = stream
        for stream in streams.values():
            try:
                stream.flush()
            except Exception:
                pass

    def drain(self, timeout=DRAIN_SECONDS):
        """Writes what is queued now, from the calling thread. It waits at most `timeout` seconds for the writer
        to let go of the file or of the console, so a stalled console cannot keep an exit from ending"""
        if self._io_lock.acquire(timeout=timeout):
            try:
                self._write_file_queued()
            finally:
                self._io_lock.release()
        if self._console_lock.acquire(timeout=timeout):
            try:
                self._write_console_queued()
            finally:
                self._console_lock.release()

    def flush_file(self):
        """Flushes the run file to the disk now, not only when a flush is due. Called before the process exits"""
        if not self._io_lock.acquire(timeout=DRAIN_SECONDS):
            return
        try:
            if self._handle is not None and not self._broken:
                self._handle.flush()
                self._unflushed = 0
                self._last_flush = time.monotonic()
        except OSError as error:
            self._fail(error)
        finally:
            self._io_lock.release()

    def _append(self, data):
        if self._handle is None or self._broken:  # closed at exit, while a thread is still printing
            return
        try:
            if self._size and self._size + len(data) > self.max_bytes:
                self._handle.close()
                self._part += 1
                self._open()
                self._unflushed = 0  # close() flushed the part that was full
                self._last_flush = time.monotonic()
            self._handle.write(data)
            self._size += len(data)
            self._unflushed += 1
        except OSError as error:
            self._fail(error)

    def _flush_if_due(self):
        if not self._unflushed:
            return
        if self._unflushed < FLUSH_EVERY_LINES and time.monotonic() - self._last_flush < FLUSH_EVERY_SECONDS:
            return
        self._unflushed = 0
        self._last_flush = time.monotonic()
        if self._handle is None or self._broken:
            return
        try:
            self._handle.flush()
        except OSError as error:
            self._fail(error)

    def _fail(self, error):
        self._broken = True
        _warn(f"writing to the run log failed, logging to it stops: {error}")

    def _plain(self, text, markup):
        text = ANSI.sub("", str(text))
        if markup and Text is not None:
            try:
                text = Text.from_markup(text).plain
            except Exception:
                pass  # not valid markup, so the text is kept as it is
        return _redact(text, self.secrets)

    def close(self):
        if not self._io_lock.acquire(timeout=DRAIN_SECONDS):  # the writer is stuck on the disk: the exit goes on
            return
        try:
            if self._handle is not None:
                self._handle.close()
                self._handle = None
            self._unflushed = 0
        finally:
            self._io_lock.release()


class _Tee:
    """Stands in for sys.stdout or sys.stderr: writes go to the real stream and into the run log"""

    def __init__(self, stream):
        self._stream = stream
        # Unfinished lines, one per thread: print() can reach the stream in several pieces,
        # and two threads must not mix their pieces into one line
        self._pending = {}
        self._lock = threading.Lock()

    def write(self, text):
        if _runlog is not None and not _console_only():
            try:
                self._log(text)
            except Exception as error:  # the console must keep working even if the log cannot
                _warn(f"run log skipped a line: {type(error).__name__}: {error}")
        _runlog.console_write(self._stream, text)  # printed by the writer thread, so a slow console blocks nobody
        return len(text)

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def flush(self):
        # Nothing to do here: the writer thread prints the queued text and flushes the console after it
        pass

    def _log(self, text):
        # Only whole lines are written: print and rich often send a line in several pieces
        thread = threading.get_ident()
        with self._lock:
            pending = self._pending.pop(thread, "") + text
            complete, found, rest = pending.rpartition("\n")
            if rest:
                self._pending[thread] = rest
        if found:
            _runlog.write(complete, account=bound_account())

    def finish(self):
        with self._lock:
            leftovers = [(thread, rest) for thread, rest in self._pending.items() if rest]
            self._pending.clear()
        for thread, rest in leftovers:
            _runlog.write(rest, account=_accounts.get(thread))

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _console_only():
    return getattr(_local, "depth", 0) > 0


@contextmanager
def console_only():
    """Output inside this block reaches the console only. Used for lines the bot logs itself"""
    _local.depth = getattr(_local, "depth", 0) + 1
    try:
        yield
    finally:
        _local.depth -= 1


def bind_account(name):
    """Output printed from this thread from now on carries the account name"""
    with _accounts_lock:
        _accounts[threading.get_ident()] = name


def bound_account():
    return _accounts.get(threading.get_ident())


def write(text, account=None, level="INFO", category=None, **extras):
    """A line the bot logs itself (self.log). Does nothing to the file while the run log is off.
    Returns True when the console should show the line, also when the run log is off"""
    if _runlog is None:
        return True
    try:
        return _runlog.write(
            text,
            account=account,
            level=level,
            category=category,
            markup=True,
            **extras,
        )
    except Exception as error:  # logging must never stop the bot from logging
        _warn(f"run log skipped a line: {type(error).__name__}: {error}")
        return True


def redact(text):
    """`text` with the collected secrets and the secret shapes masked, for the console and website sinks"""
    secrets = _runlog.secrets if _runlog is not None else ()
    return _redact(str(text), secrets)


def include_dm_raw():
    """True when the captcha trace asks for the raw DM text (redacted) on the classification line"""
    if _runlog is None:
        return False
    return _runlog.trace.include_dm_raw


def captcha_start():
    """Opens the captcha episode of the account that calls this, unless one is open already"""
    if _runlog is not None:
        _runlog.captcha.start(threading.get_ident(), bound_account())
        _runlog.trace.start(threading.get_ident(), bound_account())


def captcha_finish(result, reason):
    """Ends the captcha episode of the account that calls this as RESOLVED or FAILED"""
    if _runlog is not None:
        _runlog.captcha.finish(threading.get_ident(), result, reason)
        _runlog.trace.finish(threading.get_ident())


def _write_out_queued():
    """Writes what the tees still hold back and what is queued for the run log, from the calling thread, and
    flushes the file, as the process may end with os._exit after this"""
    for tee in _tees:
        tee.finish()
    _runlog.drain()
    _runlog.flush_file()


def add_exit_hook(hook):
    """Registers hook(code), called by exit_process just before the process ends, after the run log is written. The
    hooks run in the order they were added. A hook that fails is reported, and the exit goes on"""
    _exit_hooks.append(hook)


def exit_process(code=0):
    """Ends the process at once. Captcha episodes that have no result yet are written first, as FAILED, and the
    lines still queued for the run log are written before the process ends. The exit hooks run last"""
    if _runlog is not None:
        try:
            _runlog.captcha.finish_unfinished("unfinished (process exit)")
            _write_out_queued()
        except Exception as error:  # exiting must not wait for the logs
            _warn(f"run log not written out: {type(error).__name__}: {error}")
    for hook in list(_exit_hooks):
        try:
            hook(code)
        except Exception as error:  # a failing hook must not keep the process alive
            _warn(f"exit hook failed: {type(error).__name__}: {error}")
    os._exit(code)


def start(misc=None):
    """Starts the run log when debug.logInTextFile is true. Returns the RunLog, or None"""
    try:
        return _start(misc)
    except Exception as error:  # a broken run log must never keep the bot from starting
        _warn(f"run log off: {type(error).__name__}: {error}")
        return None


def _start(misc):
    global _runlog
    if _runlog is not None:
        return _runlog
    if misc is None:
        try:
            misc = json.loads(MISC_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            _warn(f"run log off, config/misc.json could not be read: {error}")
            return None
    settings = read_settings(misc)
    if not settings["enabled"]:
        return None

    folder = log_paths.folder_for(settings["folder"])
    try:
        _runlog = RunLog(
            folder,
            settings["max_bytes"],
            settings["keep"],
            load_secrets(),
            console_level=settings["console_level"],
            file_level=settings["file_level"],
            trace_settings=settings["trace"],
        )
    except OSError as error:
        _warn(f"run log off, {folder} cannot be used: {error}")
        return None

    if sys.stdout is not None:
        sys.stdout = _Tee(sys.stdout)
        _tees.append(sys.stdout)
    if sys.stderr is not None:
        sys.stderr = _Tee(sys.stderr)
        _tees.append(sys.stderr)
    atexit.register(_finish)
    return _runlog


def _finish():
    if _runlog is not None:
        _write_out_queued()
        _runlog.captcha.finish_unfinished("unfinished (process exit)")
        _runlog.captcha.close()
        _runlog.trace.finish_all()
        _runlog.close()
