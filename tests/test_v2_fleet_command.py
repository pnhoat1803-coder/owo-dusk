"""Fleet command center: the write API (website/api_v2.py) and the bot side that runs its commands (core/live_status.py)."""

import asyncio
import json
import os
import re
import threading
import time
import types

from flask import Flask

import website.api_v2 as api_v2
from core.live_status import LiveStatusWriter

PASSWORD = "fleet-test-pass"
AUTH = {"password": PASSWORD}
CSRF_RE = re.compile(r'name="csrf-token" content="([^"]*)"')
DEFAULT_SETTINGS = {
    "autoDaily": True,
    "sleep": {"enabled": True, "frequencyPercentage": 5, "checkTime": [10, 20], "sleepTime": [9, 17]},
    "commands": {"hunt": {"enabled": True, "cooldown": [17, 43]}},
}


def _app(tmp_path):
    # The templates and static files are the dashboard's, in website/ (the page is rendered by the real template)
    app = Flask("website", root_path=os.path.dirname(os.path.abspath(api_v2.__file__)))
    api_v2._configured = False
    api_v2._fails.clear()
    api_v2._locked_until.clear()
    api_v2._bot_last.clear()
    store = api_v2.configure(
        password=PASSWORD, version="2.5-test", refresh_secs=2, port=1200,
        host="127.0.0.1", app=app, root=str(tmp_path),
    )
    api_v2.init_app(app)
    return app, store


def _signed(app):
    """A test client signed in as the page is, and the headers of its POST calls: the password and the CSRF token"""
    client = app.test_client()
    res = client.post("/login", data={"password": PASSWORD})
    assert res.status_code == 302
    match = CSRF_RE.search(client.get("/").get_data(as_text=True))
    assert match and match.group(1)
    return client, {**AUTH, "X-Dusk-CSRF": match.group(1)}


def _config_dir(tmp_path):
    folder = tmp_path / "config"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


# ---- write API ------------------------------------------------------------
def test_control_refuses_a_body_that_is_not_json(tmp_path):
    app, store = _app(tmp_path)
    try:
        client, headers = _signed(app)
        res = client.post(
            "/api/v2/control", headers={**headers, "Content-Type": "text/plain"},
            data='{"target": "all", "action": "sleep"}',
        )
        assert res.status_code == 415
        assert not (tmp_path / "utils" / "data" / "web_commands.json").exists()
    finally:
        store.stop()


def test_control_prunes_old_commands_and_refuses_unknown_actions(tmp_path):
    app, store = _app(tmp_path)
    try:
        client, headers = _signed(app)
        cmd_file = tmp_path / "utils" / "data" / "web_commands.json"
        cmd_file.parent.mkdir(parents=True, exist_ok=True)
        old = {"cmd_id": "old1", "action": "sleep", "target": "all", "value": None, "created_at": time.time() - 300}
        cmd_file.write_text(json.dumps([old]), encoding="utf-8")

        res = client.post("/api/v2/control", headers=headers, json={"target": "all", "action": "wake"})
        assert res.status_code == 200
        queued = _read(cmd_file)
        assert [c["cmd_id"] for c in queued] == [res.get_json()["cmd_id"]]

        res = client.post("/api/v2/control", headers=headers, json={"target": "all", "action": "shutdown"})
        assert res.status_code == 400
        res = client.post("/api/v2/control", headers=headers, json={"target": "../x", "action": "wake"})
        assert res.status_code == 400
        res = client.post("/api/v2/control", headers=headers, json={"target": "all", "action": "switch_channel", "value": "12"})
        assert res.status_code == 400
    finally:
        store.stop()


def test_parallel_control_posts_are_all_kept(tmp_path):
    app, store = _app(tmp_path)
    try:
        errors = []

        def post_one(index):
            try:
                client, headers = _signed(app)
                res = client.post(
                    "/api/v2/control", headers=headers,
                    json={"target": str(1000 + index), "action": "wake"},
                )
                if res.status_code != 200:
                    errors.append(res.status_code)
            except Exception as error:  # pragma: no cover - reported below
                errors.append(repr(error))

        threads = [threading.Thread(target=post_one, args=(i,)) for i in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        cmd_file = tmp_path / "utils" / "data" / "web_commands.json"
        assert len(_read(cmd_file)) == 20
    finally:
        store.stop()


def test_dashboard_save_keeps_keys_the_form_does_not_show(tmp_path):
    app, store = _app(tmp_path)
    try:
        cfg = _config_dir(tmp_path)
        dash = cfg / "dashboard_view.json"
        dash.write_text(json.dumps({
            "lastHuntWarnSeconds": 300, "lastHuntBadSeconds": 900, "huntsSinceStartWarn": 260,
            "huntsSinceStartWarnUntil": 340, "refreshSeconds": 3,
        }), encoding="utf-8")
        client, headers = _signed(app)

        res = client.post("/api/v2/config", headers=headers, json={"scope": "dashboard", "config": {"refreshSeconds": 4}})
        assert res.status_code == 200
        assert res.get_json()["applies"] == "immediate"
        saved = _read(dash)
        assert saved["refreshSeconds"] == 4
        assert saved["huntsSinceStartWarn"] == 260 and saved["huntsSinceStartWarnUntil"] == 340

        refused = [
            {"refreshSeconds": True},  # a bool is not a number
            {"refreshSeconds": "5"},  # nor a string
            {"refreshSeconds": 500},  # out of range
            {"lastHuntWarnSeconds": 1000},  # warn above bad (900)
            {"webhookUrl": "https://example.invalid"},  # not a dashboard setting
        ]
        for config in refused:
            res = client.post("/api/v2/config", headers=headers, json={"scope": "dashboard", "config": config})
            assert res.status_code == 400, config
        assert _read(dash) == saved
    finally:
        store.stop()


def test_default_save_merges_and_refuses_type_changes(tmp_path):
    app, store = _app(tmp_path)
    try:
        cfg = _config_dir(tmp_path)
        settings = cfg / "settings.json"
        settings.write_text(json.dumps(DEFAULT_SETTINGS), encoding="utf-8")
        client, headers = _signed(app)

        res = client.post("/api/v2/config", headers=headers, json={"scope": "default", "config": {"autoDaily": False}})
        assert res.status_code == 200
        assert res.get_json()["applies"] == "next_login"
        saved = _read(settings)
        assert saved["autoDaily"] is False
        assert saved["sleep"]["sleepTime"] == [9, 17]  # not sent, so kept
        assert not (cfg / "settings.json.bak").exists()  # no .bak copy is written any more (D8)

        refused = [
            {"autoDaily": "no"},
            {"sleep": {"sleepTime": 300}},  # a single number crashes the bot's sleep check
            {"sleep": {"sleepTime": [30, 10]}},  # min above max
            {"sleep": {"frequencyPercentage": 150}},
            {"sleep": {"enabled": "yes"}},  # the sleep switch is a true or false value
        ]
        for config in refused:
            res = client.post("/api/v2/config", headers=headers, json={"scope": "default", "config": config})
            assert res.status_code == 400, config
        assert _read(settings) == saved
    finally:
        store.stop()


def test_per_account_settings_are_refused_and_nothing_is_written(tmp_path):
    """Per-account settings were removed: a Discord ID as scope answers 404 and writes no file"""
    app, store = _app(tmp_path)
    try:
        cfg = _config_dir(tmp_path)
        (cfg / "settings.json").write_text(json.dumps(DEFAULT_SETTINGS), encoding="utf-8")
        client, headers = _signed(app)

        res = client.post(
            "/api/v2/config", headers=headers,
            json={"scope": "424242424242", "config": {"autoDaily": False, "sleep": {"frequencyPercentage": 9}}},
        )
        assert res.status_code == 404
        res = client.get("/api/v2/config?scope=424242424242", headers=AUTH)
        assert res.status_code == 404
        assert not (cfg / "424242424242.settings.json").exists()
        assert _read(cfg / "settings.json") == DEFAULT_SETTINGS

        res = client.post("/api/v2/config", headers=headers, json={"scope": "../x", "config": {"autoDaily": False}})
        assert res.status_code == 400
        res = client.get("/api/v2/config?scope=../x", headers=AUTH)
        assert res.status_code in (400, 404)
    finally:
        store.stop()


def test_config_post_needs_a_json_body(tmp_path):
    app, store = _app(tmp_path)
    try:
        client, headers = _signed(app)
        res = client.post(
            "/api/v2/config", headers={**headers, "Content-Type": "text/plain"},
            data='{"scope": "dashboard", "config": {"refreshSeconds": 4}}',
        )
        assert res.status_code == 415
    finally:
        store.stop()


def test_summary_carries_the_chip_thresholds(tmp_path):
    """The Overview colours the captcha and error chips by these numbers (dashboard_view.json or the defaults)"""
    app, store = _app(tmp_path)
    try:
        res = app.test_client().get("/api/v2/summary", headers=AUTH)
        assert res.status_code == 200
        thresholds = res.get_json()["thresholds"]
        assert set(thresholds) == {"captchaWarnPerDay", "captchaBadPerDay", "errorsWarnPerHour", "errorsBadPerHour"}
        assert all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in thresholds.values())
    finally:
        store.stop()


# ---- bot side -------------------------------------------------------------
class _Status:
    def __init__(self):
        self.sleep = False
        self.captcha = False
        self.captcha_flag = False


class _Client:
    def __init__(self, uid, idx, loop=None):
        self.user = types.SimpleNamespace(id=uid)
        self.account_index = idx
        self.ch = types.SimpleNamespace(command_handler_status=_Status())
        self.loop = loop


def _writer(tmp_path, client, cmd_file):
    return LiveStatusWriter(
        path=tmp_path / "live_status.json", clients=lambda: {0: client}, accounts=lambda: {},
        autostop=lambda wall: {}, commands_path=cmd_file,
    )


def test_bot_runs_each_command_once_and_never_rewrites_the_queue(tmp_path):
    cmd_file = tmp_path / "utils" / "data" / "web_commands.json"
    cmd_file.parent.mkdir(parents=True, exist_ok=True)
    client = _Client(1111, 0)
    writer = _writer(tmp_path, client, cmd_file)
    now = time.time()

    queue = [{"cmd_id": "w1", "action": "wake", "target": "all", "value": None, "created_at": now}]
    text = json.dumps(queue)
    cmd_file.write_text(text, encoding="utf-8")
    writer._process_web_commands()
    assert client.ch.command_handler_status.sleep is False
    assert cmd_file.read_text(encoding="utf-8") == text  # the bot only reads the file

    client.ch.command_handler_status.sleep = True  # changed by something else in the meantime
    writer._process_web_commands()  # w1 is still in the file, but must not run again
    assert client.ch.command_handler_status.sleep is True


def test_bot_ignores_unknown_and_expired_commands(tmp_path):
    cmd_file = tmp_path / "utils" / "data" / "web_commands.json"
    cmd_file.parent.mkdir(parents=True, exist_ok=True)
    client = _Client(1111, 0)
    client.ch.command_handler_status.captcha = True
    now = time.time()
    cmd_file.write_text(json.dumps([
        {"cmd_id": "bad", "action": "os.system", "target": "all", "value": None, "created_at": now},
        {"cmd_id": "old", "action": "restart_captcha", "target": "all", "value": None, "created_at": now - 120},
    ]), encoding="utf-8")
    _writer(tmp_path, client, cmd_file)._process_web_commands()
    assert client.ch.command_handler_status.captcha is True


def test_switch_channel_runs_the_switcher_webhook(tmp_path):
    cmd_file = tmp_path / "utils" / "data" / "web_commands.json"
    cmd_file.parent.mkdir(parents=True, exist_ok=True)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    calls = []
    channel_id = 123456789012345678

    class Channel:
        id = channel_id
        name = "general"

    class Switcher:
        async def handle_webhook(self, channel):
            calls.append(("webhook", channel.id))

    class LoopClient(_Client):
        async def fetch_channel(self, cid):
            return Channel()

        async def empty_checks_and_switch(self, channel):
            calls.append(("switch", channel.id))

        def get_cog(self, name):
            return Switcher() if name == "ChannelSwitcher" else None

        async def log(self, text, color):
            calls.append(("log", text))

    try:
        client = LoopClient(1111, 0, loop=loop)
        writer = _writer(tmp_path, client, cmd_file)
        writer._execute_command_on_client(client, "switch_channel", str(channel_id))
        deadline = time.time() + 5
        while not any(c[0] == "log" for c in calls) and time.time() < deadline:
            time.sleep(0.02)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
    assert ("switch", channel_id) in calls
    assert ("webhook", channel_id) in calls
    assert any(c[0] == "log" and "Switched channel" in c[1] for c in calls)
