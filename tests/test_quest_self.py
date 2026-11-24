"""Autoquest phase B (prompt 27): this account's own quests are counted and chosen correctly.

Fakes only: no network, no Discord. Time is faked: asyncio.sleep and the quest clocks advance one fake clock,
so the file runs in a few seconds. Style copied from tests/test_quest_safety.py: plain test functions that call
asyncio.run() on a scenario, SimpleNamespace fakes and monkeypatch.
"""

import asyncio
import json
import random
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.cogs import quest as quest_mod
from core.cogs.quest import Quest
from utils.quest_helper import quest as qh_module
from utils.quest_helper.quest import LocalQuestHandler, QuestHandler

OWNER_ID = 5550001  # the account that posted a quest
BOT_ID = 5550002  # the account under test
PARTNER_ID = 5550003  # another account in the same process
OWO_ID = 5550004
CHANNEL_ID = 5550005
GUILD_ID = 5550006
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
        self.on_quest_put = None  # async hook: OwO answers "owo quest" through it

    async def put_queue(self, cmd_data, priority=False, quick=False):
        self.put.append((self.clock.now, dict(cmd_data)))
        if cmd_data.get("id") == "quest" and self.on_quest_put is not None:
            await self.on_quest_put(cmd_data)

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
        self.user = SimpleNamespace(id=BOT_ID, name="Dusk", display_name="Dusk")
        self.username = "Dusk"
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
                coinflip=SimpleNamespace(enabled=False, get_cd=lambda: 20.0),
                slots=SimpleNamespace(enabled=False, get_cd=lambda: 20.0),
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

    def name_phrase_in(self, text, template, message=None):
        return template.format(name=self.username) in text


async def echo_ok(*args, **kwargs):
    """Stands in for block_till_send: OwO's echo of the command came back"""
    return True


def make_cog(bot):
    cog = Quest(bot)
    cog.block_till_send = echo_ok
    return cog


def scripted_echo(outcomes):
    """block_till_send answers with these outcomes, one per command"""
    queue = list(outcomes)

    async def stub(*args, **kwargs):
        return queue.pop(0)

    return stub


def echo_with(cog, make_reply):
    """The echo comes back and, like OwO, the reply comes in right after the command went out"""

    async def stub(*args, **kwargs):
        await asyncio.sleep(0)
        await cog._handle_message(make_reply())
        return True

    return stub


def quest_entry(quest_id, total, current=0):
    return {
        "quest": quest_id,
        "quest_id": quest_id,
        "current": current,
        "total": total,
        "complete": False,
        "helpable": False,
    }


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


def make_message(components, buttons=(), embeds=()):
    return SimpleNamespace(
        author=SimpleNamespace(id=OWO_ID),
        channel=SimpleNamespace(id=CHANNEL_ID),
        channel_id=CHANNEL_ID,
        content="",
        components=components,
        buttons=list(buttons),
        embeds=list(embeds),
    )


def battle_message(bot, xp=200):
    """OwO's battle result of this account: the author line and the XP in the footer"""
    embed = SimpleNamespace(
        author=SimpleNamespace(name=f"{bot.username} goes into battle!"),
        footer=SimpleNamespace(text=f"+{xp} xp"),
        title=None,
        description=None,
        fields=[],
    )
    return make_message([], embeds=[embed])


def hunt_message(bot, text_content="you found: :cat2: **Cat**"):
    return SimpleNamespace(
        author=SimpleNamespace(id=OWO_ID),
        channel=SimpleNamespace(id=CHANNEL_ID),
        content=text_content,
        components=[],
        embeds=[],
    )


def sends_of(bot, command_id):
    return [cmd for _, cmd in bot.ch.put if cmd["id"] == command_id]


# S1: update_progress returns (current, completed), and (None, False) when there is nothing to count
def test_s1_update_progress_returns_current_then_completed():
    async def scenario():
        qh = QuestHandler()
        qh.quests = [
            {
                "userid": OWNER_ID,
                "quest_id": "action_receive",
                "current": 1,
                "total": 3,
                "complete": False,
                "claim_userid": BOT_ID,
            }
        ]
        assert await qh.update_progress(BOT_ID, OWNER_ID, "action_receive") == (2, False)
        assert await qh.update_progress(BOT_ID, OWNER_ID, "action_receive") == (3, True)
        assert await qh.update_progress(BOT_ID, OWNER_ID, "no_such_quest") == (None, False)
        qh.quests = [
            {
                "userid": OWNER_ID,
                "quest_id": "action_receive",
                "current": 1,
                "total": 3,
                "complete": False,
                "claim_userid": 999,
            }
        ]
        assert await qh.update_progress(BOT_ID, OWNER_ID, "action_receive") == (None, False)

    asyncio.run(scenario())


# S2: every caller of update_progress unpacks "current, completed", in that order
def test_s2_every_update_progress_caller_unpacks_current_then_completed():
    root = Path(quest_mod.__file__).resolve().parents[2]
    call = re.compile(r"(?P<lhs>[^=]*?)=\s*await\s+[\w.]*update_progress\(", re.S)
    found = 0
    for folder in ("core", "utils"):
        for path in (root / folder).rglob("*.py"):
            if "bak" in path.name or "__pycache__" in path.parts:
                continue
            source = path.read_text(encoding="utf-8")
            for match in call.finditer(source):
                found += 1
                lhs = re.sub(r"[\s()]", "", match.group("lhs")).rstrip(",")
                assert lhs.endswith("current,completed"), f"{path.name}: unpacks {lhs!r}"
    assert found >= 4  # quest.py (two callers), looper.py, battle.py


# S3: hunt, owo and gamble, all disabled by the player, all run in one round (B2)
def test_s3_hunt_owo_and_gamble_all_run_in_one_round(clock):
    async def scenario():
        bot = FakeBot(clock)
        cog = make_cog(bot)
        bot.settings_dict.commands.owo.enabled = False
        await cog.handle_self_quests(
            [quest_entry("hunt", 2), quest_entry("owo", 2), quest_entry("gamble", 2)]
        )
        assert len(sends_of(bot, "hunt")) == 2
        assert len(sends_of(bot, "owo")) == 2
        gamble = [cmd for _, cmd in bot.ch.put if cmd["cmd_name"] in ("slots", "coinflip")]
        assert len(gamble) == 2

    asyncio.run(scenario())


# S4: action_send runs when hunt, battle and owo are enabled by the player, and a partner exists (B2)
def test_s4_action_send_runs_with_a_partner_and_the_other_commands_enabled(clock, monkeypatch):
    async def scenario():
        bot = FakeBot(clock)
        cog = make_cog(bot)
        for name in ("hunt", "battle", "owo"):
            getattr(bot.settings_dict.commands, name).enabled = True
        partner = SimpleNamespace(user=SimpleNamespace(id=PARTNER_ID), stopped=False)
        monkeypatch.setattr(quest_mod, "clients_snapshot", lambda: {1: partner})
        await cog.handle_self_quests([quest_entry("action_send", 2)])
        actions = sends_of(bot, "action")
        assert len(actions) == 2
        assert {cmd["cmd_arguments"] for cmd in actions} == {f"<@{PARTNER_ID}>"}

    asyncio.run(scenario())


# S5: an echo that times out does not count (B3); three in a row end the run
def test_s5_echo_timeouts_do_not_count_and_three_in_a_row_end_the_run(clock):
    async def scenario():
        bot = FakeBot(clock)
        cog = make_cog(bot)
        # two timeouts, then five echoes: the quest needs five, so seven commands are sent
        cog.block_till_send = scripted_echo([False, False, True, True, True, True, True])
        await cog.handle_self_quests([quest_entry("hunt", 5)])
        assert len(sends_of(bot, "hunt")) == 7
        # never an echo: stops after three commands, with the log line
        cog.block_till_send = scripted_echo([False] * 10)
        bot.ch.put.clear()
        await cog.handle_self_quests([quest_entry("hunt", 5)])
        assert len(sends_of(bot, "hunt")) == 3
        assert "[quest] hunt 3 commands without echo, stopping run" in bot.logs

    asyncio.run(scenario())


# S6: "battle 3 times": each own battle embed is one battle, whatever its XP (B4)
def test_s6_battle_three_times_stops_after_three_battles(clock):
    async def scenario():
        bot = FakeBot(clock)
        cog = make_cog(bot)
        cog.block_till_send = echo_with(cog, lambda: battle_message(bot, xp=200))
        await cog.handle_self_quests([quest_entry("battle", 3)])
        assert len(sends_of(bot, "battle")) == 3
        assert cog.quest_self_doable["battle"]["till"] == 0

    asyncio.run(scenario())


# S7: "gain 500 xp from battle": counted in its own counter, with no "battle" key and with battle enabled (B5)
def test_s7_battle_xp_counts_its_own_xp_without_the_battle_key(clock):
    async def scenario():
        bot = FakeBot(clock)
        cog = make_cog(bot)
        cog.block_till_send = echo_with(cog, lambda: battle_message(bot, xp=200))
        await cog.handle_self_quests([quest_entry("battle_xp", 500)])
        assert len(sends_of(bot, "battle")) == 3  # 3 x 200 XP reaches 500
        assert cog.quest_self_doable["battle"]["quest"] is False

    asyncio.run(scenario())


def test_s7b_battle_xp_counts_with_the_battle_command_enabled(clock):
    async def scenario():
        bot = FakeBot(clock)
        cog = make_cog(bot)
        bot.settings_dict.commands.battle.enabled = True
        await cog.handle_self_quests([quest_entry("battle_xp", 500)])
        assert sends_of(bot, "battle") == []  # the player's own battles do the work
        for _ in range(3):
            await cog._handle_message(battle_message(bot, xp=200))
        assert cog.quest_self_doable["battle_xp"]["till"] <= 0

    asyncio.run(scenario())


# S8: "find rare animals": counted from its own matches, the "hunt" key is not needed (B5)
def test_s8_find_animal_counts_its_own_matches_without_the_hunt_key(clock):
    async def scenario():
        bot = FakeBot(clock)
        cog = make_cog(bot)
        cog.block_till_send = echo_with(cog, lambda: hunt_message(bot))
        await cog.handle_self_quests([quest_entry("find_animal_rare", 2)])
        assert len(sends_of(bot, "hunt")) == 2
        assert cog.quest_self_doable["hunt"]["quest"] is False

    asyncio.run(scenario())


# S9: the flags are reset at the start of every round and when a run ends (B6)
def test_s9_flags_are_reset_between_rounds_and_after_each_run(clock):
    async def scenario():
        bot = FakeBot(clock)
        cog = make_cog(bot)
        cog.repeat_quest_flag = True
        cog.catcha_a_rank = "rare"
        cog.quest_self_doable["hunt"] = {"quest": True, "till": 9}
        await cog.handle_self_quests([])
        assert cog.repeat_quest_flag is False
        assert cog.catcha_a_rank == ""
        assert cog.quest_self_doable["hunt"] == {"quest": False, "till": 0}
        assert cog._active_run is None
        # after a run the flags are back to normal and its own counter is off
        cog.repeat_quest_flag = True
        await cog.handle_self_quests([quest_entry("hunt", 1)])
        assert cog.repeat_quest_flag is False
        assert cog.catcha_a_rank == ""
        assert cog._active_run is None
        assert cog.quest_self_doable["hunt"]["quest"] is False

    asyncio.run(scenario())


# S10: no Quest Log reply within 30 s: the round does no quest work at all (B7)
def test_s10_no_quest_log_reply_means_no_quest_work_this_round(clock):
    async def scenario():
        bot = FakeBot(clock)
        cog = make_cog(bot)
        bot.quest_handler.quests = [
            {
                "quest": "manually hunt",
                "quest_id": "hunt",
                "current": 0,
                "total": 3,
                "complete": False,
                "helpable": False,
            }
        ]
        await cog.main_quest_loop.coro(cog)
        assert [cmd for _, cmd in bot.ch.put if cmd["id"] == "quest"]  # "owo quest" went out
        assert sends_of(bot, "hunt") == []
        assert "[quest] no quest log reply, skipping this round" in bot.logs

    asyncio.run(scenario())


# S11: the reply comes in, and the round uses the NEW list, not the old one (B7)
def test_s11_the_round_uses_the_list_from_the_reply(clock, monkeypatch):
    async def scenario():
        bot = FakeBot(clock)
        cog = make_cog(bot)
        bot.settings_dict.commands.owo.enabled = False
        bot.quest_handler.quests = [
            {
                "quest": "manually hunt",
                "quest_id": "hunt",
                "current": 0,
                "total": 3,
                "complete": False,
                "helpable": False,
            }
        ]
        new_rows = [{"quest": "say owo", "progress": "0/2", "current": 0, "total": 2, "complete": False}]

        async def fake_details(url, session, key="helloworld"):
            return new_rows

        monkeypatch.setattr(qh_module, "get_quest_details", fake_details)

        async def owo_answers(cmd_data):
            # OwO's Quest Log for this account comes in right after "owo quest" was sent
            await cog._handle_component_message(
                make_message(quest_components(text("Your quests are listed below"), gallery(QUEST_URL)))
            )

        bot.ch.on_quest_put = owo_answers
        await cog.main_quest_loop.coro(cog)
        assert sends_of(bot, "hunt") == []
        assert len(sends_of(bot, "owo")) == 2

    asyncio.run(scenario())


# S12: the help channel command is returned (B8)
def test_s12_get_quest_cmd_returns_the_command_for_a_help_channel(clock):
    bot = FakeBot(clock)
    cog = make_cog(bot)
    with_channel = cog.get_quest_cmd(777)
    assert with_channel is not None
    assert with_channel["channel"] == 777
    assert with_channel["id"] == "quest"
    assert "channel" not in cog.get_quest_cmd()


# S13: the all-done text and its next-quest time are found in a section and in an embed (B9)
def test_s13_all_done_text_in_a_section_or_an_embed_is_detected(clock):
    async def scenario():
        bot = FakeBot(clock)
        cog = make_cog(bot)
        nested = [
            section(
                text(f"<@{BOT_ID}>'s Quest Log"),
                text("UwU You finished all of your quests! Next quest <t:1700000000:R>"),
            )
        ]
        await cog.on_component_message(make_message(nested))
        assert cog.all_quests_done is True
        assert bot.quest_handler.next_quest_timestamp == 1700000000
        cog.all_quests_done = False
        embed = SimpleNamespace(
            title=None,
            description="UwU You finished all of your quests! Come back at <t:1700000500:f>",
            fields=[],
        )
        await cog.on_component_message(make_message(quest_components(), embeds=[embed]))
        assert cog.all_quests_done is True
        assert bot.quest_handler.next_quest_timestamp == 1700000500

    asyncio.run(scenario())


# S14: one raw sample per quest message, ids masked, the newest 50 lines kept (B10)
def test_s14_quest_debug_line_is_masked_and_capped_at_50(clock, tmp_path, monkeypatch):
    monkeypatch.setenv("DUSK_LOG_DIR", str(tmp_path))

    async def scenario():
        bot = FakeBot(clock)
        cog = make_cog(bot)
        message = make_message(
            quest_components(text(f"UwU You finished all of your quests! <@{BOT_ID}> <@{OWNER_ID}>"))
        )
        await cog.on_component_message(message)
        path = tmp_path / "quest_debug.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["account"] == str(BOT_ID)[-4:]
        assert record["tree"][0]["type"] == "section"
        raw = path.read_text(encoding="utf-8")
        assert str(BOT_ID) not in raw
        assert str(OWNER_ID) not in raw
        assert f"<@…{str(OWNER_ID)[-4:]}>" in raw
        for _ in range(60):
            await cog.on_component_message(message)
        assert len(path.read_text(encoding="utf-8").splitlines()) == quest_mod.QUEST_DEBUG_MAX_LINES == 50

    asyncio.run(scenario())
