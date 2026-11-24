"""Starts the low-frequency level check of one account, see core/level_check.py.

The cog keeps its task in an attribute, so MyClient.end_connection (shutdown and autoStop) cancels it like every other
per-account task. It sends nothing itself: the check goes through the normal command queue. While a check waits for its
card the cog hands it every OwO message of the channel (new messages and edits) and nothing else: the host decides.
"""

import asyncio

from discord.ext import commands

from core import level_check
from core.cogs._BASE import BaseCog


class LevelCheck(BaseCog):
    def __init__(self, bot):
        super().__init__(bot)
        self.host = None
        self.checker = None
        self.level_task = None

    async def cog_load(self):
        settings = self.bot.global_settings_dict.levelCheck
        if not level_check.process_ready(settings):
            if settings.enabled:
                await level_check.mark_active(False)  # Tesseract is missing: one line was printed, nothing is sent
            asyncio.create_task(self.bot.unload_cog("core.cogs.levelcheck"))
            return
        await level_check.mark_active(True)
        self.host = level_check.BotHost(self.bot)
        level_check.register_host(self.host)  # the OCR worker logs its result in the log of this account
        level_check.ensure_worker(settings)  # one background thread for the whole process, started once
        self.checker = level_check.LevelChecker(self.host)
        self.level_task = asyncio.create_task(self._run())

    async def _run(self):
        try:
            await level_check.await_all_ready()
            await self.checker.run()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # one short line, never a traceback
            await self.bot.log(
                f"[levelCheck] {self.bot.username}: stopped ({type(error).__name__})", "#939393", webhook_useless_log=False,
                escape_markup=True,
            )

    async def cog_unload(self):
        if self.host is not None:
            level_check.unregister_host(self.host)
        if self.level_task is not None:
            self.level_task.cancel()

    @commands.Cog.listener()
    async def on_message(self, message):
        host = self.host
        if host is not None and host.waiting and message.author.id == self.bot.owo_bot_id:
            host.offer(message)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload):
        # The card may be added to a message by an edit. payload.data is the raw gateway JSON (a partial update is fine)
        host = self.host
        if host is not None and host.waiting and payload.channel_id == host.channel_id:
            host.offer_raw(payload.data, "edit")


async def setup(bot):
    await bot.add_cog(LevelCheck(bot))
