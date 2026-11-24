"""Autoquest safety, phase A (prompt 26): no command spam, no endless loop, no message to a stranger, a clean stop.

Fakes only: no network, no Discord. Time is faked: asyncio.sleep and the quest clocks advance one fake clock,
so the file runs in a few seconds.

Style copied from tests/test_silence_requeue.py: plain test functions that call asyncio.run() on a scenario,
SimpleNamespace fakes and monkeypatch.
"""

import asyncio
import random
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.cogs import quest as quest_mod
from core.cogs.quest import Quest
from utils.image_to_text import image_to_text
from utils.image_to_text.get_quest_details import extract_quests, get_quest_details
from utils.quest_helper.quest import LocalQuestHandler, QuestHandler

OWNER_ID = 5550001  # the account that posted a quest
BOT_ID = 5550002  # the account under test
PARTNER_ID = 5550003  # another account in the same process
OWO_ID = 5550004
CHANNEL_ID = 5550005
GUILD_ID = 5550006
STOPPED_OTHER_ID = 5550007  # another account that is already stopped
START = 1_700_000_000.0
QUEST_URL = "https://cdn.example/quest-rows.png"

_real_sleep = asyncio.sleep


class Clock:
    """One fake clock for the monotonic and the wall-clock readings"""

    def __init__(self):
        self.now = START

    def monotonic(self):
        return self.now

    def wall(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    clk = Clock()
    monkeypatch.setattr(quest_mod, "_monotonic", clk.monotonic)
    monkeypatch.setattr(quest_mod, "_wall_now", clk.wall)

    async def fake_sleep(seconds, *args, **kwargs):
        clk.now += max(0.0, float(seconds))
        await _real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return clk


@pytest.fixture(autouse=True)
def no_other_accounts_and_no_autostop(monkeypatch):
    # By default this process has no other account and autoStop is off. A test that needs otherwise sets it itself
    monkeypatch.setattr(quest_mod, "clients_snapshot", lambda: {})
    monkeypatch.setattr(quest_mod, "stop_requested", lambda: False)


class FakeCH:
    """What the quest cog uses from the command handler"""

    def __init__(self, clock):
        self.clock = clock
        self.put = []  # (time, command) of every command that reached the queue
        self.removed = []  # ids passed to remove_queue, in order
        self.checks = set()
        self.fail_put = None

    async def put_queue(self, cmd_data, priority=False, quick=False):
        if self.fail_put is not None:
            raise self.fail_put
        self.put.append((self.clock.now, dict(cmd_data)))

    async def remove_queue(self, cmd_data=None, id=None):
        self.removed.append(id)
        self.checks.discard(id)

    async def search_checks(self, id):
        return id in self.checks


class FakeDispatcher:
    def __init__(self):
        self.handlers = []

    def register(self, func, edit=False):
        self.handlers.append(func)

    def unregister(self, func, edit=False):
        if func in self.handlers:
            self.handlers.remove(func)


class FakeBot:
    """What the quest cog reads from the account client"""

    def __init__(self, clock):
        self.user = SimpleNamespace(id=BOT_ID, name="Dusk")
        self.owo_bot_id = OWO_ID
        self.cm = SimpleNamespace(
            id=CHANNEL_ID,
            guild=SimpleNamespace(id=GUILD_ID, fetch_member_profile=self._fetch_member_profile),
        )
        self.alias = {"quest": {"normal": "quest"}}
        self.stopped = False
        self.random = random.Random(0)
        self.ch = FakeCH(clock)
        self.logs = []
        self.message_dispatcher = FakeDispatcher()
        self.quest_handler = LocalQuestHandler(QuestHandler(), BOT_ID, None)
        self.ws = SimpleNamespace(session_id="session")
        self.local_headers = {}
        self.quest_help_request = {
            name: {"enabled": False, "channel": None, "till": 0, "userid": None}
            for name in ("cookie", "curse", "pray", "battle")
        }
        self.settings_dict = SimpleNamespace(
            prefix="owo ",
            autoQuest=SimpleNamespace(
                enabled=True,
                canEnable=True,
                helpOthers=False,
                useHelpChannel=False,
                channelId=None,
                get_cd=lambda: 20.0,
            ),
            commands=SimpleNamespace(
                owo=SimpleNamespace(enabled=True, get_cd=lambda: 20.0),
                hunt=SimpleNamespace(enabled=False, get_cd=lambda: 20.0),
                battle=SimpleNamespace(enabled=False, get_cd=lambda: 20.0),
                pray=SimpleNamespace(enabled=False, get_cd=lambda: 20.0),
                curse=SimpleNamespace(enabled=False, get_cd=lambda: 20.0),
                cookie=SimpleNamespace(enabled=False, get_cd=lambda: 20.0),
            ),
            gamble=SimpleNamespace(
                coinflip=SimpleNamespace(enabled=False),
                slots=SimpleNamespace(enabled=False),
                blackjack=SimpleNamespace(enabled=False),
            ),
        )

    async def _fetch_member_profile(self, user_id):
        return SimpleNamespace(id=user_id)

    async def fetch_channel(self, channel_id):
        return SimpleNamespace(id=channel_id, guild=SimpleNamespace(id=GUILD_ID))

    async def log(self, text, color="#ffffff", **kwargs):
        self.logs.append(text)

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)

    async def sleep_till(self, cooldown, cd_list=True, noise=3):
        await asyncio.sleep(5)

    def is_for_us(self, message):
        return True


async def no_wait(*args, **kwargs):
    """Stands in for block_till_send: fakes never echo our own messages"""
    return None


def make_cog(bot):
    cog = Quest(bot)
    cog.block_till_send = no_wait
    return cog


def text(content):
    return SimpleNamespace(component_name="text_display", content=content)


def section(*children):
    return SimpleNamespace(component_name="section", components=list(children))


def gallery(url):
    return SimpleNamespace(
        component_name="media_gallery",
        items=[SimpleNamespace(media=SimpleNamespace(url=url))],
    )


def quest_components(*extra):
    """A quest log message: the header that marks it as ours, then the given components"""
    return [section(text(f"<@{BOT_ID}>'s Quest Log")), *extra]


def make_message(components, buttons=()):
    return SimpleNamespace(
        author=SimpleNamespace(id=OWO_ID),
        channel_id=CHANNEL_ID,
        components=components,
        buttons=list(buttons),
    )


def sends_of(bot, command_id):
    return [cmd for _, cmd in bot.ch.put if cmd["id"] == command_id]


# Q1 + Q2: the all-done message stores its next-quest time, whatever the Discord timestamp style
@pytest.mark.parametrize("stamp", ["<t:1700000000:f>", "<t:1700000000:R>", "<t:1700000000>"])
def test_q1_q2_all_done_message_stores_the_next_quest_time(clock, stamp):
    async def scenario():
        bot = FakeBot(clock)
        cog = make_cog(bot)
        message = make_message(
            quest_components(text(f"UwU You finished all of your quests! Next quest {stamp}"))
        )
        await cog.on_component_message(message)
        assert cog.all_quests_done is True
        assert bot.quest_handler.next_quest_timestamp == 1700000000

    asyncio.run(scenario())


# Q3: no timestamp in the all-done message -> now + 4 hours, with the warning
def test_q3_all_done_without_timestamp_waits_four_hours(clock):
    async def scenario():
        bot = FakeBot(clock)
        cog = make_cog(bot)
        await cog.on_component_message(
            make_message(quest_components(text("UwU You finished all of your quests!")))
        )
        assert cog.all_quests_done is True
        assert abs(bot.quest_handler.next_quest_timestamp - (clock.now + 14400)) <= 5
        assert "[quest] all done, no next-quest time found, waiting 4h" in bot.logs

    asyncio.run(scenario())


# Q4: nothing is sent while the next-quest time is in the future
def test_q4_no_owo_quest_before_the_next_quest_time(clock):
    async def scenario():
        bot = FakeBot(clock)
        cog = make_cog(bot)
        cog.all_quests_done = True
        next_ts = int(clock.now) + 3000
        bot.quest_handler.update_next_timestamp(next_ts)
        await cog.main_quest_loop.coro(cog)
        sends = [when for when, cmd in bot.ch.put if cmd["id"] == "quest"]
        assert len(sends) == 1
        assert sends[0] >= next_ts
        assert cog.all_quests_done is False

    asyncio.run(scenario())


# Q5: "owo quest" is never sent twice within QUEST_MIN_CHECK_SECONDS; its check entry is dropped after the send
def test_q5_owo_quest_never_sent_twice_within_600_seconds(clock):
    async def scenario():
        bot = FakeBot(clock)
        cog = make_cog(bot)
        await cog.main_quest_loop.coro(cog)
        await cog.main_quest_loop.coro(cog)
        sends = [when for when, cmd in bot.ch.put if cmd["id"] == "quest"]
        assert len(sends) == 2
        assert sends[1] - sends[0] >= quest_mod.QUEST_MIN_CHECK_SECONDS
        assert bot.ch.removed.count("quest") >= 2

    asyncio.run(scenario())


# Q6: the hourly cap lets 60 commands through; the 61st waits until the oldest one is an hour old
def test_q6_hourly_cap_lets_60_commands_through_then_pauses(clock):
    class Blocked(Exception):
        pass

    async def scenario():
        bot = FakeBot(clock)
        cog = make_cog(bot)
        waits = []

        async def blocking_sleep(seconds):
            waits.append(seconds)
            raise Blocked()

        cog._sleep_checked = blocking_sleep
        cmd = {
            "cmd_name": "battle",
            "cmd_arguments": None,
            "prefix": True,
            "checks": True,
            "id": "battle",
            "channel": None,
        }
        with pytest.raises(Blocked):
            for _ in range(61):
                assert await cog._quest_send(dict(cmd))
        assert len(bot.ch.put) == quest_mod.QUEST_MAX_COMMANDS_PER_HOUR == 60
        assert waits and waits[0] >= 3600
        assert bot.logs.count("[quest] hourly cap reached, pausing quest commands") == 1
        # an hour later the cap releases and the 61st command goes through
        clock.now += 3601
        assert await cog._quest_send(dict(cmd)) is True
        assert len(bot.ch.put) == 61

    asyncio.run(scenario())


# Q7: battle_xp repeat with no XP update: exactly 30 sends, then it stops
def test_q7_battle_xp_repeat_without_xp_stops_after_30_sends(clock):
    async def scenario():
        bot = FakeBot(clock)
        cog = make_cog(bot)
        assert await cog.handle_repeat_quest("battle") is True
        assert len(sends_of(bot, "battle")) == quest_mod.QUEST_REPEAT_MAX_COMMANDS == 30
        assert "[quest] battle_xp repeat limit reached, will re-check quest log" in bot.logs
        assert bot.ch.removed[-1] == "battle"

    asyncio.run(scenario())


# Q8: find_animal repeat with no matching catch: stops at 30 sends
def test_q8_find_animal_repeat_without_matches_stops_at_30(clock):
    async def scenario():
        bot = FakeBot(clock)
        cog = make_cog(bot)
        cog.catcha_a_rank = "rare"
        assert await cog.handle_repeat_quest("hunt") is True
        assert len(sends_of(bot, "hunt")) == 30
        assert "[quest] find_animal_rare repeat limit reached, will re-check quest log" in bot.logs

    asyncio.run(scenario())


# Q9: a run with a known count still stops after QUEST_REPEAT_MAX_SECONDS
def test_q9_repeat_run_stops_after_30_minutes(clock):
    async def scenario():
        bot = FakeBot(clock)
        cog = make_cog(bot)
        bot.settings_dict.commands.battle.get_cd = lambda: 120.0
        assert await cog.handle_repeat_quest("battle", till=1000) is True
        assert len(sends_of(bot, "battle")) == 15  # at 0, 120, ... 1680 seconds
        assert clock.now - START == quest_mod.QUEST_REPEAT_MAX_SECONDS
        assert any("repeat limit reached" in line for line in bot.logs)

    asyncio.run(scenario())


# Q10: an action quest with no other account in the process sends nothing (also through handle_self_quests)
def test_q10_action_quest_with_one_account_sends_nothing(clock):
    async def scenario():
        bot = FakeBot(clock)
        cog = make_cog(bot)
        await cog.handle_action_quest(5)
        assert bot.ch.put == []
        assert "[quest] action quest skipped: no partner account" in bot.logs
        # the own-quest path: canEnable False is what routes action_send to its branch
        bot.settings_dict.autoQuest.canEnable = False
        await cog.handle_self_quests([{"quest_id": "action_send", "total": 5, "current": 0}])
        assert bot.ch.put == []
        assert bot.logs.count("[quest] action quest skipped: no partner account") == 2

    asyncio.run(scenario())


# Q10b: with another running account, actions go to that account only (never to a stopped one, never to ourselves)
def test_q10b_action_quest_targets_only_the_other_running_account(clock, monkeypatch):
    async def scenario():
        bot = FakeBot(clock)
        cog = make_cog(bot)
        other = SimpleNamespace(user=SimpleNamespace(id=PARTNER_ID), stopped=False)
        stopped_other = SimpleNamespace(user=SimpleNamespace(id=STOPPED_OTHER_ID), stopped=True)
        monkeypatch.setattr(
            quest_mod,
            "clients_snapshot",
            lambda: {0: bot, 1: other, 2: stopped_other},
        )
        await cog.handle_action_quest(3)
        assert len(bot.ch.put) == 3
        assert {cmd["cmd_arguments"] for _, cmd in bot.ch.put} == {f"<@{PARTNER_ID}>"}

    asyncio.run(scenario())


# Q11: helping an action quest: the target is the quest owner, never any other id
def test_q11_action_receive_targets_only_the_quest_owner(clock):
    async def scenario():
        bot = FakeBot(clock)
        cog = make_cog(bot)

        async def claim(userid, quest_id, claim_userid):
            return True

        async def progress(claim_userid, quest_userid, quest_id):
            return (None, None)

        bot.quest_handler.qh.claim_quest = claim
        bot.quest_handler.qh.update_progress = progress
        quest = {
            "quest_id": "action_receive",
            "userid": OWNER_ID,
            "current": 2,
            "total": 5,
            "channel_id": CHANNEL_ID,
            "guild_id": GUILD_ID,
        }
        await cog.handle_helpable_quests([quest])
        assert len(bot.ch.put) == 3
        assert {cmd["cmd_arguments"] for _, cmd in bot.ch.put} == {f"<@{OWNER_ID}>"}

    asyncio.run(scenario())


# Q12: a stopped account sends nothing and its loop is cancelled (account stop, and autoStop too)
def test_q12_stopped_account_sends_nothing_and_loop_is_cancelled(clock):
    async def scenario():
        bot = FakeBot(clock)
        cog = make_cog(bot)
        bot.stopped = True
        cog.main_quest_loop.cancel = MagicMock()
        await cog.main_quest_loop.coro(cog)
        assert bot.ch.put == []
        cog.main_quest_loop.cancel.assert_called_once()

    asyncio.run(scenario())


def test_q12b_autostop_is_seen_by_the_loop_too(clock, monkeypatch):
    async def scenario():
        bot = FakeBot(clock)
        cog = make_cog(bot)
        monkeypatch.setattr(quest_mod, "stop_requested", lambda: True)
        cog.main_quest_loop.cancel = MagicMock()
        await cog.main_quest_loop.coro(cog)
        assert bot.ch.put == []
        cog.main_quest_loop.cancel.assert_called_once()

    asyncio.run(scenario())


def test_q12c_stop_during_the_all_done_wait_sends_nothing(clock, monkeypatch):
    async def scenario():
        bot = FakeBot(clock)
        cog = make_cog(bot)
        cog.all_quests_done = True
        bot.quest_handler.update_next_timestamp(int(clock.now) + 3000)
        monkeypatch.setattr(quest_mod, "stop_requested", lambda: True)
        await cog.main_quest_loop.coro(cog)
        assert bot.ch.put == []

    asyncio.run(scenario())


# Q13: an unexpected error is logged, does not escape the loop, and the next iteration works again
def test_q13_unexpected_error_is_logged_and_the_loop_goes_on(clock):
    async def scenario():
        bot = FakeBot(clock)
        cog = make_cog(bot)
        bot.ch.fail_put = RuntimeError("simulated failure")
        await cog.main_quest_loop.coro(cog)  # must not raise
        assert any("unexpected error in main_quest_loop" in line for line in bot.logs)
        bot.ch.fail_put = None
        await cog.main_quest_loop.coro(cog)
        assert [cmd["id"] for cmd in sends_of(bot, "quest")] == ["quest"]

    asyncio.run(scenario())


# Q14: cog_unload cancels the loop and stops listening to component messages
def test_q14_cog_unload_cancels_the_loop_and_stops_listening(clock):
    async def scenario():
        bot = FakeBot(clock)
        cog = make_cog(bot)
        assert cog.on_component_message in bot.message_dispatcher.handlers
        cog.main_quest_loop.cancel = MagicMock()
        await cog.cog_unload()
        cog.main_quest_loop.cancel.assert_called_once()
        assert cog.on_component_message not in bot.message_dispatcher.handlers

    asyncio.run(scenario())


# Q15: a quest message without a quest image (url None) does not crash and updates nothing
def test_q15_message_without_quest_image_does_not_crash(clock):
    async def scenario():
        bot = FakeBot(clock)
        cog = make_cog(bot)
        updates = []

        async def fake_update(*args, **kwargs):
            updates.append(args)
            return True

        bot.quest_handler.update_quests = fake_update
        await cog.on_component_message(
            make_message(quest_components(text("Your quests are listed below")))
        )
        assert updates == []
        assert "[quest] no quest image in message" in bot.logs

    asyncio.run(scenario())


# Q16: OCR returns None -> [] from get_quest_details, the previous list is kept, the cog logs it
def test_q16_ocr_failure_keeps_the_previous_quest_list(clock, monkeypatch):
    async def scenario():
        bot = FakeBot(clock)
        cog = make_cog(bot)

        async def ocr_none(url, session, key="helloworld"):
            return None

        monkeypatch.setattr(image_to_text, "get_text_from_url", ocr_none)
        assert await get_quest_details(QUEST_URL, None, "key") == []

        previous = [
            {
                "quest": "catch 3 animals",
                "quest_id": "find_animal_rare",
                "current": 0,
                "total": 3,
                "complete": False,
                "helpable": False,
            }
        ]
        bot.quest_handler.quests = list(previous)
        assert await bot.quest_handler.update_quests(QUEST_URL, CHANNEL_ID, GUILD_ID, 0) is False
        assert bot.quest_handler.quests == previous

        await cog.on_component_message(
            make_message(quest_components(text("Your quests are listed below"), gallery(QUEST_URL)))
        )
        assert "[quest] OCR failed, keeping previous quest list" in bot.logs
        assert bot.quest_handler.quests == previous

    asyncio.run(scenario())


# Q16b: results that are not a list are handled (None, empty, a dict from a response without positional text)
def test_q16b_non_list_ocr_results_are_handled():
    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        async def json(self):
            return self._payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

    class FakeSession:
        def __init__(self, payload):
            self._payload = payload

        def post(self, url, data=None, headers=None, timeout=None):
            return FakeResponse(self._payload)

    assert extract_quests(None) == []
    assert extract_quests([]) == []
    assert extract_quests({"IsErroredOnProcessing": False}) == []

    async def scenario():
        no_text = {"ParsedResults": [{"TextOverlay": {"Lines": []}}]}
        assert await image_to_text.get_text_from_url(QUEST_URL, FakeSession(no_text), "key") is None
        assert await get_quest_details(QUEST_URL, FakeSession(no_text), "key") == []
        with_text = {
            "ParsedResults": [
                {
                    "TextOverlay": {
                        "Lines": [
                            {"LineText": "Catch 3 animals", "Words": [{"Left": 1, "Top": 2}]}
                        ]
                    }
                }
            ]
        }
        lines = await image_to_text.get_text_from_url(QUEST_URL, FakeSession(with_text), "key")
        assert lines == [{"text": "Catch 3 animals", "x_axis": 1, "y_axis": 2}]

    asyncio.run(scenario())


# Q17: the hard-coded action target is gone from the quest cog (no Discord mention with a literal id, no id-sized number)
def test_q17_no_hard_coded_target_in_the_quest_cog():
    source = Path(quest_mod.__file__).read_text(encoding="utf-8")
    assert not re.search(r"<@\d{15,20}>", source)
    assert not re.search(r"(?<!\d)\d{17,20}(?!\d)", source)


# X1: the helper wait for unclaimable quests ends when the account stops
def test_x1_helper_wait_ends_when_the_account_stops(clock):
    async def scenario():
        bot = FakeBot(clock)
        cog = make_cog(bot)
        bot.quest_help_request["pray"]["enabled"] = True
        bot.stopped = True
        await asyncio.wait_for(cog.wait_till_unclaimable_quest_done(), timeout=2)

    asyncio.run(scenario())


# X2: a stop in the middle of a repeat run ends it at once
def test_x2_stop_in_the_middle_of_a_repeat_run_ends_it(clock):
    async def scenario():
        bot = FakeBot(clock)
        cog = make_cog(bot)
        echoes = {"n": 0}

        async def stop_after_three(*args, **kwargs):
            echoes["n"] += 1
            if echoes["n"] == 3:
                bot.stopped = True

        cog.block_till_send = stop_after_three
        assert await cog.handle_repeat_quest("battle") is True
        assert len(sends_of(bot, "battle")) == 3

    asyncio.run(scenario())


# X3: every quest command drops its check entry, so monitor_checks cannot queue it again
def test_x3_quest_commands_drop_their_check_entries(clock, monkeypatch):
    async def scenario():
        bot = FakeBot(clock)
        cog = make_cog(bot)
        other = SimpleNamespace(user=SimpleNamespace(id=PARTNER_ID), stopped=False)
        monkeypatch.setattr(quest_mod, "clients_snapshot", lambda: {1: other})
        await cog.handle_repeat_quest("battle")
        await cog.handle_action_quest(2)
        assert bot.ch.removed.count("battle") >= len(sends_of(bot, "battle"))
        assert bot.ch.removed.count("action") >= len(sends_of(bot, "action"))

    asyncio.run(scenario())


# X4: quest code queues commands in one place only: the hourly cap helper
def test_x4_only_quest_send_calls_put_queue():
    source = Path(quest_mod.__file__).read_text(encoding="utf-8")
    calls = [line.strip() for line in source.splitlines() if ".put_queue(" in line]
    assert calls == ["await self.bot.ch.put_queue(cmd_dict)"]
