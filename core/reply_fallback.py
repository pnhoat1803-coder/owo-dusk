"""Finds OwO replies the gateway did not deliver, by reading recent channel history.

The reply handlers are discord.py listeners, so a message read from history goes to them
through the same client dispatch as a live one (MyClient.hand_history_message). A message
reaches the cogs once: MyClient.dispatch remembers the ids it has handed on.
"""

import asyncio
import threading
import time
from datetime import timezone

import discord

from core.global_rate_limiter import rate_limiter
from core.reply_trace import reply_trace

# The DM channel is read for its newest messages only: a captcha warning is among the last few
DM_HISTORY_LIMIT = 5


def utc_timestamp(moment):
    """Seconds since the epoch. A naive datetime is taken as UTC, like the aware ones discord.py gives"""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def is_dm(message):
    return getattr(message.channel, "type", None) == discord.ChannelType.private


class FetchLimiter:
    """Process-wide cap on history reads. Every account has its own thread and loop, so an
    asyncio.Semaphore would only limit one account; this one is shared by all of them."""

    def __init__(self):
        self._lock = threading.Lock()
        self._active = 0
        self.peak = 0

    def try_acquire(self, limit):
        with self._lock:
            if self._active >= limit:
                return False
            self._active += 1
            self.peak = max(self.peak, self._active)
            return True

    def release(self):
        with self._lock:
            self._active -= 1

    async def acquire(self, limit):
        # Polls without blocking the loop, so the other accounts keep running
        while not self.try_acquire(limit):
            await asyncio.sleep(0.2)


FETCH_LIMITER = FetchLimiter()


class ReplyFallback:
    """Per account. After a send that expects a reply, reads the farm channel to find the reply
    the gateway missed, and gives the command up after giveUpAfter seconds. While the gateway is
    down it also reads the OwO DM channel, where OwO sends captcha warnings."""

    def __init__(self, bot, limiter):
        self.bot = bot
        self.limiter = limiter
        self.task = None
        self.last_safety_check = 0.0
        self.last_dm_check = 0.0
        # Messages from before this account started are never handed on, see read_messages
        self.started_at = time.time()
        # The newest OwO DM id seen, live or from history. None until one is seen, see check_dm
        self.dm_newest_id = None
        # The first DM check of this account only records the newest id: nothing already there is handed on
        self.dm_checked_once = False
        # Re-queues started by give_up, kept until they finish so the tasks are not dropped
        self.silence_tasks = set()

    @property
    def settings(self):
        return self.bot.global_settings_dict.replyFallback

    @property
    def enabled(self):
        settings = getattr(getattr(self.bot, "global_settings_dict", None), "replyFallback", None)
        return bool(getattr(settings, "enabled", False))

    def start(self):
        if self.enabled and self.task is None:
            self.task = asyncio.create_task(self.run())

    def note_dm_message(self, message):
        """Moves the newest OwO DM id on. Called for every message dispatched, live or from history"""
        if getattr(message.author, "id", None) != self.bot.owo_bot_id or not is_dm(message):
            return
        self.dm_newest_id = max(self.dm_newest_id or 0, message.id)

    async def run(self):
        while not self.bot.is_closed() and not self.bot.stopped:
            try:
                await self.tick()
            except Exception as error:
                await self.bot.log(
                    f"Warning - reply fallback failed: {type(error).__name__}",
                    "#e6e65a",
                    webhook_useless_log=False,
                )
            await asyncio.sleep(1.0)

    async def tick(self, now=None):
        """One pass: read history for entries waiting for a reply, give up old ones, and while the
        gateway is down read the farm channel and the DM channel for captcha warnings the gateway
        would have carried"""
        now = time.time() if now is None else now
        settings = self.settings
        for command in list(self.bot.ch.checks):
            if not self.bot.ch.is_pending(command):
                # Its reply was handled while an earlier read in this pass ran: no history read for it
                continue
            sent_at = command.get("sent_at")
            if sent_at is None or command.get("sent_message_id") is None:
                continue
            elapsed = now - sent_at
            if elapsed >= settings.giveUpAfter:
                await self.give_up(command)
            elif elapsed >= settings.firstCheckAfter:
                last = command.get("history_checked_at")
                if last is None or now - last >= settings.checkEvery:
                    command["history_checked_at"] = now
                    await self.read_after(command)
        if not self.bot.gateway_connected():
            if now - self.last_safety_check >= settings.checkEvery:
                self.last_safety_check = now
                await self.read_channel(self.bot.cm, since=self.bot.gateway_down_at)
            if settings.dmCaptchaCheckEvery and now - self.last_dm_check >= settings.dmCaptchaCheckEvery:
                self.last_dm_check = now
                await self.check_dm()

    async def read_after(self, command):
        channel = self.bot.get_channel(command.get("sent_channel_id")) or self.bot.cm
        await self.read_channel(channel, after_id=command["sent_message_id"])

    async def read_channel(self, channel, after_id=None, since=None):
        """Reads the newest messages (or those after `after_id`) and hands the OwO ones to the cogs"""
        if channel is None:
            return 0
        found, handled, _ = await self.read_messages(channel, after_id=after_id, since=since)
        reply_trace.hist_fetch(self.bot, found, handled)
        return handled

    async def read_messages(self, channel, after_id=None, since=None, limit=None, keep=None):
        """The history read behind read_channel. Returns the OwO messages it passed its filters, how many of
        them reached the cogs, and the newest OwO message id it held. A message is handed on only if it is
        newer than `after_id` (our command), not older than this account's start, not older than `since`,
        and `keep(message)` is true"""
        await self.limiter.acquire(self.settings.maxConcurrentFetches)
        try:
            await rate_limiter.acquire()
            kwargs = {"limit": limit or self.settings.historyLimit}
            if after_id is not None:
                kwargs["after"] = discord.Object(id=after_id)
            messages = [message async for message in channel.history(**kwargs)]
        finally:
            self.limiter.release()

        found = handled = newest = 0
        for message in sorted(messages, key=lambda item: item.id):  # oldest first, like the gateway
            if message.author.id != self.bot.owo_bot_id:
                continue
            newest = max(newest, message.id)
            if after_id is not None and message.id <= after_id:
                continue
            stamp = utc_timestamp(message.created_at)
            if stamp < self.started_at or (since is not None and stamp < since):
                continue
            if keep is not None and not keep(message):
                continue
            found += 1
            if self.bot.hand_history_message(message):
                handled += 1
        return found, handled, newest

    async def check_dm(self):
        """While the gateway is down, reads the newest messages of the OwO DM channel. An OwO message there
        is handed on like a live one, so the captcha path acts on it as it does today, but only if it is
        newer than every OwO DM seen before this check and was created while the gateway is down. The
        first check of an account only records the newest id, so an old verify message is never replayed"""
        if self.bot.gateway_connected():
            return
        if self.bot.dm is None:
            # The same channel the captcha cog opens from an OwO message
            owo = self.bot.get_user(self.bot.owo_bot_id)
            if owo is None:
                owo = await self.bot.fetch_user(self.bot.owo_bot_id)
            self.bot.dm = await owo.create_dm()
        if self.bot.gateway_connected():
            # The gateway came back while the DM channel was opened: the outage is over, nothing is read
            return
        first_check = not self.dm_checked_once
        self.dm_checked_once = True
        baseline = self.dm_newest_id
        since = self.bot.gateway_down_at
        captcha_before = self.bot.ch.command_handler_status.captcha
        found, passed, newest = await self.read_messages(
            self.bot.dm,
            since=since,
            limit=DM_HISTORY_LIMIT,
            keep=lambda message: not first_check and (baseline is None or message.id > baseline),
        )
        if newest:
            self.dm_newest_id = max(self.dm_newest_id or 0, newest)
        # The listeners of the handed-on messages run as tasks: one turn lets the captcha check set its flag
        await asyncio.sleep(0)
        captcha = self.bot.ch.command_handler_status.captcha and not captcha_before
        reply_trace.dm_check(self.bot, found, captcha, passed=passed)

    async def give_up(self, command):
        if not await self.bot.ch.remove_check(command):
            return
        cmd_name = command.get("cmd_name") or command.get("id", "?")
        await self.bot.log(
            f"no OwO reply for '{cmd_name}' after {self.settings.giveUpAfter:g}s, skipping",
            "#e6e65a",
            webhook_useless_log=False,
        )
        reply_trace.giveup(self.bot, cmd_name)
        # Hunt and battle start their next round only when a reply comes, so giving up would stop them for good.
        # Their handler queues them again. It runs as its own task: it may wait for a pause or captcha to end
        handler = self.bot.ch.silence_handlers.get(command.get("id"))
        if handler is not None:
            task = asyncio.ensure_future(self._run_silence_handler(handler, cmd_name))
            self.silence_tasks.add(task)
            task.add_done_callback(self.silence_tasks.discard)

    async def _run_silence_handler(self, handler, cmd_name):
        try:
            await handler()
        except Exception as error:
            await self.bot.log(
                f"Warning - {cmd_name} not queued again after no reply: {type(error).__name__}",
                "#e6e65a",
                webhook_useless_log=False,
            )
