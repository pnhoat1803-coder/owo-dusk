"""Autoquest phase C (prompt 28): the per-account allowlist, and accounts helping each other with quests.

Fakes only: no network, no Discord. Time is faked: asyncio.sleep and the quest clocks advance one fake clock,
so the file runs in a few seconds. Style copied from tests/test_quest_self.py.
"""

import asyncio
import random
from types import SimpleNamespace

import pytest

from core.cogs import looper as looper_mod
from core.cogs import quest as quest_mod
from core.cogs.battle import Battle
from core.cogs.looper import Looper
from core.cogs.quest import Quest
from utils.quest_helper import quest as qh_module
from utils.quest_helper.quest import LocalQuestHandler, QuestHandler
from utils.quest_helper.quest_types import QUEST_IDS

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
    """One fake clock for every quest reading"""

    def __init__(self):
        self.now = START

    def read(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    clk = Clock()
    monkeypatch.setattr(quest_mod, "_monotonic", clk.read)
    monkeypatch.setattr(quest_mod, "_wall_now", clk.read)
    monkeypatch.setattr(qh_module, "_now", clk.read)

    async def fake_sleep(seconds, *args, **kwargs):
        clk.now += max(0.0, float(seconds))
        await _real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return clk


@pytest.fixture(autouse=True)
def no_other_accounts_and_no_autostop(monkeypatch):
    monkeypatch.setattr(quest_mod, "clients_snapshot", lambda: {})
    monkeypatch.setattr(quest_mod, "stop_requested", lambda: False)


class FakeCH:
    """What the cogs use from the command handler"""

    def __init__(self, clock):
        self.clock = clock
        self.put = []  # (time, command) of every command that reached the queue
        self.removed = []
        self.released = []
        self.sent_id = 555  # what the send event says: a number when the HTTP send returned the message
        self.fail_put = None

    async def put_queue(self, cmd_data, priority=False, quick=False):
        if self.fail_put is not None:
            raise self.fail_put
        self.put.append((self.clock.read(), dict(cmd_data)))

    async def remove_queue(self, cmd_data=None, id=None):
        self.removed.append(id)

    async def search_checks(self, id):
        return False

    def new_sent_event(self, cmd_data):
        return SimpleNamespace(sent_message_id=self.sent_id)

    def release_sent(self, cmd_data):
        self.released.append(cmd_data.get("id"))


class FakeDispatcher:
    def __init__(self):
        self.handlers = []

    def register(self, func, edit=False):
        self.handlers.append(func)

    def unregister(self, func, edit=False):
        if func in self.handlers:
            self.handlers.remove(func)


class FakeQuestCog:
    """The quest cog as the helping cogs see it: send_capped is the hourly cap"""

    def __init__(self):
        self.sent = []
        self.fail = None

    async def send_capped(self, cmd_dict):
        if self.fail is not None:
            raise self.fail
        self.sent.append(dict(cmd_dict))
        return True


class FakeBot:
    """What the quest, battle and looper cogs read from the account client"""

    def __init__(self, clock):
        self.user = SimpleNamespace(id=BOT_ID, name="Dusk", display_name="Dusk")
        self.username = "Dusk"
        self.owo_bot_id = OWO_ID
        self.cm = SimpleNamespace(
            id=CHANNEL_ID,
            guild=SimpleNamespace(id=GUILD_ID, fetch_member_profile=self._fetch_member_profile),
        )
        self.alias = {
            "quest": {"normal": "quest"},
            "battle": {"normal": "battle", "shortform": "b"},
        }
        self.stopped = False
        self.random = random.Random(0)
        self.ch = FakeCH(clock)
        self.logs = []
        self.message_dispatcher = FakeDispatcher()
        self.quest_handler = LocalQuestHandler(QuestHandler(), BOT_ID, None)
        self.ws = SimpleNamespace(session_id="session")
        self.local_headers = {}
        self.quest_help_request = {
            "cookie": {"till": 0, "enabled": False, "userid": 0, "channel": 0},
            "pray": {"till": 0, "enabled": False, "userid": 0, "channel": 0},
            "curse": {"till": 0, "enabled": False, "userid": 0, "channel": 0},
            "battle": {"till": 0, "enabled": False, "userid": 0, "channel": 0},
        }
        self.danger_settings_dict = {"allow_auto_quest": True, "auto_quest_accounts": ["*"]}
        self.quest_cog = FakeQuestCog()
        self.settings_dict = SimpleNamespace(
            prefix="owo ",
            autoQuest=SimpleNamespace(
                enabled=True,
                canEnable=True,
                helpOthers=True,
                useHelpChannel=False,
                channelId=None,
                get_cd=lambda: 20.0,
            ),
            commands=SimpleNamespace(
                owo=SimpleNamespace(enabled=True, get_cd=lambda: 20.0),
                hunt=SimpleNamespace(enabled=False, get_cd=lambda: 20.0),
                battle=SimpleNamespace(
                    enabled=False, get_cd=lambda: 20.0, shortform=False
                ),
                pray=SimpleNamespace(
                    enabled=False,
                    get_cd=lambda: 20.0,
                    count=False,
                    user_id=[4242],
                    ping_user=False,
                    custom_channel=SimpleNamespace(enabled=False, channel=None),
                ),
                curse=SimpleNamespace(
                    enabled=False,
                    get_cd=lambda: 20.0,
                    count=False,
                    user_id=[4242],
                    ping_user=False,
                    custom_channel=SimpleNamespace(enabled=False, channel=None),
                ),
                cookie=SimpleNamespace(enabled=False, get_cd=lambda: 20.0),
            ),
            gamble=SimpleNamespace(
                coinflip=SimpleNamespace(enabled=False, get_cd=lambda: 20.0),
                slots=SimpleNamespace(enabled=False, get_cd=lambda: 20.0),
                blackjack=SimpleNamespace(enabled=False),
            ),
            cooldowns=SimpleNamespace(
                commandHandler=SimpleNamespace(betweenCommands=[0, 0], readdingToQueue=20),
                reactionBot=SimpleNamespace(huntAndBattle=False, owo=False, prayAndCurse=False),
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

    def name_in(self, text, message=None):
        return True

    def name_phrase_in(self, text, template, message=None):
        return template.format(name=self.username) in text

    def get_cog(self, name):
        return self.quest_cog if name == "Quest" else None

    async def unload_cog(self, name):
        self.logs.append(f"unload {name}")


async def echo_ok(*args, **kwargs):
    return True


def make_cog(bot):
    cog = Quest(bot)
    cog.block_till_send = echo_ok
    return cog


def scripted_echo(outcomes):
    queue = list(outcomes)

    async def stub(*args, **kwargs):
        return queue.pop(0)

    return stub


def quest_entry(quest_id, total, current=0, userid=OWNER_ID):
    return {
        "userid": userid,
        "quest": quest_id,
        "quest_id": quest_id,
        "current": current,
        "total": total,
        "complete": False,
        "helpable": True,
        "channel_id": CHANNEL_ID,
        "guild_id": GUILD_ID,
    }


def sends_of(bot, command_id):
    return [cmd for _, cmd in bot.ch.put if cmd["id"] == command_id]


def owner_mention():
    return f"<@{OWNER_ID}>"


# H1-H3: the allowlist (C0)
@pytest.mark.parametrize(
    "entries, expected",
    [
        ([], False),  # empty: no account runs autoquest
        ([str(BOT_ID)[-4:]], True),  # the last 4 digits of this account
        (["*"], True),  # every account
        ([str(PARTNER_ID)], False),  # somebody else
    ],
)
def test_h1_h3_allowlist_matches_empty_last4_star_and_full_id(clock, entries, expected):
    bot = FakeBot(clock)
    bot.danger_settings_dict = {"allow_auto_quest": True, "auto_quest_accounts": entries}
    assert make_cog(bot)._account_allowed() is expected


def test_h3b_missing_key_means_no_account(clock):
    bot = FakeBot(clock)
    bot.danger_settings_dict = {"allow_auto_quest": True}
    assert make_cog(bot)._account_allowed() is False
    bot.danger_settings_dict["auto_quest_accounts"] = [str(BOT_ID)]  # the full id counts too
    assert make_cog(bot)._account_allowed() is True


def test_h1b_cog_load_for_a_non_listed_account_starts_no_loop(clock):
    async def scenario():
        bot = FakeBot(clock)
        bot.danger_settings_dict["auto_quest_accounts"] = []
        cog = make_cog(bot)
        await cog.cog_load()
        assert "[quest] autoquest not enabled for this account (allowlist)" in bot.logs
        assert not cog.main_quest_loop.is_running()

    asyncio.run(scenario())


# H4: the help wait ends after 15 fake minutes, and the flags are reset (C1)
def test_h4_help_wait_stops_at_fifteen_minutes_and_resets_the_flags(clock):
    async def scenario():
        bot = FakeBot(clock)
        cog = make_cog(bot)
        bot.quest_help_request["cookie"]["enabled"] = True
        await cog.wait_till_unclaimable_quest_done()
        assert clock.now - START == quest_mod.QUEST_HELP_WAIT_MAX_SECONDS
        assert any("[quest] help wait timeout cookie" in line for line in bot.logs)
        assert bot.quest_help_request["cookie"]["enabled"] is False

    asyncio.run(scenario())


# H5: the flags are reset even when the wait itself fails (C1)
def test_h5_flags_are_reset_even_when_the_wait_fails(clock, monkeypatch):
    async def scenario():
        bot = FakeBot(clock)
        cog = make_cog(bot)
        bot.quest_help_request["cookie"]["enabled"] = True

        async def boom(*args, **kwargs):
            raise RuntimeError("simulated failure")

        monkeypatch.setattr(asyncio, "sleep", boom)
        with pytest.raises(RuntimeError):
            await cog.wait_till_unclaimable_quest_done()
        assert bot.quest_help_request["cookie"]["enabled"] is False

    asyncio.run(scenario())


# H6: the battle help accepts the full mention "<@id>" and "<@!id>", and nothing else (C2)
@pytest.mark.parametrize(
    "content, counted",
    [(f"<@{OWNER_ID}>", True), (f"<@!{OWNER_ID}>", True), (f"<@{PARTNER_ID}>", False)],
)
def test_h6_battle_help_mention_check_accepts_full_mentions_only(clock, content, counted):
    async def scenario():
        bot = FakeBot(clock)
        cog = Battle(bot)
        bot.quest_help_request["battle"] = {"till": 3, "enabled": True, "userid": OWNER_ID, "channel": CHANNEL_ID}
        progress = []

        async def update_progress(claim_userid, quest_userid, quest_id):
            progress.append(quest_id)
            return (1, False)

        bot.quest_handler.qh.update_progress = update_progress
        embed = SimpleNamespace(
            author=SimpleNamespace(name="Dusk challenges you"),
            footer=SimpleNamespace(text="this challenge will expire in 10 minutes"),
        )
        message = SimpleNamespace(
            channel=SimpleNamespace(id=CHANNEL_ID),
            author=SimpleNamespace(id=OWO_ID),
            content=content,
            embeds=[embed],
            reference=None,
        )
        await cog.on_message(message)
        assert (progress == ["battle_friend"]) is counted
        assert bot.quest_help_request["battle"]["till"] == (2 if counted else 3)
        if counted:
            # the next help battle goes out too, through the quest cap (C2)
            assert bot.quest_cog.sent and bot.quest_cog.sent[0]["cmd_arguments"] == owner_mention()

    asyncio.run(scenario())


# H7: a battle_friend help sends "owo battle <@owner>" through the cap; with no help it is the normal battle (C2)
def test_h7_help_battle_goes_through_the_cap_and_normal_battle_is_unchanged(clock):
    async def scenario():
        bot = FakeBot(clock)
        cog = Battle(bot)
        bot.quest_help_request["battle"] = {"till": 2, "enabled": True, "userid": OWNER_ID, "channel": 0}
        await cog.requeue_battle()
        assert bot.quest_cog.sent[-1]["cmd_arguments"] == owner_mention()
        assert bot.quest_cog.sent[-1]["cmd_name"] == "battle"
        assert bot.ch.put == []  # nothing went around the cap
        bot.quest_help_request["battle"]["enabled"] = False
        await cog.requeue_battle()
        assert bot.ch.put[-1][1]["cmd_arguments"] is None  # the plain battle command, as before

    asyncio.run(scenario())


def make_looper(bot, monkeypatch):
    async def ready():
        return None

    monkeypatch.setattr(looper_mod, "await_all_ready", ready)
    looper = Looper(bot)
    looper.get_pray_curse_settings = lambda cmd: bot.settings_dict.commands.pray if cmd == "pray" else bot.settings_dict.commands.curse

    async def echo(cmd):
        return f"owo {cmd['cmd_name']}"

    looper.echo_text = echo
    looper.block_till_send = echo_ok
    return looper


# H8: a pray help sends "owo pray <@owner>"; an error in it is logged and the normal pray goes on (C3)
def test_h8_pray_help_is_sent_and_an_error_never_stops_the_normal_pray(clock, monkeypatch):
    async def scenario():
        bot = FakeBot(clock)
        looper = make_looper(bot, monkeypatch)
        bot.quest_help_request["pray"] = {"till": 2, "enabled": True, "userid": OWNER_ID, "channel": 0}
        await looper.send_pray_curse("pray")
        assert bot.quest_cog.sent[-1]["cmd_name"] == "pray"
        assert bot.quest_cog.sent[-1]["cmd_arguments"] == owner_mention()
        assert bot.quest_help_request["pray"]["till"] == 1  # counted: the message went out
        # an error in the help part: logged, the flag is reset, and the next cycle is the normal pray
        bot.quest_help_request["pray"] = {"till": 2, "enabled": True, "userid": OWNER_ID, "channel": 0}
        bot.quest_cog.fail = RuntimeError("simulated")
        await looper.send_pray_curse("pray")
        assert any("quest help pray failed" in line for line in bot.logs)
        assert bot.quest_help_request["pray"]["enabled"] is False
        bot.quest_cog.fail = None
        await looper.send_pray_curse("pray")
        normal = [cmd for _, cmd in bot.ch.put if cmd["id"] == "pray"]
        assert normal and normal[-1]["cmd_name"] == "pray"
        assert owner_mention() not in normal[-1]["cmd_arguments"]

    asyncio.run(scenario())


# H9: a curse help sends "owo curse <@owner>", never "owo pray" (C3)
def test_h9_curse_help_sends_curse_never_pray(clock, monkeypatch):
    async def scenario():
        bot = FakeBot(clock)
        looper = make_looper(bot, monkeypatch)
        bot.quest_help_request["curse"] = {"till": 1, "enabled": True, "userid": OWNER_ID, "channel": 0}
        await looper.send_pray_curse("pray")  # the running loop is named pray: the help still says curse
        assert bot.quest_cog.sent[-1]["cmd_name"] == "curse"
        assert bot.quest_cog.sent[-1]["cmd_arguments"] == owner_mention()
        assert not any(cmd["cmd_name"] == "pray" and owner_mention() in cmd["cmd_arguments"] for cmd in bot.quest_cog.sent)

    asyncio.run(scenario())


# H10: "receive prayers" is claimed and helped with one id, "pray" (C4)
def test_h10_receive_prayers_uses_the_pray_id_for_registration_and_help(clock, monkeypatch):
    async def scenario():
        assert QUEST_IDS["receive prayers"]["id"] == "pray"
        owner_qh = LocalQuestHandler(QuestHandler(), OWNER_ID, None)
        rows = [{"quest": "receive prayers", "current": 0, "total": 2, "complete": False}]

        async def fake_details(url, session, key="helloworld"):
            return rows

        monkeypatch.setattr(qh_module, "get_quest_details", fake_details)
        await owner_qh.update_quests(QUEST_URL, CHANNEL_ID, GUILD_ID, 0)
        registered = [q for q in owner_qh.qh.quests if q["userid"] == OWNER_ID]
        assert [q["quest_id"] for q in registered] == ["pray"]

        bot = FakeBot(clock)
        cog = make_cog(bot)
        await cog.handle_helpable_quests([dict(registered[0], current=0, total=2)])
        prayers = sends_of(bot, "pray")
        assert len(prayers) == 2
        assert {cmd["cmd_arguments"] for cmd in prayers} == {owner_mention()}

    asyncio.run(scenario())


# H11: the owner's completed quest, or one gone from its list, loses its helper registration (C5)
def test_h11_owner_side_registration_goes_when_the_quest_completes_or_is_gone(clock, monkeypatch):
    async def scenario():
        qh = QuestHandler()
        owner = LocalQuestHandler(qh, OWNER_ID, None)
        rows = {"value": [{"quest": "receive cookies", "current": 0, "total": 3, "complete": False}]}

        async def fake_details(url, session, key="helloworld"):
            return rows["value"]

        monkeypatch.setattr(qh_module, "get_quest_details", fake_details)
        await owner.update_quests(QUEST_URL, CHANNEL_ID, GUILD_ID, 0)
        assert [q["quest_id"] for q in qh.quests] == ["cookie"]
        assert [bul["quest_id"] for bul in qh.bulletine] == ["cookie"]
        rows["value"] = [{"quest": "receive cookies", "current": 3, "total": 3, "complete": True}]
        await owner.update_quests(QUEST_URL, CHANNEL_ID, GUILD_ID, 0)
        assert qh.quests == [] and qh.bulletine == []
        # all quests done: the same cleanup
        rows["value"] = [{"quest": "receive cookies", "current": 0, "total": 3, "complete": False}]
        await owner.update_quests(QUEST_URL, CHANNEL_ID, GUILD_ID, 0)
        await owner.clear_quests()
        assert qh.quests == [] and owner.quests == []

    asyncio.run(scenario())


# H12: a claim expires after 30 fake minutes (C6), and goes back to the list when the helper stops (C6)
def test_h12_claim_expires_after_thirty_minutes_and_is_released_on_stop(clock):
    async def scenario():
        bot = FakeBot(clock)
        cog = make_cog(bot)
        qh = bot.quest_handler.qh
        await qh.register_helpable_quest(
            {"quest": "receive an action from a friend", "current": 0, "total": 3, "complete": False},
            OWNER_ID,
            CHANNEL_ID,
            GUILD_ID,
        )
        assert await qh.claim_quest(OWNER_ID, "action_receive", BOT_ID) is True
        assert qh.get_available_quests(None, claim_ttl=quest_mod.QUEST_CLAIM_TTL_SECONDS) == []
        clock.now += quest_mod.QUEST_CLAIM_TTL_SECONDS + 1
        assert len(qh.get_available_quests(None, claim_ttl=quest_mod.QUEST_CLAIM_TTL_SECONDS)) == 1
        # claimed again, then the helper stops: the wait ends and the claim goes back
        assert await qh.claim_quest(OWNER_ID, "action_receive", BOT_ID) is True
        bot.stopped = True
        await cog.wait_till_unclaimable_quest_done()
        assert len(qh.get_available_quests(None)) == 1

    asyncio.run(scenario())


# H13: an echo that times out does not count; three in a row end the help run; progress counts echoed commands (C7)
def test_h13_help_run_counts_only_echoed_commands(clock):
    async def scenario():
        bot = FakeBot(clock)
        cog = make_cog(bot)
        progress = []

        async def update_progress(claim_userid, quest_userid, quest_id):
            progress.append(quest_id)
            return (None, False)

        bot.quest_handler.qh.update_progress = update_progress
        cog.block_till_send = scripted_echo([False, False, False])
        assert await cog.handle_repeat_quest("pray", 3, owner_mention(), OWNER_ID, "pray", None, confirmed=True)
        assert len(sends_of(bot, "pray")) == 3
        assert progress == []
        assert "[quest] pray 3 commands without echo, stopping run" in bot.logs
        bot.ch.put.clear()
        cog.block_till_send = scripted_echo([False, True, True, True])
        assert await cog.handle_repeat_quest("pray", 3, owner_mention(), OWNER_ID, "pray", None, confirmed=True)
        assert len(sends_of(bot, "pray")) == 4  # one timeout, then three echoes
        assert progress == ["pray", "pray", "pray"]

    asyncio.run(scenario())


# H14: a partner that is not in this server is skipped (C8); a partner in the server is used
def test_h14_action_partner_must_be_in_this_server(clock, monkeypatch):
    async def scenario():
        bot = FakeBot(clock)
        cog = make_cog(bot)

        class Client:
            def __init__(self, user_id, guilds):
                self.user = SimpleNamespace(id=user_id)
                self.stopped = False
                self.guilds = guilds

            def get_guild(self, guild_id):
                return SimpleNamespace(id=guild_id) if guild_id in self.guilds else None

        outside = Client(PARTNER_ID, {999})
        monkeypatch.setattr(quest_mod, "clients_snapshot", lambda: {1: outside})
        await cog.handle_action_quest(2)
        assert bot.ch.put == []
        assert "[quest] no partner in this server" in bot.logs
        inside = Client(PARTNER_ID + 1, {GUILD_ID})
        monkeypatch.setattr(quest_mod, "clients_snapshot", lambda: {1: outside, 2: inside})
        await cog.handle_action_quest(2)
        assert {cmd["cmd_arguments"] for cmd in sends_of(bot, "action")} == {f"<@{PARTNER_ID + 1}>"}

    asyncio.run(scenario())


# H14b: a helper that cannot see the owner's server does not help it (C8)
def test_h14b_help_is_skipped_when_the_owner_server_is_not_visible(clock):
    async def scenario():
        bot = FakeBot(clock)
        bot.get_guild = lambda guild_id: None  # this account is not in the owner's server
        cog = make_cog(bot)
        await cog.handle_helpable_quests([quest_entry("cookie", 2)])
        assert bot.ch.put == []
        assert "[quest] no partner in this server" in bot.logs

    asyncio.run(scenario())
