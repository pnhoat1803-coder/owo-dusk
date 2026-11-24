"""The account count from the first second to the stop (Phase 3, G).

The bot side writes the live status with core/live_status.py (the real LiveStatusWriter, fed with fake registry records
and fake clients). The dashboard side reads it with website/data_v2.py (SnapshotStore) next to a fake database that holds
107 accounts of tokens.txt and 2 old accounts that are not in tokens.txt any more. Fake tokens only; nothing is sent.
"""

import base64
import json
import sqlite3
import time
import types
import uuid

import pytest

from core.live_status import LiveStatusWriter
from utils.account_identity import expected_key
from website import data_v2

N_TOKENS = 107
N_OLD = 2
FAKE_BASE = 100000000000000000
SCHEMA = [
    "CREATE TABLE commands (name TEXT PRIMARY KEY, count INTEGER)",
    "CREATE TABLE cowoncy_earnings (user_id TEXT, hour INTEGER, earnings INTEGER, PRIMARY KEY (user_id, hour))",
    "CREATE TABLE user_stats (user_id TEXT PRIMARY KEY, daily REAL, lottery REAL, cookie REAL, giveaways REAL, "
    "captchas INTEGER, cowoncy INTEGER, boss REAL, boss_ticket INTEGER)",
    "CREATE TABLE meta_data (key TEXT PRIMARY KEY, value INTEGER)",
]


def fake_id(n):
    return str(FAKE_BASE + n)


def old_id(n):
    return str(FAKE_BASE + 900 + n)


def token_for(n):
    head = base64.urlsafe_b64encode(fake_id(n).encode("ascii")).decode("ascii").rstrip("=")
    return head + ".AAAAAA.FAKE_TOKEN_MARKER_%d" % n


def _write_db(root):
    path = root / "utils" / "data" / "db.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    for statement in SCHEMA:
        conn.execute(statement)
    ids = [fake_id(n) for n in range(1, N_TOKENS + 1)] + [old_id(n) for n in range(1, N_OLD + 1)]
    for uid in ids:
        conn.execute(
            "INSERT INTO user_stats (user_id, daily, lottery, cookie, giveaways, captchas, cowoncy, boss, boss_ticket) "
            "VALUES (?, 0, 0, 0, 0, 0, 1000, 0, 0)",
            (uid,),
        )
    conn.commit()
    conn.close()


class _Status:
    def __init__(self):
        self.sleep = False
        self.captcha = False
        self.captcha_flag = False
        self.state = True
        self.hold_handler = False


def _client(index, uid):
    return types.SimpleNamespace(
        account_index=index,
        user=types.SimpleNamespace(id=int(uid)),
        username="acc%d" % index,
        ch=types.SimpleNamespace(command_handler_status=_Status(), cmds_state={}),
        stats=types.SimpleNamespace(balance=0, has_updated_balance=False, net_earnings=0, no_gem=False),
        loop=None,
        stopped=False,
        gateway_down_at=None,
        gateway_back_at=0.0,
        last_sent_at=None,
    )


@pytest.fixture
def bot(tmp_path):
    """The writer and the state it reads: records and clients change between the stages of a run"""
    root = tmp_path / "project"
    _write_db(root)
    state = {"records": {}, "clients": {}}
    writer = LiveStatusWriter(
        path=root / "utils" / "data" / "live_status.json",
        clients=lambda: dict(state["clients"]),
        accounts=lambda: dict(state["records"]),
        autostop=lambda wall: {"enabled": False, "stopping": False},
        commands_path=root / "utils" / "data" / "web_commands.json",
    )
    writer.begin_run(N_TOKENS, uuid.uuid4().hex)

    def stage(online, connecting):
        """online: logged in (READY). connecting: client made, not ready. The rest: not started yet"""
        records, clients = {}, {}
        for index in range(1, N_TOKENS + 1):
            key = expected_key(token_for(index), index)
            assert key == fake_id(index)  # the token shows the id, so the key is the id from the first write
            if index <= online:
                records[index] = {"status": "ready", "userid": int(key), "error": None, "expected_key": key}
                clients[index] = _client(index, key)
            elif index <= online + connecting:
                records[index] = {"status": "pending", "userid": None, "error": None, "expected_key": key}
                clients[index] = _client(index, key)
            else:
                records[index] = {"status": "pending", "userid": None, "error": None, "expected_key": key}
        state["records"], state["clients"] = records, clients
        writer.write_once(running=True)

    class Run:
        pass

    run = Run()
    run.root, run.writer, run.stage = root, writer, stage
    return run


def _summary(bot):
    store = data_v2.SnapshotStore(root=str(bot.root))
    store.refresh()
    return store, store.snapshot.summary


def test_total_is_the_configured_count_at_every_stage(bot):
    stages = [
        ("just started", 0, 0, 0),
        ("half connected", 60, 20, 47),
        ("all online", N_TOKENS, 0, 0),
    ]
    for label, online, connecting, _starting in stages:
        bot.stage(online, connecting)
        store, summary = _summary(bot)
        page = store.accounts_page(1, 500, "health", "desc", "", "", "")
        assert summary["accounts_total"] == N_TOKENS, label
        assert summary["accounts_online"] == online, label
        assert summary["accounts_online"] <= summary["accounts_total"], label
        assert page["total"] == N_TOKENS, label
        assert len(page["items"]) == N_TOKENS, label
        assert all(not item["id"].startswith("idx") and not item["id"].startswith("slot-") for item in page["items"])


def test_the_two_old_accounts_are_removed_and_hidden_by_default(bot):
    bot.stage(N_TOKENS, 0)
    store, summary = _summary(bot)
    hidden = store.accounts_page(1, 500, "health", "desc", "", "", "")
    shown = store.accounts_page(1, 500, "health", "desc", "", "", "", show_removed=True)
    assert hidden["total"] == N_TOKENS
    assert shown["total"] == N_TOKENS + N_OLD
    removed = [item for item in shown["items"] if item.get("removed")]
    assert len(removed) == N_OLD
    assert all(item["id"] not in {fake_id(n) for n in range(1, N_TOKENS + 1)} for item in removed)


def test_accounts_not_logged_in_show_connecting_not_offline(bot):
    # Starting (no client yet) and connecting (client made, not ready) both count in the total and show "connecting"
    bot.stage(0, 20)
    store, summary = _summary(bot)
    page = store.accounts_page(1, 500, "health", "desc", "", "", "")
    assert {item["state"] for item in page["items"]} == {"connecting"}
    assert summary["accounts_online"] == 0
    assert summary["accounts_total"] == N_TOKENS


def test_bot_stopped_keeps_the_configured_total(bot):
    bot.stage(N_TOKENS, 0)
    bot.writer.stop()  # the last file, running=false
    store, summary = _summary(bot)
    assert summary["accounts_total"] == N_TOKENS
    assert summary["accounts_online"] == 0
    page = store.accounts_page(1, 500, "health", "desc", "", "", "")
    assert page["total"] == N_TOKENS


def test_before_the_first_live_file_the_total_is_unknown_not_zero(tmp_path):
    """Right after Start the dashboard has no live file of this run yet: the total is unknown (None, shown as a dash),
    never a number that looks like a real count"""
    root = tmp_path / "project"
    _write_db(root)
    store = data_v2.SnapshotStore(root=str(root))
    store.refresh()
    summary = store.snapshot.summary
    assert summary["accounts_total"] is None
    assert summary["accounts_online"] == 0
    page = store.accounts_page(1, 500, "health", "desc", "", "", "")
    assert page["total"] == 0


def test_stop_before_the_run_begins_writes_nothing(tmp_path):
    writer = LiveStatusWriter(
        path=tmp_path / "live_status.json",
        clients=lambda: {},
        accounts=lambda: {},
        autostop=lambda wall: {},
        commands_path=tmp_path / "web_commands.json",
    )
    writer.stop()  # a run that never began keeps the file of the previous run: nothing is written
    assert not (tmp_path / "live_status.json").exists()


def _set_auto_stop(bot, written_ago):
    """Puts an auto-stop block in the live file the writer made, and sets how long ago the file was written"""
    path = bot.root / "utils" / "data" / "live_status.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["autostop"] = {"enabled": True, "stopping": False, "stops_at": time.time() + 600, "minutes": 10}
    data["written_at"] = time.time() - written_ago
    path.write_text(json.dumps(data), encoding="utf-8")


def test_a_killed_bot_leaves_no_auto_stop_countdown(bot):
    """A bot that was killed never writes its last file, so the file still says that auto-stop is on. The dashboard shows
    the bot as stopped: the countdown must not show either"""
    bot.stage(N_TOKENS, 0)
    _set_auto_stop(bot, written_ago=300)
    _, summary = _summary(bot)
    assert summary["bot_online"] is False
    assert summary["autostop"]["enabled"] is None
    assert summary["autostop"]["remaining_s"] is None


def test_a_running_bot_shows_its_auto_stop_countdown(bot):
    bot.stage(N_TOKENS, 0)
    _set_auto_stop(bot, written_ago=0)
    _, summary = _summary(bot)
    assert summary["bot_online"] is True
    assert summary["autostop"]["enabled"] is True
    assert 500 < summary["autostop"]["remaining_s"] <= 600
