"""Phone dashboard (phase 4, item M): /m, /api/v2/m/home and /api/v2/m/accounts on a fake fleet of 107 accounts."""

import gzip
import json
import os
import re
import time
from pathlib import Path

from flask import Flask

import website.api_v2 as api_v2

CSRF_RE = re.compile(r'name="csrf-token" content="([^"]*)"')
PASSWORD = "phone_test_pass"
FLEET = 107


class FakeSupervisor:
    def status(self):
        return {"state": "running", "pid": 4242, "started_at": "2026-11-18T08:00:00", "uptime_s": 3725.0,
                "supervised": True, "last_exit": None}


def _setup(tmp_path, monkeypatch):
    app = Flask("website", root_path=os.path.dirname(os.path.abspath(api_v2.__file__)))
    api_v2._configured = False
    api_v2._fails.clear()
    api_v2._locked_until.clear()
    api_v2._bot_last.clear()
    store = api_v2.configure(password=PASSWORD, version="2.7-test", refresh_secs=2, port=1234,
                             host="0.0.0.0", app=app, root=str(tmp_path), lan_urls=["http://192.168.1.20:1234/"])
    api_v2.init_app(app)
    monkeypatch.setattr(api_v2, "_supervisor", lambda: FakeSupervisor())
    _write_live(tmp_path)
    store.refresh()
    return app, store


def _write_live(root, count=FLEET):
    now = time.time()
    accounts = []
    for i in range(count):
        accounts.append({
            "i": i + 1, "slot": i + 1, "id": str(100000000000000000 + i), "name": "acct%03d" % i,
            "rs": "ready", "phase": "online", "cap": False, "cap_flag": False, "sl": i % 9 == 0, "hold": False,
            "on": True, "stp": False, "gwd": None, "ls": 10.0, "bal": 1000 + i, "net": 0, "nogem": False,
            "hunt_on": True, "rc": 0, "gwb": None, "c": {"hunt": 50 + i, "battle": 3},
            "lr": {"hunt": now - 30 - i}, "gi": [{"guild_id": "900", "kb_min": 10.5, "events_min": 40.0}],
            "fg": "900",
        })
    data = {"schema": 1, "seq": 1, "written_at": now, "started_at": now - 600, "pid": 1, "running": True,
            "run_id": "run-1", "configured_total": count, "autostop": {"enabled": False},
            "quotes": {"pool": 100, "cache": 100, "last_ok_at": now, "last_error": None}, "accounts": accounts}
    folder = Path(root) / "utils" / "data"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "live_status.json").write_text(json.dumps(data), encoding="utf-8")


def _login(client):
    res = client.post("/login", data={"password": PASSWORD})
    assert res.status_code == 302
    page = client.get("/")
    match = CSRF_RE.search(page.get_data(as_text=True))
    assert match and match.group(1)
    return match.group(1)


def test_the_phone_routes_refuse_a_visitor_without_a_login(tmp_path, monkeypatch):
    app, store = _setup(tmp_path, monkeypatch)
    client = app.test_client()
    res = client.get("/m")
    assert res.status_code == 302 and "/login" in res.headers["Location"]
    assert client.get("/api/v2/m/home").status_code == 401
    assert client.get("/api/v2/m/accounts").status_code == 401
    store.stop()


def test_the_phone_page_has_the_viewport_token_and_no_inline_script(tmp_path, monkeypatch):
    app, store = _setup(tmp_path, monkeypatch)
    client = app.test_client()
    _login(client)
    res = client.get("/m")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert 'viewport-fit=cover' in html
    assert 'name="csrf-token"' in html
    assert "/static/v2/m.js" in html and "/static/v2/m.css" in html
    assert "<script>" not in html  # the page's Content-Security-Policy allows scripts from this origin only
    assert res.headers["Content-Security-Policy"].startswith("default-src 'self'")
    store.stop()


def test_home_is_one_small_answer_with_an_etag_and_a_304(tmp_path, monkeypatch):
    app, store = _setup(tmp_path, monkeypatch)
    client = app.test_client()
    _login(client)
    res = client.get("/api/v2/m/home")
    assert res.status_code == 200
    data = json.loads(res.get_data())
    assert set(data) >= {"bot", "summary", "attention", "sources_ok", "lan", "bot_version"}
    assert data["bot"]["state"] == "running" and data["bot"]["uptime_min"] == 62
    assert data["summary"]["accounts_total"] == FLEET
    assert data["summary"]["accounts_online"] == FLEET
    assert len(data["attention"]) <= 10
    assert data["lan"]["lan_urls"] == ["http://192.168.1.20:1234/"]
    etag = res.headers["ETag"]
    again = client.get("/api/v2/m/home", headers={"If-None-Match": etag})
    assert again.status_code == 304 and again.get_data() == b""
    store.stop()


def test_home_stays_under_six_kilobytes_gzipped_for_107_accounts(tmp_path, monkeypatch):
    app, store = _setup(tmp_path, monkeypatch)
    client = app.test_client()
    _login(client)
    res = client.get("/api/v2/m/home", headers={"Accept-Encoding": "gzip"})
    body = res.get_data()
    if res.headers.get("Content-Encoding") == "gzip":
        body = gzip.decompress(body)
        wire = len(res.get_data())
    else:
        wire = len(body)
    assert wire < 6 * 1024, wire
    store.stop()


def test_account_rows_are_small_paged_and_gzipped_when_long(tmp_path, monkeypatch):
    app, store = _setup(tmp_path, monkeypatch)
    client = app.test_client()
    _login(client)
    first = json.loads(client.get("/api/v2/m/accounts?offset=0&limit=25").get_data())
    assert first["total"] == FLEET and len(first["rows"]) == 25
    assert first["fields"] == ["id", "name", "state", "health", "hunts_per_min", "last_hunt_ago_s", "cowoncy", "captcha_today"]
    row_bytes = [len(json.dumps(row, separators=(",", ":"))) for row in first["rows"]]
    assert max(row_bytes) < 120, max(row_bytes)
    second = json.loads(client.get("/api/v2/m/accounts?offset=25&limit=25").get_data())
    assert second["rows"][0][0] != first["rows"][0][0]
    tail = json.loads(client.get("/api/v2/m/accounts?offset=100&limit=25").get_data())
    assert len(tail["rows"]) == FLEET - 100
    long_res = client.get("/api/v2/m/accounts?offset=0&limit=100", headers={"Accept-Encoding": "gzip"})
    assert long_res.headers.get("Content-Encoding") == "gzip"
    store.stop()


def test_search_and_state_filter_the_rows(tmp_path, monkeypatch):
    app, store = _setup(tmp_path, monkeypatch)
    client = app.test_client()
    _login(client)
    found = json.loads(client.get("/api/v2/m/accounts?q=acct01").get_data())
    assert found["total"] == 10
    assert all("acct01" in row[1] for row in found["rows"])
    sleeping = json.loads(client.get("/api/v2/m/accounts?state=sleeping&limit=100").get_data())
    assert sleeping["total"] == len([i for i in range(FLEET) if i % 9 == 0])
    assert all(row[2] == "sleeping" for row in sleeping["rows"])
    store.stop()


def test_removed_accounts_are_hidden_from_the_phone(tmp_path, monkeypatch):
    app, store = _setup(tmp_path, monkeypatch)
    client = app.test_client()
    _login(client)
    store.snapshot.entries["999999999999999999"] = {"id": "999999999999999999", "name": "old", "state": "offline",
                                                     "health": "unknown", "removed": True}
    total = json.loads(client.get("/api/v2/m/accounts?limit=1").get_data())["total"]
    assert total == FLEET
    store.stop()


def test_bad_parameters_are_refused(tmp_path, monkeypatch):
    app, store = _setup(tmp_path, monkeypatch)
    client = app.test_client()
    _login(client)
    assert client.get("/api/v2/m/accounts?offset=-1").status_code == 400
    assert client.get("/api/v2/m/accounts?limit=500").status_code == 400
    assert client.get("/api/v2/m/accounts?state=bogus").status_code == 400
    store.stop()


def test_post_to_the_phone_api_is_refused(tmp_path, monkeypatch):
    app, store = _setup(tmp_path, monkeypatch)
    client = app.test_client()
    token = _login(client)
    headers = {"X-Dusk-CSRF": token}
    assert client.post("/api/v2/m/home", headers=headers, json={}).status_code == 405
    assert client.post("/api/v2/m/accounts", headers=headers, json={}).status_code == 405
    store.stop()
