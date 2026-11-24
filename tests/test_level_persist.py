"""The last good OwO level survives a bot restart (prompt 24).

The bot stores every good read in utils/data/levels.sqlite (core/level_store.py) and the dashboard reads that file on
every refresh (website/data_v2.py), so a restarted bot shows the stored level from its first snapshot, without waiting for
a level check. These tests cover the table of the prompt:
  L1 a good read survives a restart          L2 a failed check keeps the last good values
  L3/L4 the dashboard shows the stored level at the first refresh of a restarted bot (live status has no level)
  L5 a missing, broken or locked levels file is not an error
  L8 after a restart the check still waits for its interval          L9 no token or key text in the files or the API
  L10 a good read without a clean rank keeps the last clean rank
L3 (seeding live status) and L6/L7 (new column, old key migration) do not apply: see the prompt-24 report.

Every path is inside tmp_path. Fake ids and fake tokens only; nothing is sent and the real utils/data is not used.
"""

import asyncio
import base64
import json
import random
import sqlite3
import time
import types

import pytest

from core import level_check
from core.level_store import LevelStore
from core.live_status import LiveStatusWriter
from utils.account_identity import expected_key
from website import data_v2

ACC = "111111111111110001"  # a fake Discord user id (18 digits), like the keys in levels.sqlite
ACC2 = "111111111111110002"
FAKE_TOKEN_MARKER = "FAKE_TOKEN_MARKER_123"
FAKE_OCR_KEY = "FAKE_OCR_KEY_456"
NOW = 1_700_000_000.0


def _token(user_id, marker=FAKE_TOKEN_MARKER):
    head = base64.urlsafe_b64encode(user_id.encode("ascii")).decode("ascii").rstrip("=")
    return head + ".AAAAAA." + marker


def _bot_root(tmp_path):
    root = tmp_path / "bot"
    (root / "utils" / "data").mkdir(parents=True)
    return root


def _levels_path(root):
    return root / "utils" / "data" / "levels.sqlite"


def _write_live(root, user_ids):
    """The live status file of a bot that has just started: every account is pending, keyed by its token's user id.
    Written by the real LiveStatusWriter, no level field in it"""
    records = {}
    for index, user_id in enumerate(user_ids, start=1):
        records[index] = {"status": "pending", "userid": None, "error": None,
                          "expected_key": expected_key(_token(user_id), index)}
    writer = LiveStatusWriter(
        path=root / "utils" / "data" / "live_status.json",
        clients=lambda: {},
        accounts=lambda: dict(records),
        autostop=lambda wall: {"enabled": False, "stopping": False},
        commands_path=root / "utils" / "data" / "web_commands.json",
    )
    writer.begin_run(len(records), "run-test-p24")
    writer.write_once(running=True)
    return root / "utils" / "data" / "live_status.json"


def _item(store, account_id):
    items = store.accounts_page(1, 50, "name", "asc", "", "", "")["items"]
    return next(item for item in items if item["id"] == account_id)


def _settings(interval_minutes=360):
    return types.SimpleNamespace(intervalMinutes=interval_minutes, firstCheckMinDelayMinutes=10,
                                 firstCheckMaxDelayMinutes=40, maxChecksPerMinuteAllAccounts=6)


# ---------------------------------------------------------------------------------------------- L1, L2, L10 (the store)
def test_L1_a_good_read_survives_a_restart(tmp_path):
    path = tmp_path / "levels.sqlite"
    LevelStore(path).record_good(ACC, 42, 1234, 5000, 77, now=NOW)
    row = LevelStore(path).get(ACC)  # a new store object: what the bot sees after a restart
    assert row["level"] == 42
    assert row["xp_cur"] == 1234 and row["xp_need"] == 5000
    assert row["rank"] == 77
    assert row["checked_at"] == NOW and row["last_ok"] == 1 and row["last_error"] is None


def test_L2_a_failed_check_keeps_the_last_good_values(tmp_path):
    path = tmp_path / "levels.sqlite"
    store = LevelStore(path)
    store.record_good(ACC, 42, 1234, 5000, 77, now=NOW)
    store.record_fail(ACC, "no_image", now=NOW + 3600)
    row = LevelStore(path).get(ACC)
    assert row["level"] == 42 and row["xp_cur"] == 1234 and row["xp_need"] == 5000 and row["rank"] == 77
    assert row["checked_at"] == NOW  # the time of the last GOOD read, not of the failure
    assert row["last_try_at"] == NOW + 3600 and row["last_ok"] == 0 and row["last_error"] == "no_image"


def test_L10_a_good_read_without_a_clean_rank_keeps_the_last_clean_rank(tmp_path):
    path = tmp_path / "levels.sqlite"
    store = LevelStore(path)
    store.record_good(ACC, 42, 1234, 5000, 77, now=NOW)
    store.record_good(ACC, 43, 100, 5200, None, now=NOW + 600)  # the next card had no clean rank
    row = LevelStore(path).get(ACC)
    assert (row["level"], row["xp_cur"], row["checked_at"]) == (43, 100, NOW + 600)
    assert row["rank"] == 77
    store.record_good(ACC, 43, 200, 5200, 80, now=NOW + 1200)  # a clean rank replaces it
    assert LevelStore(path).get(ACC)["rank"] == 80


# ------------------------------------------------------------------------------- L3/L4 (the dashboard after a restart)
def test_L3_L4_the_first_snapshot_after_a_restart_shows_the_stored_level(tmp_path):
    root = _bot_root(tmp_path)
    checked = time.time() - 3600
    LevelStore(_levels_path(root)).record_good(ACC, 42, 1234, 5000, 77, now=checked)
    live = _write_live(root, [ACC])
    assert all("level" not in rec for rec in json.loads(live.read_text(encoding="utf-8"))["accounts"])

    store = data_v2.SnapshotStore(root=str(root))
    store.refresh()  # the dashboard's first refresh after the restart, before any level check ran
    item = _item(store, ACC)
    assert item["level"] == 42 and item["xp_cur"] == 1234 and item["xp_need"] == 5000 and item["xp_pct"] == 24.7
    assert item["rank"] == 77
    assert item["level_checked_at"] == round(checked, 1)
    assert item["level_checked_ago_s"] == pytest.approx(3600, abs=30)
    assert item.get("removed") is False  # the account is in the live file, so it is listed as a current account


# ----------------------------------------------------------------------------------------------------- L5 (bad files)
@pytest.mark.parametrize("case", ["missing", "no_table", "garbage", "locked"])
def test_L5_a_missing_broken_or_locked_levels_file_is_not_an_error(tmp_path, case):
    root = _bot_root(tmp_path)
    _write_live(root, [ACC])
    path = _levels_path(root)
    locker = None
    if case == "no_table":
        sqlite3.connect(path).close()  # an empty database file: no account_level table
    elif case == "garbage":
        path.write_bytes(b"this is not a sqlite database")
    elif case == "locked":
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE account_level (account_id TEXT PRIMARY KEY, level INTEGER, xp_cur INTEGER, "
                     "xp_need INTEGER, rank INTEGER, checked_at REAL, last_try_at REAL, last_ok INTEGER, last_error TEXT)")
        conn.commit()
        conn.close()
        locker = sqlite3.connect(path, isolation_level=None)
        locker.execute("BEGIN EXCLUSIVE")  # the reader waits its 2 s timeout, then gives up
    try:
        store = data_v2.SnapshotStore(root=str(root))
        store.refresh()  # must not raise
    finally:
        if locker is not None:
            locker.execute("ROLLBACK")
            locker.close()
    assert _item(store, ACC)["level"] is None
    assert store.sources["levels"].ok is False
    assert store.sources["levels"].error


# ------------------------------------------------------------------------------------ L8 (the interval is unchanged)
def test_L8_after_a_restart_the_check_still_waits_for_its_interval(tmp_path):
    path = tmp_path / "levels.sqlite"
    LevelStore(path).record_good(ACC, 42, 1234, 5000, 77, now=NOW - 600)  # the last good read was 10 minutes ago
    host = types.SimpleNamespace(account_id=ACC, settings=_settings(interval_minutes=360))
    checker = level_check.LevelChecker(host, store=LevelStore(path), clock=lambda: NOW)
    due = asyncio.run(checker.initial_due())
    assert due == pytest.approx((NOW - 600) + 360 * 60)  # 350 minutes from now, the same rule as before this change
    assert due - NOW == pytest.approx(350 * 60)


def test_L8_a_failed_last_try_still_uses_the_retry_window(tmp_path):
    path = tmp_path / "levels.sqlite"
    LevelStore(path).record_fail(ACC, "no_image", now=NOW - 600)
    host = types.SimpleNamespace(account_id=ACC, settings=_settings())
    checker = level_check.LevelChecker(host, store=LevelStore(path), clock=lambda: NOW, rng=random.Random(7))
    due = asyncio.run(checker.initial_due())
    assert NOW - 600 + 1800 <= due <= NOW - 600 + 2700  # one retry 30-45 minutes after the failed try


# ------------------------------------------------------------------------------------------------ L9 (no secrets)
def test_L9_no_token_or_key_text_in_the_files_or_the_api(tmp_path):
    root = _bot_root(tmp_path)
    (root / "tokens.txt").write_text(_token(ACC) + "\n", encoding="utf-8")  # the bot reads it, these modules never do
    store = LevelStore(_levels_path(root))
    store.record_good(ACC, 42, 1234, 5000, 77, now=NOW)
    store.record_fail(ACC2, "no_image", now=NOW)
    live = _write_live(root, [ACC])
    dashboard = data_v2.SnapshotStore(root=str(root))
    dashboard.refresh()
    api_text = json.dumps(dashboard.accounts_page(1, 50, "name", "asc", "", "", ""), default=str)
    outputs = [live.read_bytes(), api_text.encode("utf-8")]
    for name in ("levels.sqlite", "levels.sqlite-wal"):
        if (root / "utils" / "data" / name).exists():
            outputs.append((root / "utils" / "data" / name).read_bytes())
    for blob in outputs:
        assert FAKE_TOKEN_MARKER.encode() not in blob
        assert FAKE_OCR_KEY.encode() not in blob
