"""A hunt or battle that gets no OwO reply is queued again when the reply fallback gives up on it.

Their round cycle only restarts from OwO's reply, so a silent give-up used to stop them for good.
Other commands that are given up are still not queued again.
"""

import asyncio
import random
import threading
from types import SimpleNamespace

from core.cogs import commands as commands_module
from core.cogs.battle import Battle
from core.cogs.hunt import Hunt
from core.command_handler import CommandHandler
from core.reply_fallback import ReplyFallback

CHANNEL_ID = 2222
OWO_ID = 1111


class FakeClient:
    """What CommandHandler reads from the account client"""

    def __init__(self):
        self.misc = {
            "command_info": {
                "hunt": {"priority": 2, "basecd": 15, "log_color": "#8b76e6"},
                "battle": {"priority": 2, "basecd": 15, "log_color": "#8b76e6"},
                "sac": {"priority": 9, "basecd": 1, "log_color": "#d0d0b8"},
            }
        }
        self.stopped = False
        self.random = random.Random(0)
        self.db = SimpleNamespace(update_cmd_db=lambda cmd_id: None)

    async def log(self, text, color=None, **kwargs):
        return None


def make_command_settings(**extra):
    return SimpleNamespace(enabled=True, shortform=False, get_cd=lambda: 0.0, **extra)


class FakeBot:
    """What the send loop, the hunt and battle cogs and the reply fallback read from the account"""

    def __init__(self, ch):
        self.ch = ch
        self.account_index = 1
        self.username = "Dusk"
        self.owo_bot_id = OWO_ID
        self.cm = SimpleNamespace(id=CHANNEL_ID)
        self.hunt_disabled = False
        self.sent = []
        self.alias = {
            "hunt": {"normal": "hunt", "shortform": "h"},
            "battle": {"normal": "battle", "shortform": "b"},
        }
        self.settings_dict = SimpleNamespace(
            prefix="owo ",
            useSlashCommands=False,
            cooldowns=SimpleNamespace(
                commandHandler=SimpleNamespace(betweenCommands=[0, 0], readdingToQueue=20),
                reactionBot=SimpleNamespace(huntAndBattle=False),
            ),
            commands=SimpleNamespace(
                hunt=make_command_settings(),
                battle=make_command_settings(show_streak=False, notify_streak_loss=False),
            ),
        )
        self.global_settings_dict = SimpleNamespace(
            replyFallback=SimpleNamespace(
                enabled=True,
                firstCheckAfter=15,
                checkEvery=15,
                giveUpAfter=60,
                historyLimit=15,
                maxConcurrentFetches=4,
                holdWhileGatewayDown=False,
                dmCaptchaCheckEvery=0,
            )
        )

    def gateway_connected(self):
        return True

    def is_for_us(self, message):
        return True

    async def send(self, text, color=None, channel=None, **kwargs):
        self.sent.append(text)
        # The HTTP send returned the message, so the reply fallback owns this command from now on
        return SimpleNamespace(id=1000 + len(self.sent), channel=SimpleNamespace(id=CHANNEL_ID))

    async def sleep(self, seconds):
        return None

    async def sleep_till(self, cooldown, cd_list=True, noise=3):
        return None

    async def wait_gateway_up(self):
        return None

    async def wait_until_ready(self):
        return None

    async def log(self, text, color="#ffffff", **kwargs):
        return None


def make_stack():
    ch = CommandHandler(FakeClient())
    ch.cmd_priorities = {"hunt": 2, "battle": 2, "sac": 9}
    bot = FakeBot(ch)
    consumer = commands_module.Commands(bot)
    fallback = ReplyFallback(bot, None)
    return ch, bot, consumer, fallback


def patch_consumer(monkeypatch):
    ready = threading.Event()
    ready.set()
    monkeypatch.setattr(commands_module, "registry", SimpleNamespace(all_ready_event=ready))

    async def no_gate(account_index):
        return None

    monkeypatch.setattr(commands_module, "await_send_gate", no_gate)


async def send_next(consumer):
    """One pass of the send loop: takes the next queued command and sends it"""
    await commands_module.Commands.send_commands.coro(consumer)


async def silence_for_60s(fallback, command):
    """The reply fallback's pass 61 s after the command was sent, with no reply in the history"""
    await fallback.tick(now=command["sent_at"] + 61)
    await asyncio.gather(*fallback.silence_tasks)


def owo_message(content):
    return SimpleNamespace(
        content=content,
        channel=SimpleNamespace(id=CHANNEL_ID),
        author=SimpleNamespace(id=OWO_ID),
    )


def test_hunt_given_up_after_60s_is_queued_again_and_sent(monkeypatch):
    patch_consumer(monkeypatch)

    async def run():
        ch, bot, consumer, fallback = make_stack()
        hunt = Hunt(bot)
        await hunt.cog_load()  # queues the first hunt and registers the give-up handler
        await asyncio.sleep(0)
        await send_next(consumer)  # the first hunt goes out
        await silence_for_60s(fallback, ch.checks[0])  # no reply for 60 s: given up
        queued_again = ch.queue.qsize()
        if queued_again:  # the next cycle sends the hunt again; with nothing queued the assert below fails
            await send_next(consumer)
        return bot.sent, queued_again, [entry["id"] for entry in ch.checks]

    sent, queued_again, checks = asyncio.run(run())
    assert queued_again == 1
    assert sent == ["owo hunt", "owo hunt"]
    assert checks == ["hunt"]  # the new hunt waits for its reply as usual


def test_battle_given_up_after_60s_is_queued_again_and_sent(monkeypatch):
    patch_consumer(monkeypatch)

    async def run():
        ch, bot, consumer, fallback = make_stack()
        battle = Battle(bot)
        await battle.cog_load()  # queues the first battle and registers the give-up handler
        await asyncio.sleep(0)
        await send_next(consumer)
        await silence_for_60s(fallback, ch.checks[0])
        queued_again = ch.queue.qsize()
        if queued_again:  # with nothing queued the assert below fails instead of waiting forever
            await send_next(consumer)
        return bot.sent, queued_again, [entry["id"] for entry in ch.checks]

    sent, queued_again, checks = asyncio.run(run())
    assert queued_again == 1
    assert sent == ["owo battle", "owo battle"]
    assert checks == ["battle"]


def test_hunt_that_got_its_reply_is_not_queued_again(monkeypatch):
    patch_consumer(monkeypatch)

    async def run():
        ch, bot, consumer, fallback = make_stack()
        hunt = Hunt(bot)
        await hunt.cog_load()
        await asyncio.sleep(0)
        await send_next(consumer)
        entry = ch.checks[0]
        await ch.remove_queue(id="hunt")  # the result arrived and the hunt cog cleared its check
        await silence_for_60s(fallback, entry)  # nothing left to give up on
        return bot.sent, ch.queue.qsize()

    sent, queued = asyncio.run(run())
    assert sent == ["owo hunt"]
    assert queued == 0


def test_hunt_given_up_while_hunting_is_off_is_queued_once_it_is_back_on(monkeypatch):
    patch_consumer(monkeypatch)

    async def run():
        ch, bot, consumer, fallback = make_stack()
        hunt = Hunt(bot)
        await hunt.cog_load()
        await asyncio.sleep(0)
        await send_next(consumer)
        bot.hunt_disabled = True  # no gems left, so hunting is off
        await silence_for_60s(fallback, ch.checks[0])
        queued_while_off = ch.queue.qsize()
        bot.hunt_disabled = False  # gems were used again
        # Any message from us queues the hunt again after a disabled period, see Hunt.on_message
        await hunt.on_message(owo_message("Gems were used"))
        queued_after = ch.queue.qsize()
        if queued_after:  # with nothing queued the assert below fails instead of waiting forever
            await send_next(consumer)
        return bot.sent, queued_while_off, queued_after

    sent, queued_while_off, queued_after = asyncio.run(run())
    assert queued_while_off == 0
    assert queued_after == 1
    assert sent == ["owo hunt", "owo hunt"]


def test_other_commands_given_up_are_not_queued_again(monkeypatch):
    patch_consumer(monkeypatch)

    async def run():
        ch, bot, consumer, fallback = make_stack()
        await ch.put_queue(
            {"cmd_name": "sac", "cmd_arguments": "c u", "prefix": True, "checks": True, "id": "sac"}
        )
        await send_next(consumer)
        await silence_for_60s(fallback, ch.checks[0])
        return bot.sent, ch.queue.qsize()

    sent, queued = asyncio.run(run())
    assert sent == ["owo sac c u"]
    assert queued == 0
