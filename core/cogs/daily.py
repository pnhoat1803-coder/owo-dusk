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
import re
import time
from datetime import datetime, timedelta

from discord.ext import commands
from discord.ext.commands import ExtensionNotLoaded

from core.auto_stop import active_timer
from core.cogs._BASE import BaseCog

cmd = {"cmd_name": "daily", "prefix": True, "checks": True, "id": "daily"}
DAILY_WAIT_SECONDS = 24 * 60 * 60
_DURATION_PART = re.compile(
    r"(\d+(?:\.\d+)?)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)(?![a-z])",
    re.IGNORECASE,
)


def parse_daily_wait_seconds(content):
    """Parse the remaining daily cooldown from compact or written durations."""
    parts = list(_DURATION_PART.finditer(content))
    if parts:
        total = 0.0
        for part in parts:
            amount = float(part.group(1))
            unit = part.group(2).lower()
            if unit.startswith("d"):
                total += amount * 86400
            elif unit.startswith("h"):
                total += amount * 3600
            elif unit.startswith("m"):
                total += amount * 60
            else:
                total += amount
        return total if total > 0 else None

    clock = re.search(r"(?<!\d)(\d{1,3}):(\d{1,2})(?::(\d{1,2}))?(?!\d)", content)
    if clock:
        hours, minutes, seconds = clock.groups()
        if seconds is None:
            return int(hours) * 3600 + int(minutes) * 60
        return int(hours) * 3600 + int(minutes) * 60 + int(seconds)
    return None


class Daily(BaseCog):
    def __init__(self, bot):
        super().__init__(bot)
        self.daily_task = None

    @property
    def cooldowns(self):
        return self.bot.settings_dict.cooldowns

    async def _queue_daily(self):
        owner = "daily"
        await self.bot.ch.pause_with_timeout(owner)
        try:
            # Not recorded as claimed here: only OwO's success reply counts, see on_message
            await self.bot.ch.put_queue(cmd, priority=True)
        except BaseException:
            await self.bot.ch.resume_stat(owner)
            raise

    async def start_daily(self):
        last_daily_time = await self.bot.db.fetch_cmd_lastran_time("daily")

        if not self.bot.should_run(last_daily_time):
            # Already claimed today: the next claim is at the next PST midnight
            reset_in = self.bot.calc_time()
            await self._warn_if_autostop_before_reset(reset_in)
            await asyncio.sleep(reset_in)

        await self.bot.sleep_till(self.cooldowns.briefCooldown)
        await self._queue_daily()

    async def _warn_if_autostop_before_reset(self, reset_in):
        # autoStop ends before the next daily reset, so this session cannot claim the daily again
        timer = active_timer()
        if timer is None or timer.deadline - time.monotonic() >= reset_in:
            return
        reset_at = datetime.now() + timedelta(seconds=reset_in)
        await self.bot.log(
            "Warn: Daily will not run this session — autoStop "
            f"{timer.stop_at:%Y-%m-%d %H:%M} is before next daily reset {reset_at:%Y-%m-%d %H:%M}.",
            "#924444",
        )

    async def _queue_after_delay(self, delay):
        try:
            await asyncio.sleep(max(0, delay))
            if self.bot.stopped:
                return
            await self.bot.sleep_till(self.cooldowns.briefCooldown)
            await self._queue_daily()
        except asyncio.CancelledError:
            await self.bot.ch.resume_stat("daily")
            raise
        except Exception:
            await self.bot.ch.resume_stat("daily")
            raise

    async def _schedule_next_daily(self, delay, reason):
        name = getattr(self.bot, "username", None) or f"account #{getattr(self.bot, 'account_index', 'unknown')}"
        await self.bot.log(
            f"Daily rescheduled - {name} - next in {delay:.0f}s ({reason})",
            "#87af87",
        )
        if self.daily_task is not None and not self.daily_task.done():
            self.daily_task.cancel()
        self.daily_task = asyncio.create_task(self._queue_after_delay(delay))

    async def cog_load(self):
        if not self.bot.settings_dict.daily:
            try:
                asyncio.create_task(self.bot.unload_cog("core.cogs.daily"))
            except ExtensionNotLoaded:
                pass
        else:
            self.daily_task = asyncio.create_task(self.start_daily())

    async def cog_unload(self):
        await self.bot.ch.remove_queue(id="daily")
        if self.daily_task is not None and not self.daily_task.done():
            self.daily_task.cancel()
        await self.bot.ch.resume_stat("daily")

    @commands.Cog.listener()
    async def on_message(self, message):
        if (
            message.channel.id != self.bot.cm.id
            or message.author.id != self.bot.owo_bot_id
            or not self.bot.is_for_us(message)
        ):
            return

        if re.search(r"Here is your daily \*\*<:cowoncy:\d+>", message.content):
            try:
                # OwO confirmed the claim: record it now, not when it was queued
                self.bot.db.update_cmd_lastran_time("daily")
                await self.bot.ch.remove_queue(cmd)
                cash_match = re.search(
                    r"Here is your daily \*\*<:cowoncy:\d+> ([\d,]+)",
                    message.content,
                )
                if cash_match:
                    self.bot.update_cash(int(cash_match.group(1).replace(",", "")))
                await self._schedule_next_daily(self.bot.calc_time(), "success")
                if self.bot.global_settings_dict.webhook.enabled:
                    await self.bot.send_webhook("daily_claim")
            finally:
                await self.bot.ch.resume_stat("daily")
            return

        if "Nu!" in message.content and "You need to wait" in message.content:
            try:
                await self.bot.ch.remove_queue(cmd)
                delay = parse_daily_wait_seconds(message.content)
                if delay is None:
                    delay = DAILY_WAIT_SECONDS
                await self._schedule_next_daily(delay, "wait reply")
            finally:
                await self.bot.ch.resume_stat("daily")


async def setup(bot):
    await bot.add_cog(Daily(bot))
