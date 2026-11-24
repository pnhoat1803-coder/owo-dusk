"""Per-server inbound (phase 4, item K): core/gateway_stats.py GuildWindow and what the live status and the dashboard take
from it. Numbers and server ids only: the tests check that no message content is kept."""

import json
import types

from core.gateway_stats import GUILDS_LIVE_TOP, MAX_GUILDS, WINDOW_MINUTES, AccountStats, GuildWindow
from core.live_status import LiveStatusWriter, _farm_guild, _guild_inbound
from website import data_v2

FIXED_KEYS = {"guild_id", "kb_min", "events_min"}


class Clock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


def test_bytes_per_minute_are_computed_over_the_elapsed_minutes():
    clock = Clock(0.0)
    window = GuildWindow(clock=clock)
    window.add("111", 2048, now=10.0)      # minute 0
    window.add("111", 1024, now=70.0)      # minute 1
    top = window.top(now=120.0)            # two minutes have passed
    assert top == [{"guild_id": "111", "kb_min": 1.5, "events_min": 1.0}]


def test_a_new_window_is_not_diluted_by_minutes_that_have_not_passed():
    window = GuildWindow(clock=Clock(0.0))
    window.add("222", 60 * 1024, now=0.0)
    # Less than a minute has passed: the span is one minute, so the rate is the whole amount
    assert window.top(now=30.0)[0]["kb_min"] == 60.0


def test_the_window_forgets_minutes_older_than_the_window():
    window = GuildWindow(clock=Clock(0.0))
    window.add("333", 4096, now=5.0)       # minute 0
    assert window.top(now=5.0)
    assert window.top(now=(WINDOW_MINUTES + 1) * 60.0) == []


def test_at_most_fifty_servers_are_kept_and_the_smallest_is_dropped():
    window = GuildWindow(clock=Clock(0.0))
    for n in range(60):
        window.add(str(10_000 + n), (n + 1) * 100, now=1.0)
    assert len(window._totals) == MAX_GUILDS
    assert len(window.top(count=GUILDS_LIVE_TOP, now=60.0)) == GUILDS_LIVE_TOP
    # The busiest servers are the ones that stayed
    kept = {row["guild_id"] for row in window.top(count=GUILDS_LIVE_TOP, now=60.0)}
    assert str(10_059) in kept


def test_the_top_list_has_only_the_allowed_keys():
    window = GuildWindow(clock=Clock(0.0))
    window.add("444", 500, now=1.0)
    for row in window.top(now=60.0):
        assert set(row) == FIXED_KEYS
        assert isinstance(row["guild_id"], str)


def test_account_stats_counts_only_the_guild_event_types_by_server():
    stats = AccountStats(types.SimpleNamespace(account_index=1, username="t"))
    stats._guilds = GuildWindow(clock=Clock(0.0))
    stats.count('{"t":"MESSAGE_CREATE","s":3,"op":0,"d":{"guild_id":"555","content":"hello"}}')
    stats.count('{"t":"TYPING_START","d":{"guild_id":"555","user_id":"1"}}')
    stats.count('{"t":"PRESENCE_UPDATE","d":{"guild_id":"666"}}')
    stats.count('{"t":"READY","d":{"guilds":[{"id":"777"}]}}')    # not a per-server event type
    stats.count('{"t":"MESSAGE_CREATE","d":{"guild_id":null,"content":"dm"}}')  # a DM has no server
    by_guild = {row["guild_id"]: row for row in stats.top_guilds()}
    assert set(by_guild) == {"555", "666"}
    # Two events in server 555 (the message and the typing), within the first minute: 2 per minute
    assert by_guild["555"]["events_min"] == 2.0
    assert by_guild["666"]["events_min"] == 1.0


def test_no_message_content_is_kept():
    stats = AccountStats(types.SimpleNamespace(account_index=2, username="t"))
    stats._guilds = GuildWindow(clock=Clock(0.0))
    stats.count('{"t":"MESSAGE_CREATE","d":{"guild_id":"888","content":"SECRET TEXT 12345"}}')
    dump = json.dumps(stats.top_guilds())
    assert "SECRET" not in dump


class FakeClient:
    username = "alpha"
    account_index = 3
    user = None

    def __init__(self, guild_id=None, inbound=None):
        self.cm = types.SimpleNamespace(guild=types.SimpleNamespace(id=guild_id)) if guild_id else None
        self.gateway_stats = types.SimpleNamespace(top_guilds=lambda: list(inbound or []))


def test_live_status_carries_the_top_servers_and_the_farm_server(tmp_path):
    rows = [{"guild_id": "900", "kb_min": 12.5, "events_min": 40.0}, {"guild_id": "901", "kb_min": 2.0, "events_min": 3.0}]
    client = FakeClient(guild_id=900, inbound=rows)
    assert _guild_inbound(client) == rows
    assert _farm_guild(client) == "900"
    assert _farm_guild(FakeClient()) is None
    assert _guild_inbound(types.SimpleNamespace()) == []

    writer = LiveStatusWriter(
        path=tmp_path / "live.json",
        clients=lambda: {3: client},
        accounts=lambda: {3: {"userid": 123, "status": "ready"}},
        commands_path=tmp_path / "cmds.json",
    )
    data = writer.snapshot(running=True)
    account = data["accounts"][0]
    assert account["gi"] == rows
    assert account["fg"] == "900"
    assert "quotes" in data and set(data["quotes"]) >= {"pool", "cache", "last_ok_at", "last_error"}


def test_dashboard_ranks_servers_across_accounts_and_tags_the_farm_server():
    live = {"accounts": [
        {"id": "1", "name": "a", "fg": "900", "gi": [{"guild_id": "900", "kb_min": 30.0, "events_min": 5},
                                                     {"guild_id": "901", "kb_min": 50.0, "events_min": 9}]},
        {"id": "2", "name": "b", "fg": "900", "gi": [{"guild_id": "901", "kb_min": 80.0, "events_min": 9}]},
    ]}
    ranked = data_v2._noisy_servers(live, True)
    assert [row["guild_id"] for row in ranked] == ["901", "900"]
    assert ranked[0]["kb_min"] == 130.0 and ranked[0]["accounts"] == 2
    assert ranked[0]["worst"]["id"] == "2" and ranked[0]["farm"] is False
    assert ranked[1]["farm"] is True
    assert data_v2._noisy_servers(live, False) == []


def test_an_account_reports_its_busiest_server_that_is_not_its_farm_server():
    rec = {"id": "1", "fg": "900", "gi": [{"guild_id": "900", "kb_min": 500.0}, {"guild_id": "901", "kb_min": 310.0},
                                          {"guild_id": "902", "kb_min": 20.0}]}
    assert data_v2._noisy_for(rec) == {"guild_id": "901", "kb_min": 310.0}
    # Without a known farm server nothing is called noisy
    assert data_v2._noisy_for({"id": "1", "gi": rec["gi"]}) is None
