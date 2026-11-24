"""Samples the stack of an account thread whose event loop has stopped running.

One daemon thread for the whole process. Every account's loop runs run_heartbeat(), which stamps the time
every HEARTBEAT_SECONDS. When that stamp is older than debug.blockedLoopStackAfter, the stack of the
account's thread is written to the reply trace as one EVENT kind=blocked line. While the same block lasts,
one more line is written every REPEAT_SECONDS, so a changing stack shows up.

The sampler only reads sys._current_frames() and queues a trace line. It never touches an account's loop
and it writes no local variables, so no token or message text reaches the trace.
"""

import asyncio
import os
import sys
import threading
import time

from core.reply_trace import reply_trace

HEARTBEAT_SECONDS = 0.5
TICK_SECONDS = 1.0
REPEAT_SECONDS = 30.0
STACK_FRAMES = 12
DEFAULT_BLOCKED_AFTER = 5.0
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


async def run_heartbeat(client):
    """Runs on the account's own loop, until the client closes"""
    while not client.is_closed():
        client.loop_heartbeat_at = time.monotonic()
        await asyncio.sleep(HEARTBEAT_SECONDS)


def _where(filename):
    """A file inside the project by its path from the project root, any other file by its name"""
    if filename.startswith(PROJECT_ROOT + os.sep):
        return filename[len(PROJECT_ROOT) + 1 :].replace(os.sep, "/")
    return os.path.basename(filename)


def stack_text(frame):
    """The innermost STACK_FRAMES frames, innermost first, as "file:line function" joined by " <- " """
    entries = []
    while frame is not None and len(entries) < STACK_FRAMES:
        code = frame.f_code
        entries.append(f"{_where(code.co_filename)}:{frame.f_lineno} {code.co_name}")
        frame = frame.f_back
    return " <- ".join(entries)


class LoopWatchdog:
    def __init__(
        self,
        blocked_after=DEFAULT_BLOCKED_AFTER,
        tick_seconds=TICK_SECONDS,
        repeat_seconds=REPEAT_SECONDS,
    ):
        self.blocked_after = float(blocked_after)
        self.tick_seconds = tick_seconds
        self.repeat_seconds = repeat_seconds
        self._lock = threading.Lock()
        self._accounts = {}  # id(client) -> {"client", "thread_id", "last_line_at"}
        self._stop = threading.Event()
        self._thread = None

    def configure_from_settings(self, settings):
        debug = getattr(settings, "debug", None)
        self.blocked_after = float(
            getattr(debug, "blockedLoopStackAfter", DEFAULT_BLOCKED_AFTER)
        )

    def register(self, client, thread_id):
        """Called on the account's loop, from setup_hook"""
        with self._lock:
            self._accounts[id(client)] = {
                "client": client,
                "thread_id": thread_id,
                "last_line_at": None,
            }

    def start(self):
        """Starts the sampler thread. Does nothing while blocked_after is 0 (off) or the thread already runs"""
        if self._thread is not None or self.blocked_after <= 0:
            return
        self._thread = threading.Thread(
            target=self._run, name="loop-watchdog", daemon=True
        )
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.wait(self.tick_seconds):
            self.check(time.monotonic())

    def check(self, now):
        """One pass over the accounts. A line is written for each account whose loop has not run for
        blocked_after seconds, and while the block lasts, at most one line every repeat_seconds"""
        with self._lock:
            entries = list(self._accounts.items())
        for key, entry in entries:
            client = entry["client"]
            if client.is_closed():
                self._forget(key)
                continue
            stamp = getattr(client, "loop_heartbeat_at", None)
            if stamp is None:
                continue
            stopped_for = now - stamp
            if stopped_for < self.blocked_after:
                entry["last_line_at"] = None  # the next block gets its first line at once
                continue
            last_line_at = entry["last_line_at"]
            if last_line_at is not None and now - last_line_at < self.repeat_seconds:
                continue
            frame = sys._current_frames().get(entry["thread_id"])
            if frame is None:
                self._forget(key)  # the account's thread has ended
                continue
            entry["last_line_at"] = now
            reply_trace.blocked(client, stopped_for, stack_text(frame))

    def _forget(self, key):
        with self._lock:
            self._accounts.pop(key, None)


loop_watchdog = LoopWatchdog()
