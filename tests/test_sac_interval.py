"""Sac rules in core/cogs/sell.py and core/command_handler.py.

- A sac waits SAC_MIN_INTERVAL_SECONDS after the last one OwO confirmed. A refused sac does not start that wait.
- No sac is queued while one still waits for its reply. A refusal clears that wait right away.
- A confirmed sac pauses every command of the account for SAC_SUCCESS_PAUSE_SECONDS. The pause ends by itself.
"""

import asyncio
import random
import threading
import time
from types import SimpleNamespace

from core.cogs import commands as commands_module
from core.cogs import sell
from core.cogs.sell import (
    SAC_MIN_INTERVAL_SECONDS,
    SAC_SUCCESS_PAUSE_SECONDS,
    Sell,
    is_animal_refusal,
    is_sac_success,
)
from core.command_handler import CommandHandler

OWO_ID = 1111
CHANNEL_ID = 2222

SAC_REPLY = (
    "**🔪 | Dusk** sacrificed **<:common:100000000000000001>x3 <:uncommon:100000000000000002>x2** "
    "for a total of **<:essence:1234567890> 5**"
)
SELL_REPLY = (
    "**🔪 | Dusk** sold **<:common:100000000000000001>x3** "
    "for a total of **<:cowoncy:100000000000000001> 300**"
)
# OwO's refusal in the form the sac reply uses. OwO deletes it soon after it is sent
REFUSAL = "**🚫 | Dusk**, You don't have enough animals! >:c"


class FakeClock:
    def __init__(self):
        self.now = 10_000.0

    def monotonic(self):
        return self.now


class FakeCh:
    """The parts of CommandHandler that the sell cog touches"""

    def __init__(self):
        self.queued = []
        self.waiting = set()  # ids with a check still waiting for a reply
        self.removed = []
        self.pauses = []  # seconds given to start_sac_cooldown
        self.cmds_state = {"sell": {"last_ran": 0}, "sac": {"last_ran": 0}}

    async def put_queue(self, cmd_data, **kwargs):
        self.queued.append(cmd_data["cmd_name"])

    async def search_checks(self, id):
        return id in self.waiting

    async def remove_queue(self, cmd_data=None, id=None):
        self.removed.append(id)
        self.waiting.discard(id)

    def start_sac_cooldown(self, seconds, now=None):
        self.pauses.append(seconds)


class FakeBot:
    def __init__(self):
        self.ch = FakeCh()
        self.random = SimpleNamespace(shuffle=lambda items: None)  # keeps the order: sell, then sac
        self.animal_rank_in_zoo = {"common": True, "uncommon": True}
        self.owo_bot_id = OWO_ID
        self.cm = SimpleNamespace(id=CHANNEL_ID)
        self.settings_dict = SimpleNamespace(cashCheck=False, animal=None)

    def is_for_us(self, message):
        return True

    async def sleep_till(self, cooldown, cd_list=True, noise=3):
        return None

    async def log(self, text, color="#ffffff", **kwargs):
        return None


def make_cog(monkeypatch, sell_enabled=False):
    clock = FakeClock()
    monkeypatch.setattr(sell, "time", clock)  # the cog reads time.monotonic() from this module
    bot = FakeBot()
    bot.settings_dict.animal = SimpleNamespace(
        sell=SimpleNamespace(
            enabled=sell_enabled, rarity=SimpleNamespace(get_rarities=lambda: "c u")
        ),
        sac=SimpleNamespace(enabled=True, rarity=SimpleNamespace(get_rarities=lambda: "c u")),
        get_cd=lambda: 33.0,  # the existing attempt pace
    )
    return Sell(bot), bot, clock


def owo_message(content):
    return SimpleNamespace(
        channel=SimpleNamespace(id=CHANNEL_ID),
        author=SimpleNamespace(id=OWO_ID),
        content=content,
    )


def run_loop_once(cog):
    # initiate_loop is a discord.py Loop. Its .coro is the function it runs on each tick
    asyncio.run(Sell.initiate_loop.coro(cog))


def send_owo(cog, content):
    asyncio.run(cog.on_message(owo_message(content)))


def test_interval_is_15_minutes():
    assert SAC_MIN_INTERVAL_SECONDS == 900


def test_pause_after_a_confirmed_sac_is_30_seconds():
    assert SAC_SUCCESS_PAUSE_SECONDS == 30


def test_documented_sac_reply_is_a_success():
    assert is_sac_success(SAC_REPLY)


def test_sell_reply_and_refusal_are_not_sac_successes():
    assert not is_sac_success(SELL_REPLY)
    assert not is_sac_success(REFUSAL)


def test_refusal_is_matched_in_any_case():
    assert is_animal_refusal(REFUSAL)
    assert is_animal_refusal("you don't have enough animals! >:c")
    assert not is_animal_refusal(SAC_REPLY)
    assert not is_animal_refusal(SELL_REPLY)


def test_first_sac_is_queued(monkeypatch):
    cog, bot, _ = make_cog(monkeypatch)
    run_loop_once(cog)
    assert bot.ch.queued == ["sac"]


def test_confirmed_sac_holds_the_next_one_for_15_minutes(monkeypatch):
    cog, bot, clock = make_cog(monkeypatch)
    send_owo(cog, SAC_REPLY)
    assert bot.ch.removed == ["sac"]
    assert cog.sac_last_success == clock.now
    assert bot.ch.pauses == [SAC_SUCCESS_PAUSE_SECONDS]

    clock.now += SAC_MIN_INTERVAL_SECONDS - 1
    run_loop_once(cog)
    assert bot.ch.queued == []

    clock.now += 1
    run_loop_once(cog)
    assert bot.ch.queued == ["sac"]


def test_failed_sac_clears_pending_without_starting_the_15_minute_wait(monkeypatch):
    cog, bot, _ = make_cog(monkeypatch)
    bot.ch.waiting.add("sac")  # a sac that still waits for its reply
    send_owo(cog, REFUSAL)
    assert bot.ch.removed == ["sac"]
    assert asyncio.run(cog.sac_pending()) is False
    assert cog.sac_last_success is None
    assert bot.ch.pauses == []
    assert asyncio.run(cog.sac_may_send()) is True


def test_refusal_clears_only_the_earliest_unanswered_entry(monkeypatch):
    cog, bot, _ = make_cog(monkeypatch, sell_enabled=True)
    bot.ch.waiting = {"sell", "sac"}
    bot.ch.cmds_state["sell"]["last_ran"] = 100.0  # sent first, so OwO answers it first
    bot.ch.cmds_state["sac"]["last_ran"] = 200.0
    send_owo(cog, REFUSAL)
    assert bot.ch.removed == ["sell"]
    assert bot.ch.waiting == {"sac"}


def test_refusal_with_nothing_pending_changes_nothing(monkeypatch):
    cog, bot, _ = make_cog(monkeypatch)
    send_owo(cog, REFUSAL)
    assert bot.ch.removed == []
    assert bot.ch.pauses == []
    assert cog.sac_last_success is None


def test_refused_sac_does_not_start_the_wait(monkeypatch):
    cog, bot, clock = make_cog(monkeypatch)
    run_loop_once(cog)
    assert bot.ch.queued == ["sac"]
    bot.ch.queued.clear()

    send_owo(cog, REFUSAL)
    assert cog.sac_last_success is None

    # Only the existing 33-77 s pace applies, so the next attempt comes soon, not after 15 minutes
    clock.now += 40
    run_loop_once(cog)
    assert bot.ch.queued == ["sac"]


def test_sell_reply_does_not_start_the_sac_wait(monkeypatch):
    cog, bot, _ = make_cog(monkeypatch)
    send_owo(cog, SELL_REPLY)
    assert cog.sac_last_success is None
    assert bot.ch.pauses == []


def test_no_sac_while_its_reply_is_pending(monkeypatch):
    cog, bot, _ = make_cog(monkeypatch)
    bot.ch.waiting.add("sac")
    run_loop_once(cog)
    assert bot.ch.queued == []

    bot.ch.waiting.clear()
    run_loop_once(cog)
    assert bot.ch.queued == ["sac"]


def test_sell_is_not_held_back_by_the_sac_rule(monkeypatch):
    cog, bot, _ = make_cog(monkeypatch, sell_enabled=True)
    send_owo(cog, SAC_REPLY)
    run_loop_once(cog)
    assert bot.ch.queued == ["sell"]


# --- CommandHandler: the 30 second pause -------------------------------------------------------


class FakeClient:
    """What CommandHandler reads from the account client"""

    def __init__(self):
        self.misc = {
            "command_info": {"hunt": {"priority": 2, "basecd": 15, "log_color": "#8b76e6"}}
        }
        self.stopped = False
        self.random = random.Random(0)
        self.db = SimpleNamespace(update_cmd_db=lambda cmd_id: None)

    async def log(self, text, color=None, **kwargs):
        return None


def make_handler():
    ch = CommandHandler(FakeClient())
    ch.cmd_priorities = {"hunt": 2}
    return ch


def test_pause_counts_down_and_expires():
    ch = make_handler()
    assert ch.sac_cooldown_remaining(now=1000.0) == 0.0  # nothing confirmed yet
    ch.start_sac_cooldown(SAC_SUCCESS_PAUSE_SECONDS, now=1000.0)
    assert ch.sac_cooldown_remaining(now=1010.0) == 20.0  # still holding every command
    assert ch.sac_cooldown_remaining(now=1030.0) == 0.0  # expired, commands may go out
    assert ch.sac_cooldown_remaining(now=1500.0) == 0.0


# --- Commands.send_commands: what the consumer actually sends ----------------------------------


class ConsumerBot:
    """What Commands.send_commands reads from the bot. `send` records the text and sends nothing"""

    def __init__(self, ch):
        self.ch = ch
        self.account_index = 1
        self.username = "Dusk"
        self.sent = []
        self.settings_dict = SimpleNamespace(
            cooldowns=SimpleNamespace(
                commandHandler=SimpleNamespace(betweenCommands=[0, 0], readdingToQueue=20)
            ),
            useSlashCommands=False,
        )

    async def wait_gateway_up(self):
        return None

    async def wait_until_ready(self):
        return None

    async def send(self, text, color=None, channel=None, **kwargs):
        self.sent.append(text)
        return None

    async def log(self, text, color="#ffffff", **kwargs):
        return None


def hunt_cmd():
    return {
        "cmd_name": "hunt",
        "cmd_arguments": "",
        "prefix": False,
        "checks": False,
        "id": "hunt",
    }


def patch_consumer(monkeypatch):
    ready = threading.Event()
    ready.set()
    monkeypatch.setattr(commands_module, "registry", SimpleNamespace(all_ready_event=ready))

    async def no_gate(account_index):
        return None

    monkeypatch.setattr(commands_module, "await_send_gate", no_gate)


def make_consumer():
    ch = make_handler()
    bot = ConsumerBot(ch)
    return ch, bot, commands_module.Commands(bot)


async def run_consumer_once(cog):
    await commands_module.Commands.send_commands.coro(cog)


def test_a_pending_sac_does_not_hold_a_hunt(monkeypatch):
    patch_consumer(monkeypatch)

    async def run():
        ch, bot, cog = make_consumer()
        ch.checks.append({"id": "sac", "cmd_name": "sac", "checks": True})  # awaiting its reply
        await ch.put_queue(hunt_cmd())
        started = time.monotonic()
        await run_consumer_once(cog)
        return bot.sent, time.monotonic() - started

    sent, elapsed = asyncio.run(run())
    assert sent == ["hunt"]
    assert elapsed < 0.3


def test_commands_wait_out_the_pause_after_a_confirmed_sac(monkeypatch):
    patch_consumer(monkeypatch)

    async def run():
        ch, bot, cog = make_consumer()
        await ch.put_queue(hunt_cmd())
        ch.start_sac_cooldown(0.5)
        started = time.monotonic()
        task = asyncio.create_task(run_consumer_once(cog))
        await asyncio.sleep(0.15)
        held = list(bot.sent)  # nothing goes out during the pause
        await task
        return held, bot.sent, time.monotonic() - started

    held, sent, elapsed = asyncio.run(run())
    assert held == []
    assert sent == ["hunt"]
    assert elapsed >= 0.4


def test_commands_resume_once_the_pause_has_expired(monkeypatch):
    patch_consumer(monkeypatch)

    async def run():
        ch, bot, cog = make_consumer()
        ch.start_sac_cooldown(0.05)
        await asyncio.sleep(0.15)  # the pause is over before the command is queued
        await ch.put_queue(hunt_cmd())
        started = time.monotonic()
        await run_consumer_once(cog)
        return bot.sent, time.monotonic() - started

    sent, elapsed = asyncio.run(run())
    assert sent == ["hunt"]
    assert elapsed < 0.3
