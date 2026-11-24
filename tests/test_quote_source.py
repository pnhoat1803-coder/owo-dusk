"""Level quotes (prompt 31): utils/quote_source.py against the Vietnamese quotes API.

The API is a local fake on 127.0.0.1 (FakeQuotesApi). An autouse fixture points DUSK_QUOTES_URL at a closed local port
by default, so nothing in this file can reach the internet. Tests that drive refill_once() or get_quote() directly use
make_source(), which starts no background thread; test_start_fills_the_pool_without_any_get_quote starts one.
"""

import http.server
import json
import logging
import threading
import time
import urllib.parse

import pytest

from utils import quote_source as qs

VIETNAMESE = {f"Câu số {n} tiếng Việt" for n in range(1, 11)}


class FakeQuotesApi:
    """Answers like the real API: /api/quotes/<id> gives {"status":200,"data":["Câu số <id> tiếng Việt"]} for ids 1..10
    and HTTP 400 for anything else. Every requested id is recorded in `hits`. `by_id` and `all_mode` choose another
    answer: bad400, notjson, notlist (data is not a list), status500 (status 500 inside a 200 body), 429, http500,
    sleep (answers after `sleep_s` seconds)."""

    def __init__(self):
        self.hits = []
        self.by_id = {}
        self.all_mode = "ok"
        self.sleep_s = 0.0
        self._lock = threading.Lock()
        api = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send_answer(self, code, body, content_type="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = urllib.parse.urlparse(self.path).path
                tail = path[len("/api/quotes/"):] if path.startswith("/api/quotes/") else ""
                quote_id = int(tail) if tail.isdigit() else None
                with api._lock:
                    api.hits.append(quote_id)
                mode = api.by_id.get(quote_id, api.all_mode)
                if mode == "sleep":
                    time.sleep(api.sleep_s)
                    mode = "ok"
                if quote_id is None or not 1 <= quote_id <= 10 or mode == "bad400":
                    return self.send_answer(400, b"")
                if mode == "429":
                    return self.send_answer(429, b"")
                if mode == "http500":
                    return self.send_answer(500, b"")
                if mode == "notjson":
                    return self.send_answer(200, b"<html>not json</html>", "text/html")
                if mode == "notlist":
                    return self.send_answer(200, json.dumps({"status": 200, "data": "not a list"}).encode("utf-8"))
                status = 500 if mode == "status500" else 200
                body = {"status": status, "data": [f"Câu số {quote_id} tiếng Việt"]}
                return self.send_answer(200, json.dumps(body, ensure_ascii=False).encode("utf-8"))

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.template = "http://127.0.0.1:%d/api/quotes/{id}" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(autouse=True)
def api_address_is_a_closed_port(monkeypatch):
    """By default the API address points at a closed local port: a test that forgot the fake server fails fast"""
    monkeypatch.setenv(qs.URL_ENV, "http://127.0.0.1:9/api/quotes/{id}")
    monkeypatch.setattr(qs, "_template_warned", False)
    monkeypatch.setattr(qs, "REQUEST_GAP_S", 0)
    monkeypatch.setattr(qs, "COLD_RETRY_S", 0)
    monkeypatch.setattr(qs, "FETCH_TIMEOUT_S", 2)
    monkeypatch.setattr(qs, "FETCH_TIMEOUT_COLD_S", 2)


@pytest.fixture
def api(monkeypatch):
    fake = FakeQuotesApi()
    monkeypatch.setenv(qs.URL_ENV, fake.template)
    yield fake
    fake.stop()


def make_source(tmp_path, threaded=False, **options):
    kwargs = {"cache_path": str(tmp_path / "quotes_cache.json"), "lines": lambda: [], "seed": 7}
    kwargs.update(options)
    source = qs.QuoteSource(**kwargs)
    if not threaded:
        source._started = True  # no background thread: the test drives refill_once() and get_quote() itself
    return source


def test_quote_url_only_builds_ids_from_1_to_10(monkeypatch):
    monkeypatch.delenv(qs.URL_ENV, raising=False)
    for bad in (0, -1, 11, 100, True, "5", 5.0):
        with pytest.raises(ValueError):
            qs._quote_url(bad)
    assert qs._quote_url(1) == "https://blv-anh-ngok-said.onrender.com/api/quotes/1"
    assert qs._quote_url(10) == "https://blv-anh-ngok-said.onrender.com/api/quotes/10"


def test_a_refill_asks_each_id_once_and_never_another(api, tmp_path):
    src = make_source(tmp_path)
    for _ in range(50):
        before = len(api.hits)
        assert src.refill_once() is True
        assert sorted(api.hits[before:]) == list(range(1, 11))
    assert len(api.hits) == 500
    assert all(1 <= hit <= 10 for hit in api.hits)


def test_the_pool_has_the_ten_vietnamese_quotes_and_the_cache_keeps_diacritics(api, tmp_path):
    src = make_source(tmp_path, clock=lambda: 100.0)
    assert src.refill_once() is True
    assert set(src._pool) == VIETNAMESE
    assert src.status()["pool"] == 10 and src.status()["cache"] == 10
    assert src._next_at == 100.0 + qs.REFILL_INTERVAL_S  # the next refill is six hours later
    raw = (tmp_path / "quotes_cache.json").read_text(encoding="utf-8")
    assert "Câu số" in raw  # written as UTF-8, not as \u escapes
    cache = json.loads(raw)
    assert cache["source"] == qs.QUOTES_SOURCE_TAG
    assert set(cache["quotes"]) == VIETNAMESE
    fresh = make_source(tmp_path)
    fresh._load_cache()
    assert set(fresh._pool) == VIETNAMESE


def test_a_refused_id_is_skipped_with_one_warning(api, tmp_path, caplog):
    api.by_id[3] = "bad400"
    src = make_source(tmp_path)
    with caplog.at_level(logging.WARNING, logger="utils.quote_source"):
        assert src.refill_once() is True
    warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert src.status()["pool"] == 9
    assert src.status()["last_error"] == "http_400 id=3"
    assert api.hits.count(3) == 1  # not asked again in the same refill


def test_bad_answers_skip_only_their_own_id(api, tmp_path):
    api.by_id = {4: "notjson", 5: "notlist", 6: "status500"}
    src = make_source(tmp_path)
    assert src.refill_once() is True  # no exception: the refill is complete
    assert src.status()["pool"] == 7
    assert src.status()["last_error"].startswith("bad_json")
    assert len(api.hits) == 10


def test_06_a_timeout_on_the_first_id_retries_once_then_stops_the_refill(api, tmp_path, monkeypatch):
    monkeypatch.setattr(qs, "FETCH_TIMEOUT_COLD_S", 0.3)
    api.all_mode = "sleep"
    api.sleep_s = 2.0
    now = [1000.0]
    src = make_source(tmp_path, clock=lambda: now[0], lines=lambda: ["File line one is here"])
    assert src.refill_once() is False
    assert len(api.hits) == 2 and len(set(api.hits)) == 1  # the first id, its one retry, and no other id
    assert src.status()["last_error"].startswith("timeout")
    assert src._next_at - now[0] == qs.BACKOFF_FIRST  # the first back-off: 60 s
    worst = 0.0
    for _ in range(20):
        started = time.perf_counter()
        assert src.get_quote("account-6") == "File line one is here"
        worst = max(worst, time.perf_counter() - started)
    assert worst < 0.005, worst  # answers from the file at once while the API is down


def test_06_b_a_timeout_on_a_later_id_stops_the_refill_at_once(tmp_path, monkeypatch):
    monkeypatch.setattr(qs, "FETCH_TIMEOUT_COLD_S", 3)
    monkeypatch.setattr(qs, "FETCH_TIMEOUT_S", 1)
    calls = []

    def fetch(quote_id, timeout):
        calls.append((quote_id, timeout))
        if len(calls) == 2:
            raise qs.QuoteFetchError("timeout", "no answer")
        return [f"Câu số {quote_id} tiếng Việt"]

    src = make_source(tmp_path, fetch=fetch)
    assert src.refill_once() is False
    assert len(calls) == 2  # the other eight ids were not asked
    assert [timeout for _, timeout in calls] == [3, 1]  # first id: cold timeout, later ids: normal timeout
    assert src.status()["pool"] == 1  # the quote it got is kept
    assert src.status()["last_error"].startswith("timeout")


def test_a_rate_limit_stops_the_refill_and_waits_ten_minutes(api, tmp_path):
    api.all_mode = "429"
    now = [1000.0]
    src = make_source(tmp_path, clock=lambda: now[0])
    assert src.refill_once() is False
    assert len(api.hits) == 1  # the refill stopped at its first id
    assert src._next_at - now[0] >= 600.0
    assert src.status()["last_error"].startswith("rate_limited")


def test_an_old_cache_is_ignored_and_the_new_format_is_loaded(tmp_path, caplog):
    cache = tmp_path / "quotes_cache.json"
    previous_formats = (["A quote from the previous source"], {"quotes": ["No source tag here"], "saved_at": 1})
    for old in previous_formats:
        cache.write_text(json.dumps(old), encoding="utf-8")
        src = make_source(tmp_path)
        with caplog.at_level(logging.INFO, logger="utils.quote_source"):
            src._load_cache()
        assert src.status()["pool"] == 0
        assert "old quote cache ignored" in caplog.text
        caplog.clear()
    cache.write_text(
        json.dumps({"source": qs.QUOTES_SOURCE_TAG, "saved_at": 5, "quotes": ["Câu số một tiếng Việt"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    src = make_source(tmp_path)
    src._load_cache()
    assert src.status()["pool"] == 1
    assert src.status()["cache"] == 1


def test_no_account_repeats_one_of_its_last_five_with_ten_quotes(tmp_path):
    src = make_source(tmp_path)
    src._pool.extend(sorted(VIETNAMESE))
    got = [src.get_quote("account-9") for _ in range(300)]
    assert all(text in VIETNAMESE for text in got)
    for i in range(5, len(got)):
        assert got[i] not in got[i - 5:i]


def test_the_file_source_never_contacts_the_api(api, tmp_path):
    src = make_source(tmp_path, threaded=True, lines=lambda: ["File line one is here"])
    for _ in range(5):
        assert src.get_quote("account-4", source="file") == "File line one is here"
    time.sleep(0.2)
    assert api.hits == []
    assert src._started is False  # not even the refill thread starts for the file source


def test_an_empty_pool_and_an_empty_cache_use_the_file(tmp_path):
    src = make_source(tmp_path, lines=lambda: ["File line one is here"])
    assert src.get_quote("account-3") == "File line one is here"


def test_107_threads_answer_without_errors_or_a_deadlock(tmp_path):
    src = make_source(tmp_path, lines=lambda: ["File line one is here", "File line two is here"])
    src._pool.extend(sorted(VIETNAMESE))
    errors = []

    def worker(number):
        try:
            for _ in range(200):
                assert src.get_quote("account-%d" % number) is not None
        except Exception as error:  # reported below
            errors.append(error)

    threads = [threading.Thread(target=worker, args=(number,)) for number in range(107)]
    started = time.perf_counter()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    elapsed = time.perf_counter() - started
    assert not errors
    assert not any(thread.is_alive() for thread in threads)
    assert elapsed < 2.0, elapsed


def test_a_template_without_id_is_ignored_with_one_warning(monkeypatch, caplog):
    monkeypatch.setenv(qs.URL_ENV, "http://127.0.0.1:9/api/quotes")  # no {id}
    sent = []

    def refuse(request, timeout=None):
        sent.append(request.full_url)
        raise OSError("this test sends nothing")

    monkeypatch.setattr(qs.urllib.request, "urlopen", refuse)
    with caplog.at_level(logging.WARNING, logger="utils.quote_source"):
        for quote_id in (1, 2):
            with pytest.raises(qs.QuoteFetchError):
                qs._fetch_one(quote_id)
    assert sent == [qs.QUOTES_URL_TEMPLATE.format(id=1), qs.QUOTES_URL_TEMPLATE.format(id=2)]
    assert sum("has no {id}" in record.getMessage() for record in caplog.records) == 1


def test_start_fills_the_pool_without_any_get_quote(api, tmp_path):
    src = make_source(tmp_path, threaded=True)
    src.start()  # what core/bot_runner.py calls when the bot starts; this test never calls get_quote()
    deadline = time.time() + 15
    while src.status()["pool"] < 10 and time.time() < deadline:
        time.sleep(0.02)
    src.stop()
    assert src.status()["pool"] == 10
    assert sorted(api.hits) == list(range(1, 11))


def test_an_interrupted_refill_adds_quotes_and_never_shrinks_the_pool(tmp_path):
    src = make_source(tmp_path)
    src._pool.extend(f"Câu cũ số {n} tiếng Việt" for n in range(1, 11))
    calls = []

    def fetch(quote_id, timeout):
        calls.append(quote_id)
        if len(calls) == 4:
            raise qs.QuoteFetchError("network", "no connection")
        return [f"Câu mới {quote_id} tiếng Việt"]

    src._fetch = fetch
    assert src.refill_once() is False
    assert len(calls) == 4
    assert src.status()["pool"] == 13  # 10 old quotes + the 3 new ones it got before the stop
    assert src.status()["last_error"].startswith("network")


def test_the_filter_keeps_2_to_300_characters_and_drops_links_and_mentions():
    assert qs.clean_quote("  Keep   this\n\nquote\tplease  ") == "Keep this quote please"
    assert qs.clean_quote("a") is None
    assert qs.clean_quote("ok") == "ok"
    assert qs.clean_quote("x" * 300) == "x" * 300
    assert qs.clean_quote("x" * 301) is None
    assert qs.clean_quote("Anh sống như thế đấy em") == "Anh sống như thế đấy em"
    for bad in ("mail me at a@b.c now", "see http://x.y for more", "join discord.gg/abc please", "run `rm` now"):
        assert qs.clean_quote(bad) is None, bad
    assert qs.clean_quote(None) is None
