import json
import os
import re
import tempfile
import time
from pathlib import Path
from flask import Flask

import website.api_v2 as api_v2
from core.live_status import LiveStatusWriter

CSRF_RE = re.compile(r'name="csrf-token" content="([^"]*)"')


def _setup_test_app(tmp_path, password="test_secret_pass"):
    # The templates and static files are the dashboard's, in website/ (the page is rendered by the real template)
    app = Flask("website", root_path=os.path.dirname(os.path.abspath(api_v2.__file__)))
    api_v2._configured = False
    api_v2._fails.clear()
    api_v2._locked_until.clear()
    api_v2._bot_last.clear()
    store = api_v2.configure(
        password=password,
        version="2.5-test",
        refresh_secs=2,
        port=1200,
        host="127.0.0.1",
        app=app,
        root=str(tmp_path),
    )
    api_v2.init_app(app)
    return app, store


def _csrf_token(client, password):
    """Signs the test client in, as the login page does, and returns the CSRF token the dashboard page carries"""
    res = client.post("/login", data={"password": password})
    assert res.status_code == 302
    page = client.get("/")
    match = CSRF_RE.search(page.get_data(as_text=True))
    assert match and match.group(1)
    return match.group(1)


def test_header_auth_rate_limiting(tmp_path):
    app, store = _setup_test_app(tmp_path, "secret123")
    client = app.test_client()

    # 5 failed attempts: each one is refused, and the fifth locks this address
    for i in range(5):
        res = client.get("/api/v2/summary", headers={"password": "wrong"})
        assert res.status_code == 401

    # The next attempt hits the lock (429), and the right password is refused too
    res = client.get("/api/v2/summary", headers={"password": "wrong"})
    assert res.status_code == 429
    assert b"Too many" in res.data
    res = client.get("/api/v2/summary", headers={"password": "secret123"})
    assert res.status_code == 429
    store.stop()


def test_post_control_endpoint(tmp_path):
    app, store = _setup_test_app(tmp_path, "secret123")
    client = app.test_client()

    # Unauthorized POST
    res = client.post("/api/v2/control", json={"target": "all", "action": "sleep"})
    assert res.status_code == 401

    # Signed in: every POST also carries the CSRF token of the session
    token = _csrf_token(client, "secret123")
    headers = {"password": "secret123", "X-Dusk-CSRF": token}
    res = client.post(
        "/api/v2/control",
        headers=headers,
        json={"target": "all", "action": "sleep"},
    )
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    assert data["action"] == "sleep"
    assert data["target"] == "all"
    assert "cmd_id" in data

    # Verify command is written to utils/data/web_commands.json
    cmd_file = tmp_path / "utils" / "data" / "web_commands.json"
    assert cmd_file.exists()
    commands = json.loads(cmd_file.read_text(encoding="utf-8"))
    assert len(commands) == 1
    assert commands[0]["cmd_id"] == data["cmd_id"]
    assert commands[0]["action"] == "sleep"

    # Test invalid action
    res = client.post(
        "/api/v2/control",
        headers=headers,
        json={"target": "all", "action": "nuke_everything"},
    )
    assert res.status_code == 400

    # Test switch_channel without channel id
    res = client.post(
        "/api/v2/control",
        headers=headers,
        json={"target": "12345", "action": "switch_channel"},
    )
    assert res.status_code == 400

    # Test switch_channel with valid snowflake
    res = client.post(
        "/api/v2/control",
        headers=headers,
        json={"target": "12345", "action": "switch_channel", "value": "123456789012345678"},
    )
    assert res.status_code == 200
    store.stop()


def test_live_status_ipc_execution(tmp_path):
    cmd_file = tmp_path / "utils" / "data" / "web_commands.json"
    cmd_file.parent.mkdir(parents=True, exist_ok=True)

    class DummyStatus:
        def __init__(self):
            self.sleep = False
            self.captcha = True
            self.captcha_flag = True

    class DummyCH:
        def __init__(self):
            self.command_handler_status = DummyStatus()

    class DummyUser:
        def __init__(self, uid):
            self.id = uid

    class DummyClient:
        def __init__(self, uid, idx):
            self.user = DummyUser(uid)
            self.account_index = idx
            self.ch = DummyCH()
            self.loop = None

    c1 = DummyClient(1111, 0)
    c2 = DummyClient(2222, 1)
    fake_clients = {0: c1, 1: c2}

    writer = LiveStatusWriter(
        path=tmp_path / "live_status.json",
        clients=lambda: fake_clients,
        accounts=lambda: {},
        autostop=lambda w: {},
        commands_path=cmd_file,
    )

    # Queue commands: sleep c1, reset captcha on all
    commands = [
        {"cmd_id": "c1_sleep", "action": "sleep", "target": "1111", "created_at": time.time()},
        {"cmd_id": "all_cap", "action": "restart_captcha", "target": "all", "created_at": time.time()},
    ]
    cmd_file.write_text(json.dumps(commands), encoding="utf-8")

    # Run command processing
    writer._process_web_commands()

    # c1 should be sleeping
    assert c1.ch.command_handler_status.sleep is True
    # c2 should not be sleeping
    assert c2.ch.command_handler_status.sleep is False

    # Both should have captcha flags reset
    assert c1.ch.command_handler_status.captcha is False
    assert c1.ch.command_handler_status.captcha_flag is False
    assert c2.ch.command_handler_status.captcha is False
    assert c2.ch.command_handler_status.captcha_flag is False

    # Second pass: already processed IDs should not re-run
    c1.ch.command_handler_status.sleep = False
    writer._process_web_commands()
    assert c1.ch.command_handler_status.sleep is False


def test_get_and_post_config_scopes(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)

    # Create dummy config files
    dash_json = config_dir / "dashboard_view.json"
    dash_json.write_text(json.dumps({"refreshSeconds": 5, "lastHuntWarnSeconds": 300}), encoding="utf-8")

    global_json = config_dir / "global_settings.json"
    global_json.write_text(
        json.dumps({
            "website": {"password": "REAL_SECRET_PASSWORD", "port": 1200},
            "ocrApi": "REAL_OCR_KEY",
            "webhook": {
                "webhookUrl": "https://discord.com/api/webhooks/FAKE-test-webhook/FAKE-test-token",
                "webhookUserIdToPingOnCaptcha": "99999",
                "enabled": True,
            },
            "account": {"waveSize": 15},
        }),
        encoding="utf-8",
    )

    settings_json = config_dir / "settings.json"
    settings_json.write_text(json.dumps({"autoDaily": True, "commands": {"hunt": {"enabled": True}}}), encoding="utf-8")

    app, store = _setup_test_app(tmp_path, "secret123")
    client = app.test_client()
    token = _csrf_token(client, "secret123")
    write_headers = {"password": "secret123", "X-Dusk-CSRF": token}

    # 1. GET dashboard config
    res = client.get("/api/v2/config?scope=dashboard", headers={"password": "secret123"})
    assert res.status_code == 200
    assert res.get_json()["hot_reloaded"] is True
    assert res.get_json()["config"]["refreshSeconds"] == 5

    # 2. GET global config (MUST redact secrets)
    res = client.get("/api/v2/config?scope=global", headers={"password": "secret123"})
    assert res.status_code == 200
    cfg = res.get_json()["config"]
    assert cfg["website"]["password"] == "***REDACTED***"
    assert cfg["ocrApi"] == "***REDACTED***"
    assert cfg["webhook"]["webhookUrl"] == "***REDACTED***"
    assert cfg["webhook"]["webhookUserIdToPingOnCaptcha"] == "***REDACTED***"
    assert cfg["account"]["waveSize"] == 15

    # 3. GET default settings
    res = client.get("/api/v2/config?scope=default", headers={"password": "secret123"})
    assert res.status_code == 200
    assert res.get_json()["config"]["autoDaily"] is True

    # 4. GET for a per-account scope: removed, so 404 (every account uses the default settings)
    res = client.get("/api/v2/config?scope=12345678", headers={"password": "secret123"})
    assert res.status_code == 404

    # 5. POST dashboard config (hot-reloaded)
    res = client.post(
        "/api/v2/config",
        headers=write_headers,
        json={"scope": "dashboard", "config": {"refreshSeconds": 2, "lastHuntWarnSeconds": 250}},
    )
    assert res.status_code == 200
    assert res.get_json()["hot_reloaded"] is True
    assert not (config_dir / "dashboard_view.json.bak").exists()  # no .bak copy is written any more (D8)
    assert not (config_dir / "dashboard_view.json.bak1").exists()
    saved_dash = json.loads(dash_json.read_text(encoding="utf-8"))
    assert saved_dash["refreshSeconds"] == 2

    # 6. POST global config (MUST preserve secrets even if redacted string submitted)
    res = client.post(
        "/api/v2/config",
        headers=write_headers,
        json={
            "scope": "global",
            "config": {
                "website": {"password": "***REDACTED***", "port": 1300},
                "ocrApi": "***REDACTED***",
                "webhook": {
                    "webhookUrl": "***REDACTED***",
                    "webhookUserIdToPingOnCaptcha": "***REDACTED***",
                    "enabled": False,
                },
                "account": {"waveSize": 20},
            },
        },
    )
    assert res.status_code == 200
    saved_global = json.loads(global_json.read_text(encoding="utf-8"))
    assert saved_global["website"]["password"] == "REAL_SECRET_PASSWORD"  # Preserved!
    assert saved_global["website"]["port"] == 1300  # Updated!
    assert saved_global["ocrApi"] == "REAL_OCR_KEY"  # Preserved!
    assert saved_global["webhook"]["webhookUrl"] == "https://discord.com/api/webhooks/FAKE-test-webhook/FAKE-test-token"  # Preserved!
    assert saved_global["account"]["waveSize"] == 20  # Updated!
    assert not (config_dir / "global_settings.json.bak").exists()  # no .bak copy is written any more (D8)

    # 7. POST for a per-account scope is refused (404) and no per-account file is written
    res = client.post(
        "/api/v2/config",
        headers=write_headers,
        json={"scope": "12345678", "config": {"autoDaily": False, "custom": 42}},
    )
    assert res.status_code == 404
    assert not (config_dir / "12345678.settings.json").exists()
    store.stop()


def test_all_v2_dom_ids_and_svg_symbols():
    with open("website/templates/v2.html", encoding="utf-8") as f:
        html = f.read()

    required_ids = [
        "banner", "pill-conn", "bot-state", "bot-cli", "bot-msg", "bot-start", "bot-stop", "bot-restart",
        "ov-bot", "ov-bot-start", "ov-bot-stop", "ov-bot-restart", "modal-start", "modal-start-min",
        "modal-start-ok", "kpis", "ring", "legend", "attention", "uptime", "sources",
        "view-overview", "view-accounts", "view-events", "view-network", "view-logs", "view-runtime", "view-account", "view-config",
        "q", "f-state", "f-health", "f-sort", "f-order", "f-size", "f-removed", "m-cards", "m-table", "cards", "tablebox", "thead", "rows",
        "acc-empty", "p-prev", "p-next", "p-info", "d-title", "d-chips", "d-why", "d-groups", "c-hunts", "c-cow", "c-net",
        "d-events", "e-type", "e-q", "e-info", "e-list", "n-info", "n-bars", "l-q", "l-level", "l-follow", "console",
        "c-runtime", "r-info", "lan-list", "lan-none", "lan-hint", "cfg-tab-server", "cfg-pane-server", "form-cfg-server",
        "cfg-srv-host", "cfg-srv-port", "cfg-srv-restart", "cfg-srv-launch", "cfg-srv-stopwith"
    ]

    for req_id in required_ids:
        assert f'id="{req_id}"' in html or f"id='{req_id}'" in html, f"Missing required ID: {req_id}"

    # Check data-view attributes on tabs
    for view in ["overview", "accounts", "events", "network", "logs", "runtime", "config"]:
        assert f'data-view="{view}"' in html

    # The page carries the CSRF token for the POST calls
    assert 'name="csrf-token"' in html

    # Check 31 original symbols in icons.svg
    with open("website/static/v2/icons.svg", encoding="utf-8") as f:
        svg_content = f.read()

    original_31 = [
        "i-logo", "i-overview", "i-accounts", "i-events", "i-network", "i-logs", "i-runtime",
        "i-hunt", "i-battle", "i-cowoncy", "i-daily", "i-gem", "i-captcha", "i-sleep", "i-error",
        "i-reconnect", "i-clock", "i-search", "i-sort", "i-filter", "i-close", "i-info", "i-check",
        "i-pause", "i-stop", "i-offline", "i-grid", "i-list", "i-back", "i-level", "i-wave"
    ]
    for sym in original_31:
        assert f'id="{sym}"' in svg_content, f"Missing required symbol: {sym}"

    # Check new symbols
    for sym in ["i-settings", "i-power", "i-refresh", "i-channel", "i-play", "i-restart", "i-half"]:
        assert f'id="{sym}"' in svg_content, f"Missing new symbol: {sym}"


def test_logout_session_rotation(tmp_path):
    app, store = _setup_test_app(tmp_path, "secret123")
    client = app.test_client()

    # Login
    res = client.post("/login", data={"password": "secret123"})
    assert res.status_code == 302

    # Access authorized
    res = client.get("/api/v2/summary")
    assert res.status_code == 200

    # Logout
    res = client.post("/logout")
    assert res.status_code == 302
    assert res.headers["Location"].endswith("/login")

    # Access after logout should be unauthorized
    res = client.get("/api/v2/summary")
    assert res.status_code == 401
    store.stop()
