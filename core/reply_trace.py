"""Diagnostic trace of the OwO replies and echoes each account sees.

It is turned on by `debug.replyTrace` in config/global_settings.json and only writes
a file. It never changes what is sent, queued or waited for. Lines go to
logs/reply_trace_<run start>.txt, written by one background thread.

Lines:
  START     once per account, when its farm channel is known: also its loop thread and start position
  EVENT     ready, resumed and disconnect of an account, and kind=blocked when its loop stops running
            (the stack is sampled by core/loop_watchdog.py)
  MSG       each message in a farm channel from OwO or from the account itself,
            as discord.py dispatched it to the cogs' on_message listeners (src=evt)
  SENDFAIL  a text send dropped by a connection error (send() returns quietly)
  SUMMARY   once a minute per account, counts for that minute

handler= and name= use the same checks as the cogs (see _handler_for), so they show
what the cogs would accept from the message.
"""

import asyncio
import atexit
import logging
import math
import os
import queue
import re
import threading
from collections import Counter
from datetime import datetime

from utils import log_paths
from utils.name_match import normalize, template_in_text

OWO_BOT_ID = 408785106942164992
SUMMARY_SECONDS = 60
TEXT_LIMIT = 100
STACK_LIMIT = 1200  # the stack of a blocked account loop, see core/loop_watchdog.py
MB = 1024 * 1024
# Kept free at the end of the file, so the "stopped" line always fits
CAP_RESERVE = 512
# Library loggers whose warnings are copied into the trace, see GatewayLogCapture
GATEWAY_LOGGERS = ("discord.gateway", "discord.client")

_WEBHOOK = re.compile(
    r"https?://[\w.-]*discord(?:app)?\.com/api/(?:v\d+/)?webhooks/\d+/[\w-]+"
)
_MFA_TOKEN = re.compile(r"mfa\.[\w-]{20,}")
_TOKEN = re.compile(r"[\w-]{24,}\.[\w-]{6,}\.[\w-]{20,}")
_LONG_RUN = re.compile(r"[\w-]{40,}")
_MENTION = re.compile(r"<@!?(\d+)>")

logger = logging.getLogger(__name__)


def redact(text):
    """Removes webhook URLs and token-looking strings, the trace never needs them"""
    text = _WEBHOOK.sub("[webhook]", text)
    text = _MFA_TOKEN.sub("[token]", text)
    text = _TOKEN.sub("[token]", text)
    return _LONG_RUN.sub("[token]", text)


def _flat(text, limit=TEXT_LIMIT):
    """One short line: whitespace collapsed, secrets removed, quotes made safe"""
    text = redact(" ".join(str(text or "").split()))
    return text[:limit].replace('"', "'")


def _now():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def _field(obj, name):
    """Reads a field from a discord.py object or from a dict of the gateway payload"""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _as_list(value):
    return list(value) if isinstance(value, (list, tuple)) else []


def _author_kind(client, author_id):
    if author_id is None:
        return None
    if str(author_id) == str(OWO_BOT_ID):
        return "OWO"
    user_id = getattr(getattr(client, "user", None), "id", None)
    if user_id is not None and str(author_id) == str(user_id):
        return "SELF"
    return None


def _embed_text(embeds):
    parts = []
    for embed in _as_list(embeds):
        author = _field(embed, "author")
        footer = _field(embed, "footer")
        for value in (
            _field(embed, "title"),
            _field(embed, "description"),
            _field(author, "name"),
            _field(footer, "text"),
        ):
            if value:
                parts.append(str(value))
    return " | ".join(parts)


def _component_text(components):
    parts = []
    pending = _as_list(components)
    while pending:
        item = pending.pop(0)
        label = _field(item, "label")
        if label:
            parts.append(str(label))
        pending.extend(
            _as_list(_field(item, "children")) or _as_list(_field(item, "components"))
        )
    return " | ".join(parts)


def _message_text(message):
    """Content followed by component text, as gems.py reads it"""
    return "\n".join(
        [message.content or "", _component_text(getattr(message, "components", None))]
    )


def _name_ok(client, message):
    """The name check hunt.py and others.py make before they read a message"""
    try:
        return bool(client.is_for_us(message))
    except Exception:
        return False


def _battle_match(client, message):
    """The name check battle.py makes on the embed author"""
    try:
        for embed in _as_list(getattr(message, "embeds", None)):
            author = _field(_field(embed, "author"), "name")
            if author and client.name_phrase_in(
                author, "{name} goes into battle!", message
            ):
                return True
    except Exception:
        return False
    return False


def _handler_for(client, message, content, name_ok):
    """The first cog rule that would take this OwO message, in the order the cogs run them"""
    lowered = content.lower()
    if name_ok and ("you found:" in lowered or "caught" in lowered):
        return "hunt"  # hunt.py:157 and 180-182
    if _battle_match(client, message):
        return "battle"  # battle.py:79-89
    if name_ok and "you currently have **__" in content:
        return "cash"  # others.py:132 and 136
    if name_ok and "'s Inventory" in _message_text(message):
        return "gems"  # gems.py:425-426 and is_inventory_text (gems.py:130-131)
    return "none"


def _reference_id(message):
    resolved = getattr(getattr(message, "reference", None), "resolved", None)
    return getattr(getattr(resolved, "author", None), "id", None)


def _targets(text, reference_id, names, ids):
    """Labels of our accounts the message is about: by name, by mention or by reply"""
    found = []
    normalized = normalize(text)
    for label, needles in names:
        # The plain substring test first, the exact name test only when that passes
        if label not in found and any(
            needle in normalized and template_in_text(text, "{name}", name)
            for needle, name in needles.items()
        ):
            found.append(label)
    mentioned = [ids.get(user_id) for user_id in _MENTION.findall(text)]
    if reference_id is not None:
        mentioned.append(ids.get(str(reference_id)))
    for label in mentioned:
        if label and label not in found:
            found.append(label)
    return ",".join(found) or "-"


def _mentions(text, ids):
    """Our accounts are shown by label, any other user only as 'user'"""
    return _MENTION.sub(lambda match: "@" + ids.get(match.group(1), "user"), text)


def _subscribed(client, guild):
    manager = getattr(getattr(client, "_connection", None), "subscriptions", None)
    if manager is None or guild is None:
        return "unknown"
    try:
        return "true" if manager.is_subscribed(guild) else "false"
    except Exception:
        return "unknown"


def _me_cached(guild):
    if guild is None:
        return "unknown"
    try:
        return "true" if guild.me is not None else "false"
    except Exception:
        return "unknown"


def _large(guild):
    if guild is None:
        return "unknown"
    try:
        return str(bool(guild.large))
    except Exception:
        return "unknown"


def _members(guild):
    count = getattr(guild, "member_count", None)
    return str(count) if isinstance(count, int) else "unknown"


def _close_code_text(code):
    return "none" if code is None else str(code)


def _latency_ms(client):
    """Heartbeat round trip of the gateway in ms, -1 until the first heartbeat is acknowledged"""
    latency = getattr(client, "latency", None)
    if not isinstance(latency, (int, float)) or not math.isfinite(latency):
        return -1
    return int(latency * 1000)


async def sample_loop_lag(seconds):
    """Largest lateness in ms of a 1 s asyncio.sleep over `seconds`. A blocked loop shows up as lateness"""
    loop = asyncio.get_running_loop()
    worst = 0.0
    for _ in range(max(1, int(seconds))):
        started = loop.time()
        await asyncio.sleep(1.0)
        worst = max(worst, loop.time() - started - 1.0)
    return int(worst * 1000)


class GatewayLogCapture(logging.Handler):
    """Copies discord.gateway and discord.client warnings into the trace as EVENT kind=libwarn.

    Errors still reach the console through the `discord` logger, once: this handler does not send them on again.
    """

    def emit(self, record):
        try:
            reply_trace.libwarn(record)
        except Exception:
            self.handleError(record)


class ReplyTrace:
    def __init__(self):
        self.enabled = False
        self.max_bytes = 20 * MB
        self.path = None
        # One lock for every thread: the counters, the account registry and the file state
        self._lock = threading.Lock()
        self._queue = queue.Queue()
        self._thread = None
        self._handle = None
        self._written = 0
        self._capped = False
        self._labels = {}  # account key -> label shown in the trace
        self._names = {}  # account key -> names OwO may use for that account
        self._ids = {}  # user id of one of our accounts -> label, no other user is stored
        self._counts = {}  # account key -> Counter for the current minute
        self._thread_labels = {}  # thread id -> account label, names the library warnings
        self._capture = None  # GatewayLogCapture while this run is traced

    def configure(self, enabled, max_mb):
        self.enabled = bool(enabled)
        self.max_bytes = max(1, int(float(max_mb) * MB))

    def configure_from_settings(self, settings):
        debug = getattr(settings, "debug", None)
        self.configure(
            getattr(debug, "replyTrace", True), getattr(debug, "replyTraceMaxMB", 20)
        )

    def active(self):
        return self.enabled and self._thread is not None

    def start(self, log_dir=None):
        """Opens this run's file and starts the writer. Does nothing while the trace is off. Without a folder the file
        goes to the bot's log folder, read now (utils/log_paths.py)"""
        with self._lock:
            if not self.enabled or self._thread is not None:
                return self.path
            try:
                if log_dir is None:
                    log_dir = log_paths.log_dir()
                os.makedirs(log_dir, exist_ok=True)
                stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                self.path = os.path.join(log_dir, f"reply_trace_{stamp}.txt")
                self._handle = open(self.path, "ab")
            except OSError:
                # The bot must still start, the trace just stays off
                logger.exception("Reply trace could not open its file, the trace stays off")
                self.enabled = False
                return None
            self._written = self._handle.tell()
            self._capped = False
            self._thread = threading.Thread(
                target=self._write_loop, name="reply-trace", daemon=True
            )
            self._thread.start()
        atexit.register(self.stop)
        self._install_capture()
        return self.path

    def stop(self):
        """Writes what is queued and closes the file. Runs at exit and in the tests"""
        self._remove_capture()
        with self._lock:
            thread = self._thread
            self._thread = None  # lines emitted from now on are dropped
        if thread is None:
            return
        self._queue.put(None)
        thread.join(timeout=10.0)
        with self._lock:
            handle, self._handle = self._handle, None
        if handle is not None:
            handle.close()

    def flush(self):
        """Waits until every line queued so far is in the file"""
        self._queue.join()

    def _emit(self, line):
        if self._thread is not None:
            self._queue.put(line)

    def _write_loop(self):
        while True:
            line = self._queue.get()
            try:
                if line is None:
                    return
                self._write(line)
            except Exception:
                logger.exception("Reply trace could not write a line")
            finally:
                self._queue.task_done()

    def _write(self, line):
        data = (line + "\n").encode("utf-8")
        if self._capped or self._written + len(data) > self.max_bytes - CAP_RESERVE:
            if not self._capped:
                self._capped = True
                notice = (
                    f"TRACE STOPPED time={_now()} "
                    f"reason=size cap {self.max_bytes / MB:g}MB reached\n"
                ).encode("utf-8")
                self._handle.write(notice)
                self._written += len(notice)
                self._handle.flush()
            return
        self._handle.write(data)
        self._written += len(data)
        self._handle.flush()

    def _key(self, client):
        index = getattr(client, "account_index", None)
        return index if index is not None else id(client)

    def _label(self, client):
        return getattr(client, "username", None) or (
            f"account #{getattr(client, 'account_index', '?')}"
        )

    def _snapshot(self):
        with self._lock:
            names = [
                (self._labels.get(key), dict(needles))
                for key, needles in self._names.items()
            ]
            return names, dict(self._ids)

    def _count(self, client, name):
        if not self.active():
            return
        with self._lock:
            self._counts.setdefault(self._key(client), Counter())[name] += 1

    def account_started(self, client):
        """Called once the farm channel is known: registers the account and writes its START line"""
        if not self.active():
            return
        try:
            key = self._key(client)
            label = self._label(client)
            user = getattr(client, "user", None)
            names = {
                normalize(name): name
                for name in (
                    getattr(user, "name", None),
                    getattr(user, "display_name", None),
                )
                if name
            }
            names.pop("", None)
            with self._lock:
                self._labels[key] = label
                self._names[key] = names
                if getattr(user, "id", None) is not None:
                    self._ids[str(user.id)] = label
                self._counts.setdefault(key, Counter())
            cm = getattr(client, "cm", None)
            guild = getattr(cm, "guild", None)
            self._emit(
                f"START time={_now()} account={label} "
                f"channel={getattr(cm, 'id', 'unknown')} "
                f"guild={getattr(guild, 'id', 'unknown')} large={_large(guild)} "
                f"members={_members(guild)} subscribed={_subscribed(client, guild)} "
                f"me_cached={_me_cached(guild)} "
                f"thread={getattr(client, 'loop_thread_id', 'unknown')} "
                f"position={getattr(client, 'account_index', 'unknown')}"
            )
        except Exception:
            logger.exception("Reply trace could not record an account start")

    def event(self, client, kind):
        """ready, resumed or disconnect of this account"""
        if not self.active():
            return
        try:
            guild = getattr(getattr(client, "cm", None), "guild", None)
            self._emit(
                f"EVENT time={_now()} account={self._label(client)} kind={kind} "
                f"subscribed={_subscribed(client, guild)}"
            )
        except Exception:
            logger.exception("Reply trace could not record an event")

    def blocked(self, client, for_seconds, stack):
        """The loop of this account has not run for `for_seconds`. `stack` is its thread's stack at that time, see core/loop_watchdog.py"""
        if not self.active():
            return
        try:
            self._emit(
                f"EVENT time={_now()} account={self._label(client)} kind=blocked "
                f'for_s={int(for_seconds)} stack="{_flat(stack, STACK_LIMIT)}"'
            )
        except Exception:
            logger.exception("Reply trace could not record a blocked loop")

    def bind_thread(self, client):
        """Remembers which account this thread runs, so a library warning from it can name the account"""
        with self._lock:
            self._thread_labels[threading.get_ident()] = self._label(client)

    def close_event(self, client, *, code, reason, invalid_session, resumable, gov_wait_ms):
        """A dropped gateway connection: close code, reason, op 9, whether RESUME is allowed, governor wait"""
        if not self.active():
            return
        try:
            guild = getattr(getattr(client, "cm", None), "guild", None)
            self._emit(
                f"EVENT time={_now()} account={self._label(client)} kind=close "
                f'code={_close_code_text(code)} reason="{_flat(reason)}" '
                f"invalid_session={str(bool(invalid_session)).lower()} "
                f"resumable={str(bool(resumable)).lower()} gov_wait_ms={gov_wait_ms} "
                f"subscribed={_subscribed(client, guild)}"
            )
        except Exception:
            logger.exception("Reply trace could not record a close")

    def attempt(self, client, kind, gov_wait_ms):
        """A gateway connect attempt, resume_try or identify_try, with its wait in the governor"""
        if not self.active():
            return
        try:
            self._emit(
                f"EVENT time={_now()} account={self._label(client)} kind={kind} "
                f"gov_wait_ms={gov_wait_ms}"
            )
        except Exception:
            logger.exception("Reply trace could not record an attempt")

    def hist_fetch(self, client, found, handled):
        """A history read after a sent command: OwO messages it held, and how many reached the cogs"""
        if not self.active():
            return
        try:
            self._emit(
                f"EVENT time={_now()} account={self._label(client)} kind=hist_fetch "
                f"found={found} handled={handled}"
            )
        except Exception:
            logger.exception("Reply trace could not record a history read")

    def giveup(self, client, cmd_name):
        """A command that got no OwO reply in time: given up. Only hunt and battle are queued again, see ReplyFallback.give_up"""
        if not self.active():
            return
        try:
            self._emit(
                f"EVENT time={_now()} account={self._label(client)} kind=giveup "
                f"cmd={_flat(cmd_name, 40)}"
            )
        except Exception:
            logger.exception("Reply trace could not record a give-up")

    def dm_check(self, client, found, captcha, passed=0):
        """A read of the OwO DM channel while the gateway is down: OwO messages it held, how many of them
        were handed on, and whether one raised a captcha"""
        if not self.active():
            return
        try:
            self._emit(
                f"EVENT time={_now()} account={self._label(client)} kind=dm_check "
                f"found={found} passed={passed} captcha={str(bool(captcha)).lower()}"
            )
        except Exception:
            logger.exception("Reply trace could not record a DM check")

    def libwarn(self, record):
        """A warning from discord.gateway or discord.client, redacted and cut to 200 characters"""
        if not self.active() or record.levelno < logging.WARNING:
            return
        try:
            with self._lock:
                label = self._thread_labels.get(record.thread, "-")
            self._emit(
                f"EVENT time={_now()} account={label} kind=libwarn "
                f"logger={record.name} level={record.levelname} "
                f'text="{_flat(record.getMessage(), 200)}"'
            )
        except Exception:
            logger.exception("Reply trace could not record a library warning")

    def _install_capture(self):
        """Routes the gateway loggers' warnings into this trace. Once per run"""
        if self._capture is not None:
            return
        self._capture = GatewayLogCapture()
        for name in GATEWAY_LOGGERS:
            gateway_logger = logging.getLogger(name)
            gateway_logger.setLevel(logging.WARNING)
            gateway_logger.addHandler(self._capture)

    def _remove_capture(self):
        if self._capture is None:
            return
        for name in GATEWAY_LOGGERS:
            gateway_logger = logging.getLogger(name)
            gateway_logger.removeHandler(self._capture)
            gateway_logger.propagate = True
            gateway_logger.setLevel(logging.NOTSET)
        self._capture = None

    def note_sent(self, client):
        self._count(client, "sent")

    def note_failed(self, client, error):
        if not self.active():
            return
        self._count(client, "failed")
        self._emit(
            f"SENDFAIL time={_now()} account={self._label(client)} "
            f"error={type(error).__name__}"
        )

    def raw_message(self, client, data):
        """A MESSAGE_CREATE from the gateway, counted before discord.py parses it.

        Only counts are kept: raw_owo/raw_self next to owo_seen/self_seen in SUMMARY show
        whether a message was lost before discord.py (gateway) or after it (library).
        """
        if not self.active():
            return
        try:
            cm = getattr(client, "cm", None)
            if cm is None or str(data.get("channel_id")) != str(cm.id):
                return
            kind = _author_kind(client, _field(_field(data, "author"), "id"))
            if kind is not None:
                self._count(client, "raw_owo" if kind == "OWO" else "raw_self")
        except Exception:
            logger.exception("Reply trace could not read a gateway message")

    def on_message(self, client, message):
        """discord.py's message event, the same message the cogs' on_message listeners get"""
        if not self.active():
            return
        try:
            cm = getattr(client, "cm", None)
            channel = getattr(message, "channel", None)
            if cm is None or getattr(channel, "id", None) != cm.id:
                return
            kind = _author_kind(
                client, getattr(getattr(message, "author", None), "id", None)
            )
            if kind is None:
                return
            content = message.content or ""
            names, ids = self._snapshot()
            if kind == "OWO":
                self._count(client, "owo_seen")
                name_ok = _name_ok(client, message)
                handler = _handler_for(client, message, content, name_ok)
                if handler != "none":
                    self._count(client, "owo_matched")
                name = "hit" if name_ok or handler == "battle" else "miss"
                target = _targets(content, _reference_id(message), names, ids)
            else:
                self._count(client, "self_seen")
                handler, name, target = "echo", "n/a", "-"
            self._emit(
                self._msg_line(
                    label=self._label(client),
                    src="evt",
                    message_id=getattr(message, "id", None),
                    kind=kind,
                    content=content,
                    embed=_embed_text(getattr(message, "embeds", None)),
                    comp=_component_text(getattr(message, "components", None)),
                    handler=handler,
                    name=name,
                    target=target,
                    ids=ids,
                )
            )
        except Exception:
            logger.exception("Reply trace could not read a message")

    def _msg_line(
        self, *, label, src, message_id, kind, content, embed, comp, handler, name, target, ids
    ):
        return (
            f"MSG time={_now()} account={label} src={src} id={message_id} "
            f"author={kind} handler={handler} name={name} target={target} "
            f'text="{_flat(_mentions(content, ids))}" '
            f'embed="{_flat(embed)}" comp="{_flat(comp)}"'
        )

    def note_summary_counts(self, client):
        """Returns this minute's counts for the account and starts a new minute"""
        key = self._key(client)
        with self._lock:
            counts = self._counts.get(key, Counter())
            self._counts[key] = Counter()
        return counts

    def summary(self, client, loop_lag_ms=0):
        if not self.active():
            return
        try:
            counts = self.note_summary_counts(client)
            guild = getattr(getattr(client, "cm", None), "guild", None)
            self._emit(
                f"SUMMARY time={_now()} account={self._label(client)} "
                f"window={SUMMARY_SECONDS}s sent={counts['sent']} "
                f"self_seen={counts['self_seen']} owo_seen={counts['owo_seen']} "
                f"owo_matched={counts['owo_matched']} failed={counts['failed']} "
                f"raw_self={counts['raw_self']} raw_owo={counts['raw_owo']} "
                f"subscribed={_subscribed(client, guild)} me_cached={_me_cached(guild)} "
                f"latency_ms={_latency_ms(client)} loop_lag_ms={loop_lag_ms}"
            )
        except Exception:
            logger.exception("Reply trace could not write a summary")

    async def run_summaries(self, client):
        """One SUMMARY line a minute for this account, until it stops or closes"""
        while not getattr(client, "stopped", False) and not client.is_closed():
            loop_lag_ms = await sample_loop_lag(SUMMARY_SECONDS)
            self.summary(client, loop_lag_ms=loop_lag_ms)


reply_trace = ReplyTrace()
