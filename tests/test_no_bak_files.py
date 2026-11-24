"""The app never writes a .bak file: not when a settings file is saved, not when the weekly runtime is migrated.

Everything runs in tmp_path; the real config and utils/data are never touched."""

import json

import pytest
from flask import Flask

import website.api_v2 as api_v2
from utils import runtime_handler

PASSWORD = "no-bak-test-pass"
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


@pytest.fixture
def config_dir(tmp_path):
    folder = tmp_path / "config"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "global_settings.json").write_text(
        json.dumps({"account": {"waveSize": 15}, "website": {"password": PASSWORD, "port": 1200}}, indent=4),
        encoding="utf-8",
    )
    (folder / "settings.json").write_text(json.dumps({"autoDaily": True}), encoding="utf-8")
    (folder / "dashboard_view.json").write_text(json.dumps({"refreshSeconds": 5}), encoding="utf-8")
    return folder


def test_saving_each_settings_file_through_the_dashboard_writes_no_bak(tmp_path, config_dir):
    app, store = _setup_test_app(tmp_path)
    try:
        client = app.test_client()
        requests = [
            {"scope": "global", "config": {"account": {"waveSize": 22}}},
            {"scope": "dashboard", "config": {"refreshSeconds": 7}},
            {"scope": "default", "config": {"autoDaily": False}},
        ]
        for body in requests:
            res = client.post("/api/v2/config", headers=_auth(client), json=body)
            assert res.status_code == 200, res.get_json()
    finally:
        store.stop()
    assert list(tmp_path.rglob("*.bak*")) == []


def test_migrating_a_legacy_weekly_runtime_file_writes_no_bak(tmp_path):
    data_dir = tmp_path / "utils" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    legacy = data_dir / "weekly_runtime.json"
    # A file from before schema_version 2: the migration replaces it with a new one
    legacy.write_text(json.dumps({"monday": {"start": 9, "end": 17}}), encoding="utf-8")

    runtime = runtime_handler._load_for_update(str(legacy))

    assert runtime is not None
    assert runtime["schema_version"] == runtime_handler.RUNTIME_SCHEMA_VERSION
    assert list(tmp_path.rglob("*.bak*")) == []
    assert not (data_dir / "weekly_runtime.json.bak12").exists()
