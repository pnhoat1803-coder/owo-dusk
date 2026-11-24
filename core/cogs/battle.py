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
import traceback

from discord.ext import commands

from core.cogs._BASE import BaseCog
from utils.system.notification import notify


class Battle(BaseCog):
    def __init__(self, bot):
        super().__init__(bot)
        self.cmd = {
            "cmd_name": "",
            "prefix": True,
            "checks": True,
            "id": "battle",
            "slash_cmd_name": "battle",
            "removed": False,
            "channel": None,
            "cmd_arguments": None,
        }

    @property
    def settings(self):
        return self.bot.settings_dict.commands.battle

    async def cog_load(self):
        if (
            not self.settings.enabled
            or self.bot.settings_dict.cooldowns.reactionBot.huntAndBattle
        ):
            try:
                asyncio.create_task(self.bot.unload_cog("core.cogs.battle"))
            except Exception:
                pass
        else:
            self.cmd["cmd_name"] = (
                self.bot.alias["battle"]["shortform"]
                if self.settings.shortform
                else self.bot.alias["battle"]["normal"]
            )
            self.bot.ch.silence_handlers["battle"] = self.requeue_battle
            asyncio.create_task(self.bot.ch.put_queue(self.cmd))

    async def cog_unload(self):
        self.bot.ch.silence_handlers.pop("battle", None)
        await self.bot.ch.remove_queue(id="battle")

    async def log_battle_recovery_error(self, error):
        await self.bot.log(
            f"Error - {self.bot.username}: battle reply not read "
            f"({type(error).__name__}: {error})",
            "#c25560",
        )
        traceback.print_exception(error)

    def help_request_active(self) -> bool:
        """C2: a battle_friend help is on: its owner is known and the help flag is set"""
        help_requests = getattr(self.bot, "quest_help_request", None) or {}
        cnf = help_requests.get("battle", {})
        return bool(cnf.get("enabled")) and bool(cnf.get("userid"))

    async def send_help_battle(self) -> bool:
        """C2: "owo battle <@owner>" for the battle_friend help. It goes out through the quest cog's hourly cap"""
        cnf = self.bot.quest_help_request["battle"]
        quest_cog = self.bot.get_cog("Quest")
        if quest_cog is None:
            return False
        cmd = {
            "cmd_name": self.cmd["cmd_name"],
            "cmd_arguments": f"<@{cnf['userid']}>",
            "prefix": True,
            "checks": True,
            "id": "battle",
            "channel": cnf["channel"] or None,
        }
        return await quest_cog.send_capped(cmd)

    async def requeue_battle(self):
        await self.bot.sleep(self.settings.get_cd())
        self.cmd["cmd_name"] = (
            self.bot.alias["battle"]["shortform"]
            if self.settings.shortform
            else self.bot.alias["battle"]["normal"]
        )
        if self.help_request_active():
            await self.send_help_battle()
            return
        await self.bot.ch.put_queue(self.cmd)

    @commands.Cog.listener()
    async def on_message(self, message):
        if (
            message.channel.id == self.bot.cm.id
            and message.author.id == self.bot.owo_bot_id
            and message.embeds
        ):
            for embed in message.embeds:
                try:
                    author_name = embed.author.name
                    matched_battle = author_name is not None and self.bot.name_phrase_in(
                        author_name, "{name} goes into battle!", message
                    )
                    if not matched_battle:
                        continue

                    if message.reference is not None:
                        referenced_message = await message.channel.fetch_message(
                            message.reference.message_id
                        )
                        if (
                            referenced_message.embeds
                            or "You found a **weapon crate**!"
                            not in referenced_message.content
                        ):
                            # Return from battle embed reply
                            return

                    await self.bot.ch.remove_queue(id="battle")
                    try:
                        if embed.footer:
                            if self.settings.show_streak:
                                await self.bot.log(f"{embed.footer.text}", "#292252")
                            if "Streak ended at" in embed.footer.text:
                                if self.settings.notify_streak_loss:
                                    notify(embed.footer.text, "You lost your streak!")
                    except Exception as error:
                        await self.log_battle_recovery_error(error)
                    finally:
                        await self.requeue_battle()
                    return
                except Exception as error:
                    await self.log_battle_recovery_error(error)
                    await self.bot.ch.remove_queue(id="battle")
                    await self.requeue_battle()
                    return

        cnf = self.bot.quest_help_request["battle"]
        if not cnf["enabled"]:
            return

        # C2: the owner's full mention, "<@id>" or "<@!id>" (the check used to lack the closing ">")
        if message.content in (f"<@{cnf['userid']}>", f"<@!{cnf['userid']}>"):
            emb = message.embeds[0] if message.embeds else None
            if not emb:
                return
            if (
                emb.author.name is not None
                and self.bot.name_in(emb.author.name, message)
            ):
                if (
                    emb.footer.text.lower()
                    == "this challenge will expire in 10 minutes"
                ):
                    await self.bot.ch.remove_queue(id="battle")
                    self.bot.quest_help_request["battle"]["till"] -= 1
                    (
                        current,
                        completed,
                    ) = await self.bot.quest_handler.qh.update_progress(
                        self.bot.user.id, cnf["userid"], "battle_friend"
                    )
                    if current is not None:
                        await self.bot.quest_handler.sync_progress(
                            "battle_friend", current, completed
                        )

                    if completed or self.bot.quest_help_request["battle"]["till"] <= 0:
                        # reset
                        self.bot.quest_help_request["battle"] = {
                            "till": 0,
                            "enabled": False,
                            "userid": 0,
                            "channel": 0,
                        }
                    else:
                        # C2: the next help battle goes out after the cooldown, through the quest cap
                        await self.requeue_battle()


async def setup(bot):
    await bot.add_cog(Battle(bot))
