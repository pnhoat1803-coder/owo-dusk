"""The dashboard's LAN access and login rules (Phase 3, I): the bind address and its password rule, the LAN addresses (found
without sending a packet), the login lockout, the session cookie flags, and the CSRF and Origin checks on every POST under
/api/v2/. Flask test client only: nothing listens on the network, and the bot files are temp files."""

import json
import os
import re
import socket
import time

import pytest
from flask import Flask

import website.api_v2 as api_v2
from utils import bot_state
from utils import lan

PASSWORD = "lan-test-pass"
CSRF_RE = re.compile(r'name="csrf-token" content="([^"]*)"')


def _app(tmp_path, monkeypatch, lan_urls=None, password_banner=False):
    data = tmp_path / "utils" / "data"
    monkeypatch.setattr(bot_state, "PID_PATH", data / "bot.pid")
    monkeypatch.setattr(bot_state, "CONTROL_DIR", data / "control")
    monkeypatch.setattr(bot_state, "STOP_REQUEST_PATH", data / "control" / "stop.request")
    monkeypatch.setattr(bot_state, "LAST_EXIT_PATH", data / "control" / "last_exit.json")
    # The templates and static files are the dashboard's, in website/ (the page is rendered by the real template)
    app = Flask("website", root_path=os.path.dirname(os.path.abspath(api_v2.__file__)))
    api_v2._configured = False
    api_v2._fails.clear()
    api_v2._locked_until.clear()
    api_v2._bot_last.clear()
    store = api_v2.configure(
        password=PASSWORD, version="test", refresh_secs=2, port=18133, host="127.0.0.1", app=app,
        root=str(tmp_path), lan_urls=lan_urls, password_banner=password_banner,
    )
    api_v2.init_app(app)
    return app, store


def _signed_in(app):
    client = app.test_client()
    res = client.post("/login", data={"password": PASSWORD})
    assert res.status_code == 302
    page = client.get("/")
    assert page.status_code == 200
    match = CSRF_RE.search(page.get_data(as_text=True))
    assert match and match.group(1), "the page carries the CSRF token in a meta tag"
    return client, match.group(1)


# ---- the bind address and the password rule -----------------------------------------------------
@pytest.mark.parametrize("host,password,expected", [
    ("0.0.0.0", "", ("127.0.0.1", "no_password")),  # no password: this machine only, with the banner
    ("0.0.0.0", "short", ("127.0.0.1", "short_password")),  # five characters is not enough
    ("0.0.0.0", "x" * lan.MIN_PASSWORD, ("0.0.0.0", "lan_ok")),  # MIN_PASSWORD characters: every device on the network may connect
    ("127.0.0.1", "", ("127.0.0.1", "local_only_config")),  # this machine needs no password
    ("192.168.1.20", "", ("127.0.0.1", "no_password")),  # one LAN address needs the password too
    ("192.168.1.20", "a-long-enough-password", ("192.168.1.20", "lan_ok")),
])
def test_a_lan_address_needs_a_password_of_the_minimum_length(host, password, expected):
    assert lan.effective_bind(host, password) == expected


def test_the_configured_host_default_and_enablehost_is_ignored():
    assert lan.configured_host({}) == "0.0.0.0"
    assert lan.configured_host({"enableHost": True}) == "0.0.0.0"
    assert lan.configured_host({"enableHost": False}) == "0.0.0.0"  # an old file with enableHost false listens on the LAN
    assert lan.configured_host({"host": "", "enableHost": False}) == "0.0.0.0"
    assert lan.configured_host({"host": "0.0.0.0", "enableHost": False}) == "0.0.0.0"
    assert lan.configured_host({"host": "192.168.1.20", "enableHost": False}) == "192.168.1.20"
    assert lan.configured_host({"host": "127.0.0.1", "enableHost": True}) == "127.0.0.1"
    assert lan.effective_bind(lan.configured_host({"host": "not an address"}), "x" * 20) == ("127.0.0.1", "invalid_host")


# ---- LAN addresses, found without sending a packet ----------------------------------------------
def test_lan_addresses_come_from_the_network_setup_and_no_packet_is_sent(monkeypatch):
    """The UDP connect and the host name lookup are answered by fakes here, so the test is offline and fixed"""
    def no_packet(*args, **kwargs):
        raise AssertionError("the LAN address check must not send a packet")

    for name in ("sendto", "send", "sendall"):
        monkeypatch.setattr(socket.socket, name, no_packet)
    monkeypatch.setattr(socket.socket, "connect", lambda self, address: None)  # UDP connect: no packet
    monkeypatch.setattr(socket.socket, "getsockname", lambda self: ("192.168.1.20", 0))

    def fake_lookup(host, port, family=0, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))
                for ip in ("127.0.0.1", "169.254.7.7", "10.0.0.5", "192.168.1.20")]

    monkeypatch.setattr(socket, "getaddrinfo", fake_lookup)
    assert lan.lan_addresses() == ["192.168.1.20", "10.0.0.5"]  # loopback and link-local dropped, no duplicates


def test_usable_addresses_and_the_urls_and_log_line():
    assert not lan.usable_lan_ipv4("127.0.0.1")
    assert not lan.usable_lan_ipv4("169.254.10.1")
    assert not lan.usable_lan_ipv4("0.0.0.0")
    assert lan.usable_lan_ipv4("192.168.1.20")
    assert lan.lan_urls("127.0.0.1", 18133) == []
    assert lan.lan_urls("192.168.1.20", 18133) == ["http://192.168.1.20:18133"]
    assert lan.lan_urls("0.0.0.0", 18133, addresses=["10.0.0.5", "192.168.1.20"]) == [
        "http://10.0.0.5:18133", "http://192.168.1.20:18133",
    ]
    assert lan.startup_lines("0.0.0.0", 18133, ["http://192.168.1.20:18133"], "lan_ok") == [
        "Dashboard (LAN): http://192.168.1.20:18133",
        "Dashboard (máy này): http://127.0.0.1:18133",
    ]
    assert lan.startup_lines("127.0.0.1", 18133, [], "local_only_config") == [
        "Dashboard chỉ mở trên máy này (127.0.0.1) vì website.host = 127.0.0.1. "
        "Đặt website.host = 0.0.0.0 để mở cho mạng LAN.",
        "Dashboard (máy này): http://127.0.0.1:18133",
    ]


# ---- login hardening ------------------------------------------------------------------------------
def test_five_wrong_passwords_refuse_the_address_for_five_minutes(tmp_path, monkeypatch):
    app, store = _app(tmp_path, monkeypatch)
    try:
        client = app.test_client()
        for _ in range(5):
            res = client.post("/login", data={"password": "wrong-pass-1"})
            assert res.status_code == 401
        res = client.post("/login", data={"password": PASSWORD})  # even the right password is refused now
        assert res.status_code == 429
        assert res.headers.get("Retry-After") == "300"
        assert b"Wait 5 minutes" in res.data
        res = client.get("/api/v2/summary", headers={"password": PASSWORD})
        assert res.status_code == 429
        assert res.get_json()["code"] == "locked"
    finally:
        store.stop()


def test_the_lock_ends_after_its_five_minutes(tmp_path, monkeypatch):
    app, store = _app(tmp_path, monkeypatch)
    try:
        client = app.test_client()
        for _ in range(5):
            client.post("/login", data={"password": "wrong-pass-1"})
        assert client.post("/login", data={"password": PASSWORD}).status_code == 429
        # Move the lock into the past, as five minutes later would
        api_v2._locked_until["127.0.0.1"] = time.monotonic() - 1
        assert client.post("/login", data={"password": PASSWORD}).status_code == 302
    finally:
        store.stop()


def test_the_session_cookie_is_httponly_strict_and_secure_only_over_https(tmp_path, monkeypatch):
    app, store = _app(tmp_path, monkeypatch)
    try:
        res = app.test_client().post("/login", data={"password": PASSWORD})
        cookie = res.headers.get("Set-Cookie", "")
        assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
        assert "Secure" not in cookie  # plain HTTP on the LAN
        res = app.test_client().post("/login", data={"password": PASSWORD}, base_url="https://localhost")
        assert "Secure" in res.headers.get("Set-Cookie", "")
    finally:
        store.stop()


# ---- CSRF and Origin on every POST under /api/v2/ -------------------------------------------------
def test_a_post_under_api_v2_needs_the_session_csrf_token_and_the_same_origin(tmp_path, monkeypatch):
    app, store = _app(tmp_path, monkeypatch)
    try:
        client, token = _signed_in(app)
        body = {"target": "all", "action": "wake"}
        assert client.post("/api/v2/control", json=body).status_code == 403  # no header
        assert client.post("/api/v2/control", json=body, headers={"X-Dusk-CSRF": "not-the-token"}).status_code == 403
        bad_origin = {"X-Dusk-CSRF": token, "Origin": "http://evil.example"}
        assert client.post("/api/v2/control", json=body, headers=bad_origin).status_code == 403
        right = {"X-Dusk-CSRF": token, "Origin": "http://localhost"}  # the test client's host is localhost
        assert client.post("/api/v2/control", json=body, headers=right).status_code == 200
    finally:
        store.stop()


def test_the_password_header_alone_cannot_post(tmp_path, monkeypatch):
    app, store = _app(tmp_path, monkeypatch)
    try:
        res = app.test_client().post(
            "/api/v2/control", headers={"password": PASSWORD}, json={"target": "all", "action": "wake"},
        )
        assert res.status_code == 403
    finally:
        store.stop()


def test_the_bot_actions_are_limited_to_one_per_five_seconds_per_session():
    api_v2._bot_last.clear()
    assert api_v2._bot_wait("session-a") == 0
    assert api_v2._bot_wait("session-a") > 0
    assert api_v2._bot_wait("session-b") == 0  # another session is not held back


def test_the_bot_status_is_readable_after_a_login(tmp_path, monkeypatch):
    app, store = _app(tmp_path, monkeypatch)
    try:
        client, _token = _signed_in(app)
        res = client.get("/api/v2/bot")
        assert res.status_code == 200
        assert res.get_json()["state"] == "stopped"  # no bot.pid in the temp folder
    finally:
        store.stop()


def test_the_summary_carries_the_lan_urls_and_the_banner_flag(tmp_path, monkeypatch):
    app, store = _app(tmp_path, monkeypatch, lan_urls=["http://192.168.1.20:18133"], password_banner=False)
    try:
        block = app.test_client().get("/api/v2/summary", headers={"password": PASSWORD}).get_json()["lan"]
        assert block["lan_urls"] == ["http://192.168.1.20:18133"]
        assert block["local_url"] == "http://127.0.0.1:18133"
        assert block["password_banner"] is False
    finally:
        store.stop()


def test_the_config_rejects_a_bad_host_and_non_boolean_bot_options(tmp_path, monkeypatch):
    app, store = _app(tmp_path, monkeypatch)
    try:
        client, token = _signed_in(app)
        cfg = tmp_path / "config"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "global_settings.json").write_text(json.dumps({"website": {"password": PASSWORD, "port": 18133}}), encoding="utf-8")
        headers = {"X-Dusk-CSRF": token}
        for website in ({"host": "example.com"}, {"host": "300.1.1.1"}, {"restartOnCrash": "yes"}):
            res = client.post("/api/v2/config", json={"scope": "global", "config": {"website": website}}, headers=headers)
            assert res.status_code == 400, website
        res = client.post("/api/v2/config", json={"scope": "global", "config": {"website": {"host": "192.168.1.20", "restartOnCrash": True}}}, headers=headers)
        assert res.status_code == 200
        saved = json.loads((cfg / "global_settings.json").read_text(encoding="utf-8"))
        assert saved["website"]["host"] == "192.168.1.20" and saved["website"]["password"] == PASSWORD
    finally:
        store.stop()
