"""Stops the whole bot after a run time the user sets: "autoStop" in config/global_settings.json, or --max-runtime.

When the time is reached, request_stop() sets STOP_EVENT. It is process-wide: from then on no account sends a
command (refuse_command) and no account starts again (register_client, bot_runner). Each account then gets the grace
time, on its own loop: the commands it already sent may get their reply (REPLY_WAIT_SECONDS at most), its stats are
committed, and its loops, cogs and connection are ended (MyClient.end_connection). The dashboard process is stopped,
the writers are flushed, and the process exits with code 0. Accounts still running when the grace time is over are
named in the log, and the process ends anyway.
"""

import argparse
import asyncio
import logging
import sys
import threading
import time
from datetime import datetime, timedelta

from core.account_registry import registry
import utils.bot_state as bot_state
import utils.run_log as run_log

logger = logging.getLogger(__name__)

# The timer counts afterMinutes with this; tests shorten a minute by patching it
SECONDS_PER_MINUTE = 60
# The timer checks the clock at least this often
STEP_SECONDS = 30.0
# Longest wait for the replies to commands an account already sent. Also at most a third of the grace time
REPLY_WAIT_SECONDS = 30.0
# Longest wait for the SQLite writes and for the webhook queue
WRITER_WAIT_SECONDS = 10.0
# Longest wait of wait_for_shutdown(), the main thread keeps waiting for the stop to end the process
SHUTDOWN_WAIT_SECONDS = 300.0
REASON = "time reached"
# The reason an account carries in the registry once autoStop has stopped it
STOPPED_REASON = f"autoStop: {REASON}"
MIN_MINUTES = 1
MAX_MINUTES = 10080

# Set once, when the stop starts. Process-wide: no command is sent after it
STOP_EVENT = threading.Event()
# Set by cancel_timer() on Ctrl+C: the timer ends without stopping anything
_TIMER_CANCELLED = threading.Event()
# Set when request_stop() returns. Only reached when the process does not end first (os._exit is patched)
_STOP_DONE = threading.Event()

_lock = threading.Lock()
_clients = {}  # account index -> client, see register_client()
_threads = {}  # account index -> thread of the account, see register_thread()
_active_timer = None  # the AutoStopTimer that runs, read by core/live_status.py for the dashboard


def clients_snapshot():
    """A copy of the registered clients (account index -> client). Read only, for the live status file"""
    with _lock:
        return dict(_clients)


def active_timer():
    """The running AutoStopTimer, or None when autoStop is off. Read only, for the live status file"""
    return _active_timer


def stop_requested():
    """True once the stop has started: no account sends a command or starts again from then on"""
    return STOP_EVENT.is_set()


def refuse_command(client, name):
    """True when a command must not be sent, because the stop has started. Logged once per account, at debug level"""
    if not STOP_EVENT.is_set():
        return False
    if not getattr(client, "auto_stop_skip_logged", False):
        client.auto_stop_skip_logged = True
        logger.debug("autoStop: skip new command %s, %s", name, _account_name(client.account_index, client))
    return True


def register_client(index, client):
    """Called by bot_runner before an account's client runs. False when the stop has started: it must not run"""
    with _lock:
        if STOP_EVENT.is_set():
            return False
        _clients[index] = client
        return True


def register_thread(index, thread):
    """Called by bot_runner for the thread of each account, so the stop can wait for it"""
    with _lock:
        _threads[index] = thread


def parse_minutes(text):
    """The value of --max-runtime: a whole number of minutes from MIN_MINUTES to MAX_MINUTES"""
    try:
        minutes = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number of minutes")
    if not MIN_MINUTES <= minutes <= MAX_MINUTES:
        raise argparse.ArgumentTypeError(f"{minutes} is not from {MIN_MINUTES} to {MAX_MINUTES} minutes")
    return minutes


class AutoStopTimer:
    """Counts the run time from started_at, and calls request_stop() when it is over. A reminder line every log_every_minutes"""

    def __init__(self, minutes, grace_seconds, log_every_minutes, started_at=None):
        self.minutes = minutes
        self.grace_seconds = grace_seconds
        self.started_at = time.monotonic() if started_at is None else started_at
        self.deadline = self.started_at + minutes * SECONDS_PER_MINUTE
        self.log_every = log_every_minutes * SECONDS_PER_MINUTE  # 0 means no reminder lines
        self.stop_at = datetime.now() + timedelta(seconds=self.deadline - time.monotonic())

    def start(self):
        print(f"autoStop: ON, will stop at {self.stop_at:%Y-%m-%d %H:%M:%S} (after {self.minutes} min)")
        self._thread = threading.Thread(target=self._run, name="autostop-timer", daemon=True)
        self._thread.start()
        return self

    def _run(self):
        next_log = self.started_at + self.log_every if self.log_every > 0 else None
        while True:
            now = time.monotonic()
            if now >= self.deadline:
                break
            wake = min(self.deadline, now + STEP_SECONDS)
            if next_log is not None:
                wake = min(wake, next_log)
            if _TIMER_CANCELLED.wait(max(0.0, wake - now)) or STOP_EVENT.is_set():
                return
            now = time.monotonic()
            if next_log is not None and now >= next_log:
                self._print_left(now)
                next_log += self.log_every
        request_stop(REASON, self.grace_seconds)

    def _print_left(self, now):
        left = max(0.0, self.deadline - now)
        hours, minutes = divmod(int(left // SECONDS_PER_MINUTE), 60)
        print(f"autoStop: {hours}h{minutes:02d}m left (stops at {self.stop_at:%H:%M})")


def start_timer(settings, max_runtime=None, started_at=None):
    """Starts the timer when autoStop is on: for max_runtime minutes when --max-runtime is given, else as settings say.
    Returns the timer, or None"""
    if max_runtime is not None:
        minutes = max_runtime
    elif settings.enabled:
        minutes = settings.afterMinutes
    else:
        print("autoStop: OFF")
        return None
    global _active_timer
    timer = AutoStopTimer(minutes, settings.graceSeconds, settings.logEveryMinutes, started_at=started_at).start()
    _active_timer = timer
    return timer


def cancel_timer():
    """Ends the timer without stopping anything. Ctrl+C calls it, and the exit that follows is the same as before"""
    _TIMER_CANCELLED.set()


def request_stop(reason, grace_seconds=90, exit_reason="auto_stop"):
    """Stops every account and the dashboard, then ends the process with code 0. Runs once: a later call does nothing
    and returns False. exit_reason goes to last_exit.json (utils/bot_state.py): auto_stop for the timer, user_stop for
    a stop request from the dashboard"""
    with _lock:
        if STOP_EVENT.is_set():
            return False
        STOP_EVENT.set()
        clients = dict(_clients)
        threads = dict(_threads)
    bot_state.set_exit_reason(exit_reason)
    try:
        _run_stop(reason, float(grace_seconds), clients, threads)
    finally:
        _STOP_DONE.set()
    return True


def wait_for_shutdown():
    """Called when the accounts are done. While a stop runs, keeps this thread here, so the interpreter does not start
    its own exit before the stop ends the process. Returns at once when no stop has started"""
    if STOP_EVENT.is_set():
        _STOP_DONE.wait(timeout=SHUTDOWN_WAIT_SECONDS)


def _run_stop(reason, grace_seconds, clients, threads):
    started = time.monotonic()
    deadline = started + grace_seconds
    reply_wait = min(REPLY_WAIT_SECONDS, grace_seconds / 3)
    print(f"autoStop: {reason}, stopping all accounts")
    waiting = dict(clients)  # clients whose loop is not running yet, they are closed as soon as it runs
    _close_started(waiting, reply_wait)
    _stop_dashboard()
    while True:
        _close_started(waiting, reply_wait)
        if not any(thread.is_alive() for thread in threads.values()):
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(0.2)

    total = len(threads)
    alive = [index for index, thread in threads.items() if thread.is_alive()]
    names = [_account_name(index, clients.get(index)) for index in alive]
    if alive:
        print(f"autoStop: grace time of {grace_seconds:g}s is over, still running: {', '.join(names)}")
    line = f"autoStop: done, {total - len(alive)}/{total} accounts closed cleanly, {len(alive)} forced"
    if names:
        line += f" ({', '.join(names)})"
    print(line)
    _flush_writers()
    # exit_process runs the exit hooks just before os._exit: bot_state.on_exit writes last_exit.json and removes bot.pid
    run_log.exit_process(0)


def _close_started(waiting, reply_wait):
    """Schedules close_account for each waiting client whose loop runs now, and forgets it from waiting"""
    for index, client in list(waiting.items()):
        if _start_close(client, reply_wait) is not None:
            del waiting[index]


def _start_close(client, reply_wait):
    """Schedules close_account on the account's own loop. None while that loop is not running, or has ended"""
    loop = getattr(client, "loop", None)
    if not isinstance(loop, asyncio.AbstractEventLoop) or not loop.is_running():
        return None
    future = asyncio.run_coroutine_threadsafe(close_account(client, reply_wait), loop)
    future.add_done_callback(lambda done: _report_close_error(done, client))
    return future


def _report_close_error(future, client):
    if future.cancelled():
        return
    error = future.exception()
    if error is not None:
        name = _account_name(client.account_index, client)
        print(f"autoStop: closing {name} failed ({type(error).__name__})")


async def close_account(client, reply_wait):
    """Runs on the account's own loop. The commands it already sent may get their reply, its stats are committed,
    then its loops, cogs and connection are ended"""
    registry.signal_stopped(client.account_index, STOPPED_REASON)
    client.stopped = True
    loop = asyncio.get_running_loop()
    until = loop.time() + reply_wait
    while replies_pending(client) and loop.time() < until:
        await asyncio.sleep(0.2)
    if not await save_stats(client):
        print(f"autoStop: SQLite writes of {_account_name(client.account_index, client)} were not committed in time")
    await client.end_connection()


def replies_pending(client):
    """True while a command this account sent still waits for its reply, see Looper.block_till_send"""
    return any(getattr(cog, "echo_waits", 0) > 0 for cog in client.cogs.values())


async def save_stats(client):
    """Waits until the SQLite writes queued so far are committed, at most WRITER_WAIT_SECONDS. Stats are queued as they
    change (core/database), so this is the save step: nothing is written twice. False when the wait ran out"""
    from core.database.database import database_handler

    return await asyncio.to_thread(database_handler.flush, WRITER_WAIT_SECONDS)


def _stop_dashboard():
    """Stops the dashboard process with DashboardProcessManager.stop(), the call its atexit hook makes"""
    from website.process_manager import stop_all

    try:
        stop_all()
    except Exception as error:
        print(f"autoStop: dashboard stop failed ({type(error).__name__})")


def _flush_writers():
    """Writes what the writers still hold. The run log is written last, by exit_process. Each step is bounded"""
    try:
        from core import live_status

        live_status.stop()
    except Exception as error:
        print(f"autoStop: live status not written ({type(error).__name__})")
    try:
        from core.gateway_stats import writer as gateway_stats

        gateway_stats.drain()
    except Exception as error:
        print(f"autoStop: gateway stats not written ({type(error).__name__})")
    try:
        from core.reply_trace import reply_trace

        reply_trace.stop()
    except Exception as error:
        print(f"autoStop: reply trace not written ({type(error).__name__})")
    try:
        from core.database.database import database_handler

        database_handler.flush(WRITER_WAIT_SECONDS)
    except Exception as error:
        print(f"autoStop: SQLite writes not committed ({type(error).__name__})")
    _wait_webhooks()


def _wait_webhooks():
    """Waits, at most WRITER_WAIT_SECONDS, until the webhook sender has sent what it queued"""
    handler = getattr(sys.modules.get("core.client"), "webhook_handler", None)
    queue = getattr(handler, "queue", None)
    if queue is None:
        return
    waiter = threading.Thread(target=queue.join, daemon=True)
    waiter.start()
    waiter.join(timeout=WRITER_WAIT_SECONDS)


def _account_name(index, client):
    return getattr(client, "username", None) or f"account #{index}"
