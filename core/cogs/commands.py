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
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from discord.ext import tasks

from core.account_registry import registry
from core.cogs._BASE import BaseCog
from core.send_gate import await_send_gate, send_permission_context

# How many times a command in `checks` is re-queued without seeing its reply before giving up
MAX_CHECK_READDS = 5


class Commands(BaseCog):
    def __init__(self, bot):
        super().__init__(bot)
        self.bot.ch.checks = []
        self.calc_time = timedelta(0)
        self.command_times = deque(maxlen=3)
        self.updated_between_cd = False
        self.last_msg = 0

    @property
    def command_handler_settings(self):
        return self.bot.settings_dict.cooldowns.commandHandler

    def sleep_required(self):
        """makes sure three commands are within 5 second limit"""
        now = time.time()

        while self.command_times and now - self.command_times[0] >= 5:
            """Command has to be within 5 second limit"""
            self.command_times.popleft()

        if len(self.command_times) < 3:
            return False, 0
        else:
            wait_time = max(0, 5 - (now - self.command_times[0]))
            return True, wait_time

    async def sleep_between_commands(self, betweenCommands):
        if betweenCommands[0] > self.bot.cm_slowmode_cd:
            await self.bot.sleep_till(betweenCommands)
        else:
            await self.bot.sleep_till(self.bot.cm_slowmode_cd, cd_list=False)
            if not self.updated_between_cd:
                await self.bot.log(
                    f"Channel has a cooldown of {self.bot.cm_slowmode_cd}s, increasing delay between commands!",
                    "#8f6b09",
                )
                self.updated_between_cd = True

    def construct_command(self, data, guild_id):
        prefix = self.bot.settings_dict.prefix if data.get("prefix") else ""

        if guild_id and guild_id != self.bot.cm.guild.id:
            # Revert
            prefix = "owo "

        return f"{prefix}{data['cmd_name']} {data.get('cmd_arguments') or ''}".strip()

    def retry_wait(self, cmd_id, delay):
        """Seconds a check waits after exhausting its no-reply re-queues."""
        cnf = getattr(self.bot.settings_dict.commands, cmd_id, None)
        if cnf is not None and getattr(cnf, "cooldown", None) and hasattr(cnf, "get_cd"):
            cooldown = cnf.get_cd()
        else:
            cooldown = delay
        return max(cooldown, 300)

    async def start_commands(self):
        await self.bot.sleep_till(
            self.bot.global_settings_dict.account.commandsHandlerStart
        )
        if self.bot.stopped:
            return
        await self.bot.ch.shuffle_queue()
        await self.bot.wait_until_ready()
        # The stall watchdog can start these before this runs, and a second start() raises RuntimeError
        if not self.send_commands.is_running():
            self.send_commands.start()
        if not self.monitor_checks.is_running():
            self.monitor_checks.start()

    async def cog_load(self):
        """Run join_previous_giveaways when bot is ready"""
        asyncio.create_task(self.start_commands())

    """send commands"""

    @tasks.loop()
    async def send_commands(self):
        cmd = {}
        try:
            while not registry.all_ready_event.is_set():
                await asyncio.sleep(0.5)
            cnf = self.command_handler_settings
            # Commands wait in the queue while the gateway is down, so their order is kept
            await self.bot.wait_gateway_up()
            priority, _, cmd = await self.bot.ch.queue.get()
            # A confirmed sac pauses every command of this account, see CommandHandler.wait_sac_cooldown
            await self.bot.ch.wait_sac_cooldown()
            cmd_id = cmd.get("id")
            command_info = await self.bot.ch.get_command_info(cmd_id)
            custom_channel_id = cmd.get("channel")
            channel = None

            if custom_channel_id:
                try:
                    channel = await self.bot.fetch_channel(custom_channel_id)
                except Exception as e:
                    await self.bot.log(
                        f"Error - Failed to fetch channel with id {custom_channel_id}: {e}",
                        "#c25560",
                    )

            if priority != 0:
                while (
                    time.time() - self.bot.ch.cmds_state["global"]["last_ran"]
                ) < cnf.betweenCommands[0]:
                    await self.sleep_between_commands(cnf.betweenCommands)

            sleep_req, sleep_time = self.sleep_required()
            if sleep_req:
                await self.bot.log(
                    f"sleep required by {sleep_time}s (to prevent `slow down` message)",
                    "#8f6b09",
                )
                await self.bot.sleep_till([sleep_time, sleep_time + 0.4])
                self.command_times.clear()

            """Update Command state"""
            await self.bot.ch.upd_cmd_state(cmd_id)

            """Append to checks"""
            if cmd.get("checks") and cmd_id:
                in_queue = await self.bot.ch.search_checks(id=cmd_id)
                if not in_queue:
                    async with self.bot.ch._hold_or_create_lock():
                        self.bot.ch.checks.append(cmd)

            if custom_channel_id and channel is None:
                # Not sent to the main channel instead: the echo is waited for in the custom one
                await self.bot.log(
                    f"Warning - {cmd_id} not sent, channel {custom_channel_id} is unavailable",
                    "#e6e65a",
                )
            elif self.bot.settings_dict.useSlashCommands and cmd.get(
                "slash_cmd_name", False
            ):
                await await_send_gate(self.bot.account_index)
                await self.bot.wait_until_ready()
                with send_permission_context(self.bot.account_index):
                    await self.bot.slashCommandSender(
                        cmd["slash_cmd_name"],
                        command_info["log_color"],
                        channel=channel,
                    )
            else:
                await await_send_gate(self.bot.account_index)
                await self.bot.wait_until_ready()
                with send_permission_context(self.bot.account_index):
                    sent_message = await self.bot.send(
                        self.construct_command(
                            cmd, guild_id=channel.guild.id if channel else None
                        ),
                        command_info["log_color"],
                        channel=channel,
                    )
                if sent_message is not None:
                    # The HTTP send returned the message: that is the proof it went out, no echo is needed
                    cmd["sent_message_id"] = sent_message.id
                    cmd["sent_channel_id"] = sent_message.channel.id
                    cmd["sent_at"] = time.time()

            """add command to the deque"""
            self.command_times.append(time.time())

        except Exception as e:
            await self.bot.log(
                f"Error - send_commands() loop: {e}. {cmd.get('cmd_name', None)}",
                "#c25560",
            )
            await self.sleep_between_commands(cnf.betweenCommands)
        finally:
            # Sent or dropped, this copy is no longer pending, and block_till_send may start its echo wait
            self.bot.ch.finish_send(cmd)
            repeat_remaining = cmd.get("repeat_remaining", 1)
            if repeat_remaining > 1:
                next_cmd = cmd.copy()
                next_cmd["repeat_remaining"] = repeat_remaining - 1
                await self.bot.ch.put_queue(next_cmd)

    @tasks.loop(seconds=1)
    async def monitor_checks(self):
        try:
            delay = self.command_handler_settings.readdingToQueue
            current_time = datetime.now(timezone.utc)
            if (
                not self.bot.ch.command_handler_status.state
                or self.bot.ch.command_handler_status.sleep
                or self.bot.ch.command_handler_status.captcha
                or self.bot.ch.command_handler_status.safety_check
                or self.bot.ch.command_handler_status.hold_handler
                or self.bot.gateway_hold_active()
            ):
                self.calc_time += current_time - getattr(
                    self, "last_check_time", current_time
                )
            else:
                for command in self.bot.ch.checks[:]:
                    if time.time() < command.get("retry_at", 0):
                        # Out of re-queues for now: waiting out one cooldown before trying again
                        continue
                    if command.get("sent_message_id") is not None and self.bot.reply_fallback.enabled:
                        # The reply fallback owns a sent command: it reads history, then gives up
                        continue
                    await self.bot.ch.get_command_info(command["id"])
                    cnf = self.bot.ch.cmds_state[command["id"]]
                    # After an outage the cooldown restarts from the reconnect, not from the last send
                    last_ran = max(cnf["last_ran"], self.bot.gateway_back_at)
                    if (time.time() - last_ran > delay) and not (
                        cnf["in_queue"] or cnf["pending_send"]
                    ):
                        readds = command.get("readds", 0)
                        if readds >= MAX_CHECK_READDS:
                            # Stays in checks, so an owo reply still clears it; otherwise it is tried again later
                            wait = self.retry_wait(command["id"], delay)
                            command["readds"] = 0
                            command["retry_at"] = time.time() + wait
                            await self.bot.log(
                                f"Warning - {command['id']} got no reply after {readds} re-queues, retrying in {wait:.0f}s",
                                "#e6e65a",
                            )
                            continue
                        async with self.bot.ch._hold_or_create_lock():
                            self.bot.ch.checks.remove(command)
                        command.pop("retry_at", None)
                        command["readds"] = readds + 1
                        await self.bot.ch.put_queue(command)

                self.calc_time = timedelta(0)
            self.last_check_time = current_time
        except Exception as e:
            await self.bot.log(f"Error - monitor_checks(): {e}", "#c25560")


async def setup(bot):
    await bot.add_cog(Commands(bot))
