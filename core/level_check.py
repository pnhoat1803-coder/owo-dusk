"""Low-frequency level check: every account sends `owolvl` now and then and reads its level from the image card.

The scheduler (LevelChecker) knows nothing about Discord. It talks to a host object (BotHost for the real bot, a fake in
the tests) and gets its clock, sleep, random source, store, rate bucket and OCR runner injected.

Rules it keeps
  * the first check of an account is staggered at random; a check that was done lately (levels.sqlite) is not repeated
  * never while the account sleeps, is paused, has a captcha pending, a captcha wave runs, or it is stopping
  * at most `maxChecksPerMinuteAllAccounts` level commands per minute for the whole process (RateBucket, one tiny lock)
  * the command goes through the normal queue (put_queue -> send_commands), no fast path
  * at most one level check waits for a card in one channel at a time (the others wait for their turn)
  * the card is looked for in every form OwO may send it (attachment, embed image, Components V2 media; new message, edit,
    channel history) and DOWNLOADED right away into utils/data/level_queue/; one process-wide worker thread reads the files
    later (OCR, behind a process-wide Semaphore(2)) and deletes each file when it is done, success or not
  * a failed check is retried once after 30-45 minutes, then the normal interval; a level that went down is not stored
  * levelCheck.debug writes one JSON line per check to utils/data/level_debug.jsonl: what OwO really sent (nothing private, no URL)
"""

import asyncio
import contextlib
import functools
import json
import os
import random
import re
import threading
import time
from collections import deque
from pathlib import Path

from core.level_ocr import describe_tesseract, find_tesseract, ocr_available, read_level_card
from core.level_store import LevelStore, save_failed_card
from utils import log_paths

COMMAND_ID = "levelcheck"
LOG_COLOR = "#939393"
BLOCK_RETRY_SECONDS = 600.0  # an account that sleeps, is paused or has a captcha is asked again in 10 minutes
BUCKET_RETRY = (60.0, 120.0)
FAIL_RETRY = (1800.0, 2700.0)  # one retry 30-45 minutes after a failed check
CAPTCHA_WAVE_ACCOUNTS = 2  # this many accounts with a captcha pending at once is a wave
SENT_WAIT_SECONDS = 600.0  # longest wait for the queue to send the command
MAX_CARD_BYTES = 3_000_000
DOWNLOAD_TIMEOUT = 10.0
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")
COMPONENTS_V2 = 1 << 15  # message flag: the message is made of Components V2 (containers, media galleries, ...)
HISTORY_LIMIT = 15  # messages read after our command when the wait ran out
RAW_FETCH_MAX = 3  # raw re-reads of one OwO message per check (Components V2 messages the library cannot show)
CHANNEL_WAIT_SECONDS = 600.0  # longest wait for the turn in a channel (one level check per channel at a time)
QUEUE_RESULT_WAIT = 600.0  # how long a check waits for the OCR worker to report on its card (then it counts as queued)
QUEUE_DIR = Path("utils") / "data" / "level_queue"
QUEUE_CAP = 300
QUEUE_MAX_AGE = 24 * 3600.0
DEBUG_PATH = log_paths.PROJECT_ROOT / "utils" / "data" / "level_debug.jsonl"  # absolute: never the working folder
DEBUG_MAX_BYTES = 2_000_000
OFF_LINE = (
    "[levelCheck] off: Tesseract OCR not found. Run setup.bat (it puts Tesseract into third_party/tesseract), or install "
    "it (Windows: UB-Mannheim Tesseract installer) or set levelCheck.tesseractPath. Level will show '\u2014' on the dashboard."
)


STORE = LevelStore()


# ---------------------------------------------------------------------------------------------- shared limits
class RateBucket:
    """At most `limit` takes in any 60 seconds, shared by all accounts. The lock covers only the counter"""

    def __init__(self, clock=time.monotonic):
        self._lock = threading.Lock()
        self._stamps = deque()
        self._clock = clock

    def try_take(self, limit):
        now = self._clock()
        with self._lock:
            while self._stamps and now - self._stamps[0] >= 60.0:
                self._stamps.popleft()
            if len(self._stamps) >= max(1, int(limit)):
                return False
            self._stamps.append(now)
            return True


class OcrGate:
    """At most `size` OCR runs at the same time in the whole process (the accounts run in different threads)"""

    def __init__(self, size=2):
        self._semaphore = threading.Semaphore(size)
        self._lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def run(self, reader, data):
        with self._semaphore:
            with self._lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            try:
                return reader(data)
            finally:
                with self._lock:
                    self.active -= 1


BUCKET = RateBucket()
OCR_GATE = OcrGate(2)


async def run_in_thread(function, *args):
    return await asyncio.get_running_loop().run_in_executor(None, functools.partial(function, *args))


async def read_card_async(data, reader=read_level_card, gate=None):
    """OCR in an executor thread behind the process-wide gate. The event loop only awaits"""
    gate = gate or OCR_GATE
    return await asyncio.get_running_loop().run_in_executor(None, functools.partial(gate.run, reader, data))


# ---------------------------------------------------------------------------------------------- process start-up
_startup_lock = threading.Lock()
_startup = {"done": False, "ready": False}


def _say(printer, line):
    try:
        printer(line)
    except UnicodeEncodeError:  # a console that cannot show the dash or a non-ASCII folder name
        printer(line.encode("ascii", "replace").decode("ascii"))


def process_ready(settings, printer=print):
    """Once per process: True when the level check may run. When it is on but Tesseract is missing, one line is
    printed and False is returned, so no level command is ever sent in this run"""
    with _startup_lock:
        if _startup["done"]:
            return _startup["ready"]
        _startup["done"] = True
        if not settings.enabled:
            _startup["ready"] = False
            return False
        available, _reason = ocr_available()
        program = find_tesseract(settings.tesseractPath) if available else None
        if program is None:
            _say(printer, OFF_LINE)
            _startup["ready"] = False
            return False
        _say(printer, describe_tesseract(program))  # which Tesseract is used, once per process
        _startup["ready"] = True
        return True


def reset_process_state():
    """Tests only: forget the once-per-process answer"""
    with _startup_lock:
        _startup["done"] = False
        _startup["ready"] = False
        _startup["marked"] = False


async def mark_active(active, store=None):
    """Once per process: creates levels.sqlite if needed, drops history older than 90 days, and tells the dashboard
    (meta check_active) whether the check runs, so it can say so. The write runs in an executor"""
    with _startup_lock:
        if _startup.get("marked"):
            return
        _startup["marked"] = True
    store = store or STORE

    def write():
        store.init()
        store.set_meta("check_active", "1" if active else "0")
        store.set_meta("check_active_at", str(time.time()))

    try:
        await run_in_thread(write)
    except Exception:
        pass  # the dashboard then simply has no level data


async def await_all_ready():
    from core.send_gate import await_all_ready as wait

    await wait()


# ---------------------------------------------------------------------------------------------- the scheduler
class LevelChecker:
    """One per account. host: account_id, name, settings, stopping(), block_reason(), request_card(), log()"""

    def __init__(self, host, store=None, bucket=None, ocr=None, clock=time.time, sleep=asyncio.sleep, rng=None,
                 save_card=None, call=None):
        self.host = host
        self.store = store or STORE
        self.bucket = bucket or BUCKET
        self.ocr = ocr or read_card_async
        self.clock = clock
        self.sleep = sleep
        self.rng = rng or random.Random()
        self.save_card = save_card or save_failed_card
        self.call = call or run_in_thread  # runs a blocking store function off the event loop (tests inline it)
        self.checks_done = 0
        self.last_error = None

    @property
    def settings(self):
        return self.host.settings

    def interval_seconds(self):
        return float(self.settings.intervalMinutes) * 60.0

    def first_delay(self):
        low = float(self.settings.firstCheckMinDelayMinutes) * 60.0
        high = float(self.settings.firstCheckMaxDelayMinutes) * 60.0
        return self.rng.uniform(min(low, high), max(low, high))

    async def initial_due(self):
        """When the first check is due: from the stored time of the last try, else a random delay (staggered)"""
        now = self.clock()
        try:
            row = await self.call(self.store.get, self.host.account_id)
        except Exception:
            row = None
        if row:
            if row.get("last_ok") == 0 and row.get("last_try_at"):
                due = row["last_try_at"] + self.rng.uniform(*FAIL_RETRY)
            else:
                base = row.get("checked_at") or row.get("last_try_at")
                due = base + self.interval_seconds() if base else None
            if due is not None and due > now:
                return due
        return now + self.first_delay()

    async def run(self):
        failures = 0
        due = await self.initial_due()
        while not self.host.stopping():
            wait = due - self.clock()
            if wait > 0:
                await self.sleep(wait)
            if self.host.stopping():
                return
            if self.block_reason():
                due = self.clock() + BLOCK_RETRY_SECONDS
                continue
            if not self.bucket.try_take(self.settings.maxChecksPerMinuteAllAccounts):
                due = self.clock() + self.rng.uniform(*BUCKET_RETRY)
                continue
            if await self.check_once():
                failures = 0
                due = self.clock() + self.interval_seconds()
            else:
                failures += 1
                if failures == 1:
                    due = self.clock() + self.rng.uniform(*FAIL_RETRY)
                else:
                    failures = 0
                    due = self.clock() + self.interval_seconds()

    def block_reason(self):
        try:
            return self.host.block_reason()
        except Exception:
            return "unknown"

    async def check_once(self):
        """One check. True when a good level was stored. Never raises (except cancellation)"""
        self.checks_done += 1
        card = None
        try:
            card, error = await self.host.request_card()
            if card is None:
                await self._fail(error or "no_image")
                return False
            if isinstance(card, QueuedCard):
                return await self._after_queued(card)
            result = await self.ocr(card)
            if not result.get("ok"):
                await self._fail(result.get("error") or "ocr_failed", card)
                return False
            previous = await self.call(self.store.get, self.host.account_id)
            level = result["level"]
            if previous and previous.get("level") is not None and level < previous["level"]:
                await self._fail("level_decreased", card)
                return False
            rank = result["rank"] if result.get("rank_ok") else None
            await self.call(
                self.store.record_good, self.host.account_id, level, result["xp_cur"], result["xp_need"], rank, self.clock()
            )
            pct = result["xp_cur"] / result["xp_need"] * 100.0
            self.last_error = None
            await self.host.log(f"[levelCheck] {self.host.name}: LVL {level}, XP {result['xp_cur']}/{result['xp_need']} ({pct:.1f}%)")
            return True
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._fail(f"error:{type(error).__name__}", card)
            return False

    async def _after_queued(self, card):
        """The card is on disk and the OCR worker reads it later (it logs the result and stores it). Waits a while for its
        verdict only to keep the retry rule: a failed read is retried once after 30-45 minutes. No verdict yet = queued = ok"""
        future = card.future
        if future is None:
            return True
        try:
            ok, error = await asyncio.wait_for(asyncio.shield(future), timeout=QUEUE_RESULT_WAIT)
        except asyncio.TimeoutError:
            return True
        self.last_error = None if ok else error
        return bool(ok)

    async def _fail(self, error, card=None):
        self.last_error = error
        try:
            await self.call(self.store.record_fail, self.host.account_id, error, self.clock())
        except Exception:
            pass
        if card is not None and getattr(self.settings, "saveFailedCards", False):
            try:
                await self.call(self.save_card, self.host.account_id, card)
            except Exception:
                pass
        note = getattr(self.host, "note", None) if error == "reply_no_image" else None
        suffix = f" - {note}" if isinstance(note, str) and note else ""
        await self.host.log(f"[levelCheck] {self.host.name}: failed ({error}){suffix}")


# ---------------------------------------------------------------------------------------------- the card in every form
class ImageRef:
    """One image that could be the card: an attachment, an embed image/thumbnail, or Components V2 media"""

    __slots__ = ("kind", "url", "filename", "size", "obj")

    def __init__(self, kind, url, filename=None, size=None, obj=None):
        self.kind, self.url, self.filename, self.size, self.obj = kind, url, filename, size, obj

    @property
    def strong(self):
        # An embed thumbnail is often an icon or an avatar: it is only taken when the message is clearly the answer
        return self.kind != "embed_thumbnail"


_KIND_ORDER = {"attachment": 0, "embed_image": 1, "component": 2, "embed_thumbnail": 3}


class MsgView:
    """What the level check needs to know of one OwO message, whether it came as a library Message, as raw gateway
    JSON (an edit) or as raw history JSON. Plain data, so it can be matched, logged and tested without Discord"""

    def __init__(self, message_id):
        self.id = int(message_id)
        self.sources = []  # "event", "edit", "history", "fetch"
        self.seen_s = 0.0  # seconds after our command was sent when it was first seen here
        self.has_reference = False
        self.reference_id = None
        self.addressed = False
        self.flags = 0
        self.content = ""
        self.attachments = []  # (filename, content_type, size)
        self.embeds = []  # (type, has_image, has_thumbnail)
        self.component_types = []
        self.components_raw = False  # True when component_types come from the raw payload, else from the library
        self.parts = {"attachments": [], "embeds": [], "components": []}
        self.needs_raw = False  # Components V2 flag set, no image found, library view only: ask for the raw message

    @property
    def images(self):
        found = []
        for key in ("attachments", "embeds", "components"):
            found.extend(self.parts[key])
        return found


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _flags_value(flags):
    value = getattr(flags, "value", flags)
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0


def _is_image_file(filename, content_type):
    name = (filename or "").lower()
    ctype = (content_type or "").lower()
    return ctype.startswith("image/") or name.endswith(IMAGE_SUFFIXES)


def _name_from_url(url):
    if not isinstance(url, str):
        return None
    return url.split("?", 1)[0].rsplit("/", 1)[-1] or None


def _embed_url(proxy):
    """url of an embed image/thumbnail (library EmbedProxy or raw dict); the Discord media proxy copy is preferred"""
    if proxy is None:
        return None
    if isinstance(proxy, dict):
        url = proxy.get("proxy_url") or proxy.get("url")
    else:
        url = getattr(proxy, "proxy_url", None) or getattr(proxy, "url", None)
    return url if isinstance(url, str) and url else None


def view_from_message(message, addressed=False):
    """A discord.py Message -> MsgView. Only attribute reads"""
    view = MsgView(message.id)
    reference = getattr(message, "reference", None)
    if reference is not None:
        view.has_reference = True
        view.reference_id = _int_or_none(getattr(reference, "message_id", None))
    view.addressed = bool(addressed)
    view.flags = _flags_value(getattr(message, "flags", None))
    content = getattr(message, "content", None)
    view.content = content if isinstance(content, str) else ""
    for attachment in getattr(message, "attachments", None) or []:
        filename = getattr(attachment, "filename", None)
        content_type = getattr(attachment, "content_type", None)
        size = getattr(attachment, "size", None)
        view.attachments.append((filename, content_type, size))
        if _is_image_file(filename, content_type):
            view.parts["attachments"].append(ImageRef("attachment", getattr(attachment, "url", None), filename, size, attachment))
    for embed in getattr(message, "embeds", None) or []:
        etype = getattr(embed, "type", None)
        etype = str(getattr(etype, "value", etype) or "")
        image = _embed_url(getattr(embed, "image", None))
        thumbnail = _embed_url(getattr(embed, "thumbnail", None))
        view.embeds.append((etype, bool(image), bool(thumbnail)))
        if image:
            view.parts["embeds"].append(ImageRef("embed_image", image, _name_from_url(image)))
        if thumbnail:
            view.parts["embeds"].append(ImageRef("embed_thumbnail", thumbnail, _name_from_url(thumbnail)))
    types = []
    for component in getattr(message, "components", None) or []:
        ctype = getattr(component, "type", None)
        ctype = getattr(ctype, "value", ctype)
        types.append(ctype if isinstance(ctype, int) else None)
    view.component_types = types
    view.components_raw = False
    view.needs_raw = bool(view.flags & COMPONENTS_V2) and not any(i.strong for i in view.images)
    return view


def _walk_media(node, found, depth=0):
    """Every {"media": {...}} / {"file": {...}} of a (nested) Components V2 tree: (component type, media dict)"""
    if depth > 10:
        return
    if isinstance(node, dict):
        for key in ("media", "file"):
            media = node.get(key)
            if isinstance(media, dict):
                found.append((node.get("type"), media))
        for value in node.values():
            if isinstance(value, (dict, list)):
                _walk_media(value, found, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _walk_media(item, found, depth + 1)


def view_from_raw(data, our_id=None, previous=None):
    """Raw message JSON (gateway edit, history, GET message) -> MsgView. With `previous` (a partial update) only the keys
    that are present replace what was known"""
    view = previous if previous is not None else MsgView(data["id"])
    if "message_reference" in data:
        reference = data.get("message_reference")
        view.has_reference = isinstance(reference, dict)
        view.reference_id = _int_or_none(reference.get("message_id")) if isinstance(reference, dict) else None
    if "flags" in data:
        view.flags = _flags_value(data.get("flags"))
    if isinstance(data.get("content"), str):
        view.content = data["content"]
    if our_id is not None:
        mark = (f"<@{our_id}>", f"<@!{our_id}>")
        if any(item in view.content for item in mark):
            view.addressed = True
        for mention in data.get("mentions") or []:
            if isinstance(mention, dict) and _int_or_none(mention.get("id")) == int(our_id):
                view.addressed = True
        replied = data.get("referenced_message")
        if isinstance(replied, dict) and _int_or_none((replied.get("author") or {}).get("id")) == int(our_id):
            view.addressed = True
    by_name = {}
    if "attachments" in data:
        view.attachments, view.parts["attachments"] = [], []
        for item in data.get("attachments") or []:
            if not isinstance(item, dict):
                continue
            filename, content_type, size = item.get("filename"), item.get("content_type"), item.get("size")
            url = item.get("url") or item.get("proxy_url")
            view.attachments.append((filename, content_type, size))
            if filename and url:
                by_name[filename] = url
            if _is_image_file(filename, content_type):
                view.parts["attachments"].append(ImageRef("attachment", url, filename, size))
    else:
        for ref in view.parts["attachments"]:
            if ref.filename and ref.url:
                by_name[ref.filename] = ref.url
    if "embeds" in data:
        view.embeds, view.parts["embeds"] = [], []
        for item in data.get("embeds") or []:
            if not isinstance(item, dict):
                continue
            image, thumbnail = _embed_url(item.get("image")), _embed_url(item.get("thumbnail"))
            view.embeds.append((str(item.get("type") or ""), bool(image), bool(thumbnail)))
            if image:
                view.parts["embeds"].append(ImageRef("embed_image", image, _name_from_url(image)))
            if thumbnail:
                view.parts["embeds"].append(ImageRef("embed_thumbnail", thumbnail, _name_from_url(thumbnail)))
    if "components" in data:
        components = data.get("components") or []
        view.component_types = [item.get("type") if isinstance(item, dict) else None for item in components]
        view.components_raw = True
        view.parts["components"] = []
        media = []
        _walk_media(components, media)
        known = {ref.url for ref in view.parts["attachments"]}
        for ctype, item in media:
            raw_url = item.get("url") if isinstance(item.get("url"), str) else ""
            if raw_url.startswith("attachment://"):
                url = by_name.get(raw_url[len("attachment://"):])
                filename = raw_url[len("attachment://"):]
            elif raw_url.startswith("https://"):
                url = item.get("proxy_url") if isinstance(item.get("proxy_url"), str) else raw_url
                filename = _name_from_url(raw_url)
            else:
                continue
            if not url or url in known:
                continue
            content_type = item.get("content_type")
            if ctype in (11, 12):  # thumbnail, media gallery item: an image unless it says it is something else
                ok = not content_type or str(content_type).lower().startswith("image/") or _is_image_file(filename, None)
            else:
                ok = _is_image_file(filename, content_type)
            if ok:
                known.add(url)
                view.parts["components"].append(ImageRef("component", url, filename, item.get("size")))
    view.needs_raw = False
    return view


def best_image(view):
    images = view.images
    if not images:
        return None
    return sorted(images, key=lambda ref: _KIND_ORDER.get(ref.kind, 9))[0]


def pick_view(views, sent_id, others_waiting):
    """The message that answers our command: a reply to it, else one addressed to us, else (when no other level check waits
    in this channel) the first message with a strong image and no reply reference to somebody else's message"""
    newer = sorted((v for v in views if sent_id is not None and v.id > sent_id and v.images), key=lambda v: v.id)
    for view in newer:
        if view.has_reference and view.reference_id == sent_id:
            return view
    for view in newer:
        if view.addressed:
            return view
    if not others_waiting:
        for view in newer:
            if not view.has_reference and any(ref.strong for ref in view.images):
                return view
    return None


def card_extension(filename, data):
    head = bytes(data[:16])
    if head.startswith(b"\x89PNG"):
        return "png"
    if head.startswith(b"\xff\xd8"):
        return "jpg"
    if head.startswith(b"GIF8"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    name = (filename or "").lower()
    for suffix in IMAGE_SUFFIXES:
        if name.endswith(suffix):
            return suffix[1:]
    return "png"


# ---------------------------------------------------------------------------------------------- the card queue
class QueuedCard:
    """What request_card returns once the card image is on disk. `future` gets (ok, error) when the worker has read it"""

    def __init__(self, account_id, message_id, path=None, future=None, queued_at=None):
        self.account_id, self.message_id, self.path = account_id, message_id, path
        self.future = future
        self.queued_at = queued_at


class QueueItem:
    def __init__(self, path, meta_path, account_id, message_id, name, received_at):
        self.path, self.meta_path = path, meta_path
        self.account_id, self.message_id, self.name, self.received_at = account_id, message_id, name, received_at


def _safe_part(text):
    return "".join(ch for ch in str(text) if ch.isalnum())[:30] or "x"


def _write_atomic(path, data):
    temp = path.with_name(path.name + ".tmp")
    with open(temp, "wb") as handle:
        handle.write(data)
    os.replace(temp, path)


class LevelQueue:
    """utils/data/level_queue/<account>_<message>.<ext> + <account>_<message>.json (account id, name, message id, time).
    The image is written last and atomically (.tmp, os.replace), so a half-written card is never listed"""

    def __init__(self, folder=QUEUE_DIR, cap=QUEUE_CAP, max_age=QUEUE_MAX_AGE, clock=time.time):
        self.folder = Path(folder)
        self.cap = cap
        self.max_age = max_age
        self.clock = clock

    def _read_meta(self, meta_path):
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def items(self):
        """Oldest first"""
        found = []
        try:
            entries = list(self.folder.iterdir())
        except OSError:
            return found
        for path in entries:
            if path.suffix.lower() not in IMAGE_SUFFIXES or not path.is_file():
                continue
            meta_path = path.with_suffix(".json")
            meta = self._read_meta(meta_path)
            account_id, _, rest = path.stem.partition("_")
            try:
                received = float(meta.get("received_at"))
            except (TypeError, ValueError):
                try:
                    received = path.stat().st_mtime
                except OSError:
                    continue
            found.append(
                QueueItem(path, meta_path, str(meta.get("account_id") or account_id), _int_or_none(meta.get("message_id") or rest),
                          str(meta.get("name") or account_id), received)
            )
        found.sort(key=lambda item: (item.received_at, item.path.name))
        return found

    def add(self, account_id, name, message_id, data, ext="png", received_at=None):
        """Writes the card and its sidecar. Returns (path, dropped): `dropped` oldest cards were removed to stay under the cap"""
        self.folder.mkdir(parents=True, exist_ok=True)
        dropped = 0
        items = self.items()
        while len(items) >= self.cap > 0:
            self.remove(items.pop(0))
            dropped += 1
        stem = f"{_safe_part(account_id)}_{int(message_id)}"
        ext = ext if ext in ("png", "jpg", "jpeg", "gif", "webp") else "png"
        path = self.folder / f"{stem}.{ext}"
        meta = {
            "account_id": str(account_id), "name": str(name), "message_id": int(message_id),
            "received_at": float(self.clock() if received_at is None else received_at),
        }
        _write_atomic(self.folder / f"{stem}.json", json.dumps(meta).encode("utf-8"))
        _write_atomic(path, bytes(data))
        return path, dropped

    def remove(self, item):
        for path in (item.path, item.meta_path):
            try:
                os.remove(path)
            except OSError:
                pass

    def purge(self):
        """Deletes, unread: cards older than max_age, leftover .tmp files and sidecars without a card. Returns the number of cards"""
        now = self.clock()
        removed = 0
        for item in self.items():
            if now - item.received_at > self.max_age:
                self.remove(item)
                removed += 1
        try:
            entries = list(self.folder.iterdir())
        except OSError:
            return removed
        for path in entries:
            try:
                age = now - path.stat().st_mtime
                if path.name.endswith(".tmp") and age > 600:
                    os.remove(path)
                elif path.suffix == ".json" and age > 600 and not any(path.with_suffix(s).exists() for s in IMAGE_SUFFIXES):
                    os.remove(path)
            except OSError:
                pass
        return removed


class OcrWorker:
    """ONE background thread for the whole process. Takes the oldest card from the queue, reads it (behind the OCR gate),
    stores the result, and deletes the card and its sidecar in `finally`. It stops after the current card; the cards left
    are read in the next run. The thread is a daemon: it never keeps the process (or the autoStop) waiting"""

    def __init__(self, queue, store, reader=None, gate=None, clock=time.time, save_card=None, stop_flag=None, idle=1.0, purge_every=3600.0):
        self.queue, self.store = queue, store
        self.reader = reader or read_level_card
        self.gate = gate or OCR_GATE
        self.clock = clock
        self.save_card = save_card or save_failed_card
        self.stop_flag = stop_flag
        self.idle = idle
        self.purge_every = purge_every
        self.save_failed_cards = False
        self.processed = 0
        self.log_sink = None  # callable(account_id, text), runs in the worker thread
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._waiters = {}

    def configure(self, save_failed_cards=None):
        if save_failed_cards is not None:
            self.save_failed_cards = bool(save_failed_cards)

    # -- control -------------------------------------------------------------
    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="level-ocr-worker", daemon=True)
            self._thread.start()

    def stop(self, timeout=2.0):
        """Asks the worker to stop after the current card and waits at most `timeout` seconds. True when it has ended"""
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is None or not thread.is_alive():
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def wake(self):
        self._wake.set()

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def should_stop(self):
        if self._stop.is_set():
            return True
        try:
            flag = self.stop_flag
            if flag is None:
                from core.auto_stop import stop_requested as flag
            return bool(flag())
        except Exception:
            return False

    def expect(self, account_id, message_id, loop, future):
        with self._lock:
            self._waiters[(str(account_id), int(message_id))] = (loop, future)

    def forget(self, account_id, message_id):
        with self._lock:
            self._waiters.pop((str(account_id), int(message_id)), None)

    # -- the loop ------------------------------------------------------------
    def _run(self):
        last_purge = None
        while not self.should_stop():
            now = time.monotonic()
            if last_purge is None or now - last_purge >= self.purge_every:
                last_purge = now
                try:
                    self.queue.purge()  # older than 24 h: deleted without being read
                except Exception:
                    pass
            try:
                items = self.queue.items()
            except Exception:
                items = []
            if not items:
                self._wake.wait(self.idle)
                self._wake.clear()
                continue
            try:
                self.process(items[0])
            except Exception:
                try:
                    self.queue.remove(items[0])
                except Exception:
                    pass

    def process(self, item):
        """Reads one card, stores the verdict, tells the account, and always deletes the files"""
        ok, error, text, data = False, None, None, None
        try:
            try:
                data = item.path.read_bytes()
                result = self.gate.run(self.reader, data)
                if not result.get("ok"):
                    error = f"ocr_failed:{result.get('error') or 'unknown'}"
                else:
                    previous = self.store.get(item.account_id)
                    level = result["level"]
                    if previous and previous.get("level") is not None and level < previous["level"]:
                        error = "level_decreased"
                    else:
                        rank = result["rank"] if result.get("rank_ok") else None
                        self.store.record_good(item.account_id, level, result["xp_cur"], result["xp_need"], rank, self.clock())
                        pct = result["xp_cur"] / result["xp_need"] * 100.0
                        waited = max(0, int(self.clock() - item.received_at))
                        ok = True
                        text = f"[levelCheck] {item.name}: LVL {level}, XP {result['xp_cur']}/{result['xp_need']} ({pct:.1f}%) [queued {waited} s]"
            except Exception as problem:
                error = f"error:{type(problem).__name__}"
            if not ok:
                try:
                    self.store.record_fail(item.account_id, error, self.clock())
                except Exception:
                    pass
                if self.save_failed_cards and data:
                    try:
                        self.save_card(item.account_id, data)
                    except Exception:
                        pass
                text = f"[levelCheck] {item.name}: failed ({error})"
        finally:
            self.queue.remove(item)
        self.processed += 1
        self._report(item, ok, error, text)

    def _report(self, item, ok, error, text):
        if text and self.log_sink is not None:
            try:
                self.log_sink(item.account_id, text)
            except Exception:
                pass
        with self._lock:
            waiter = self._waiters.pop((str(item.account_id), item.message_id or -1), None)
        if waiter is not None:
            loop, future = waiter

            def resolve():
                if not future.done():
                    future.set_result((ok, error))

            try:
                loop.call_soon_threadsafe(resolve)
            except Exception:
                pass  # that account's loop is gone


QUEUE = LevelQueue()
WORKER = OcrWorker(QUEUE, STORE)
_worker_lock = threading.Lock()


def ensure_worker(settings=None):
    """Starts the process-wide OCR worker (once) and tells it whether failed cards are kept"""
    with _worker_lock:
        if WORKER.log_sink is None:
            WORKER.log_sink = log_for_account
        if settings is not None:
            WORKER.configure(save_failed_cards=getattr(settings, "saveFailedCards", False))
        WORKER.start()
    return WORKER


_hosts = {}
_hosts_lock = threading.Lock()


def register_host(host):
    with _hosts_lock:
        _hosts[str(host.account_id)] = host


def unregister_host(host):
    with _hosts_lock:
        if _hosts.get(str(host.account_id)) is host:
            _hosts.pop(str(host.account_id), None)


def log_for_account(account_id, text):
    """From the worker thread: one log line in the log of that account (nothing when that account is not running now)"""
    with _hosts_lock:
        host = _hosts.get(str(account_id))
    if host is not None:
        host.log_threadsafe(text)


# ---------------------------------------------------------------------------------------------- the debug dump
_AUTH_LIKE = re.compile(r"[\w-]{20,}\.[\w-]{5,}\.[\w-]{20,}|mfa\.[\w-]{20,}")


def clean_text(text, limit=60):
    """First `limit` characters of a message text for the debug file: one line, no key-like string"""
    text = _AUTH_LIKE.sub("[x]", str(text or ""))
    return " ".join(text.split())[:limit]


class DebugLog:
    """utils/data/level_debug.jsonl: one JSON line per level check. Over `max_bytes` the file is moved to .1 (one old file)"""

    def __init__(self, path=DEBUG_PATH, max_bytes=DEBUG_MAX_BYTES):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def write(self, record):
        line = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                size = self.path.stat().st_size
            except OSError:
                size = 0
            if size and size + len(line) > self.max_bytes:
                log_paths.rotate_to_part(self.path, self.path.parent)  # one old file is kept; only <name>.1 is replaced
            with open(self.path, "ab") as handle:
                handle.write(line)


DEBUG_LOG = DebugLog()


# ---------------------------------------------------------------------------------------------- the real bot
_inflight_lock = threading.Lock()
_inflight = {}  # channel id -> level checks that wait for a card there, across all accounts (at most 1 per channel)


def _inflight_add(channel_id, delta):
    with _inflight_lock:
        count = _inflight.get(channel_id, 0) + delta
        if count <= 0:
            _inflight.pop(channel_id, None)
        else:
            _inflight[channel_id] = count


def _inflight_claim(channel_id):
    """Takes the turn in a channel: True when no other level check waits there (the caller must _inflight_add(.., -1) later)"""
    with _inflight_lock:
        if _inflight.get(channel_id, 0) >= 1:
            return False
        _inflight[channel_id] = 1
        return True


def inflight_count(channel_id):
    with _inflight_lock:
        return _inflight.get(channel_id, 0)


def has_image(message):
    for attachment in getattr(message, "attachments", None) or []:
        if _is_image_file(getattr(attachment, "filename", None), getattr(attachment, "content_type", None)):
            return True
    return False


def first_image(message):
    for attachment in getattr(message, "attachments", None) or []:
        if _is_image_file(getattr(attachment, "filename", None), getattr(attachment, "content_type", None)):
            return attachment
    return None


def captcha_pending_count():
    """Accounts of this process with a captcha pending now (plain attribute reads, like core/live_status.py)"""
    try:
        from core import auto_stop

        count = 0
        for client in auto_stop.clients_snapshot().values():
            status = getattr(getattr(client, "ch", None), "command_handler_status", None)
            if getattr(status, "captcha", False):
                count += 1
        return count
    except Exception:
        return 0


def register_command(bot):
    """Gives the level command an id and a priority like an id that misc.json does not list, without the warning line
    and without touching misc.json or the database: it goes last in the queue"""
    ch = bot.ch
    if COMMAND_ID in bot.misc.get("command_info", {}):
        return
    if COMMAND_ID not in ch.unknown_command_ids:
        info = dict(bot.misc.get("command_info", {}).get("level") or {"priority": 8, "basecd": 15, "log_color": LOG_COLOR})
        info["log_color"] = LOG_COLOR
        ch.unknown_command_info[COMMAND_ID] = info
        ch.cmd_priorities[COMMAND_ID] = max(ch.cmd_priorities.values(), default=0) + 1
        ch.unknown_command_ids.add(COMMAND_ID)
    ch.ensure_command_state(COMMAND_ID)


@contextlib.asynccontextmanager
async def _fetch_guard(bot):
    """The limits of every history read of the bot: the process-wide fetch limiter and the global request bucket"""
    try:
        from core.global_rate_limiter import rate_limiter
        from core.reply_fallback import FETCH_LIMITER
    except Exception:
        yield
        return
    settings = getattr(getattr(bot, "global_settings_dict", None), "replyFallback", None)
    limit = int(getattr(settings, "maxConcurrentFetches", 4) or 4)
    await FETCH_LIMITER.acquire(limit)
    try:
        await rate_limiter.acquire()
        yield
    finally:
        FETCH_LIMITER.release()


class BotHost:
    """The Discord side of one account's level check"""

    def __init__(self, bot, queue=None, worker=None, debug_log=None):
        self.bot = bot
        self.queue = queue or QUEUE
        self.worker = worker or WORKER
        self.debug_log = debug_log or DEBUG_LOG
        self.guard = None  # factory of an async context manager around history reads; None = the real limits
        self.loop = None
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
        self.waiting = False
        self.channel_id = None
        self.note = None
        self._reset()

    def _reset(self):
        self.views = {}
        self.sent_id = None
        self.sent_at = None
        self.note = None
        self.history_done = False
        self.history_error = None
        self._raw_tried = set()

    # -- facts -------------------------------------------------------------
    @property
    def account_id(self):
        return str(self.bot.user.id)

    @property
    def name(self):
        return self.bot.username or f"account #{self.bot.account_index}"

    @property
    def settings(self):
        return self.bot.global_settings_dict.levelCheck

    def stopping(self):
        from core.auto_stop import stop_requested

        return bool(self.bot.stopped or stop_requested() or self.bot.is_closed())

    def block_reason(self):
        bot = self.bot
        if self.stopping():
            return "stopping"
        status = bot.ch.command_handler_status
        if status.captcha:
            return "captcha"
        if status.safety_check:
            return "safety_check"
        if status.sleep:
            return "sleep"
        if not status.state or status.hold_handler or bot.ch.pause_owners:
            return "paused"
        if not bot.gateway_connected() or bot.gateway_hold_active():
            return "gateway"
        if captcha_pending_count() >= CAPTCHA_WAVE_ACCOUNTS:
            return "captcha_wave"
        return None

    async def log(self, text):
        await self.bot.log(text, LOG_COLOR, webhook_useless_log=False, escape_markup=True)

    def log_threadsafe(self, text):
        """From another thread (the OCR worker): schedules the log line on this account's loop"""
        loop = self.loop
        if loop is not None and not loop.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(self.log(text), loop)
            except Exception:
                pass

    # -- messages ----------------------------------------------------------
    def _elapsed(self):
        if self.sent_at is None or self.loop is None:
            return 0.0
        return max(0.0, self.loop.time() - self.sent_at)

    def _store(self, view, source):
        if self.sent_id is not None and view.id <= self.sent_id:
            return
        if view.id not in self.views:
            view.seen_s = self._elapsed()
        if source not in view.sources:
            view.sources.append(source)
        self.views[view.id] = view
        if len(self.views) > 60:
            for old in sorted(self.views)[: len(self.views) - 60]:
                self.views.pop(old, None)

    def _addressed(self, message):
        try:
            return bool(self.bot.is_addressed_to_us(message))
        except Exception:
            return False

    def offer(self, message):
        """Called by the cog for every OwO message while a check waits (the library Message of a new message). Cheap"""
        if not self.waiting or getattr(getattr(message, "channel", None), "id", None) != self.channel_id:
            return
        self._store(view_from_message(message, self._addressed(message)), "event")

    def offer_raw(self, data, source="edit"):
        """Raw JSON of an OwO message: the payload of an edit. A partial update of a message we saw adds to what we know"""
        if not self.waiting or not isinstance(data, dict):
            return
        message_id = _int_or_none(data.get("id"))
        if message_id is None or _int_or_none(data.get("channel_id")) != self.channel_id:
            return
        author = data.get("author")
        if isinstance(author, dict) and _int_or_none(author.get("id")) != self.bot.owo_bot_id:
            return
        previous = self.views.get(message_id)
        if not isinstance(author, dict) and previous is None:
            return  # a partial update of a message that is not known to be OwO's
        self._store(view_from_raw(data, self.bot.user.id, previous), source)

    def pick(self, sent_id=None):
        sent_id = self.sent_id if sent_id is None else sent_id
        return pick_view(self.views.values(), sent_id, inflight_count(self.channel_id) > 1)

    # -- one check ---------------------------------------------------------------
    async def _claim_channel(self, channel_id):
        """One level check per channel at a time: waits for the turn (not for ever)"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CHANNEL_WAIT_SECONDS
        while not _inflight_claim(channel_id):
            if loop.time() >= deadline or self.stopping():
                return False
            await asyncio.sleep(0.5)
        return True

    async def request_card(self):
        """Sends the level command through the normal queue, finds the card, downloads it into the queue folder.
        (QueuedCard, None) or (None, error code)"""
        bot = self.bot
        if self.stopping() or bot.cm is None:
            return None, "stopping"
        register_command(bot)
        self.loop = asyncio.get_running_loop()
        self._reset()
        channel_id = bot.cm.id
        self.channel_id = channel_id
        if not await self._claim_channel(channel_id):
            return None, "channel_busy"
        cmd = {"cmd_name": self.settings.command, "prefix": True, "checks": False, "id": COMMAND_ID}
        result = (None, "error:unknown")
        try:
            self.waiting = True
            sent = bot.ch.new_sent_event(cmd)
            result = await self._request(cmd, sent, float(self.settings.replyTimeoutSeconds))
        finally:
            self.waiting = False
            _inflight_add(channel_id, -1)
            bot.ch.sent_events.pop(cmd.get("sent_token"), None)
        await self._write_debug(result)
        return result

    async def _request(self, cmd, sent, timeout):
        bot = self.bot
        try:
            # put_queue itself waits while the handler is paused (captcha, sleep): not for ever
            await asyncio.wait_for(bot.ch.put_queue(cmd), timeout=SENT_WAIT_SECONDS / 2)
            await asyncio.wait_for(sent.wait(), timeout=SENT_WAIT_SECONDS)
        except asyncio.TimeoutError:
            bot.ch.sent_events.pop(cmd.get("sent_token"), None)
            return None, "not_sent"
        sent_id = getattr(sent, "sent_message_id", None)
        if sent_id is None:
            return None, "not_sent"
        self.sent_id = int(sent_id)
        self.sent_at = self.loop.time()
        for old in [mid for mid in self.views if mid <= self.sent_id]:
            self.views.pop(old, None)
        view = await self._wait_for_card(timeout)
        if view is None:
            return None, self._missing_code()
        ref = best_image(view)
        data, error = await self._download(ref)
        if data is None:
            return None, error
        card, error = await self._enqueue(view, ref, data)
        if card is None:
            return None, error
        if getattr(self.settings, "debug", False) and not self.history_done:
            await self._read_history()  # only to show the raw form of what OwO sent, the card is already safe
        return card, None

    async def _wait_for_card(self, timeout):
        loop = self.loop
        deadline = loop.time() + timeout
        while True:
            found = self.pick()
            if found is not None:
                return found
            await self._fetch_components_v2()
            found = self.pick()
            if found is not None:
                return found
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.4, remaining))
        await self._read_history()  # whatever the gateway did not deliver
        return self.pick()

    async def _guarded(self, call):
        async with (self.guard or (lambda: _fetch_guard(self.bot)))():
            return await call()

    async def _fetch_components_v2(self):
        """A Components V2 message the library shows without its media: read it raw (a few times per check at most)"""
        for view in list(self.views.values()):
            if not view.needs_raw or view.id in self._raw_tried or len(self._raw_tried) >= RAW_FETCH_MAX:
                continue
            self._raw_tried.add(view.id)
            try:
                data = await self._guarded(lambda message_id=view.id: self.bot.http.get_message(self.channel_id, message_id))
            except asyncio.CancelledError:
                raise
            except Exception:
                view.needs_raw = False
                continue
            if isinstance(data, dict):
                self._store(view_from_raw(data, self.bot.user.id, view), "fetch")
            view.needs_raw = False

    async def _read_history(self):
        """The newest messages after our command, as raw JSON: what the gateway dropped, and the raw form of the rest"""
        self.history_done = True
        try:
            rows = await self._guarded(
                lambda: self.bot.http.logs_from(self.channel_id, HISTORY_LIMIT, after=self.sent_id)
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.history_error = type(error).__name__
            return
        for data in sorted((row for row in rows or [] if isinstance(row, dict)), key=lambda row: _int_or_none(row.get("id")) or 0):
            try:
                author = _int_or_none((data.get("author") or {}).get("id"))
                if author != self.bot.owo_bot_id or _int_or_none(data.get("id")) is None:
                    continue
                self._store(view_from_raw(data, self.bot.user.id, self.views.get(int(data["id"]))), "history")
            except Exception:
                continue

    def _missing_code(self):
        newer = [view for view in self.views.values() if view.id > (self.sent_id or 0)]
        if not newer:
            return "history_failed" if self.history_error else "no_reply"
        latest = max(newer, key=lambda view: view.id)
        text = clean_text(latest.content)
        self.note = f'OwO said: "{text}"' if text else "OwO sent a message without text and without an image"
        return "reply_no_image"

    async def _download(self, ref):
        if isinstance(ref.size, int) and ref.size > MAX_CARD_BYTES:
            return None, "too_big"
        try:
            if ref.obj is not None and hasattr(ref.obj, "read"):
                data = await asyncio.wait_for(ref.obj.read(), timeout=DOWNLOAD_TIMEOUT)
            elif isinstance(ref.url, str) and ref.url.startswith("https://"):
                data = await asyncio.wait_for(self.bot.http.get_from_cdn(ref.url), timeout=DOWNLOAD_TIMEOUT)
            else:
                return None, "download_failed"
        except asyncio.CancelledError:
            raise
        except Exception:
            return None, "download_failed"
        if not data:
            return None, "download_failed"
        if len(data) > MAX_CARD_BYTES:
            return None, "too_big"
        return data, None

    async def _enqueue(self, view, ref, data):
        """Saves the card in level_queue/ (off the event loop) and wakes the OCR worker"""
        future = self.loop.create_future()
        self.worker.expect(self.account_id, view.id, self.loop, future)
        try:
            path, dropped = await run_in_thread(
                self.queue.add, self.account_id, self.name, view.id, data, card_extension(ref.filename, data), time.time()
            )
        except asyncio.CancelledError:
            self.worker.forget(self.account_id, view.id)
            raise
        except Exception:
            self.worker.forget(self.account_id, view.id)
            return None, "queue_failed"
        if dropped:
            await self.log(f"[levelCheck] the card queue is full ({QUEUE_CAP}): dropped the oldest card")
        self.worker.wake()
        return QueuedCard(self.account_id, view.id, path, future, time.time()), None

    # -- debug dump ----------------------------------------------------------------
    def _debug_record(self, code):
        messages = []
        for view in sorted(self.views.values(), key=lambda item: item.id):
            after = None
            if self.sent_id is not None:
                after = round(((view.id - self.sent_id) >> 22) / 1000.0, 3)
            messages.append(
                {
                    "id": view.id,
                    "after_s": after,
                    "seen_s": round(view.seen_s, 2),
                    "via": "+".join(view.sources),
                    "has_reference": view.has_reference,
                    "reference_id": view.reference_id,
                    "n_attachments": len(view.attachments),
                    "attachments": [[clean_text(a[0], 80), a[1], a[2]] for a in view.attachments],
                    "n_embeds": len(view.embeds),
                    "embeds": [list(e) for e in view.embeds],
                    "flags": view.flags,
                    "n_components": len(view.component_types),
                    "component_types": view.component_types,
                    "components_from": "raw" if view.components_raw else "library",
                    "images": [ref.kind for ref in view.images],
                    "addressed": view.addressed,
                    "content": clean_text(view.content),
                }
            )
        return {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "account": clean_text(self.name, 40),
            "channel_id": self.channel_id,
            "command_message_id": self.sent_id,
            "timeout": float(self.settings.replyTimeoutSeconds),
            "result": code,
            "history_read": self.history_done,
            "history_error": self.history_error,
            "owo_messages": messages,
        }

    async def _write_debug(self, result):
        if not getattr(self.settings, "debug", False):
            return
        card, error = result
        try:
            record = self._debug_record("queued" if card is not None else error)
            await run_in_thread(self.debug_log.write, record)
        except Exception:
            pass  # a debug file must never break a check
