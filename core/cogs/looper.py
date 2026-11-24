# This file is part of owo-dusk.
#
# Copyright (c) 2024-present EchoQuill
#
# Portions of this file are based on code by EchoQuill, licensed under the
# GNU General Public License v3.0 (GPL-3.0).
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

import asyncio
import collections
import heapq
import itertools
import random
import string
import time
from datetime import datetime, timezone

from discord.ext import tasks
from discord.ext.commands import ExtensionNotLoaded

from core.auto_stop import stop_requested
from core.cogs._BASE import BaseCog
from core.send_gate import await_all_ready
from utils.quote_source import quotes as quote_source

"""
These commands: owo, pray, curse, level - can simply be send at specific intervals
"""

"""
TASK:

handle pray/curse enabled at same times

"""

LOOPABLE_COMMANDS = ["owo", "pray", "curse", "level"]

# Level grind text: with quotes on, the line comes from utils/quote_source.py (quoteSource "api": blv-anh-ngok-said.onrender.com, or "file":
# config/level_lines.txt). The random string path keeps its own memory: an account does not send any of its last
# RECENT_LEVEL_LINES lines again
RECENT_LEVEL_LINES = 20


def generate_random_string(min, max):
    """something like a list?"""
    characters = string.ascii_lowercase + " "
    length = random.randint(min, max)
    random_string = "".join(random.choice(characters) for _ in range(length))
    return random_string


def pray_cmd_argument(userid, ping):
    if userid:
        return f"<@{random.choice(userid)}>" if ping else random.choice(userid)
    else:
        return ""


class Looper(BaseCog):
    def __init__(self, bot):
        super().__init__(bot)
        self.startup = True
        self.counter = itertools.count()  # for tie-breaking
        self.schedule_heap = []  # heap we are going to use
        self.pray_counter = itertools.count(start=1)
        self.curse_counter = itertools.count(start=1)

        # For making code easier to read, last_ran will be connected
        # to `heap_settings` property
        self.last_ran = {"owo": 0, "pray": 0, "curse": 0, "level": 0}
        # The last lines this account sent with the level sender, see pick_level_text
        self.level_recent = collections.deque(maxlen=RECENT_LEVEL_LINES)
        # Commands sent that still wait for their reply in block_till_send, read by core/auto_stop.py
        self.echo_waits = 0

    @property
    def owo_settings(self):
        return self.bot.settings_dict.commands.owo

    @property
    def pray_settings(self):
        return self.bot.settings_dict.commands.pray

    @property
    def curse_settings(self):
        return self.bot.settings_dict.commands.curse

    @property
    def reaction_bot_settings(self):
        return self.bot.settings_dict.cooldowns.reactionBot

    @property
    def is_enabled_dict(self):
        pray_curse_reaction = self.reaction_bot_settings.prayAndCurse
        return {
            "owo": self.owo_settings.enabled and not self.reaction_bot_settings.owo,
            "level": self.level_settings.enabled,
            "pray": self.pray_settings.enabled and not pray_curse_reaction,
            "curse": self.curse_settings.enabled and not pray_curse_reaction,
        }

    def get_pray_curse_settings(self, cmd):
        # temporary, look if something like to_dict exists for objects
        if cmd == "pray":
            return self.pray_settings
        else:  # Please don't accidentally misspell lol
            return self.curse_settings

    @property
    def level_settings(self):
        return self.bot.settings_dict.commands.lvlGrind

    def heap_settings(self, cmd):
        now = time.monotonic()
        enabled = self.is_enabled_dict
        return {
            "owo": {
                "enabled": enabled["owo"],
                "next_run": now + self.owo_settings.get_cd(),
                "send": self.send_owo,
                "id": "owo",
            },
            "level": {
                "enabled": enabled["level"],
                "next_run": now + self.level_settings.get_cd(),
                "send": self.send_level,
                "id": "level",
            },
            # This is soo cool, wrapping function in another function (lambda)
            # also owo's cd being used here is intentional but temporary btw
            # i mean it just does what its supposed to do: send command first!
            "pray": {
                "enabled": enabled["pray"],
                "next_run": now
                + (
                    self.pray_settings.get_cd()
                    if not self.startup
                    else self.owo_settings.get_cd()
                ),
                "send": lambda: self.send_pray_curse("pray"),
                "id": "pray",
            },
            "curse": {
                "enabled": enabled["curse"]
                and not enabled["pray"],  # Temporary fix, needs to be removed!
                "next_run": now
                + (
                    self.curse_settings.get_cd()
                    if not self.startup
                    else self.owo_settings.get_cd()
                ),
                "send": lambda: self.send_pray_curse("curse"),
                "id": "curse",
            },
        }[cmd]

    def append_heap(self, cmd_name):
        settings = self.heap_settings(cmd_name)
        if settings["enabled"]:
            heapq.heappush(
                self.schedule_heap, (settings["next_run"], next(self.counter), settings)
            )

    def try_append_all(self):
        # sometimes I love how references work in Python
        # sometimes I hate them.... ;<
        ids = LOOPABLE_COMMANDS.copy()
        for _, _, settings in self.schedule_heap:
            if settings["id"] in ids:
                ids.remove(settings["id"])
        for cmd in ids:
            self.append_heap(cmd)

    def check(self, message, content, channel_id=None):
        return (
            message.author.id == self.bot.user.id
            and message.channel.id == (channel_id if channel_id else self.bot.cm.id)
            and message.content == content
        )

    async def sent_since(self, content, channel_id, since):
        """True if this account already posted `content` in the channel after `since`"""
        try:
            if channel_id:
                channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            else:
                channel = self.bot.cm
            after = datetime.fromtimestamp(since - 2, timezone.utc)
            async for message in channel.history(limit=10, after=after):
                if self.check(message, content, channel_id):
                    return True
        except Exception:
            # Only a safety net: when the history cannot be read, keep the old behaviour and send again
            return False
        return False

    async def block_till_send(
        self, content, channel_id=None, *, retry_cmd=None, quick=False, sent=None
    ):
        # Listen before anything can be sent, and keep listening through the retries,
        # so an echo that lands during a backoff is not missed
        started_at = time.time()
        echo = asyncio.ensure_future(
            self.bot.wait_for(
                "message",
                check=lambda m: self.check(m, content, channel_id),
            )
        )
        await asyncio.sleep(0)
        retries = 0
        self.echo_waits += 1
        try:
            while True:
                if sent is not None:
                    # The 30s echo window starts when the command is actually sent, so time
                    # spent in the queue or behind the first-send stagger does not count
                    try:
                        await asyncio.wait_for(sent.wait(), timeout=600.0)
                    except asyncio.TimeoutError:
                        await self.bot.log(
                            f"Warning - {content!r} still not sent after 600s, giving up on it",
                            "#e6e65a",
                        )
                        return
                    if getattr(sent, "sent_message_id", None) is not None and self.bot.reply_fallback.enabled:
                        # The HTTP send returned the message: it went out, so the echo is neither waited for nor resent
                        return

                done, _ = await asyncio.wait({echo}, timeout=30.0)
                if done:
                    return
                if stop_requested():
                    return  # autoStop has started: the command is not sent again

                retries += 1
                print(
                    f"Block till send got timeout error, likely network issue! (looper) - {self.bot.username} - {content}"
                )
                if retry_cmd is None or retries > 3:
                    await self.bot.log(
                        f"Warning - giving up on {content!r} after {retries} echo timeouts",
                        "#e6e65a",
                    )
                    return

                await asyncio.sleep(min(30.0 * 2**retries, 240.0))
                if echo.done():
                    return
                if self.bot.is_closed():
                    return
                while (
                    not self.bot.is_ready()
                    or self.bot.ws is None
                    or not self.bot.ws.open
                ):
                    if self.bot.is_closed():
                        return
                    await self.bot.wait_until_ready()
                    if self.bot.ws is None or not self.bot.ws.open:
                        await asyncio.sleep(0.5)

                cmd_state = self.bot.ch.cmds_state[retry_cmd["id"]]
                if cmd_state["in_queue"] or cmd_state["pending_send"]:
                    # An older copy is still on its way: keep waiting for its echo, add nothing
                    continue
                if await self.sent_since(content, channel_id, started_at):
                    # The copy did go out and only its echo was missed: sending it again would duplicate it
                    await self.bot.log(
                        f"Warn - {content!r} is already in the channel, echo missed, not sending it again",
                        "#e6e65a",
                    )
                    return
                sent = self.bot.ch.new_sent_event(retry_cmd)
                await self.bot.ch.put_queue(retry_cmd, quick=quick)
        finally:
            echo.cancel()
            self.echo_waits -= 1

    async def echo_text(self, cmd):
        """The text construct_command sends for `cmd`, which is what its echo carries"""
        guild_id = None
        if cmd.get("channel"):
            try:
                guild_id = (await self.bot.fetch_channel(cmd["channel"])).guild.id
            except Exception:
                # send_commands cannot fetch the channel either, so it skips the send and no echo comes
                pass
        commands_cog = self.bot.get_cog("Commands")
        if commands_cog is None:
            # Should not happen, the Commands cog is always loaded; plain prefixed text instead
            return f"{self.bot.settings_dict.prefix}{cmd['cmd_name']} {cmd.get('cmd_arguments') or ''}".strip()
        return commands_cog.construct_command(cmd, guild_id)

    async def send_owo(self):
        await await_all_ready()
        cmd = {
            "cmd_name": self.bot.alias["owo"]["normal"],
            "prefix": False,
            "checks": False,
            "id": "owo",
        }

        # We are using quick for OwO for faster leaderboard progression and accuracy
        sent = self.bot.ch.new_sent_event(cmd)
        await self.bot.ch.put_queue(cmd, quick=self.owo_settings.prioritise)
        await self.block_till_send(
            cmd["cmd_name"],
            retry_cmd=cmd,
            quick=self.owo_settings.prioritise,
            sent=sent,
        )

    async def send_pray_curse(self, cmd_name):
        await await_all_ready()
        cnf = self.get_pray_curse_settings(cmd_name)
        # C3: a quest help for pray or curse has its own path. The command is the one the help asks for ("pray" for
        # a pray help, "curse" for a curse help), not the name of the running loop. An error there is logged, the flag
        # is reset, and the normal pray/curse goes on at the next cycle
        help_id = next(
            (qid for qid in ("pray", "curse") if self.bot.quest_help_request[qid]["enabled"]),
            None,
        )
        if help_id is not None:
            keep_help = False
            try:
                help_cnf = self.bot.quest_help_request[help_id]
                help_cmd = {
                    "cmd_name": help_id,
                    "cmd_arguments": f"<@{help_cnf['userid']}>",
                    "prefix": True,
                    "checks": False,
                    "id": "pray",  # pray will be utilised as id for curse as well
                    "channel": help_cnf["channel"] or None,
                }
                full_content = await self.echo_text(help_cmd)
                quest_cog = self.bot.get_cog("Quest")
                sent = self.bot.ch.new_sent_event(help_cmd)
                # the same hourly cap as the other quest commands (C2, C3)
                if quest_cog is None or not await quest_cog.send_capped(help_cmd):
                    self.bot.ch.release_sent(help_cmd)
                    return
                self.startup = False
                await self.block_till_send(
                    full_content,
                    channel_id=help_cmd.get("channel"),
                    retry_cmd=help_cmd,
                    sent=sent,
                )
                if getattr(sent, "sent_message_id", None) is not None:
                    # C7: counted only when the message really went out. A send that never came back is not progress
                    help_cnf["till"] -= 1
                    current, completed = await self.bot.quest_handler.qh.update_progress(
                        self.bot.user.id, help_cnf["userid"], help_id
                    )
                    if current is not None:
                        await self.bot.quest_handler.sync_progress(help_id, current, completed)
                    keep_help = not (completed or help_cnf["till"] <= 0)
                else:
                    keep_help = True
            except Exception as error:
                import traceback

                await self.bot.log(
                    f"Error - quest help {help_id} failed ({type(error).__name__}), the normal {cmd_name} goes on",
                    "#c25560",
                )
                traceback.print_exc()
            finally:
                if not keep_help:
                    self.bot.quest_help_request[help_id] = {
                        "till": 0,
                        "enabled": False,
                        "userid": 0,
                        "channel": 0,
                    }
            return

        cmd = {
            "cmd_name": cmd_name,
            "cmd_arguments": str(pray_cmd_argument(cnf.user_id, cnf.ping_user)),
            "prefix": True,
            "checks": False,
            "id": "pray",  # pray will be utilised as id for curse as well
            "channel": None
            if not cnf.custom_channel.enabled
            else cnf.custom_channel.channel,
        }
        if cmd["cmd_arguments"] and getattr(self, f"{cmd_name}_settings").count:
            cmd["cmd_arguments"] += f" {next(self.__dict__[f'{cmd_name}_counter'])}"

        # Built before queueing, by the same code that sends it, so the echo is matched exactly
        full_content = await self.echo_text(cmd)

        sent = self.bot.ch.new_sent_event(cmd)
        await self.bot.ch.put_queue(cmd)
        self.startup = False

        await self.block_till_send(
            full_content, channel_id=cmd.get("channel"), retry_cmd=cmd, sent=sent
        )

    async def pick_level_text(self):
        if (
            self.level_settings.useQuote
            and self.bot.danger_settings_dict["allow_quotes"]
        ):
            # quoteSource "api": blv-anh-ngok-said.onrender.com, from the pool in memory; "file": config/level_lines.txt. Neither one waits
            # for the network, see utils/quote_source.py. None when both are empty: the random text below is used
            line = quote_source.get_quote(
                f"account-{self.bot.account_index}",
                source=getattr(self.level_settings, "quoteSource", "api"),
            )
            if line:
                return line
        # Random text when quotes are off, or when the pool and the local lines have nothing to offer
        msg = ""
        for _ in range(50):
            msg = generate_random_string(
                self.level_settings.minLength, self.level_settings.maxLength
            )
            # construct_command strips what it sends, so the echo we wait for must be stripped too
            msg = msg.strip()
            if msg and msg not in self.level_recent:
                break
        return msg

    async def send_level(self):
        await await_all_ready()
        msg = await self.pick_level_text()
        if not msg:
            return
        self.level_recent.append(msg)
        cmd = {
            "cmd_name": msg,
            "prefix": False,
            "checks": False,
            "id": "level",
        }

        sent = self.bot.ch.new_sent_event(cmd)
        await self.bot.ch.put_queue(cmd)
        await self.block_till_send(cmd["cmd_name"], retry_cmd=cmd, sent=sent)

    @tasks.loop()
    async def initiate_loop(self):
        if stop_requested():
            # autoStop has started: nothing is scheduled any more, so the loop ends here
            self.initiate_loop.stop()
            return
        self.try_append_all()

        if not self.schedule_heap:
            # weird why the code would'nt stop
            await asyncio.sleep(1)
            return

        next_run, _, settings = self.schedule_heap[0]
        sleep_for = next_run - time.monotonic()
        if sleep_for > 0:
            # Guard against incase a command already finished its wait time
            # Iam curious how .sleep handles negatives 0o
            await asyncio.sleep(sleep_for)
        heapq.heappop(self.schedule_heap)
        await settings["send"]()
        self.append_heap(settings["id"])

    """gets executed when the cog is first loaded"""

    async def cog_load(self):
        should_run = False
        for item in LOOPABLE_COMMANDS:
            if self.is_enabled_dict[item]:
                should_run = True

        if not should_run:
            try:
                asyncio.create_task(self.bot.unload_cog("core.cogs.looper"))
            except ExtensionNotLoaded:
                pass
        else:
            if (
                self.level_settings.useQuote
                and not self.bot.danger_settings_dict["allow_quotes"]
            ):
                await self.bot.log(
                    "For level grind, quotes are disabled unless manually enabled through `owo-dusk/config/danger.toml` file due to risks. Please give the file a read and enable it after considering those risks",
                    "#e6e65a",
                )
            if self.pray_settings.enabled and self.curse_settings.enabled:
                await self.bot.log(
                    "Both pray and curse are enabled. In this case, curse will be disabled. On a later date, I will make it possible to use both pray and curse at the same time, this is a temp fix till then",
                    "#e6e65a",
                )

            self.initiate_loop.start()

    async def cog_unload(self):
        # if not self.initiate_loop.done():
        self.initiate_loop.cancel()


async def setup(bot):
    await bot.add_cog(Looper(bot))
