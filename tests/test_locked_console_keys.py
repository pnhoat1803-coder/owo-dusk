import json

from flask import Flask

import website.api_v2 as api_v2

PASSWORD = "lock-test-pass"
SAVED_COMMAND = "echo saved-by-owner-test"
AUTH = {"password": PASSWORD}


CSRF_TOKEN = "test-csrf-token"


def _auth(client):
    """Puts the dashboard CSRF token in the session of this client and returns the headers for a POST
    (api_v2._csrf_problem): the token in the session and in the X-Dusk-CSRF header must match"""
    with client.session_transaction() as sess:
        sess["csrf"] = CSRF_TOKEN
    return {"password": PASSWORD, "X-Dusk-CSRF": CSRF_TOKEN}


def _setup_test_app(tmp_path):
    app = Flask(__name__)
    api_v2._configured = False
    api_v2._fails.clear()
    store = api_v2.configure(
        password=PASSWORD,
        version="2.6-test",
        refresh_secs=2,
        port=1200,
        host="127.0.0.1",
        app=app,
        root=str(tmp_path),
    )
    api_v2.init_app(app)
    return app, store


def _write_global(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "global_settings.json"
    path.write_text(
        json.dumps(
            {
                "account": {"waveSize": 15},
                "console": {
                    "commandToRunOnCaptcha": SAVED_COMMAND,
                    "commandToRunOnBan": "",
                    "runConsoleCommandOnCaptcha": False,
                    "runConsoleCommandOnBan": False,
                },
                "levelCheck": {"tesseractPath": "third_party/tesseract/tesseract.exe"},
                "website": {"password": PASSWORD, "port": 1200},
            },
            indent=4,
        ),
        encoding="utf-8",
    )
    return path


def _post_global(client, config):
    return client.post("/api/v2/config", headers=_auth(client), json={"scope": "global", "config": config})


def test_changing_a_locked_command_is_refused_and_the_file_is_unchanged(tmp_path):
    path = _write_global(tmp_path)
    before = path.read_bytes()
    app, store = _setup_test_app(tmp_path)
    try:
        res = _post_global(app.test_client(), {"console": {"commandToRunOnCaptcha": "echo injected"}})
        assert res.status_code == 400
        body = res.get_json()
        assert body["ok"] is False
        assert body["error"] == "locked_key"
        assert body["keys"] == ["console.commandToRunOnCaptcha"]
        assert path.read_bytes() == before
    finally:
        store.stop()


def test_the_run_flags_and_the_tesseract_path_are_locked_too(tmp_path):
    path = _write_global(tmp_path)
    before = path.read_bytes()
    app, store = _setup_test_app(tmp_path)
    try:
        client = app.test_client()
        res = _post_global(client, {"console": {"runConsoleCommandOnBan": True}})
        assert res.status_code == 400
        assert res.get_json()["keys"] == ["console.runConsoleCommandOnBan"]
        res = _post_global(client, {"levelCheck": {"tesseractPath": "C:/fake/other.exe"}})
        assert res.status_code == 400
        assert res.get_json()["keys"] == ["levelCheck.tesseractPath"]
        assert path.read_bytes() == before
    finally:
        store.stop()


def test_a_harmless_key_is_still_saved(tmp_path):
    path = _write_global(tmp_path)
    app, store = _setup_test_app(tmp_path)
    try:
        res = _post_global(app.test_client(), {"account": {"waveSize": 22}})
        assert res.status_code == 200
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["account"]["waveSize"] == 22
        assert saved["console"]["commandToRunOnCaptcha"] == SAVED_COMMAND
    finally:
        store.stop()


def test_get_hides_the_command_text_and_marks_the_block_locked(tmp_path):
    _write_global(tmp_path)
    app, store = _setup_test_app(tmp_path)
    try:
        res = app.test_client().get("/api/v2/config?scope=global", headers=AUTH)
        assert res.status_code == 200
        console = res.get_json()["config"]["console"]
        assert console["commandToRunOnCaptcha"] == ""
        assert console["locked"] is True
        assert SAVED_COMMAND.encode("utf-8") not in res.data
    finally:
        store.stop()


def test_get_then_post_back_is_accepted_and_keeps_the_locked_values(tmp_path):
    path = _write_global(tmp_path)
    app, store = _setup_test_app(tmp_path)
    try:
        client = app.test_client()
        config = client.get("/api/v2/config?scope=global", headers=AUTH).get_json()["config"]
        config["account"]["waveSize"] = 17
        res = _post_global(client, config)
        assert res.status_code == 200
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["console"]["commandToRunOnCaptcha"] == SAVED_COMMAND
        assert "locked" not in saved["console"]
        assert saved["account"]["waveSize"] == 17
    finally:
        store.stop()


def test_blank_or_redacted_command_does_not_clear_the_saved_command(tmp_path):
    path = _write_global(tmp_path)
    app, store = _setup_test_app(tmp_path)
    try:
        client = app.test_client()
        for blank in ("", "***REDACTED***"):
            res = _post_global(client, {"console": {"commandToRunOnCaptcha": blank}})
            assert res.status_code == 200
            saved = json.loads(path.read_text(encoding="utf-8"))
            assert saved["console"]["commandToRunOnCaptcha"] == SAVED_COMMAND
    finally:
        store.stop()
