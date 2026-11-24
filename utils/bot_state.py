"""Run state of the bot, shared by the bot (uwu.py, core/auto_stop.py) and the dashboard (website/bot_supervisor.py).

Files, all under utils/data. The paths are fixed relative to the project folder, never the current folder:
  bot.pid                  JSON of the running bot: {"pid", "started_at", "run_id", "supervised", "cmd"}. Written
                           atomically when the bot starts and removed when it exits (see on_exit).
  control/stop.request     JSON {"run_id", "requested_at", "reason"}, written by the dashboard. The bot checks for it
                           every STOP_POLL_SECONDS and stops itself when run_id matches. Any other request is deleted.
  control/last_exit.json   JSON {"run_id", "code", "reason", "at"}, written by the bot just before it exits.
                           reason: user_stop | auto_stop | crash | unknown.

Nothing here logs, prints or returns a token or a password. The process check never kills anything: killing is the
dashboard's job, and it only kills a process that classify() calls "bot".
"""

import json
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "utils" / "data"
PID_PATH = DATA / "bot.pid"
CONTROL_DIR = DATA / "control"
STOP_REQUEST_PATH = CONTROL_DIR / "stop.request"
LAST_EXIT_PATH = CONTROL_DIR / "last_exit.json"
STOP_POLL_SECONDS = 2.0
# Set to "1" by the dashboard when it starts the bot: the bot then does not start a dashboard of its own
SUPERVISED_ENV = "DUSK_SUPERVISED"
# The dashboard sets this to the run id it expects, so both sides agree on which run a file belongs to
RUN_ID_ENV = "DUSK_RUN_ID"
REASON_USER_STOP = "user_stop"
REASON_AUTO_STOP = "auto_stop"
REASON_CRASH = "crash"
REASON_UNKNOWN = "unknown"
EXIT_REASONS = (REASON_USER_STOP, REASON_AUTO_STOP, REASON_CRASH, REASON_UNKNOWN)
_RUN_ID_RE = re.compile(r"^[0-9a-f]{16,64}$")

# run_id: this run's id, None before begin_run() and after on_exit(). reason: the exit reason set on purpose.
# crashed: an exception ended the main thread (see uwu.py)
_RUN = {"run_id": None, "reason": None, "crashed": False}
_RUN_LOCK = threading.Lock()


class BotAlreadyRunning(Exception):
    def __init__(self, pid):
        super().__init__(f"OwO-Dusk is already running (pid {pid})")
        self.pid = pid


def new_run_id():
    return uuid.uuid4().hex


def valid_run_id(text):
    return isinstance(text, str) and bool(_RUN_ID_RE.match(text))


def _write_json_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    last_error = None
    for _ in range(5):
        try:
            os.replace(tmp, path)  # on Windows this fails while a reader has the file open: try again
            return
        except PermissionError as error:
            last_error = error
            time.sleep(0.05)
    raise last_error


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _unlink(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _psutil():
    try:
        import psutil
    except ImportError:
        return None
    return psutil


def classify(pid):
    """What runs under pid: "bot" (python running uwu.py), "other" (alive, anything else), "unknown" (alive, but the
    command line cannot be read: Windows without psutil, or no permission), or "dead" (not running)"""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return "dead"
    if pid == os.getpid():
        return "bot"
    psutil = _psutil()
    if psutil is not None:
        try:
            proc = psutil.Process(pid)
            if proc.status() == psutil.STATUS_ZOMBIE:
                return "dead"
            name = (proc.name() or "").lower()
            text = " ".join(proc.cmdline())
        except psutil.NoSuchProcess:
            return "dead"
        except (psutil.Error, OSError):
            return "unknown"
        return "bot" if "python" in name and "uwu.py" in text else "other"
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=15,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return "unknown"
        if '"%d"' % pid not in out:
            return "dead"
        image = out.split(",", 1)[0].strip().strip('"').lower()
        # A python process cannot be told apart from the bot without its command line
        return "unknown" if "python" in image else "other"
    try:
        with open("/proc/%d/stat" % pid, encoding="utf-8") as handle:
            state = handle.read().rsplit(")", 1)[1].split()[0]
        if state == "Z":
            return "dead"
        with open("/proc/%d/cmdline" % pid, "rb") as handle:
            text = handle.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except (OSError, IndexError):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return "dead"
        except PermissionError:
            return "unknown"
        return "unknown"
    return "bot" if "uwu.py" in text and "python" in text.lower() else "other"


def read_pid():
    return _read_json(PID_PATH)


def write_pid(run_id, supervised):
    _write_json_atomic(PID_PATH, {
        "pid": os.getpid(),
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "run_id": run_id,
        "supervised": bool(supervised),
        "cmd": "uwu.py",
    })


def claim_pid_file(run_id, supervised):
    """Single instance: raises BotAlreadyRunning when a bot of this project runs (or cannot be ruled out). A pid file
    whose process is dead, or is not a bot, is stale and is replaced"""
    current = read_pid()
    if current is not None:
        pid = current.get("pid")
        if isinstance(pid, int) and not isinstance(pid, bool) and pid != os.getpid():
            if classify(pid) in ("bot", "unknown"):
                raise BotAlreadyRunning(pid)
    write_pid(run_id, supervised)


def remove_pid(run_id=None):
    """Removes bot.pid when it belongs to this process (and to run_id, when one is given). Never removes another
    bot's file"""
    current = read_pid()
    if current is None or current.get("pid") != os.getpid():
        return
    if run_id is not None and current.get("run_id") != run_id:
        return
    _unlink(PID_PATH)


def remove_stale_pid():
    """Removes bot.pid when its pid is not running any more. A live process is left alone, whatever it is"""
    current = read_pid()
    if current is not None and classify(current.get("pid")) == "dead":
        _unlink(PID_PATH)


def begin_run(run_id):
    with _RUN_LOCK:
        _RUN["run_id"] = run_id
        _RUN["reason"] = None
        _RUN["crashed"] = False


def current_run_id():
    with _RUN_LOCK:
        return _RUN["run_id"]


def set_exit_reason(reason):
    """The reason of the exit that is about to happen on purpose. The first reason set is kept"""
    if reason not in EXIT_REASONS:
        return
    with _RUN_LOCK:
        if _RUN["reason"] is None:
            _RUN["reason"] = reason


def mark_crashed():
    with _RUN_LOCK:
        _RUN["crashed"] = True


def write_last_exit(run_id, code, reason):
    _write_json_atomic(LAST_EXIT_PATH, {
        "run_id": run_id,
        "code": int(code),
        "reason": str(reason),
        "at": round(time.time(), 1),
    })


def read_last_exit():
    return _read_json(LAST_EXIT_PATH)


def on_exit(code=None):
    """Called once when the bot's process ends. run_log.exit_process calls it with the exit code, just before os._exit
    (which skips atexit). atexit calls it with no code on a normal end. It writes last_exit.json and removes bot.pid.
    A second call, or a call when no run began, does nothing"""
    with _RUN_LOCK:
        run_id = _RUN["run_id"]
        if run_id is None:
            return
        _RUN["run_id"] = None
        reason = _RUN["reason"]
        crashed = _RUN["crashed"]
    if code is None:
        code = 1 if crashed else 0
    if reason is None:
        reason = REASON_CRASH if code else REASON_UNKNOWN
    try:
        write_last_exit(run_id, code, reason)
    except Exception:  # the exit goes on: a missing record only means "unknown" to the dashboard
        pass
    remove_pid(run_id)


def write_stop_request(run_id, reason="dashboard"):
    _write_json_atomic(STOP_REQUEST_PATH, {
        "run_id": run_id,
        "requested_at": round(time.time(), 1),
        "reason": str(reason),
    })


def pop_stop_request():
    """The stop request, deleted when it is read. None when there is none or it is unreadable"""
    data = _read_json(STOP_REQUEST_PATH)
    _unlink(STOP_REQUEST_PATH)
    return data


def clear_stop_request():
    _unlink(STOP_REQUEST_PATH)


def start_stop_watch(run_id, on_stop, poll=STOP_POLL_SECONDS):
    """Starts a daemon thread of the bot. It calls on_stop(reason) once, when a stop request with this run id arrives.
    Requests for other runs are deleted and ignored"""

    def loop():
        while True:
            time.sleep(poll)
            try:
                request = pop_stop_request()
            except Exception:
                request = None
            if request is None or request.get("run_id") != run_id:
                continue
            on_stop(str(request.get("reason") or "dashboard"))
            return

    thread = threading.Thread(target=loop, name="bot-stop-watch", daemon=True)
    thread.start()
    return thread
