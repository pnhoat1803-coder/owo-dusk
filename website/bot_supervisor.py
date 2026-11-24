"""Starts, stops and restarts the bot from the dashboard process.

The dashboard is the long-running process (for example under pm2). It starts the bot as a child process,
`python uwu.py --no-dashboard`, with DUSK_SUPERVISED=1, and it finds a bot that was started from the command line
through utils/data/bot.pid. To stop a bot, it writes a stop request that the bot obeys through its own shutdown path
(utils/bot_state.py). Only when the bot does not stop in time is it terminated, and then killed. A process is only ever
signalled when it is the bot: a pid from bot.pid that belongs to something else is treated as stale.

Logs: logs/bot_supervised.log gets the bot's output and one "dashboard:" line per event. No token or password is
written there.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from utils import bot_state, log_paths

PROJECT_ROOT = bot_state.ROOT
LOG_NAME = Path("logs") / "bot_supervised.log"
LOG_ROTATE_BYTES = 20 * 1024 * 1024
RESTART_DELAY_SECONDS = 30.0
MAX_RESTARTS_PER_HOUR = 3
STARTING_SECONDS = 120.0
WATCH_SECONDS = 2.0
STOP_TIMEOUT_SECONDS = 30.0
TERMINATE_WAIT_SECONDS = 10.0
KILL_WAIT_SECONDS = 5.0
MAX_RUNTIME_MIN = 1
MAX_RUNTIME_MAX = 1440
LIVE_FRESH_SECONDS = 30.0
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


class BotBusy(Exception):
    """Another start, stop or restart is running now"""


class BotRefused(Exception):
    """The action does not apply now. code: already_running, not_stopped or start_failed"""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_settings(root):
    """The three website options that control the bot, read from config/global_settings.json each time they are needed.
    A missing or unreadable file means all off. Nothing else is read from the file, and nothing is printed"""
    data = _read_json(Path(root) / "config" / "global_settings.json") or {}
    site = data.get("website") if isinstance(data.get("website"), dict) else {}
    return {
        "restartOnCrash": site.get("restartOnCrash") is True,
        "startBotOnLaunch": site.get("startBotOnLaunch") is True,
        "stopBotWithDashboard": site.get("stopBotWithDashboard") is True,
    }


def extra_args(max_runtime_minutes):
    """The only extra arguments the dashboard gives the bot: --max-runtime with a whole number of minutes, or none.
    Nothing typed by the user reaches the command line"""
    if max_runtime_minutes is None:
        return []
    if isinstance(max_runtime_minutes, bool) or not isinstance(max_runtime_minutes, int):
        raise ValueError("maxRuntimeMinutes must be a whole number of minutes")
    if not MAX_RUNTIME_MIN <= max_runtime_minutes <= MAX_RUNTIME_MAX:
        raise ValueError(f"maxRuntimeMinutes must be from {MAX_RUNTIME_MIN} to {MAX_RUNTIME_MAX}")
    return ["--max-runtime", str(max_runtime_minutes)]


def live_snapshot(root):
    """What the bot wrote to utils/data/live_status.json: {"pid", "running", "written_at", "online"}, or None"""
    data = _read_json(Path(root) / "utils" / "data" / "live_status.json")
    if data is None:
        return None
    accounts = data.get("accounts") if isinstance(data.get("accounts"), list) else []
    online = sum(1 for item in accounts if isinstance(item, dict) and item.get("phase") == "online")
    return {
        "pid": data.get("pid"),
        "running": data.get("running") is True,
        "written_at": data.get("written_at"),
        "online": online,
    }


def _rotate_log(path):
    try:
        if path.exists() and path.stat().st_size > LOG_ROTATE_BYTES:
            log_paths.rotate_to_part(path, path.parent)  # one old part is kept; only <name>.1 is ever replaced
    except OSError:
        pass


def _iso_to_wall(text):
    try:
        return datetime.fromisoformat(str(text)).timestamp()
    except (TypeError, ValueError):
        return None


class BotSupervisor:
    def __init__(self, root=None, python=None, restart_delay=RESTART_DELAY_SECONDS, watch_seconds=WATCH_SECONDS):
        self.root = Path(root) if root is not None else PROJECT_ROOT
        self.log_path = log_paths.log_file(LOG_NAME.name, root=self.root)
        self.python = python or sys.executable
        self.restart_delay = float(restart_delay)
        self.watch_seconds = float(watch_seconds)
        self._action = threading.Lock()  # one start, stop or restart at a time
        self._state_lock = threading.Lock()  # the fields below, held for a few microseconds only
        self._child = None  # subprocess.Popen of the bot this process started
        self._child_run_id = None
        self._child_started = None  # wall clock
        self._child_extra = []
        self._stopping = False
        self._last_exit = None  # {"code", "at", "reason"}
        self._restart_times = []  # wall clock of each automatic restart after a crash
        self._restart_due = None  # wall clock of the pending automatic restart
        self._restart_timer = None
        self._watch_thread = None
        self._closed = False

    # ---- what runs now --------------------------------------------------
    def _live_matches(self, pid):
        snap = live_snapshot(self.root)
        if not snap or snap["pid"] != pid or not snap["running"]:
            return False
        written = snap.get("written_at")
        return isinstance(written, (int, float)) and time.time() - written < LIVE_FRESH_SECONDS

    def _target(self):
        """The bot that runs now: our child, or the bot named in bot.pid when that pid is the bot. None otherwise"""
        with self._state_lock:
            child = self._child
            child_run = self._child_run_id
            child_started = self._child_started
        if child is not None and child.poll() is None:
            return {"pid": child.pid, "run_id": child_run, "supervised": True, "proc": child, "started": child_started}
        info = bot_state.read_pid()
        if not info:
            return None
        pid = info.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int):
            return None
        verdict = bot_state.classify(pid)
        if verdict == "unknown" and self._live_matches(pid):
            verdict = "bot"  # Windows without psutil: a python process that the live status file says runs the bot
        if verdict != "bot":
            return None  # dead, or not the bot: stale, and never signalled
        return {
            "pid": pid,
            "run_id": info.get("run_id"),
            "supervised": bool(info.get("supervised")),
            "proc": None,
            "started": _iso_to_wall(info.get("started_at")),
        }

    def _recent_restarts(self, now):
        return len([t for t in self._restart_times if now - t < 3600])

    def status(self):
        self._reap()
        target = self._target()
        now = time.time()
        with self._state_lock:
            last_exit = dict(self._last_exit) if self._last_exit else None
            stopping = self._stopping
            restarts = self._recent_restarts(now)
            due = self._restart_due
        restart_in = None if due is None else max(0, round(due - now))
        if target is None:
            crashed = last_exit is not None and last_exit.get("reason") == bot_state.REASON_CRASH
            return {
                "state": "crashed" if crashed else "stopped",
                "pid": None,
                "started_at": None,
                "uptime_s": None,
                "supervised": False,
                "last_exit": last_exit,
                "restarts_last_hour": restarts,
                "restart_in_s": restart_in,
            }
        started = target["started"]
        uptime = max(0.0, now - started) if started else None
        if stopping:
            state = "stopping"
        else:
            online = (live_snapshot(self.root) or {}).get("online", 0)
            starting = online == 0 and (uptime is None or uptime < STARTING_SECONDS)
            state = "starting" if starting else "running"
        started_text = datetime.fromtimestamp(started).isoformat(timespec="seconds") if started else None
        return {
            "state": state,
            "pid": target["pid"],
            "started_at": started_text,
            "uptime_s": None if uptime is None else int(uptime),
            "supervised": target["supervised"],
            "last_exit": last_exit,
            "restarts_last_hour": restarts,
            "restart_in_s": restart_in,
        }

    # ---- exits ----------------------------------------------------------
    @staticmethod
    def _exit_reason(run_id, code, user_stop):
        last = bot_state.read_last_exit()
        if isinstance(last, dict) and run_id and last.get("run_id") == run_id:
            if last.get("reason") in (bot_state.REASON_USER_STOP, bot_state.REASON_AUTO_STOP):
                return last["reason"]
        if user_stop:
            return bot_state.REASON_USER_STOP
        if code not in (0, None):
            return bot_state.REASON_CRASH
        return bot_state.REASON_UNKNOWN

    def _reap(self):
        """Records the exit of our child, once it has ended. A crash may schedule a restart"""
        with self._state_lock:
            child = self._child
            if child is None or child.poll() is None:
                return
            code = child.returncode
            run_id = self._child_run_id
            user_stop = self._stopping
            self._child = None
        reason = self._exit_reason(run_id, code, user_stop)
        with self._state_lock:
            self._last_exit = {"code": code, "at": round(time.time(), 1), "reason": reason}
        self._log(f"bot exited: code {code}, reason {reason}")
        if reason == bot_state.REASON_CRASH:
            self._schedule_restart()

    def _schedule_restart(self):
        if not read_settings(self.root)["restartOnCrash"]:
            self._log("not restarted: website.restartOnCrash is off")
            return
        now = time.time()
        with self._state_lock:
            self._restart_times = [t for t in self._restart_times if now - t < 3600]
            if len(self._restart_times) >= MAX_RESTARTS_PER_HOUR:
                allowed = False
            else:
                allowed = True
                self._restart_times.append(now)
                self._restart_due = now + self.restart_delay
                timer = threading.Timer(self.restart_delay, self._auto_restart)
                timer.daemon = True
                self._restart_timer = timer
        if not allowed:
            self._log(f"not restarted: {MAX_RESTARTS_PER_HOUR} automatic restarts in the last hour")
            return
        self._log(f"crash: restarting in {self.restart_delay:g} s")
        timer.start()

    def _cancel_restart(self):
        with self._state_lock:
            timer, self._restart_timer = self._restart_timer, None
            self._restart_due = None
        if timer is not None:
            timer.cancel()

    def _auto_restart(self):
        with self._state_lock:
            self._restart_timer = None
            self._restart_due = None
            if self._closed:
                return
        if not self._action.acquire(blocking=False):
            return  # a start, stop or restart from the user runs: it decides what happens now
        try:
            if self._target() is None:
                self._start_locked([])
                self._log("restarted after a crash")
        except (BotRefused, OSError) as error:
            self._log(f"automatic restart failed: {error}")
        finally:
            self._action.release()

    # ---- start ----------------------------------------------------------
    def _start_locked(self, extra):
        if self._target() is not None:
            raise BotRefused("already_running", "The bot is already running.")
        self._cancel_restart()
        if not (self.root / "uwu.py").exists():
            raise BotRefused("start_failed", "uwu.py was not found in the project folder.")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_log(self.log_path)
        run_id = bot_state.new_run_id()
        env = dict(os.environ)
        env[bot_state.SUPERVISED_ENV] = "1"
        env[bot_state.RUN_ID_ENV] = run_id
        env["PYTHONUNBUFFERED"] = "1"
        command = [self.python, "uwu.py", "--no-dashboard", *extra]
        options = {}
        if os.name == "nt":
            # Its own process group: Ctrl+C in the dashboard window does not reach the bot. No console window either
            options["creationflags"] = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        else:
            options["start_new_session"] = True  # its own process group, so the bot can be signalled as a group
        with open(self.log_path, "ab") as log:
            note = " ".join(extra) or "(no extra arguments)"
            log.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} dashboard: start {note}\n".encode("utf-8"))
            try:
                child = subprocess.Popen(
                    command,
                    cwd=str(self.root),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                    **options,
                )
            except OSError as error:
                raise BotRefused("start_failed", f"The bot could not start ({type(error).__name__}).")
        with self._state_lock:
            self._child = child
            self._child_run_id = run_id
            self._child_started = time.time()
            self._child_extra = list(extra)
            self._stopping = False
        self._log(f"started pid {child.pid} (run {run_id[:8]})")
        self._ensure_watch()

    def _ensure_watch(self):
        with self._state_lock:
            if self._watch_thread is not None and self._watch_thread.is_alive():
                return
            thread = threading.Thread(target=self._watch_loop, name="bot-supervisor-watch", daemon=True)
            self._watch_thread = thread
        thread.start()

    def _watch_loop(self):
        while True:
            time.sleep(self.watch_seconds)
            with self._state_lock:
                if self._closed:
                    return
            try:
                self._reap()
            except Exception:  # the watcher keeps going
                pass

    # ---- stop -----------------------------------------------------------
    def _is_gone(self, target):
        proc = target["proc"]
        if proc is not None:
            return proc.poll() is not None
        return bot_state.classify(target["pid"]) in ("dead", "other")

    def _wait_gone(self, target, seconds):
        deadline = time.monotonic() + seconds
        while True:
            if self._is_gone(target):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.2)

    def _signal(self, target, kill):
        pid = target["pid"]
        proc = target["proc"]
        if proc is None and bot_state.classify(pid) != "bot":
            return  # the pid is not the bot any more: it is never signalled
        try:
            if os.name == "nt":
                if proc is not None and not kill:
                    proc.send_signal(signal.CTRL_BREAK_EVENT)  # our child has its own process group
                else:
                    args = ["taskkill", "/F", "/T", "/PID", str(pid)] if kill else ["taskkill", "/PID", str(pid), "/T"]
                    subprocess.run(args, capture_output=True, timeout=30)
            else:
                sig = signal.SIGKILL if kill else signal.SIGTERM
                if proc is not None:
                    os.killpg(proc.pid, sig)  # our child leads its own session and process group
                else:
                    os.kill(pid, sig)
        except (OSError, subprocess.SubprocessError, ValueError):
            pass  # it may have ended on its own in the meantime

    def _finish_stop(self, target):
        proc = target["proc"]
        if proc is not None:
            self._reap()  # _stopping is still set: a bot that was killed is recorded as user_stop
        else:
            last = bot_state.read_last_exit()
            matching = isinstance(last, dict) and target["run_id"] and last.get("run_id") == target["run_id"]
            reason = last["reason"] if matching and last.get("reason") in (
                bot_state.REASON_USER_STOP, bot_state.REASON_AUTO_STOP) else bot_state.REASON_USER_STOP
            code = last.get("code") if matching else None
            with self._state_lock:
                self._last_exit = {"code": code, "at": round(time.time(), 1), "reason": reason}
            self._log(f"bot exited: reason {reason}")
        bot_state.remove_stale_pid()  # a killed bot could not remove its own pid file
        bot_state.clear_stop_request()

    def _stop_locked(self, timeout):
        self._cancel_restart()
        target = self._target()
        if target is None:
            self._reap()
            return
        with self._state_lock:
            self._stopping = True
        try:
            self._log(f"stop: pid {target['pid']}")
            if target["run_id"]:
                bot_state.write_stop_request(target["run_id"], "dashboard")
            done = self._wait_gone(target, timeout)
            if not done:
                self._log(f"no answer in {timeout:g} s: terminating pid {target['pid']}")
                self._signal(target, kill=False)
                done = self._wait_gone(target, TERMINATE_WAIT_SECONDS)
            if not done:
                self._log(f"still running: killing pid {target['pid']}")
                self._signal(target, kill=True)
                done = self._wait_gone(target, KILL_WAIT_SECONDS)
            if not done:
                raise BotRefused("not_stopped", f"The bot (pid {target['pid']}) did not stop.")
            self._finish_stop(target)
        finally:
            with self._state_lock:
                self._stopping = False

    # ---- public actions -------------------------------------------------
    def _with_action(self, run):
        if not self._action.acquire(blocking=False):
            raise BotBusy()
        try:
            run()
        finally:
            self._action.release()
        return self.status()

    def start(self, max_runtime_minutes=None):
        extra = extra_args(max_runtime_minutes)
        return self._with_action(lambda: self._start_locked(extra))

    def stop(self, timeout=None):
        timeout = STOP_TIMEOUT_SECONDS if timeout is None else float(timeout)
        return self._with_action(lambda: self._stop_locked(timeout))

    def restart(self):
        def run():
            target = self._target()
            with self._state_lock:
                extra = list(self._child_extra) if target is not None and target["proc"] is not None else []
            self._stop_locked(STOP_TIMEOUT_SECONDS)
            self._start_locked(extra)

        return self._with_action(run)

    def start_on_launch(self):
        """Starts the bot when website.startBotOnLaunch is on and no bot runs. Returns the status, or None"""
        if not read_settings(self.root)["startBotOnLaunch"]:
            return None
        try:
            return self.start()
        except (BotBusy, BotRefused) as error:
            self._log(f"not started at launch: {error}")
            return None

    def shutdown(self):
        """Called when the dashboard process ends. The bot keeps running, unless website.stopBotWithDashboard is on"""
        with self._state_lock:
            self._closed = True
        self._cancel_restart()
        if read_settings(self.root)["stopBotWithDashboard"]:
            try:
                self.stop()
            except (BotBusy, BotRefused) as error:
                self._log(f"not stopped with the dashboard: {error}")

    def _log(self, text):
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} dashboard: {text}\n")
        except OSError:
            pass  # a log that cannot be written must not stop the dashboard


_instance = None
_instance_lock = threading.Lock()


def get_supervisor(root=None):
    """The supervisor of this dashboard process, created at the first call"""
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = BotSupervisor(root)
        return _instance
