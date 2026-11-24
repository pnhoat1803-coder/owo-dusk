"""The quote refill starts only when the level grind will use the API (core/bot_runner.py: start_level_quotes).

The API is a local fake on 127.0.0.1 that records every request. With the grind or the quotes off, or with
quoteSource "file", the bot must not make a single request.
"""

import http.server
import json
import threading
import time

import pytest

from core import bot_runner
from utils import quote_source as qs


class RecordingApi:
    def __init__(self):
        self.hits = []
        api = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                api.hits.append(self.path)
                body = json.dumps({"status": 200, "data": ["Câu thử nghiệm"]}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/quotes/{{id}}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def api(monkeypatch, tmp_path):
    fake = RecordingApi()
    monkeypatch.setenv(qs.URL_ENV, fake.url)
    source = qs.QuoteSource(cache_path=str(tmp_path / "quotes_cache.json"))
    monkeypatch.setattr(bot_runner.quote_source, "quotes", source)
    monkeypatch.setattr(qs, "REQUEST_GAP_S", 0.0, raising=False)
    yield fake
    source.stop()
    fake.close()


def write_settings(tmp_path, monkeypatch, grind):
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "settings.json").write_text(json.dumps({"commands": {"lvlGrind": grind}}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)


@pytest.mark.parametrize(
    "grind, allow, wanted",
    [
        ({"enabled": True, "useQuoteInstead": True}, True, True),  # the shipped defaults: quoteSource is "api"
        ({"enabled": True, "useQuoteInstead": True, "quoteSource": "api"}, True, True),
        ({"enabled": True, "useQuoteInstead": True, "quoteSource": "file"}, True, False),
        ({"enabled": True, "useQuoteInstead": False}, True, False),
        ({"enabled": False, "useQuoteInstead": True}, True, False),
        ({"enabled": True, "useQuoteInstead": True}, False, False),
    ],
)
def test_the_gate_follows_the_level_grind_settings(tmp_path, monkeypatch, grind, allow, wanted):
    write_settings(tmp_path, monkeypatch, grind)
    monkeypatch.setitem(bot_runner.danger_settings_dict, "allow_quotes", allow)
    assert bot_runner.level_quotes_wanted() is wanted


def test_a_missing_or_broken_settings_file_means_no_quotes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(bot_runner.danger_settings_dict, "allow_quotes", True)
    assert bot_runner.level_quotes_wanted() is False
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.json").write_text("{not json", encoding="utf-8")
    assert bot_runner.level_quotes_wanted() is False


def test_with_quotes_off_no_request_reaches_the_api(api, tmp_path, monkeypatch):
    write_settings(tmp_path, monkeypatch, {"enabled": True, "useQuoteInstead": False})
    monkeypatch.setitem(bot_runner.danger_settings_dict, "allow_quotes", True)
    assert bot_runner.start_level_quotes() is False
    time.sleep(0.6)
    assert api.hits == []


def test_with_quotes_on_the_refill_reaches_the_api(api, tmp_path, monkeypatch):
    write_settings(tmp_path, monkeypatch, {"enabled": True, "useQuoteInstead": True})
    monkeypatch.setitem(bot_runner.danger_settings_dict, "allow_quotes", True)
    assert bot_runner.start_level_quotes() is True
    deadline = time.time() + 10
    while not api.hits and time.time() < deadline:
        time.sleep(0.05)
    assert api.hits
