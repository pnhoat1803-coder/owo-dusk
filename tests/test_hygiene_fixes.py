"""Small checks of the hygiene fixes: requirements, one import path, and the one-click launchers."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_requirements_list_werkzeug_right_under_flask():
    lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    flask_at = lines.index("Flask==3.1.3")
    assert lines[flask_at + 1] == "werkzeug>=3.1"


def test_the_type_checking_import_uses_the_real_module_path():
    text = (ROOT / "core" / "cogs" / "_BASE.py").read_text(encoding="utf-8")
    assert "from core.client import MyClient" in text
    assert "from client import MyClient" not in text


def test_the_old_installers_are_gone_and_the_launchers_exist():
    assert not (ROOT / "install.bat").exists()
    assert not (ROOT / "install.sh").exists()
    for name in ("setup.bat", "start.bat", "setup.sh", "start.sh"):
        assert (ROOT / name).is_file(), name


def test_windows_launchers_are_crlf_and_ascii_and_shell_scripts_are_lf():
    for name in ("setup.bat", "start.bat", "dashboard.bat"):
        raw = (ROOT / name).read_bytes()
        assert raw.count(b"\r\n") > 0 and raw.count(b"\n") == raw.count(b"\r\n"), name
        raw.decode("ascii")
    for name in ("setup.sh", "start.sh"):
        raw = (ROOT / name).read_bytes()
        assert b"\r" not in raw and raw.startswith(b"#!/usr/bin/env bash"), name
