"""Gateway volume per account, written to logs/gateway_stats_<run timestamp>.txt.

One READY line each time an account is ready, and one STATS line a minute per account: its frames and bytes,
and the largest event types by bytes. Frames are only measured here. The event type is read from the first
300 characters with a regex and the length of the frame is taken, so nothing is parsed. No message text, user
id of another person or token is written, only guild ids and numbers.
"""

import atexit
import logging
import re
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from utils import log_paths

STATS_SECONDS = 60
MAX_QUEUED_LINES = 10000
TOP_TYPES = 8
TOP_GUILDS = 5
BIG_GUILD_MEMBERS = 1000
HEAD_CHARS = 300

_TYPE = re.compile(r'"t":"([A-Z_]+)"')
_OP = re.compile(r'"op":(\d+)')
# Inbound per server (see GuildWindow): the event types counted by server, the server id of a frame, the window
GUILD_EVENTS = ("MESSAGE_CREATE", "MESSAGE_UPDATE", "TYPING_START", "PRESENCE_UPDATE")
_GUILD_ID = re.compile(r'"guild_id":"(\d{1,20})"')
WINDOW_MINUTES = 10
MAX_GUILDS = 50
GUILDS_LIVE_TOP = 10

_log = logging.getLogger(__name__)


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _label(client):
    return getattr(client, "username", None) or f"account #{getattr(client, 'account_index', '?')}"


class GuildWindow:
    """Bytes and events per server over the last WINDOW_MINUTES minutes, kept one bucket per minute.

    At most MAX_GUILDS servers are kept per account: when a new one does not fit, the one with the fewest bytes is
    dropped. Numbers and server ids only, never message content.
    """

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._started = clock()
        self._buckets = deque()  # (minute, {guild_id: [bytes, events]}), oldest first
        self._totals = {}  # guild_id -> [bytes, events] over the buckets kept

    def add(self, guild_id, size, now=None):
        now = self._clock() if now is None else now
        minute = int(now // 60)
        with self._lock:
            self._expire(minute)
            if guild_id not in self._totals and len(self._totals) >= MAX_GUILDS:
                self._drop(min(self._totals, key=lambda known: self._totals[known][0]))
            if not self._buckets or self._buckets[-1][0] != minute:
                self._buckets.append((minute, {}))
            entry = self._buckets[-1][1].setdefault(guild_id, [0, 0])
            entry[0] += size
            entry[1] += 1
            total = self._totals.setdefault(guild_id, [0, 0])
            total[0] += size
            total[1] += 1

    def top(self, count=GUILDS_LIVE_TOP, now=None):
        """The busiest servers of the window: {guild_id, kb_min, events_min}, most bytes first"""
        now = self._clock() if now is None else now
        with self._lock:
            self._expire(int(now // 60))
            span = min(float(WINDOW_MINUTES), max(1.0, (now - self._started) / 60.0))
            ranked = sorted(self._totals.items(), key=lambda item: item[1][0], reverse=True)[:count]
        return [
            {"guild_id": guild_id, "kb_min": round(size / 1024.0 / span, 2), "events_min": round(events / span, 1)}
            for guild_id, (size, events) in ranked
        ]

    def _expire(self, minute):
        while self._buckets and self._buckets[0][0] <= minute - WINDOW_MINUTES:
            _, old = self._buckets.popleft()
            for guild_id, (size, events) in old.items():
                total = self._totals.get(guild_id)
                if total is None:
                    continue
                total[0] -= size
                total[1] -= events
                if total[1] <= 0:
                    del self._totals[guild_id]

    def _drop(self, guild_id):
        self._totals.pop(guild_id, None)
        for _, bucket in self._buckets:
            bucket.pop(guild_id, None)


class AccountStats:
    """Frame counters of one account. count() runs on the account's loop, the writer takes them once a minute"""

    def __init__(self, client):
        self.client = client
        self.ready_bytes = -1  # length of the last READY frame, -1 before the first one
        self._lock = threading.Lock()
        self._frames = 0
        self._bytes = 0
        self._types = {}  # label -> [frames, bytes]
        self._guilds = GuildWindow()
        writer.add_account(self)

    def count(self, text):
        """Counts one gateway frame by its event type and length. Parses nothing"""
        head = text[:HEAD_CHARS]
        found = _TYPE.search(head)
        if found:
            label = found.group(1)
        else:
            op = _OP.search(head)
            label = f"OP{op.group(1)}" if op else "UNKNOWN"
        if label in GUILD_EVENTS:
            guild = _GUILD_ID.search(text)
            if guild is not None:
                self._guilds.add(guild.group(1), len(text))
        if not writer.enabled:
            return
        size = len(text)
        with self._lock:
            self._frames += 1
            self._bytes += size
            entry = self._types.get(label)
            if entry is None:
                self._types[label] = [1, size]
            else:
                entry[0] += 1
                entry[1] += size
        if label == "READY":
            self.ready_bytes = size

    def take(self):
        """Returns the counters of the minute that ends now, and starts a new minute"""
        with self._lock:
            frames, size, types = self._frames, self._bytes, self._types
            self._frames, self._bytes, self._types = 0, 0, {}
        return frames, size, types

    def top_guilds(self, count=GUILDS_LIVE_TOP):
        """The busiest servers of the last WINDOW_MINUTES minutes, for the dashboard: ids and numbers only"""
        return self._guilds.top(count)


def stats_line(account, now_text):
    frames, size, types = account.take()
    ranked = sorted(types.items(), key=lambda item: item[1][1], reverse=True)[:TOP_TYPES]
    top = ",".join(f"{label}:{count}/{total / 1024:.1f}" for label, (count, total) in ranked)
    return (
        f"STATS time={now_text} account={_label(account.client)} msgs={frames} bytes={size} "
        f'top_types="{top}"'
    )


def _member_count(guild):
    # This library keeps the count as _member_count, and approximate_member_count when that is missing
    count = getattr(guild, "_member_count", None)
    if count is None:
        count = getattr(guild, "approximate_member_count", None)
    return count if isinstance(count, int) else None


def ready_line(account, now_text):
    client = account.client
    guilds = list(client.guilds)
    known = []
    for guild in guilds:
        count = _member_count(guild)
        if count is not None:
            known.append((guild.id, count))
    # -1 when no guild has a count; with some counts known, members_total is their sum
    members_total = sum(count for _, count in known) if known else -1
    big = sum(1 for _, count in known if count >= BIG_GUILD_MEMBERS)
    top = sorted(known, key=lambda item: item[1], reverse=True)[:TOP_GUILDS]
    top5 = ",".join(f"{guild_id}:{count}" for guild_id, count in top)
    started = getattr(client, "identify_started_at", None)
    identify_to_ready = round(time.monotonic() - started, 1) if started is not None else -1
    return (
        f"READY time={now_text} account={_label(client)} position={client.account_index} "
        f"ready_bytes={account.ready_bytes} guilds={len(guilds)} members_total={members_total} "
        f'big_guilds={big} top5="{top5}" private_channels={len(client.private_channels)} '
        f"relationships={len(client.relationships)} identify_to_ready_s={identify_to_ready}"
    )


def record_ready(account):
    """Queues the READY line of this account. Called on the account's loop when discord.py dispatches ready"""
    if not writer.enabled:
        return
    try:
        writer.enqueue(ready_line(account, _now()))
    except Exception:
        _log.exception("Gateway stats could not write a READY line")


class StatsWriter:
    """One background thread writes the lines. Lines are queued without waiting, and dropped when the queue is full"""

    def __init__(self):
        self.enabled = False
        self.path = None
        self._accounts = []
        self._accounts_lock = threading.Lock()
        self._lines = deque()
        self._queue_lock = threading.Lock()  # held for a few microseconds, never while a line is written
        self._dropped = 0
        self._wake = threading.Event()
        self._io_lock = threading.Lock()
        self._handle = None
        self._thread = None

    def configure_from_settings(self, settings):
        debug = getattr(settings, "debug", None)
        self.enabled = bool(getattr(debug, "gatewayStats", True))

    def add_account(self, account):
        with self._accounts_lock:
            self._accounts.append(account)

    def start(self, folder=None):
        """Opens this run's file and starts the writer thread. Does nothing while gatewayStats is off. Without a folder
        the file goes to the bot's log folder, read now (utils/log_paths.py)"""
        if not self.enabled or self._thread is not None:
            return self.path
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path = (log_paths.log_dir() if folder is None else Path(folder)) / f"gateway_stats_{stamp}.txt"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = open(path, "a", encoding="utf-8")
        except OSError:
            # The bot must still start, the stats just stay off
            _log.exception("Gateway stats could not open its file, the stats stay off")
            self.enabled = False
            return None
        self.path = path
        atexit.register(self.drain)
        self._thread = threading.Thread(target=self._run, name="gateway-stats-writer", daemon=True)
        self._thread.start()
        return path

    def enqueue(self, line):
        """Queues one line. Returns False, and counts the line as dropped, when the queue is full"""
        with self._queue_lock:
            if len(self._lines) >= MAX_QUEUED_LINES:
                self._dropped += 1
                return False
            self._lines.append(line)
        self._wake.set()
        return True

    def drain(self):
        """Writes every queued line to the file. The writer thread calls it, and so do tests and exit"""
        if self._handle is None:
            return  # not started: the lines wait in the queue
        with self._queue_lock:
            lines, self._lines = self._lines, deque()
            dropped, self._dropped = self._dropped, 0
        if dropped:
            lines.append(f"gateway stats: {dropped} lines dropped, the queue was full time={_now()}")
        if not lines:
            return
        with self._io_lock:
            self._handle.write("".join(f"{line}\n" for line in lines))
            self._handle.flush()

    def _queue_stats(self, now_text):
        with self._accounts_lock:
            accounts = list(self._accounts)
        for account in accounts:
            if account.client.is_closed():
                continue
            self.enqueue(stats_line(account, now_text))

    def _run(self):
        next_stats = time.monotonic() + STATS_SECONDS
        while True:
            self._wake.wait(timeout=1.0)
            self._wake.clear()  # cleared before the queue is taken: a line queued after this sets it again
            try:
                now = time.monotonic()
                if now >= next_stats:
                    next_stats = now + STATS_SECONDS
                    self._queue_stats(_now())
                self.drain()
            except Exception:  # the writer must keep going
                _log.exception("Gateway stats writer failed")


writer = StatsWriter()
