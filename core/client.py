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
import html
import json
import logging
import os
import random
import re
import time
import threading
import traceback
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

# Third-Party Libraries
import aiohttp
import discord
from rich.markup import escape
import pytz
from discord.backoff import ExponentialBackoff
from discord.ext import commands, tasks
from discord.gateway import DiscordWebSocket, ReconnectWebSocket

# Local
from core.account_registry import registry
from core.auto_stop import refuse_command, stop_requested
from core.reconnect_governor import reconnect_governor
from core.send_gate import await_all_ready, await_send_permission
from core.stall_watchdog import recover_stalled_account, should_check_stall
from core.global_rate_limiter import rate_limiter
from core.reply_fallback import FETCH_LIMITER, ReplyFallback
from core.reply_trace import reply_trace
from core.loop_watchdog import loop_watchdog, run_heartbeat
from core.gateway_stats import AccountStats, record_ready
from core.gateway_options import gateway_options
import core.command_handler as command_handler
import core.target_filter as target_filter
import core.components as components
import core.database as database
import utils.config_models as config_models
import utils.run_log as run_log
import utils.system as syst
from utils.constants import owo_dusk_api, version
from utils.errors import suppress_and_log, suppress_and_log_block
from utils.loader import (
    captcha_settings_dict,
    danger_settings_dict,
    global_settings_dict,
    webhook_data_dict,
)
from utils.misspell import misspell_word
from utils.name_match import NameMatchMixin
from utils.quest_helper.quest import LocalQuestHandler
from website import website_append


TRANSIENT_CONNECTION_ERRORS = (
    discord.HTTPException,
    aiohttp.ClientError,
    ConnectionError,
    OSError,
    discord.ConnectionClosed,
    discord.GatewayNotFound,
)
_RECONNECT_ERRORS = TRANSIENT_CONNECTION_ERRORS + (asyncio.TimeoutError,)
# Same codes as discord.gateway.DiscordWebSocket._can_handle_close: no RESUME after these
NON_RESUMABLE_CLOSE_CODES = (1000, 4004, 4010, 4011, 4012, 4013, 4014)
_TERMINAL_CONNECT_ERRORS = tuple(
    error_type
    for error_type in (
        discord.LoginFailure,
        getattr(discord, "PrivilegedIntentsRequired", None),
    )
    if isinstance(error_type, type)
)


def _socket_close_code(ws):
    """The close code of a gateway socket, or None when there is no socket"""
    if ws is None:
        return None
    code = getattr(ws, "_close_code", None)
    if code is None:
        code = getattr(getattr(ws, "socket", None), "close_code", None)
    return code


def account_loop_exception_handler(loop, context):
    error = context.get("exception")
    handle = context.get("handle")
    callback = getattr(handle, "_callback", None)
    callback_name = getattr(callback, "__qualname__", "")
    message = str(context.get("message", ""))
    callback_source = f"{callback_name} {message}"
    if (
        isinstance(error, ConnectionResetError)
        and getattr(error, "winerror", None) == 10054
        and "_ProactorBasePipeTransport._call_connection_lost"
        in callback_source
    ):
        return
    loop.default_exception_handler(context)

_presence_loop_seconds = 90
_safety_check_loop_seconds = 30


def configure_task_loop_intervals(
    presence_seconds: int, safety_check_seconds: int
) -> None:
    global _presence_loop_seconds, _safety_check_loop_seconds
    _presence_loop_seconds = max(90, int(presence_seconds))
    _safety_check_loop_seconds = max(30, int(safety_check_seconds))

list_user_ids = []
webhook_handler = None
captcha_orchestrator = None
# Deprecated alias, kept for code that still reads the old name
hcaptcha_solver = None
global_quest_handler = None
lock = threading.Lock()
_extension_load_lock = threading.RLock()
# A waiting account polls the lock, starting at the minimum interval and doubling up to the maximum, see _extension_load_turn
EXTENSION_LOCK_POLL_MIN_SECONDS = 0.005
EXTENSION_LOCK_POLL_MAX_SECONDS = 0.05
# Only these frames are used by on_socket_raw_receive, so the others are not parsed there
MESSAGE_EVENTS = ("MESSAGE_CREATE", "MESSAGE_UPDATE")
_FRAME_START = re.compile(r'\{"t":(?:"([A-Z_]+)"|null)')


@asynccontextmanager
async def _extension_load_turn():
    """Takes _extension_load_lock as `with` does, but without blocking the thread of this account: a waiting
    account polls the lock, so its loop keeps running. The thread that holds the lock may take it again at once"""
    delay = EXTENSION_LOCK_POLL_MIN_SECONDS
    while not _extension_load_lock.acquire(blocking=False):
        await asyncio.sleep(delay)
        delay = min(delay * 2, EXTENSION_LOCK_POLL_MAX_SECONDS)
    try:
        yield
    finally:
        _extension_load_lock.release()


_stop_webhook_warned = False


class MessageDispatcher:
    """
    This is used like a middle man between on_socket_raw_receive and
    receiver functions
    """

    def __init__(self):
        self._message_handlers = []
        self._edit_handlers = []

    def register(self, func, edit=False):
        if not edit:
            self._message_handlers.append(func)
        else:
            self._edit_handlers.append(func)

    def unregister(self, func, edit=False):
        handlers = self._edit_handlers if edit else self._message_handlers
        if func in handlers:
            handlers.remove(func)

    async def dispatch_on_message(self, message):
        for func in self._message_handlers:
            await func(message)

    async def dispatch_on_edit(self, message):
        for func in self._edit_handlers:
            await func(message)


class Stats:
    """
    This tracks certain required statistics like cowoncy
    """

    def __init__(self):
        # Cowoncy Tracker
        self.balance = 0  # current cowoncy
        self.net_earnings = 0  # loss/gain of cowoncy
        self.has_updated_balance = False
        # Gambling Tracker
        self.gain_or_lose = 0
        # Gems
        self.no_gem = False


class MyClient(NameMatchMixin, commands.Bot):
    async def connect(self, *, reconnect: bool = True):
        """Reconnect through the process-wide governor, but not the first RESUME after a working session."""
        # The pinned library closes its HTTP client when connect(reconnect=False)
        # sees a transient failure, so this subclass owns the connection loop.
        # The reconnect argument is retained for API compatibility; retries here
        # are paced by the governor, except the first RESUME after a READY or RESUMED.
        backoff = ExponentialBackoff()
        ws_params = {"initial": True}
        reply_trace.bind_thread(self)

        while not self.is_closed():
            # The first RESUME after a READY or RESUMED skips the governor, see resume_unpaced.
            # IDENTIFY, and a RESUME that retries after a failed attempt, wait for a slot as before.
            resuming = bool(ws_params.get("resume"))
            if resuming and self.resume_unpaced:
                self.resume_unpaced = False
                self.gov_wait_ms = 0
            else:
                wait_started = time.monotonic()
                await reconnect_governor.acquire()
                self.gov_wait_ms = int((time.monotonic() - wait_started) * 1000)
            if self.is_closed():
                return
            reply_trace.attempt(
                self, "resume_try" if resuming else "identify_try", self.gov_wait_ms
            )
            if not resuming:
                self.identify_started_at = time.monotonic()

            try:
                coro = DiscordWebSocket.from_client(self, **ws_params)
                self.ws = await asyncio.wait_for(coro, timeout=60.0)
                ws_params["initial"] = False
                while True:
                    await self.ws.poll_event()
            except asyncio.CancelledError:
                raise
            except _TERMINAL_CONNECT_ERRORS:
                raise
            except ReconnectWebSocket as error:
                self.dispatch("disconnect")
                if self.is_closed():
                    return
                reconnect_governor.record_disconnect()
                reply_trace.close_event(
                    self,
                    code=_socket_close_code(self.ws),
                    reason=(
                        "reconnect requested" if error.resume else "invalid session (op 9)"
                    ),
                    invalid_session=not error.resume,
                    resumable=error.resume,
                    gov_wait_ms=self.gov_wait_ms,
                )
                if self.ws is not None:
                    ws_params.update(
                        sequence=self.ws.sequence,
                        resume=error.resume,
                        session=self.ws.session_id,
                    )
                    if error.resume:
                        ws_params["gateway"] = self.ws.gateway
            except _RECONNECT_ERRORS as error:
                self.dispatch("disconnect")
                if self.is_closed():
                    return
                reconnect_governor.record_disconnect()
                code = getattr(error, "code", None)
                reply_trace.close_event(
                    self,
                    code=code,
                    reason=type(error).__name__,
                    invalid_session=False,
                    resumable=code not in NON_RESUMABLE_CLOSE_CODES,
                    gov_wait_ms=self.gov_wait_ms,
                )

                if (
                    isinstance(error, OSError)
                    and error.errno in (54, 10054)
                    and self.ws is not None
                ):
                    ws_params.update(
                        sequence=self.ws.sequence,
                        gateway=self.ws.gateway,
                        initial=False,
                        resume=True,
                        session=self.ws.session_id,
                    )
                    continue

                await asyncio.sleep(backoff.delay())
                if self.ws is not None:
                    ws_params.update(
                        sequence=self.ws.sequence,
                        gateway=self.ws.gateway,
                        resume=True,
                        session=self.ws.session_id,
                    )

    def __init__(
        self,
        token,
        channel_id,
        global_settings_dict,
        token_len,
        *args,
        account_index: int = 0,
        **kwargs,
    ):
        # What the connection asks Discord for, see core/gateway_options.py. Empty when gateway.lean is off
        gateway = getattr(global_settings_dict, "gateway", None)
        super().__init__(
            command_prefix="-",
            self_bot=True,
            enable_debug_events=True,
            *args,
            **{**gateway_options(gateway), **kwargs},
        )
        # Read by core/target_filter.py: with lean on, only the spam channel and the OwO DM reach the cogs
        self.gateway_lean = bool(getattr(gateway, "lean", False))
        self.presence.change_interval(seconds=_presence_loop_seconds)
        self.safety_check_loop.change_interval(seconds=_safety_check_loop_seconds)
        self.token = token
        self.boss_channel_id = 0
        self.token_len = token_len
        self.account_index = account_index
        self.stopped = False
        self.last_sent_at = time.monotonic()
        self.last_stall_at = 0.0
        # Gateway state for the send gate, see _trace_disconnect: time of the drop, None while connected
        self.gateway_down_at = None
        self.gateway_back_at = 0.0
        self.gateway_hold_logged = False
        self.gov_wait_ms = None
        # True after READY or RESUMED: the next RESUME may skip the governor once, see connect()
        self.resume_unpaced = False
        # Frame counts of this account and the time its last IDENTIFY started, see core/gateway_stats.py
        self.gateway_stats = AccountStats(self)
        self.identify_started_at = None
        self.reply_fallback = ReplyFallback(self, FETCH_LIMITER)
        # Ids of the messages already handed to the cogs, see dispatch()
        self._recent_message_ids = OrderedDict()
        self.channel_id = int(channel_id)
        self.list_channel = [self.channel_id]
        self.session = None
        self.message_dispatcher = MessageDispatcher()
        self.settings_dict = None
        self.global_settings_dict = global_settings_dict
        self.captcha_settings_dict = captcha_settings_dict
        self.commands_dict = {}
        self.dm, self.cm = None, None
        self.hunt_disabled = False
        self.username = None
        self.nick_name = None
        self.reaction_bot_id = 519287796549156864
        self.owo_bot_id = 408785106942164992
        self.captcha_orchestrator = captcha_orchestrator
        # Deprecated alias, kept for code that still reads the old name
        self.hcaptcha_solver = hcaptcha_solver
        self.db = database.Database(self)
        self.quest_handler = None
        self.danger_settings_dict = danger_settings_dict
        self.stats = Stats()
        if not syst.system.on_mobile:
            self.add_popup_queue = syst.popup.add_popup_queue
        else:
            # Shouldn't be used on Termux Devices...
            self.add_popup_queue = None

        self.quest_help_request = {
            "cookie": {"till": 0, "enabled": False, "userid": 0, "channel": 0},
            "pray": {"till": 0, "enabled": False, "userid": 0, "channel": 0},
            "curse": {"till": 0, "enabled": False, "userid": 0, "channel": 0},
            "battle": {"till": 0, "enabled": False, "userid": 0, "channel": 0},
        }
        self.ongoing_battle_external_quest = False

        # For sell/sac to know the rank of animals caught from hunt to dynamically handle them
        # Updated through hunt.py Cog.
        self.animal_rank_in_zoo = {
            "common": False,
            "uncommon": False,
            "rare": False,
            "epic": False,
            "special": False,
            "mythical": False,
            "gem": False,
            "legendary": False,
            "fabled": False,
            "distorted": False,
            "hidden": False,
        }

        # discord.py-self's module sets global random to fixed seed. reset that, locally.
        self.random = random.Random()

        self.ongoing_owobot_event = False

        with open("config/misc.json", "r", encoding="utf-8") as config_file:
            self.misc = json.load(config_file)

        self.ch = command_handler.CommandHandler(self)

        self.alias = self.misc["alias"]

        # Diagnostic trace, see core/reply_trace.py. Listeners only, no send or cog path changes
        self.add_listener(self._trace_message, "on_message")
        self.add_listener(self._trace_ready, "on_ready")
        self.add_listener(self._trace_resumed, "on_resumed")
        self.add_listener(self._trace_disconnect, "on_disconnect")

    def dispatch(self, event, /, *args, **kwargs):
        # A message reaches the cogs once: whichever copy arrives first, the gateway or a history read
        if event == "message" and args:
            # Every OwO DM seen, live or from history, moves on the newest id the DM check compares with
            self.reply_fallback.note_dm_message(args[0])
            if not target_filter.event_wanted(self, event, args):
                return  # lean gateway: not this account's channel, see core/target_filter.py
            message_id = getattr(args[0], "id", None)
            if message_id is not None:
                if message_id in self._recent_message_ids:
                    return
                self._recent_message_ids[message_id] = True
                if len(self._recent_message_ids) > 5000:
                    self._recent_message_ids.popitem(last=False)
        elif event in ("message_edit", "raw_message_edit") and not target_filter.event_wanted(self, event, args):
            return  # lean gateway: not this account's channel, see core/target_filter.py
        return super().dispatch(event, *args, **kwargs)

    def hand_history_message(self, message):
        """Hands a message read from channel history to the cogs, unless the gateway or history already did"""
        if message.id in self._recent_message_ids:
            return False
        self.dispatch("message", message)
        return True

    async def on_socket_raw_receive(self, msg):
        """
        Raw socket messages from Discord.py-self comes over here.
        """

        self.gateway_stats.count(msg)  # counts only, nothing is parsed for it
        frame = _FRAME_START.match(msg, 0, 300)
        if frame is not None and frame.group(1) not in MESSAGE_EVENTS:
            # Nothing below uses these frames: they were parsed and dropped here before
            return
        parsed_msg = json.loads(msg)
        if parsed_msg.get("t") not in ["MESSAGE_CREATE", "MESSAGE_UPDATE"]:
            return

        if parsed_msg["t"] == "MESSAGE_CREATE":
            reply_trace.raw_message(self, parsed_msg["d"])

        if not target_filter.raw_message_wanted(self, parsed_msg["d"]):
            return  # lean gateway: not this account's channel, see core/target_filter.py

        message = components.message.get_message_obj(parsed_msg["d"])

        if parsed_msg["t"] == "MESSAGE_CREATE":
            await self.message_dispatcher.dispatch_on_message(message)
        else:
            await self.message_dispatcher.dispatch_on_edit(message)

    async def _trace_message(self, message):
        reply_trace.on_message(self, message)

    async def _trace_ready(self):
        reply_trace.event(self, "ready")
        record_ready(self.gateway_stats)
        self.resume_unpaced = True
        await self._gateway_back()

    async def _trace_resumed(self):
        reply_trace.event(self, "resumed")
        self.resume_unpaced = True
        await self._gateway_back()

    async def _trace_disconnect(self):
        reply_trace.event(self, "disconnect")
        if self.gateway_down_at is None:
            self.gateway_down_at = time.time()

    def gateway_connected(self):
        return self.gateway_down_at is None

    def holds_while_gateway_down(self):
        settings = getattr(self.global_settings_dict, "replyFallback", None)
        return bool(getattr(settings, "holdWhileGatewayDown", False))

    def gateway_hold_active(self):
        """True while commands are held because the gateway is down (replyFallback.holdWhileGatewayDown)"""
        return not self.gateway_connected() and self.holds_while_gateway_down()

    async def wait_gateway_up(self):
        """Holds a send while the gateway is down, if replyFallback.holdWhileGatewayDown is set"""
        if not self.gateway_hold_active() or self.stopped or self.is_closed():
            return
        if not self.gateway_hold_logged:
            self.gateway_hold_logged = True
            await self.log("gateway down, holding commands", "#e6e65a", webhook_useless_log=False)
        while not self.gateway_connected() and not self.stopped and not self.is_closed():
            await asyncio.sleep(0.5)

    async def _gateway_back(self):
        if self.gateway_down_at is None:
            return
        down_at = self.gateway_down_at
        back_at = time.time()
        outage = back_at - down_at
        self.gateway_down_at = None
        self.gateway_back_at = back_at
        # Re-queue cooldowns do not run during the outage, so they carry on from the reconnect
        for command in self.ch.checks:
            if command.get("retry_at", 0) > down_at:
                command["retry_at"] += outage
        if self.gateway_hold_logged:
            self.gateway_hold_logged = False
            await self.log(f"gateway back after {outage:.0f}s, resuming", "#87af87", webhook_useless_log=False)

    @property
    def active_channel_ids(self):
        ids = set()
        if self.cm:
            ids.add(self.cm.id)
        if self.dm:
            ids.add(self.dm.id)
        if self.boss_channel_id:
            ids.add(self.boss_channel_id)
        if self.settings_dict:
            for cmd_name in ("pray", "curse"):
                cnf = getattr(self.settings_dict.commands, cmd_name, None)
                custom = getattr(cnf, "custom_channel", None) if cnf else None
                if custom and custom.enabled:
                    ids.add(custom.channel)
        return ids

    async def empty_checks_and_switch(self, channel):
        self.ch.command_handler_status.hold_handler = True
        await self.sleep_till(
            self.global_settings_dict.channelSwitcher.delayBeforeSwitch
        )
        self.cm = channel
        self.channel_id = self.cm.id
        self.ch.command_handler_status.hold_handler = False

    @tasks.loop(seconds=90)
    async def presence(self):
        if self.status != discord.Status.invisible:
            try:
                await self.change_presence(
                    status=discord.Status.invisible, activity=self.activity
                )
                self.presence.stop()
            except TRANSIENT_CONNECTION_ERRORS:
                await asyncio.sleep(2.0)
                return
            except Exception:
                pass
        else:
            self.presence.stop()

    @tasks.loop(seconds=1)
    async def random_sleep(self):
        sleep_obj = self.settings_dict.sleep
        await asyncio.sleep(sleep_obj.get_check_time())
        if sleep_obj.should_sleep():
            sleep_time = sleep_obj.get_sleep_time()
            owner = "random_sleep"
            self.ch.command_handler_status.sleep = True
            try:
                await self.ch.pause_with_timeout(owner, sleep_time + 30)
                await self.log(f"sleeping for {sleep_time}", "#87af87")
                await asyncio.sleep(sleep_time)
                await self.log("sleeping finished!", "#87af87")
            finally:
                self.ch.command_handler_status.sleep = False
                await self.ch.resume_stat(owner)

    @tasks.loop(seconds=60)
    async def stall_watchdog(self):
        try:
            status = self.ch.command_handler_status
            if status.captcha or status.safety_check or status.sleep or self.stopped or self.gateway_hold_active() or stop_requested():
                return
            threshold = float(self.global_settings_dict.account.stallThresholdSeconds)
            now = time.monotonic()
            if not should_check_stall(
                now=now,
                last_sent_at=self.last_sent_at,
                last_stall_at=self.last_stall_at,
                threshold=threshold,
                captcha=status.captcha,
                sleep=status.sleep,
                stopped=self.stopped,
            ):
                return
            await recover_stalled_account(self, now)
        except Exception as error:
            try:
                await self.log(
                    f"Warning - STALL watchdog failed: {type(error).__name__}",
                    "#e6e65a",
                )
            except Exception:
                logging.getLogger(__name__).exception("STALL watchdog failed")

    @tasks.loop(seconds=30)
    async def safety_check_loop(self):
        if not self.session:
            return
        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with self.session.get(
                f"{owo_dusk_api}/safety_check.json", timeout=timeout
            ) as resp:
                safety_check = await resp.json()
            async with self.session.get(
                f"{owo_dusk_api}/version.json", timeout=timeout
            ) as resp:
                latest_version = await resp.json()
        except TRANSIENT_CONNECTION_ERRORS:
            await asyncio.sleep(2.0)
            return
        except Exception as e:
            await self.log(
                f"Safety check request failed, will retry next cycle: {e}",
                "#5c0018",
            )
            return
        status = self.ch.command_handler_status
        if syst.system.compare_versions(version, safety_check["version"]):
            # Only the safety flag is set here. The captcha flag is never touched by this check
            if not status.safety_check:
                status.safety_check = True
                reason = " ".join(str(safety_check.get("reason", "")).split())[:200]
                author = " ".join(str(safety_check.get("author", "")).split())[:80]
                line = f"Safety check: paused (remote flag) - reason: {reason} (triggered by {author})"
                if syst.system.compare_versions(latest_version["version"], safety_check["version"]):
                    line += f" - update to v{latest_version['version']} to continue"
                await self.log(line, "#5c0018")
        elif status.safety_check:
            # Only the start and the end of a pause are logged, not every 30 seconds
            status.safety_check = False
            await self.log("Safety check: resumed", "#33245e")

    async def start_cogs(self):
        files = os.listdir(
            syst.system.resource_path("./core/cogs")
        )  # Get the list of files
        self.random.shuffle(files)
        self.refresh_commands_dict()
        for filename in files:
            if not filename.endswith(".py"):
                continue
            cog_name = filename[:-3]
            extension = f"core.cogs.{cog_name}"
            if extension in self.extensions:
                """skip if already loaded"""
                self.refresh_commands_dict()
                if not self.commands_dict[cog_name]:
                    await self.unload_cog(extension)
                continue

            await self.sleep_till(
                self.global_settings_dict.account.commandsStart
            )
            if not self.commands_dict.get(cog_name, False):
                continue

            try:
                async with _extension_load_turn():
                    await self.load_extension(extension)
            except Exception as e:
                short_reason = " ".join(str(e).split())[:160] or type(e).__name__
                account_name = getattr(self, "username", "unknown account")
                await self.log(
                    f"Error - {account_name} failed to load cog {extension}: {short_reason}",
                    "#c25560",
                    webhook_useless_log=False,
                )
                if extension == "core.cogs.captcha":
                    await self.stop_account(
                        f"captcha cog failed to load: {short_reason}"
                    )
                    return

        if "core.cogs.captcha" not in self.extensions:
            reason = "captcha extension was disabled or did not load"
            account_name = getattr(self, "username", "unknown account")
            await self.log(
                f"Error - {account_name} failed to load cog core.cogs.captcha: {reason}",
                "#c25560",
                webhook_useless_log=False,
            )
            await self.stop_account(f"captcha cog failed to load: {reason}")

    async def update_config(self):
        async with self.lock:
            # Every account uses the default profile, config/settings.json. Per-account settings files are not read
            with open("config/settings.json", "r", encoding="utf-8") as config_file:
                self.settings_dict = config_models.configs.FetchSettings(
                    json.load(config_file)
                )

            await self.start_cogs()

    @suppress_and_log("Unloading Cog")
    async def unload_cog(self, cog_name):
        if cog_name in self.extensions:
            async with _extension_load_turn():
                await self.unload_extension(cog_name)

    def refresh_commands_dict(self):
        commands_obj = self.settings_dict.commands
        reaction_bot_obj = self.settings_dict.cooldowns.reactionBot
        gamble_obj = self.settings_dict.gamble

        # Reaction Bot:
        if (
            (
                reaction_bot_obj.huntAndBattle
                and (commands_obj.hunt.enabled or commands_obj.battle.enabled)
            )
            or (reaction_bot_obj.owo and commands_obj.owo.enabled)
            or reaction_bot_obj.prayAndCurse
            and (commands_obj.pray.enabled or commands_obj.curse.enabled)
        ):
            reactionbot = True
        else:
            reactionbot = False

        should_start_looper = (
            # owo
            (commands_obj.owo.enabled and not reaction_bot_obj.owo)
            # pray/curse
            or (
                (commands_obj.pray.enabled or commands_obj.curse.enabled)
                and not reaction_bot_obj.prayAndCurse
            )
            # level grind
            or commands_obj.lvlGrind.enabled
        )

        self.commands_dict = {
            "army": commands_obj.army.enabled,
            "battle": commands_obj.battle.enabled
            and not reaction_bot_obj.huntAndBattle,
            "blackjack": gamble_obj.blackjack.enabled,
            "boss": self.settings_dict.boss.enabled,
            "captcha": True,
            "channelswitcher": self.global_settings_dict.channelSwitcher.enabled,
            "chat": True,
            "coinflip": gamble_obj.coinflip.enabled,
            "commands": True,
            "cookie": commands_obj.cookie.enabled,
            "customcommands": self.settings_dict.customCommands.enabled,
            "daily": self.settings_dict.daily,
            "gems": self.settings_dict.autoUse.gems.enabled,
            "giveaway": self.settings_dict.giveaway.enabled,
            "levelcheck": bool(getattr(self.global_settings_dict, "levelCheck", None) and self.global_settings_dict.levelCheck.enabled),
            "hunt": commands_obj.hunt.enabled and not reaction_bot_obj.huntAndBattle,
            "huntbot": commands_obj.huntbot.enabled,
            "looper": should_start_looper,
            "lottery": commands_obj.lottery.enabled,
            "mail": self.settings_dict.mail,
            "others": True,
            "pupiku": commands_obj.pup.enabled
            or commands_obj.piku.enabled
            or commands_obj.run.enabled,
            "quest": self.settings_dict.autoQuest.enabled,
            "reactionbot": reactionbot,
            "sell": True,
            "shop": commands_obj.shop.enabled,
            "slots": gamble_obj.slots.enabled,
        }

    """To make the code cleaner when accessing cooldowns from config."""

    def random_float(self, cooldown_list):
        return self.random.uniform(cooldown_list[0], cooldown_list[1])

    async def sleep_till(self, cooldown, cd_list=True, noise=3):
        if cd_list:
            await asyncio.sleep(self.random_float(cooldown))
        else:
            await asyncio.sleep(self.random.uniform(cooldown, cooldown + noise))

    async def sleep(self, time):
        # to save imports
        await asyncio.sleep(time)

    async def log(
        self,
        text,
        color="#ffffff",
        bold=False,
        web_log=global_settings_dict.website.enabled,
        webhook_useless_log=global_settings_dict.webhook.logCommandSend,
        lineno=None,
        filename=None,
        escape_markup=False,
        level="INFO",
        category=None,
    ):
        current_time = datetime.now().strftime("%H:%M:%S")
        # escape_markup: the console and the run log must show a `[tag]` in the text as it is (rich reads it as a style)
        shown = escape(str(text)) if escape_markup else text
        # The console and the website get the redacted text; the file sink redacts inside run_log.write
        safe = run_log.redact(shown)
        # The run log copy is plain text with the full date, see utils/run_log.py
        visible = run_log.write(
            shown, account=self.username, level=level, category=category
        )
        if not visible:
            return
        if self.misc["debug"]["enabled"]:
            if not lineno and not filename:
                frame_info = traceback.extract_stack()[-2]
                filename = os.path.basename(frame_info.filename)
                lineno = frame_info.lineno

            content_to_print = f"[#676585]❲{current_time}❳[/#676585] {self.username} - {safe} | [#676585]❲{filename}:{lineno}❳[/#676585]"
            with run_log.console_only():
                syst.system.console.print(content_to_print, style=color, markup=True)
        else:
            with run_log.console_only():
                syst.system.console.print(
                    f"{self.username}| {safe}".center(syst.system.console_width - 2),
                    style=color,
                )
        if web_log:
            with lock:
                website_append(
                    f"<div class='message'><span class='timestamp'>[{current_time}]</span><span class='text'>{html.escape(self.username)}| {html.escape(str(safe))}</span></div>"
                )

        if webhook_useless_log:
            await self.send_webhook("on_command_send", command_send=text)

    async def fetch_slash_commands(self, channel):
        if self.slash_commands.get(str(channel.id)):
            return

        self.slash_commands[str(channel.id)] = []
        for command in await channel.application_commands():
            if command.application.id == self.owo_bot_id:
                self.slash_commands[str(channel.id)].append(command)

    async def send_webhook(
        self,
        data_id: str,
        username: str = "OwO-Dusk",
        webhook_url: str = None,
        pingid: str = None,
        **kwargs,
    ):
        """ "example_data": {
            "title": "",
            "description": "",
            "content": "",
            "color": "",
            "thumbnail": "",
            "author_name": "",
            "author_image": "",
            "footer": ""
        }"""

        data = webhook_data_dict.get(data_id, None)
        if not data:
            raise ValueError("Invalid data_id passed for fetching webhook embed data")
        data = data.copy()

        formattable_fields = [
            "title",
            "description",
            "content",
            "footer",
            "author_name",
            "thumbnail",
            "author_image",
        ]
        format_kwargs = {
            "username": self.user.name,
            "userid": self.user.id,
            "current_time": datetime.now().strftime("%H:%M:%S"),
            **kwargs,
        }
        for field in formattable_fields:
            if data.get(field):
                data[field] = data[field].format(**format_kwargs)

        color = data.get("color", None)
        if color:
            if isinstance(color, str) and color.startswith("#"):
                color = int(color.lstrip("#"), 16)
            else:
                color = int(color)
        else:
            color = 0x412280

        author_name = data.get("author_name", None)
        if not author_name and data.get("author_image"):
            author_name = "OwO-Dusk"

        embed = {
            "title": data.get("title", None),
            "description": data.get("description", None),
            "color": color,
            "footer": {"text": data.get("footer", None)},
            "thumbnail": {"url": data.get("thumbnail", None)},
            "author": {"name": author_name, "icon_url": data.get("author_image", None)},
        }

        content = data.get("content", "")
        if pingid:
            content += f"\n<@{pingid}>"

        payload = {
            "username": username,
            "embeds": [embed],
            "content": content,
        }

        if not registry.all_ready_event.is_set():
            asyncio.create_task(
                self._send_webhook_after_ready(payload, webhook_url)
            )
            return

        await self._deliver_webhook_payload(payload, webhook_url)

    async def _send_webhook_after_ready(self, payload, webhook_url):
        await await_all_ready()
        await self._deliver_webhook_payload(payload, webhook_url)

    async def _deliver_webhook_payload(self, payload, webhook_url):
        await rate_limiter.acquire()
        try:
            async with self.webhook_lock:
                if not webhook_url:
                    webhook_handler.send(payload)
                else:
                    await webhook_handler.custom_send(payload, webhook_url)
        except TRANSIENT_CONNECTION_ERRORS:
            await asyncio.sleep(2.0)
            return

    async def _send_stop_webhook(self, reason):
        global _stop_webhook_warned
        webhook = self.global_settings_dict.webhook
        if not webhook.enabled:
            return
        url = webhook.webhookCaptchaUrl
        if not isinstance(url, str) or "discord.com" not in url:
            if not _stop_webhook_warned:
                _stop_webhook_warned = True
                await self.log(
                    "Stop webhook not sent - webhookCaptchaUrl is empty or not a Discord webhook",
                    "#e6e65a",
                )
            return
        with suppress_and_log_block("Stop Webhook"):
            await self.send_webhook(
                "on_account_stopped", webhook_url=url, reason=reason
            )

    async def stop_account(self, reason):
        """Stops this account alone: its loops, cogs, queue and connection. The others keep running"""
        if self.stopped:
            return
        self.stopped = True
        run_log.captcha_start()
        await self.log(f"STOPPED - {self.username} - {reason}", "#d70000")
        run_log.captcha_finish("FAILED", reason)
        registry.signal_stopped(self.account_index, reason)
        await self._send_stop_webhook(reason)

        await self.end_connection()
        if registry.all_stopped() and not stop_requested():
            await self.log("All accounts are stopped, exiting", "#d70000")
            run_log.exit_process(0)

    async def end_connection(self):
        """Ends this account's loops, cogs, queue and connection. Runs on the account's own loop, see core/auto_stop.py"""
        current = asyncio.current_task()
        candidates = [
            self.presence,
            self.random_sleep,
            self.safety_check_loop,
            self.stall_watchdog,
            self.ch.stat_timer,
            *self.ch.pause_timers.values(),
        ]
        for cog in self.cogs.values():
            candidates.extend(vars(cog).values())
        for item in candidates:
            if isinstance(item, tasks.Loop):
                if item.is_running():
                    item.cancel()
            elif isinstance(item, asyncio.Task) and item is not current:
                item.cancel()
        for name in list(self.extensions):
            await self.unload_cog(name)
        while not self.ch.queue.empty():
            self.ch.queue.get_nowait()
        self.ch.checks.clear()

        if self.session is not None and not self.session.closed:
            await self.session.close()
        await self.close()

    def calculate_correction_time(self, command):
        command = command.replace(" ", "")  # Remove spaces for accurate timing
        base_delay = self.random_float(self.settings_dict.misspell.baseDelay)
        rectification_time = sum(
            self.random_float(self.settings_dict.misspell.rectificationTime)
            for _ in command
        )
        total_time = base_delay + rectification_time
        return total_time

    # send commands
    async def send(
        self,
        message,
        color=None,
        bypass=False,
        channel=None,
        silent=global_settings_dict.silentMessage,
        typingIndicator=global_settings_dict.typingIndicator,
    ):
        """
        TASK: Refactor
        """

        if self.stopped or refuse_command(self, message):
            return
        await await_send_permission(self.account_index)
        await self.wait_gateway_up()
        if self.stopped:
            return
        if not channel:
            channel = self.cm
        disable_log = self.misc["console"]["disableCommandSendLog"]
        msg = message
        misspelled = False
        if self.settings_dict.misspell.enabled:
            misspelled = self.settings_dict.misspell.should_misspell()
            msg = misspell_word(message)
            # left off here!

        """
        TASK: remove repetition here
        """
        await self.wait_until_ready()
        sent_message = None
        if (not self.ch.command_handler_status.captcha and not self.ch.command_handler_status.safety_check) or bypass:
            try:
                if typingIndicator:
                    async with channel.typing():
                        sent_message = await channel.send(msg, silent=silent)
                else:
                    sent_message = await channel.send(msg, silent=silent)
            except TRANSIENT_CONNECTION_ERRORS as error:
                reply_trace.note_failed(self, error)
                await asyncio.sleep(2.0)
                return

            self.last_sent_at = time.monotonic()
            reply_trace.note_sent(self)
            frame_info = traceback.extract_stack()[-2]
            filename = os.path.basename(frame_info.filename)
            lineno = frame_info.lineno
            if not disable_log:
                await self.log(
                    f"Ran: {msg}",
                    color if color else "#5432a8",
                    lineno=lineno,
                    filename=filename,
                )
            if misspelled:
                owner = "misspell_fix"
                await self.ch.pause_with_timeout(owner)
                try:
                    correction_time = self.calculate_correction_time(message)
                    await self.log(
                        f"correcting: {msg} -> {message} in {correction_time}s",
                        "#422052",
                        lineno=lineno,
                        filename=filename,
                    )
                    await asyncio.sleep(correction_time)
                    await await_send_permission(self.account_index)
                    await self.wait_gateway_up()
                    try:
                        if typingIndicator:
                            async with channel.typing():
                                sent_message = await channel.send(message, silent=silent)
                        else:
                            sent_message = await channel.send(message, silent=silent)
                        self.last_sent_at = time.monotonic()
                        reply_trace.note_sent(self)
                    except TRANSIENT_CONNECTION_ERRORS as error:
                        reply_trace.note_failed(self, error)
                        await asyncio.sleep(2.0)
                        return
                finally:
                    await self.ch.resume_stat(owner)
        return sent_message

    @suppress_and_log("Sending Slash Command")
    async def slashCommandSender(self, msg, color, channel, **kwargs):
        if self.stopped or refuse_command(self, msg):
            return
        await await_send_permission(self.account_index)
        await self.wait_gateway_up()
        try:
            if not channel:
                channel = self.cm
            if not self.slash_commands.get(str(channel.id)):
                await self.fetch_slash_commands(channel)

            for command in self.slash_commands[str(channel.id)]:
                if command.name == msg:
                    await self.wait_until_ready()
                    await command(**kwargs)
                    self.last_sent_at = time.monotonic()
                    await self.log(f"Ran: /{msg}", color if color else "#5432a8")
        except TRANSIENT_CONNECTION_ERRORS:
            await asyncio.sleep(2.0)
            return

    def calc_time(self):
        pst_timezone = pytz.timezone("America/Los_Angeles")  # gets timezone
        current_time_pst = datetime.now(timezone.utc).astimezone(
            pst_timezone
        )  # current pst time
        midnight_pst = pst_timezone.localize(
            datetime(
                current_time_pst.year,
                current_time_pst.month,
                current_time_pst.day,
                0,
                0,
                0,
            )
        )  # gets 00:00 of the day
        time_until_12am_pst = (
            midnight_pst + timedelta(days=1) - current_time_pst
        )  # adds a day to the midnight to get time till next midnight, then subtract it with current time
        total_seconds = time_until_12am_pst.total_seconds()  # turn that time to seconds
        # 12am = 00:00, I might need this the next time I take a look here.
        return total_seconds

    # TBR
    def should_run(self, last_timestamp):
        # gets timezone
        pst = pytz.timezone("America/Los_Angeles")

        now_pst = datetime.now(timezone.utc).astimezone(pst)  # current pst time
        last_pst = datetime.fromtimestamp(last_timestamp, timezone.utc).astimezone(pst)

        return now_pst.date() != last_pst.date()

    # TBR
    def time_in_seconds(self):
        """
        timestamp is basically seconds passed after 1970 jan 1st
        """
        time_now = datetime.now(timezone.utc).astimezone(pytz.timezone("America/Los_Angeles"))
        return time_now.timestamp()

    # TBR
    def pst_midnight_timestamp(self):
        now = datetime.now(timezone.utc).astimezone(pytz.timezone("America/Los_Angeles"))
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight.timestamp()

    async def check_for_cash(self):
        await asyncio.sleep(self.random.uniform(4.5, 34.4))
        await self.ch.put_queue(
            {
                "cmd_name": self.alias["cash"]["normal"],
                "prefix": True,
                "checks": True,
                "id": "cash",
                "removed": False,
            }
        )

    def update_cash(self, amount, override=False, reduce=False, assumed=False):
        if override and self.settings_dict.cashCheck:
            self.stats.balance = amount
            self.stats.has_updated_balance = True
        else:
            if self.settings_dict.cashCheck and not assumed:
                if reduce:
                    self.stats.balance -= amount
                else:
                    self.stats.balance += amount

            if reduce:
                self.stats.net_earnings -= amount
            else:
                self.stats.net_earnings += amount
        self.db.update_cash_db()

    async def setup_hook(self):
        asyncio.get_running_loop().set_exception_handler(
            account_loop_exception_handler
        )
        # Heartbeat for the stack sampler, see core/loop_watchdog.py. It runs on this loop, in this thread
        self.loop_thread_id = threading.get_ident()
        self.loop_heartbeat_at = time.monotonic()
        self.loop_heartbeat_task = asyncio.create_task(run_heartbeat(self))
        loop_watchdog.register(self, self.loop_thread_id)
        # Randomise user
        self.lock = asyncio.Lock()
        self.webhook_lock = asyncio.Lock()
        if self.misc["debug"]["hideUser"]:
            x = [
                "Sunny",
                "River",
                "Echo",
                "Sky",
                "Shadow",
                "Nova",
                "Jelly",
                "Pixel",
                "Cloud",
                "Mint",
                "Flare",
                "Breeze",
                "Dusty",
                "Blip",
            ]
            random_part = self.random.choice(x)
            self.username = (
                f"{random_part}_{abs(hash(str(self.user.id) + random_part)) % 10000}"
            )
        else:
            self.username = self.user.name
        run_log.bind_account(self.username)

        self.safety_check_loop.start()
        self.local_headers = await components.headers.generate_headers()
        self.local_headers["Authorization"] = self.token
        if self.session is None:
            self.session = aiohttp.ClientSession()

        self.quest_handler = LocalQuestHandler(
            global_quest_handler, self.user.id, self.session
        )

        syst.system.print_box(
            f"-Loaded {self.username}[*].".center(syst.system.console_width - 2),
            "bold royal_blue1 ",
        )
        list_user_ids.append(self.user.id)

        await self.db.update_priorities()

        # Fetch the channel
        self.cm = self.get_channel(self.channel_id)

        if not self.cm:
            try:
                self.cm = await self.fetch_channel(self.channel_id)
            except discord.NotFound:
                try:
                    registry.signal_failed(self.account_index, "channel_id not found")
                except Exception:
                    pass
                await self.log(
                    f"Error - Channel with ID {self.channel_id} does not exist.",
                    "#c25560",
                )
                return
            except discord.Forbidden:
                try:
                    registry.signal_failed(self.account_index, "channel_id forbidden")
                except Exception:
                    pass
                await self.log(
                    f"Bot lacks permissions to access channel {self.channel_id}.",
                    "#c25560",
                )
                return
            except discord.HTTPException as e:
                try:
                    registry.signal_failed(
                        self.account_index, "HTTP error fetching channel"
                    )
                except Exception:
                    pass
                await self.log(
                    f"Failed to fetch channel {self.channel_id}: {e}", "#c25560"
                )
                return

        self.cm_slowmode_cd = self.cm.slowmode_delay

        reply_trace.account_started(self)
        self.reply_trace_task = asyncio.create_task(reply_trace.run_summaries(self))

        # self.nick_name = self.cm.guild.me.nick

        # self.dm = await (await self.fetch_user(self.owo_bot_id)).create_dm()
        # remove temp fix in `cogs/captcha.py` if uncommenting

        # Stores slash commands. This will be populated.
        self.slash_commands = {}

        await self.fetch_slash_commands(self.cm)

        # Charts
        await self.db.populate_stats_db()

        await self.db.populate_cowoncy_earnings()
        await self.db.reset_gamble_wins_or_losses()

        await self.db.fetch_net_earnings()

        # Start various tasks and updates
        # self.config_update_checker.start()
        # disabled since unnecessory
        # 1. Immediately signal ready upon successful login to unblock the wave sequence
        try:
            registry.signal_ready(self.account_index, self.user.id)
        except Exception:
            pass

        # 2. Gate execution until all waves and accounts are fully connected
        while not registry.all_ready_event.is_set():
            if registry.get(self.account_index)["status"] in {"failed", "stopped"}:
                await self.close()
                return
            await asyncio.sleep(0.5)

        if registry.get(self.account_index)["status"] in {"failed", "stopped"}:
            await self.close()
            return

        if self.token_len > 1:
            time_to_sleep = self.random_float(global_settings_dict.account.startupDelay)
            await self.log(f"{self.username} sleeping {time_to_sleep}s before starting")
            await asyncio.sleep(time_to_sleep)

        await self.update_config()

        if self.global_settings_dict.offlineStatus:
            self.presence.start()

        self.stall_watchdog.start()
        self.reply_fallback.start()

        if self.settings_dict.sleep.enabled:
            self.random_sleep.start()

        if self.settings_dict.cashCheck:
            asyncio.create_task(self.check_for_cash())
