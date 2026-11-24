"""Level-grind quotes from the Vietnamese quotes API (prompt 31).

get_quote(account_key, source) answers at once from a pool in memory and never waits for the network. One background
daemon thread fetches the 10 ids of https://blv-anh-ngok-said.onrender.com/api/quotes/{id}. It starts when the bot
starts (start(), called from core/bot_runner.py), so the pool fills while the accounts log in, and it fetches again
every REFILL_INTERVAL_S. The pool is kept in utils/data/quotes_cache.json, so a restart without internet still has
quotes. When the pool is empty, or the account's quoteSource is "file", the line comes from config/level_lines.txt.

One instance serves the whole process: module attribute `quotes`.
Test-only environment variable DUSK_QUOTES_URL replaces the API address; it must contain {id}.
"""

import collections
import json
import logging
import os
import random
import re
import socket
import threading
import time
import urllib.error
import urllib.request

from utils.level_lines import local_level_lines

_log = logging.getLogger(__name__)

QUOTES_URL_TEMPLATE = "https://blv-anh-ngok-said.onrender.com/api/quotes/{id}"
URL_ENV = "DUSK_QUOTES_URL"
QUOTE_ID_MIN = 1
QUOTE_ID_MAX = 10
QUOTES_SOURCE_TAG = "blv-quotes-v1"
REFILL_INTERVAL_S = 6 * 3600  # the 10 ids are fetched again every 6 hours
REQUEST_GAP_S = 1.5  # pause between two requests of one refill
FETCH_TIMEOUT_COLD_S = 40  # the first id of a refill: Render free tier cold start
FETCH_TIMEOUT_S = 15  # the other ids of a refill
COLD_RETRY_S = 20  # wait before the one retry of a first id that timed out or lost the connection
USER_AGENT = "OwO-Dusk"
CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "quotes_cache.json")
MIN_LEN = 2
MAX_LEN = 300
BLOCKED = ("@", "http", "discord.gg", "`")
RECENT = 30  # quotes remembered per account; with a small pool the window is smaller, see _pick
MAX_POOL = 1000
MAX_CACHE = 1000
MAX_BYTES = 2 * 1024 * 1024
BACKOFF_FIRST = 60.0
BACKOFF_MAX = 1800.0
RATE_LIMIT_WAIT = 600.0
WAKE_SECONDS = 5.0
# A failure of these kinds only skips its own id; any other failure ends the refill (see refill_once)
SKIP_KINDS = ("http_400", "bad_json")
_SPACES = re.compile(r"\s+")
_template_warned = False


class QuoteFetchError(Exception):
    """One failed request. kind: timeout, network, http (5xx), rate_limited (429), http_400, bad_json"""

    def __init__(self, kind, detail=""):
        super().__init__(f"{kind} {detail}".strip())
        self.kind = kind
        self.detail = detail


def _template():
    """The address template: DUSK_QUOTES_URL when it holds {id} (tests), else the real one. A value without {id} is
    ignored, with one warning"""
    global _template_warned
    value = os.environ.get(URL_ENV, "").strip()
    if value and "{id}" in value:
        return value
    if value and not _template_warned:
        _template_warned = True
        _log.warning("%s has no {id}; the real quotes API address is used", URL_ENV)
    return QUOTES_URL_TEMPLATE


def _quote_url(quote_id):
    """The only way an address is built: a whole number from QUOTE_ID_MIN to QUOTE_ID_MAX, nothing else"""
    if not isinstance(quote_id, int) or isinstance(quote_id, bool):
        raise ValueError(f"quote id must be int, got {type(quote_id).__name__}")
    if quote_id < QUOTE_ID_MIN or quote_id > QUOTE_ID_MAX:
        raise ValueError(f"quote id {quote_id} out of range {QUOTE_ID_MIN}..{QUOTE_ID_MAX}")
    return _template().format(id=quote_id)


def clean_quote(text):
    """One usable quote, or None: whitespace and newlines collapsed, 2 to 300 characters, no @, link, invite or backtick"""
    if not isinstance(text, str):
        return None
    line = _SPACES.sub(" ", text).strip()
    if not MIN_LEN <= len(line) <= MAX_LEN:
        return None
    lowered = line.lower()
    if any(part in lowered for part in BLOCKED):
        return None
    return line


def _clean_all(texts):
    """The usable quotes of a list, in order, without repeats"""
    kept, seen = [], set()
    for item in texts:
        text = clean_quote(item)
        if text and text not in seen:
            seen.add(text)
            kept.append(text)
    return kept


def _fetch_one(quote_id, timeout=FETCH_TIMEOUT_S):
    """One id from the API. Returns the quote strings it holds (normally one). Raises QuoteFetchError: rate_limited
    (429), http (5xx), timeout and network stop the refill; http_400 and bad_json only skip this id"""
    request = urllib.request.Request(
        _quote_url(quote_id), headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            raw = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as error:
        if error.code == 429:
            raise QuoteFetchError("rate_limited", "429") from None
        if error.code == 400:
            raise QuoteFetchError("http_400", f"id={quote_id}") from None
        raise QuoteFetchError("http", str(error.code)) from None
    except urllib.error.URLError as error:
        if isinstance(error.reason, (socket.timeout, TimeoutError)):
            raise QuoteFetchError("timeout", "no answer") from None
        raise QuoteFetchError("network", "no connection") from None
    except (socket.timeout, TimeoutError):
        raise QuoteFetchError("timeout", "no answer") from None
    except OSError:
        raise QuoteFetchError("network", "no connection") from None
    if status != 200:
        raise QuoteFetchError("http", str(status))
    if len(raw) > MAX_BYTES:
        raise QuoteFetchError("bad_json", "too large")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise QuoteFetchError("bad_json", "not JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise QuoteFetchError("bad_json", "no data list")
    if "status" in data and data["status"] != 200:
        raise QuoteFetchError("bad_json", "status is not 200")
    return [item for item in data["data"] if isinstance(item, str)]


class QuoteSource:
    def __init__(self, cache_path=None, fetch=None, lines=None, clock=time.monotonic, seed=None):
        self.cache_path = cache_path or CACHE_PATH
        self._fetch = fetch or _fetch_one
        self._lines = lines or local_level_lines
        self._clock = clock
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self._pool = collections.deque(maxlen=MAX_POOL)
        self._cache_count = 0
        self._recent = {}
        self._started = False
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._next_at = 0.0  # clock time when the next refill may start
        self._failures = 0
        self.last_ok_at = None
        self.last_error = None

    # ----- answering the caller -----

    def get_quote(self, account_key, source="api"):
        """A level line for this account, or None when neither the pool nor the file has one.

        Never waits for the network. source "file" reads config/level_lines.txt only.
        """
        key = str(account_key)
        if source != "api":
            return self._answer(key, None)
        self._ensure_started()  # a no-op once start() has run, which the bot does when it starts
        return self._answer(key, "pool")

    def _answer(self, key, first):
        with self._lock:
            recent = self._recent.get(key)
            if recent is None:
                recent = self._recent[key] = collections.deque(maxlen=RECENT)
            text = self._pick(recent) if first == "pool" else None
            if text is None:
                text = self._pick_file(recent)
            if text is None:
                return None
            recent.append(text)
            return text

    def _pick(self, recent):
        """A random pool quote this account did not get lately. None when the pool is empty"""
        count = len(self._pool)
        if count == 0:
            return None
        window = min(RECENT, count // 2)  # with 10 quotes the last 5 are skipped for this account
        avoid = set(list(recent)[-window:]) if window else set()
        fresh = [text for text in self._pool if text not in avoid]
        if fresh:
            return self._rng.choice(fresh)
        return self._rng.choice(list(self._pool))  # nothing is left: a repeat rather than a block

    def _pick_file(self, recent):
        """A line of config/level_lines.txt: not one of this account's last RECENT lines when it can be avoided"""
        lines = self._lines()
        if not lines:
            return None
        avoid = set(recent)
        fresh = [line for line in lines if line not in avoid]
        return self._rng.choice(fresh if fresh else lines)

    def status(self):
        """The numbers the dashboard shows: pool size, cache size, last good fetch, last error"""
        with self._lock:
            return {
                "pool": len(self._pool),
                "cache": self._cache_count,
                "last_ok_at": self.last_ok_at,
                "last_error": self.last_error,
            }

    # ----- background refill -----

    def start(self):
        """Starts the refill thread, once. The bot calls it when it starts, so the pool fills before the first level line"""
        self._ensure_started()

    def _ensure_started(self):
        with self._lock:
            if self._started:
                return
            self._started = True
        thread = threading.Thread(target=self._run, name="quote-source", daemon=True)
        thread.start()

    def _run(self):
        self._load_cache()  # in this thread, so the bot never waits for the disk
        while not self._stop.is_set():
            if self._clock() >= self._next_at:
                try:
                    self.refill_once()
                except Exception:  # a bug must not end the thread: log it and retry after the first back-off
                    _log.exception("Level quote refill failed unexpectedly")
                    with self._lock:
                        self._next_at = self._clock() + BACKOFF_FIRST
            self._wake.wait(timeout=WAKE_SECONDS)
            self._wake.clear()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def refill_once(self):
        """One refill: the 10 ids in random order, REQUEST_GAP_S apart. Returns True when it was complete.

        A complete refill replaces the pool with the quotes it got; ids refused with 400 or with bad JSON are skipped.
        A refill that stopped early (timeout, connection error, 5xx, 429) only adds the quotes it got, so a failure never
        shrinks the pool, and the back-off applies. Nothing here raises.
        """
        ids = self._rng.sample(range(QUOTE_ID_MIN, QUOTE_ID_MAX + 1), QUOTE_ID_MAX - QUOTE_ID_MIN + 1)
        got = []
        skipped = None
        for position, quote_id in enumerate(ids):
            if position and self._stop.wait(REQUEST_GAP_S):
                return False  # the bot is stopping
            try:
                got.extend(self._fetch_id(quote_id, first=position == 0))
            except QuoteFetchError as error:
                if error.kind in SKIP_KINDS:
                    skipped = error
                    if error.kind == "http_400":
                        _log.warning(
                            "Level quote id refused by the API (%s); skipped in this refill, the server may have changed",
                            error.detail,
                        )
                    continue
                self._keep_partial(got)
                self._on_failure(error)
                return False
        return self._keep_full(got, skipped)

    def _fetch_id(self, quote_id, first):
        """One id. The first id of a refill waits longer (cold start) and gets one retry after COLD_RETRY_S when it
        timed out or lost the connection"""
        if not first:
            return self._fetch(quote_id, FETCH_TIMEOUT_S)
        try:
            return self._fetch(quote_id, FETCH_TIMEOUT_COLD_S)
        except QuoteFetchError as error:
            if error.kind not in ("timeout", "network"):
                raise
            if self._stop.wait(COLD_RETRY_S):
                raise
            return self._fetch(quote_id, FETCH_TIMEOUT_COLD_S)

    def _keep_full(self, got, skipped):
        """A complete refill: the pool becomes the quotes it got. Nothing back at all counts as a failure"""
        kept = _clean_all(got)
        if not kept:
            self._on_failure(QuoteFetchError("bad_json", "no usable quote in a complete refill"))
            return False
        self._rng.shuffle(kept)
        with self._lock:
            self._pool = collections.deque(kept, maxlen=MAX_POOL)
            self.last_ok_at = time.time()
            self.last_error = str(skipped) if skipped else None
            if self._failures:
                _log.info("Level quotes are back after %d failed attempts", self._failures)
            self._failures = 0
            self._next_at = self._clock() + REFILL_INTERVAL_S
            snapshot = list(self._pool)
        self._save_cache(snapshot)
        return True

    def _keep_partial(self, got):
        """A refill that stopped early: the quotes it got join the pool, and nothing is removed"""
        kept = _clean_all(got)
        if not kept:
            return
        with self._lock:
            known = set(self._pool)
            for text in kept:
                if text not in known:
                    self._pool.append(text)
                    known.add(text)
            self.last_ok_at = time.time()
            snapshot = list(self._pool)
        self._save_cache(snapshot)

    def _on_failure(self, error):
        with self._lock:
            self._failures += 1
            if error.kind == "rate_limited":
                wait = max(RATE_LIMIT_WAIT, min(BACKOFF_FIRST * (2 ** (self._failures - 1)), BACKOFF_MAX))
            else:
                wait = min(BACKOFF_FIRST * (2 ** (self._failures - 1)), BACKOFF_MAX)
            self._next_at = self._clock() + wait
            self.last_error = str(error)
            first = self._failures == 1
        if first:
            _log.warning("Level quote fetch failed (%s); retrying in %d s, the local lines are used meanwhile", error, wait)
        else:
            _log.debug("Level quote fetch failed again (%s); next try in %d s", error, wait)

    # ----- cache file -----

    def _load_cache(self):
        try:
            with open(self.cache_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return
        if not (isinstance(data, dict) and data.get("source") == QUOTES_SOURCE_TAG):
            # A plain list, or no source tag: the file of the previous source. The next refill overwrites it
            _log.info("old quote cache ignored (no %s source tag)", QUOTES_SOURCE_TAG)
            return
        items = data.get("quotes")
        if not isinstance(items, list):
            return
        kept = _clean_all(items)
        with self._lock:
            for text in kept:
                if text not in self._pool:
                    self._pool.append(text)
            self._cache_count = len(kept)

    def _save_cache(self, quotes):
        keep = quotes[-MAX_CACHE:]
        payload = {"source": QUOTES_SOURCE_TAG, "saved_at": int(time.time()), "quotes": keep}
        tmp = self.cache_path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            os.replace(tmp, self.cache_path)
        except OSError:
            _log.warning("Level quote cache could not be written; the pool stays in memory only")
            return
        with self._lock:
            self._cache_count = len(keep)


quotes = QuoteSource()
