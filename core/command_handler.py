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
import itertools
import logging
import math
import time
from contextlib import asynccontextmanager
from copy import deepcopy

from core.auto_stop import stop_requested
from utils.errors import suppress_and_log

logger = logging.getLogger(__name__)


class CommandHandlerStatus:
    def __init__(self):
        # This is
        self.state = True
        # This is toggled when a captcha appears
        self.captcha = False
        # Set when a captcha episode ran past its deadline, see core/cogs/captcha.py
        self.captcha_flag = False
        # This is toggled when sleep is required (user preference)
        self.sleep = False
        # This is toggled for temporary pauses as required by other commands
        self.hold_handler = False
        # Set while the remote safety check says this version must not run (client.safety_check_loop).
        # It pauses the bot exactly like captcha, but only the safety check sets and clears it
        self.safety_check = False


class CommandHandler:
    def __init__(self, client):
        self.lock = None
        # Queue at which commands will be queued
        self.queue = asyncio.PriorityQueue()
        # Tie-breaker
        self.cmd_counter = itertools.count()
        # Priorities of command. Will be populated from `database` from `misc.json` config
        self.cmd_priorities = {}
        self.unknown_command_ids = set()
        self.unknown_command_info = {}
        # A list of commands that must be re-ran
        self.checks = []
        # Per command id, an async function that queues the command again when OwO gives no reply and the
        # reply fallback gives up on it, see ReplyFallback.give_up. Only cycles that wait for a reply register one
        self.silence_handlers = {}
        # State event for setting `self.state`
        self.state_event = asyncio.Event()
        # Set once a queued copy has been sent or dropped, keyed by its token
        self.sent_events = {}
        # Keep each pause owner independent so one handler cannot resume another.
        self.pause_owners = {}
        self.paused_by = self.pause_owners
        self.paused_at = {}
        self.pause_timers = {}
        # Backwards-compatible handle for code that inspected the old single timer.
        self.stat_timer = None
        # Monotonic time until which every command of this account waits, set when OwO confirms a sac
        self.sac_cooldown_until = 0.0

        self.client = client

        self.cmds_state = {"global": {"last_ran": 0}}
        for key in self.client.misc["command_info"]:
            self.cmds_state[key] = self._new_command_state()

        self.command_handler_status = CommandHandlerStatus()

    def _new_command_state(self):
        return {
            "in_queue": False,
            "in_monitor": False,
            "last_ran": 0,
            # Taken off the queue by the consumer, not yet sent or dropped
            "pending_send": False,
        }

    def ensure_command_state(self, cmd_id):
        return self.cmds_state.setdefault(cmd_id, self._new_command_state())

    def _default_command_priority(self):
        command_info = self.client.misc["command_info"]
        known_priorities = [
            priority
            for cmd_id, priority in self.cmd_priorities.items()
            if cmd_id in command_info
        ]
        configured_priority = max(
            (info.get("priority", 0) for info in command_info.values()), default=0
        )
        return max(known_priorities, default=max(len(command_info), configured_priority)) + 1

    async def get_command_info(self, cmd_id):
        command_info = self.client.misc["command_info"]
        if cmd_id in command_info:
            self.ensure_command_state(cmd_id)
            return command_info[cmd_id]

        self.ensure_command_state(cmd_id)
        if cmd_id not in self.unknown_command_ids:
            self.unknown_command_ids.add(cmd_id)
            if command_info:
                default_info = max(
                    command_info.values(),
                    key=lambda info: info.get("priority", 0),
                )
                self.unknown_command_info[cmd_id] = deepcopy(default_info)
            else:
                self.unknown_command_info[cmd_id] = {
                    "priority": 0,
                    "basecd": 0,
                    "log_color": "#ffffff",
                }
            self.cmd_priorities[cmd_id] = self._default_command_priority()
            await self.client.log(
                f"Warning - unknown command id '{cmd_id}' not in misc.json, using defaults",
                "#e6e65a",
                webhook_useless_log=False,
            )

        return self.unknown_command_info[cmd_id]

    @asynccontextmanager
    async def _hold_or_create_lock(self):
        if not self.lock:
            self.lock = asyncio.Lock()

        async with self.lock:
            yield

    @property
    def should_not_send(self):
        """
        Commands must not be send in these cases
        """
        return (
            not self.command_handler_status.state
            or self.command_handler_status.hold_handler
            or self.command_handler_status.sleep
            or self.command_handler_status.captcha
            or self.command_handler_status.safety_check
        )

    @property
    def can_send_if_priority(self):
        """
        Some commands are required to be send quickly. So if its just
        """
        return (
            not self.command_handler_status.sleep
            and not self.command_handler_status.hold_handler
            and not self.command_handler_status.captcha
            and not self.command_handler_status.safety_check
        )

    @suppress_and_log("Attempting to append queue")
    async def put_queue(self, cmd_data, priority=False, quick=False):
        if self.client.stopped:
            return
        if stop_requested():
            # No new command once autoStop has started. Whoever waits for this copy is released, see release_sent
            self.release_sent(cmd_data)
            return
        while self.should_not_send:
            if priority and self.can_send_if_priority:
                break
            await asyncio.sleep(self.client.random.uniform(1.4, 2.9))

        cmd_id = cmd_data["id"]
        await self.get_command_info(cmd_id)
        cmd_state = self.ensure_command_state(cmd_id)
        if cmd_state["in_queue"] or cmd_state["pending_send"]:
            # Add exception for custom commands
            if cmd_data["id"] != "customcommand":
                # Deliberate dedupe: a copy is still queued or being sent, a second one would send twice
                logger.debug(
                    "Command with id %s is already queued or being sent, not adding it again",
                    cmd_data["id"],
                )
                # Whoever waits for this copy to leave the queue must not wait forever
                self.release_sent(cmd_data)
                return

        # Get priority
        priority_int = self.cmd_priorities.get(cmd_id) if not quick else 0

        if not priority_int and priority_int != 0:
            await self.client.log(
                f"Error - command with id: {cmd_data['id']} is missing priority.",
                "#c25560",
            )
            return

        async with self._hold_or_create_lock():
            await self.queue.put(
                (
                    priority_int,  # Priority to sort commands with
                    next(self.cmd_counter),  # A counter to serve as a tie-breaker
                    deepcopy(cmd_data),  # actual data
                )
            )
            self.cmds_state[cmd_data["id"]]["in_queue"] = True

    @suppress_and_log("Remove Queue")
    async def remove_queue(self, cmd_data=None, id=None):
        if not cmd_data and not id:
            await self.client.log(
                "Error: No id or command data provided for removing item from queue.",
                "#c25560",
            )
            return

        # A handler passes its command template, which lacks the sent_* keys a pending entry gets on send,
        # so a template is matched by its id
        if id is None:
            id = cmd_data.get("id", None)
        async with self._hold_or_create_lock():
            # Filter rather than pop by index while enumerating: popping skipped the entry after each removal
            self.checks[:] = [
                command for command in self.checks if command.get("id", None) != id
            ]

    def is_pending(self, command):
        """True while this exact entry still waits for its reply. Compared by identity, a copy can look the same"""
        return any(pending is command for pending in self.checks)

    async def remove_check(self, command):
        """Drops this exact entry and no other with the same id. False if it was already gone"""
        async with self._hold_or_create_lock():
            for index, pending in enumerate(self.checks):
                if pending is command:
                    del self.checks[index]
                    return True
            return False

    async def search_checks(self, id):
        async with self._hold_or_create_lock():
            for command in self.checks:
                if command.get("id", None) == id:
                    return True
            return False

    async def shuffle_queue(self):
        async with self._hold_or_create_lock():
            items = []
            while not self.queue.empty():
                items.append(await self.queue.get())

            self.client.random.shuffle(items)

            for item in items:
                await self.queue.put(item)

    async def upd_cmd_state(self, id, reactionBot=False):
        await self.get_command_info(id)
        async with self._hold_or_create_lock():
            self.cmds_state["global"]["last_ran"] = time.time()
            self.cmds_state[id]["last_ran"] = time.time()
            if not reactionBot:
                self.cmds_state[id]["in_queue"] = False
                self.cmds_state[id]["pending_send"] = True
            self.client.db.update_cmd_db(id)

    def new_sent_event(self, cmd_data):
        """Tag a copy before it is queued. The returned event is set once the consumer has sent or dropped it."""
        cmd_data["sent_token"] = next(self.cmd_counter)
        event = asyncio.Event()
        self.sent_events[cmd_data["sent_token"]] = event
        return event

    def release_sent(self, cmd_data):
        event = self.sent_events.pop(cmd_data.get("sent_token"), None)
        if event is not None:
            # The message the HTTP send returned, if any: block_till_send takes it as proof of the send
            event.sent_message_id = cmd_data.get("sent_message_id")
            event.set()

    def finish_send(self, cmd_data):
        """Called by the consumer after it has tried to send a dequeued copy, whatever the outcome."""
        cmd_state = self.cmds_state.get(cmd_data.get("id"))
        if cmd_state is not None:
            cmd_state["pending_send"] = False
        self.release_sent(cmd_data)

    async def set_stat(self, value, owner=None):
        if value:
            await self.resume_stat(owner)
        else:
            await self.pause_with_timeout(owner or "legacy")

    def _default_pause_timeout(self):
        settings = getattr(self.client, "global_settings_dict", None)
        account = getattr(settings, "account", None)
        timeout = getattr(account, "pauseTimeoutSeconds", 60)
        try:
            timeout = float(timeout)
        except (TypeError, ValueError, OverflowError):
            return 60.0
        return timeout if math.isfinite(timeout) and timeout > 0 else 60.0

    async def pause_with_timeout(self, owner="legacy", timeout=None):
        """Pause normal sends for `owner`; the owner is removed on timeout."""
        if isinstance(owner, (int, float)) and timeout is None:
            # Older callers passed a timeout as the first positional argument.
            timeout = owner
            owner = "legacy"
        owner = str(owner or "legacy")
        if timeout is None:
            timeout = self._default_pause_timeout()
        try:
            timeout = float(timeout)
        except (TypeError, ValueError, OverflowError):
            timeout = self._default_pause_timeout()
        if not math.isfinite(timeout) or timeout <= 0:
            timeout = self._default_pause_timeout()

        old_timer = self.pause_timers.pop(owner, None)
        if old_timer is not None and old_timer is not asyncio.current_task():
            old_timer.cancel()

        self.paused_at[owner] = time.time()
        self.pause_owners[owner] = time.monotonic() + timeout
        self.command_handler_status.state = False
        self.state_event.clear()
        timer = asyncio.create_task(self._resume_after(owner, timeout))
        self.pause_timers[owner] = timer
        self.stat_timer = timer

    async def _resume_after(self, owner, timeout):
        try:
            await asyncio.sleep(timeout)
            if owner not in self.pause_owners:
                return
            paused_at = self.paused_at.get(owner, time.time())
            elapsed = max(0.0, time.time() - paused_at)
            name = getattr(self.client, "username", None) or f"account #{getattr(self.client, 'account_index', '?')}"
            try:
                await self.client.log(
                    f"Warning - pause timeout - {name} - owner={owner} paused_for={elapsed:.1f}s",
                    "#e6e65a",
                    webhook_useless_log=False,
                )
            except Exception:
                logger.warning(
                    "Warning - pause timeout - %s - owner=%s paused_for=%.1fs",
                    name,
                    owner,
                    elapsed,
                )
            finally:
                await self.resume_stat(owner)
        finally:
            if self.pause_timers.get(owner) is asyncio.current_task():
                self.pause_timers.pop(owner, None)
            if self.stat_timer is asyncio.current_task():
                self.stat_timer = next(iter(self.pause_timers.values()), None)

    async def resume_stat(self, owner=None):
        if owner is None:
            for timer in self.pause_timers.values():
                if timer is not asyncio.current_task():
                    timer.cancel()
            self.pause_timers.clear()
            self.pause_owners.clear()
            self.paused_at.clear()
            self.stat_timer = None
        else:
            owner = str(owner)
            timer = self.pause_timers.pop(owner, None)
            if timer is not None and timer is not asyncio.current_task():
                timer.cancel()
            self.pause_owners.pop(owner, None)
            self.paused_at.pop(owner, None)

        if not self.pause_owners:
            self.command_handler_status.state = True
            self.state_event.set()
        else:
            self.command_handler_status.state = False
            self.state_event.clear()
            self.stat_timer = next(iter(self.pause_timers.values()), None)

    def start_sac_cooldown(self, seconds, now=None):
        """Holds every command of this account for `seconds` from `now`. Called when OwO confirms a sac"""
        now = time.monotonic() if now is None else now
        self.sac_cooldown_until = now + seconds

    def sac_cooldown_remaining(self, now=None):
        """Seconds left of the pause after a confirmed sac, 0 when no pause is running"""
        now = time.monotonic() if now is None else now
        return max(0.0, self.sac_cooldown_until - now)

    async def wait_sac_cooldown(self):
        """Keeps a dequeued command from being sent until the pause after a confirmed sac is over.
        Waits again if a newer pause started meanwhile. Called by Commands.send_commands"""
        while (remaining := self.sac_cooldown_remaining()) > 0:
            await asyncio.sleep(remaining)
