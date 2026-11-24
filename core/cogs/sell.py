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

import re
import time

from discord.ext import commands, tasks

from core.cogs._BASE import BaseCog

RARITY_MAP = {
    "c": "common",
    "u": "uncommon",
    "r": "rare",
    "e": "epic",
    "s": "special",  # special
    "m": "mythical",
    "g": "gem",
    "l": "legendary",
    "d": "distorted",
    "f": "fabled",
    "h": "hidden",
}

# Minimum time between two sacs OwO confirmed on one account. Only a confirmed sac starts this wait
SAC_MIN_INTERVAL_SECONDS = 900
# After a confirmed sac every command of the account waits this long, see CommandHandler.wait_sac_cooldown
SAC_SUCCESS_PAUSE_SECONDS = 30


def is_sac_success(content):
    """True for OwO's reply to a sac that went through, e.g.
    "**🔪 | name** sacrificed **<emoji>x3 <emoji>x2** for a total of **<emoji> 5**".
    Kept loose on purpose: a missed match means the 15-minute wait never starts"""
    return "sacrificed" in content and "for a total of" in content.lower()


def is_animal_refusal(content):
    """True for OwO's reply when a sell or sac has no animals to take, e.g.
    "**🚫 | name**, You don't have enough animals! >:c". OwO deletes it soon after, so it is acted on when it arrives"""
    return "you don't have enough animals" in content.lower()


class Sell(BaseCog):
    def __init__(self, bot):
        super().__init__(bot)
        self.sell_lastran = 0
        self.sac_lastran = 0
        # time.monotonic() when OwO last confirmed a sac, None until then. One Sell cog runs per account
        self.sac_last_success = None
        self.startup = True

    @property
    def animal_setting(self):
        return self.bot.settings_dict.animal

    @property
    def sell_settings(self):
        return self.animal_setting.sell

    @property
    def sac_settings(self):
        return self.animal_setting.sac

    """def allocate_points(self, command: str, rarities: str):
        if command not in ("sell", "sac"):
            raise ValueError("Invalid command name")
            
        rarities_list = rarities.split()
        for item in rarities_list:
            self.__dict__[f"{command}_point"] += POINT_CHART[item]

    def calculate_allocation(self, command: str):
        if command not in ("sell", "sac"):
            raise ValueError("Invalid command name")
        cmds = ["sell", "sac"]
        cmds.pop(command)"""

    def get_cmd_argument(self, cmd):
        arg = getattr(self, f"{cmd}_settings").rarity.get_rarities()
        if self.startup:
            self.startup = False
            return arg

        arg_list = arg.split()
        filtered_items = [
            item
            for item in arg_list
            if (rarity := RARITY_MAP[item]) and self.bot.animal_rank_in_zoo[rarity]
            # https://www.geeksforgeeks.org/python/walrus-operator-in-python-3-8/
        ]
        arg = " ".join(filtered_items).strip()
        if arg != "":
            return " ".join(filtered_items)
        return None

    def get_command(self, name: str):
        if name not in ("sell", "sac"):
            raise ValueError("Invalid command name")

        arg = self.get_cmd_argument(name)
        if not arg:
            return None

        base = {
            "cmd_name": name,
            "cmd_arguments": arg,
            "prefix": True,
            "checks": True,
            "id": name,
        }
        return base

    async def sac_pending(self):
        """True while a sac still waits for OwO's reply, i.e. its entry is in the command handler's check list"""
        return await self.bot.ch.search_checks("sac")

    async def sac_may_send(self):
        """Checked before a sac is queued. False within SAC_MIN_INTERVAL_SECONDS of the last sac OwO
        confirmed, and while a sac still waits for its reply, so one sac cannot follow another"""
        if (
            self.sac_last_success is not None
            and time.monotonic() - self.sac_last_success < SAC_MIN_INTERVAL_SECONDS
        ):
            return False
        return not await self.sac_pending()

    async def clear_refused_pending(self):
        """OwO refused a sell or a sac, so that command no longer waits for a reply. OwO answers in order,
        so the refusal belongs to the earliest unanswered one. A refusal starts no 15-minute wait and no pause"""
        waiting = [
            cmd_id for cmd_id in ("sell", "sac") if await self.bot.ch.search_checks(cmd_id)
        ]
        if waiting:
            earliest = min(
                waiting, key=lambda cmd_id: self.bot.ch.cmds_state[cmd_id]["last_ran"]
            )
            await self.bot.ch.remove_queue(id=earliest)

    @tasks.loop(seconds=2)
    async def initiate_loop(self):
        choices = ["sell", "sac"]
        self.bot.random.shuffle(choices)
        for cmd in choices:
            if not getattr(self, f"{cmd}_settings").enabled:
                # skip disabled ones
                continue
            if cmd == "sac" and not await self.sac_may_send():
                # Within 15 minutes of the last confirmed sac, or one still waits for its reply
                continue
            last_ran = self.__dict__[f"{cmd}_lastran"]
            cd = self.animal_setting.get_cd()
            gap = time.monotonic() - last_ran
            if last_ran == 0 or gap > cd:
                cmd_data = self.get_command(cmd)
                if cmd_data:
                    await self.bot.sleep_till([10, 15])
                    await self.bot.ch.put_queue(cmd_data)
                    self.__dict__[f"{cmd}_lastran"] = time.monotonic()

            # To prevent sell and sac being send right after
            await self.bot.sleep_till([1, 2])

    async def cog_load(self):
        # start loop, cog will stay awake due to the necessity to calculate value
        self.initiate_loop.start()

    async def cog_unload(self):
        # this shouldn't get removed since its supposed to run.
        await self.bot.ch.remove_queue(id="sell")
        await self.bot.ch.remove_queue(id="sac")

    @commands.Cog.listener()
    async def on_message(self, message):
        if not self.bot.is_for_us(message):
            return

        if (
            message.channel.id == self.bot.cm.id
            and message.author.id == self.bot.owo_bot_id
        ):
            if (
                "for a total of **<:cowoncy:416043450337853441>"
                in message.content.lower()
            ):
                await self.bot.ch.remove_queue(id="sell")

                if self.bot.settings_dict.cashCheck:
                    try:
                        self.bot.update_cash(
                            int(
                                re.search(
                                    r"for a total of \*\*<:cowoncy:\d+> ([\d,]+)",
                                    message.content,
                                )
                                .group(1)
                                .replace(",", "")
                            )
                        )
                    except Exception:
                        await self.bot.log(
                            "failed to fetch cowoncy from sales", "#af0087"
                        )

            elif is_sac_success(message.content):
                await self.bot.ch.remove_queue(id="sac")
                # The 15-minute wait starts at this reply, and every command of the account pauses for a while
                self.sac_last_success = time.monotonic()
                self.bot.ch.start_sac_cooldown(SAC_SUCCESS_PAUSE_SECONDS)

            elif is_animal_refusal(message.content):
                await self.clear_refused_pending()


async def setup(bot):
    await bot.add_cog(Sell(bot))
