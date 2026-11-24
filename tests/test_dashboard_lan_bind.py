"""The dashboard's bind address (Prompt 08). One decision in utils/lan.py (effective_bind), used by every launch path,
and no launch path that forces this machine only. Pure functions and text checks: nothing listens on the network."""

import json
import re
from pathlib import Path

import pytest

from utils import lan

ROOT = Path(__file__).resolve().parent.parent
LAUNCH_FILES = [
    ROOT / "website" / "server.py",
    ROOT / "website" / "website.py",
    ROOT / "website" / "__init__.py",
    ROOT / "website" / "bot_supervisor.py",
    ROOT / "website" / "process_manager.py",
    ROOT / "uwu.py",
    ROOT / "dashboard.bat",
    ROOT / "dashboard.sh",
]
# len(...password...) compared with a number: the old hard-coded check, for example len(paswd) < 8
PASSWORD_LENGTH_LITERAL = re.compile(r"len\([^)]*(pass|paswd|pw)[^)]*\)\s*(>=|<=|<|>)\s*\d+", re.IGNORECASE)
# a line that starts a server, or hands a host to one
SERVER_START = re.compile(r"make_server\(|app\.run\(|web_start\(|DashboardProcessManager\(|--host", re.IGNORECASE)
LOOPBACK_LITERAL = re.compile(r"127\.0\.0\.1|localhost", re.IGNORECASE)


def _read(path):
    return path.read_text(encoding="utf-8", errors="replace")


def test_the_bind_rules_of_the_prompt():
    assert lan.effective_bind("0.0.0.0", "") == ("127.0.0.1", "no_password")
    assert lan.effective_bind("0.0.0.0", "x" * (lan.MIN_PASSWORD - 1)) == ("127.0.0.1", "short_password")
    assert lan.effective_bind("0.0.0.0", "x" * lan.MIN_PASSWORD) == ("0.0.0.0", "lan_ok")
    assert lan.effective_bind("", "x" * lan.MIN_PASSWORD) == ("0.0.0.0", "lan_ok")
    assert lan.effective_bind("127.0.0.1", "x" * 20) == ("127.0.0.1", "local_only_config")


def test_the_password_threshold_is_a_constant_in_one_place():
    assert isinstance(lan.MIN_PASSWORD, int) and lan.MIN_PASSWORD >= 6


def test_the_owners_old_file_without_website_host_listens_on_the_lan(tmp_path):
    """The file as it was before this change: no website.host, enableHost false, a password of MIN_PASSWORD characters"""
    config = tmp_path / "global_settings.json"
    config.write_text(json.dumps({"website": {
        "enabled": True, "port": 1200, "password": "x" * lan.MIN_PASSWORD, "enableHost": False,
    }}), encoding="utf-8")
    site = json.loads(config.read_text(encoding="utf-8"))["website"]
    assert lan.effective_bind(lan.configured_host(site), site["password"]) == ("0.0.0.0", "lan_ok")


def test_website_host_0000_wins_over_enablehost_false():
    site = {"host": "0.0.0.0", "enableHost": False, "password": "x" * lan.MIN_PASSWORD}
    assert lan.effective_bind(lan.configured_host(site), site["password"]) == ("0.0.0.0", "lan_ok")


def test_the_banner_is_only_for_the_two_password_reasons():
    assert lan.shows_password_banner("no_password")
    assert lan.shows_password_banner("short_password")
    for reason in ("lan_ok", "local_only_config", "invalid_host"):
        assert not lan.shows_password_banner(reason)


def test_the_startup_block_lists_each_lan_address_then_this_machine():
    assert lan.startup_lines("0.0.0.0", 1200, ["http://192.168.1.25:1200", "http://100.64.1.1:1200"], "lan_ok") == [
        "Dashboard (LAN): http://192.168.1.25:1200",
        "Dashboard (LAN): http://100.64.1.1:1200",
        "Dashboard (máy này): http://127.0.0.1:1200",
    ]


def test_the_startup_block_gives_the_reason_when_it_stays_on_this_machine():
    assert lan.startup_lines("127.0.0.1", 1200, [], "short_password") == [
        "Dashboard chỉ mở trên máy này (127.0.0.1) vì mật khẩu dashboard ngắn hơn " f"{lan.MIN_PASSWORD} ký tự. "
        "Đặt website.password dài hơn để mở cho mạng LAN.",
        "Dashboard (máy này): http://127.0.0.1:1200",
    ]
    assert lan.startup_lines("127.0.0.1", 1200, [], "no_password")[0] == (
        "Dashboard chỉ mở trên máy này (127.0.0.1) vì chưa đặt mật khẩu dashboard. "
        "Đặt website.password dài ít nhất " f"{lan.MIN_PASSWORD} ký tự để mở cho mạng LAN."
    )


def test_a_lan_bind_with_no_address_found_says_so():
    assert lan.startup_lines("0.0.0.0", 1200, [], "lan_ok")[0].startswith("Dashboard (LAN): không tìm thấy")


def test_every_launch_path_asks_the_one_bind_function():
    for path in (ROOT / "website" / "server.py", ROOT / "website" / "website.py", ROOT / "uwu.py"):
        assert "lan.effective_bind(" in _read(path), path.name


def test_the_launch_paths_do_not_read_enablehost_for_the_host():
    assert "enableHost" not in _read(ROOT / "website" / "server.py")
    assert "enableHost" not in _read(ROOT / "uwu.py")


@pytest.mark.parametrize("path", LAUNCH_FILES, ids=lambda p: p.name)
def test_no_password_length_literal_in_the_launch_files(path):
    assert PASSWORD_LENGTH_LITERAL.search(_read(path)) is None, f"{path.name} compares a password length with a number"


@pytest.mark.parametrize("path", LAUNCH_FILES, ids=lambda p: p.name)
def test_no_server_start_is_given_a_loopback_literal(path):
    for number, line in enumerate(_read(path).splitlines(), 1):
        assert not (SERVER_START.search(line) and LOOPBACK_LITERAL.search(line)), f"{path.name}:{number}"
