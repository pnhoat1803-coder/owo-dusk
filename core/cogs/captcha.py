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
import os
import re
import sys
import time
import tomllib

import aiohttp
import discord
from discord import DMChannel
from discord.ext import commands, tasks

from core.cogs._BASE import BaseCog
from core.send_gate import await_send_permission
from utils.captcha_solver.base import CaptchaTotalFailure
from utils.captcha_solver.orchestrator import hcaptcha_enabled
from utils.captcha_solver.yescaptcha import SOLVE_COST_POINTS
from utils.errors import suppress_and_log_block
import utils.run_log as run_log
from utils.system.notification import notify
from utils.system.system import on_mobile, run_system_command
from utils.timestamp import validate_snowflake

list_captcha = ["human", "captcha", "link", "letterword"]
# A verify message is only acted on if this account saw a captcha this recently, see Captcha.captcha_recent
CAPTCHA_RECENT_SECONDS = 15 * 60

# OwO's DM bodies that end or warn about a captcha episode, see _poll_owo_dm_for_confirmation
OWO_DM_SUCCESS_TEXT = "I have verified that you are human"
OWO_DM_FAILURE_TEXT = "Captchas failed to solve within 10 minutes"
OWO_DM_WARNING_TEXT = "Please make sure to solve captchas within 10 minutes"
# The confirmation is not instant: OwO sends it after its own verify, so poll for it
DM_POLL_INTERVAL_SECONDS = 5.0
DM_POLL_DEADLINE_SECONDS = 90.0


def classify_owo_dm(content):
    """Classify an OwO DM body: "failure", "success", "warning" or None.

    Precedence when several patterns appear: success > failure > warning.
    """
    text = content or ""
    if OWO_DM_SUCCESS_TEXT in text:
        return "success"
    if OWO_DM_FAILURE_TEXT in text:
        return "failure"
    if OWO_DM_WARNING_TEXT in text:
        return "warning"
    return None


def get_path(path):
    cur_dir = os.getcwd()
    if os.path.isfile(path):
        """See if complete path"""
        return path
    audio_folder_path = os.path.join(cur_dir, "audio", path)
    if os.path.isfile(audio_folder_path):
        """See if audio file is in audio folder"""
        return audio_folder_path
    file_in_cwd = os.path.join(cur_dir, path)
    if os.path.isfile(file_in_cwd):
        """See if audio file is in working directory"""
        return file_in_cwd
    """None otherwise"""
    return None


def clean(msg):
    return re.sub(r"[^a-zA-Z0-9]", "", msg)


if not on_mobile:
    # desktop
    from playsound3 import playsound


def load_json_dict(file_path="config/captcha.toml"):
    with open(file_path, "rb") as config_file:
        return tomllib.load(config_file)


cap_cnf_dict = load_json_dict()

if cap_cnf_dict:
    if cap_cnf_dict["image_solver"]["enabled"]:
        from utils.captcha_solver.image_captcha import solveImageCaptcha


def get_channel_name(channel):
    if isinstance(channel, DMChannel):
        return "owo DMs"
    return channel.name


def console_handler(cnf, captcha=True):
    # If captcha flag set to False, Ban command will be ran
    if cnf.onCaptcha and captcha:
        run_system_command(cnf.captchaCommand, timeout=5)
    elif cnf.onBan and not captcha:
        run_system_command(cnf.banCommand, timeout=5)


def get_reccur_sleep_time(times_to_reccur):
    if times_to_reccur > 600:
        # I wonder what would hapeen without this check.
        return 200
    return 600 / times_to_reccur


class Captcha(BaseCog):
    def __init__(self, bot):
        super().__init__(bot)
        self.sound = None
        self.reccured = 0
        self.content_to_notify = ""
        # Hard deadline task for the captcha episode being solved, see solve_deadline
        self.deadline_task = None
        # Guard against two solves for the same account at once
        self.hcaptcha_solve_in_progress = False
        self.captcha_site_opened = False
        # Set when an episode ran past its deadline; stays set for the account
        self.captcha_flag = False
        # Set while a verify message is being handled, so its copies are not handled again
        self.solve_running = False
        # When this account last saw a captcha. None once that captcha is solved
        self.captcha_seen_at = None
        # Snowflake of the newest OwO DM when this episode started; newer DMs are this episode's
        self.episode_start_message_id = None
        # Set when the OwO DM confirmed the solve, so the deadline cannot stop the account
        self.episode_dm_confirmed_at = None
        # Cached OwO DM channel id, resolved once per account
        self.owo_dm_channel_id = None

    def fetch_settings(self, cmd):
        return getattr(self.bot.settings_dict.commands, cmd)

    @property
    def captcha_settings(self):
        return self.bot.global_settings_dict.captcha

    @property
    def notification_settings(self):
        return self.captcha_settings.notifications

    @property
    def toast_settings(self):
        return self.captcha_settings.toastOrPopup

    @property
    def termux_settings(self):
        return self.captcha_settings.termux

    @property
    def webhook_settings(self):
        return self.bot.global_settings_dict.webhook

    async def get_webhook(self):
        webhook_url = self.webhook_settings.webhookCaptchaUrl
        if not isinstance(webhook_url, str):
            # Ensure webhook url is valid
            webhook_url = None
        elif "discord.com" not in webhook_url:
            # Only accept discord webhook.
            await self.bot.log(
                "webhook Url seems invalid",
                color="#e6e65a",
                level="WARNING",
                category="captcha",
            )
            webhook_url = None
        return webhook_url

    async def stop_on_failure(self, reason):
        """Stops only this account when stopCodeIfFailedToSolve is true and stopScope is "account".
        Otherwise the whole process stops, as it always did"""
        self._reset_episode_dm_state()
        if (
            self.captcha_settings.stopIfFailure
            and self.captcha_settings.stopScope == "account"
        ):
            await self.bot.stop_account(reason)
            return
        run_log.captcha_finish("FAILED", reason)
        run_log.exit_process(0)

    async def start_solve_deadline(self):
        """Arm the hard deadline for the episode that is starting now."""
        if self.deadline_task is not None and not self.deadline_task.done():
            if self.hcaptcha_solve_in_progress:
                # A duplicate captcha message must not extend the running episode
                return
            self.deadline_task.cancel()
        self.episode_dm_confirmed_at = None
        try:
            # The cursor must predate the solve, or a DM sent during it would look old
            await asyncio.wait_for(self._capture_episode_cursor(), timeout=10)
        except (asyncio.TimeoutError, TimeoutError):
            self.episode_start_message_id = None
        self.deadline_task = asyncio.create_task(self.solve_deadline())

    async def solve_deadline(self):
        """Ends the captcha episode after the configured deadline (600 s by default).

        Only a verified solve cancels this timer: a provider failure does not.
        """
        timeout = self.captcha_settings.timeoutSeconds
        await asyncio.sleep(timeout)
        await self.flag_captcha_timeout(timeout)

    async def _resolve_owo_dm_channel(self) -> int | None:
        """Return the cached OwO DM channel id, resolving it once per account if needed.

        Returns None when the channel cannot be resolved; the caller handles that.
        """
        if self.owo_dm_channel_id is not None:
            return self.owo_dm_channel_id
        cached = getattr(self.bot, "dm", None)
        if cached is not None:
            self.owo_dm_channel_id = cached.id
            return self.owo_dm_channel_id
        try:
            user = self.bot.get_user(self.bot.owo_bot_id)
            if user is None:
                user = await self.bot.fetch_user(self.bot.owo_bot_id)
            channel = getattr(user, "dm_channel", None)
            if channel is None:
                channel = await user.create_dm()
            self.owo_dm_channel_id = channel.id
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self.bot.log(
                f"[captcha] OwO DM channel unavailable ({type(error).__name__})",
                "#e6e65a",
            )
            return None
        return self.owo_dm_channel_id

    async def _get_owo_dm_channel(self):
        """Return the OwO DM channel object, or None when it cannot be resolved."""
        channel_id = await self._resolve_owo_dm_channel()
        if channel_id is None:
            return None
        channel = self.bot.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            return await self.bot.fetch_channel(channel_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            return None

    async def _capture_episode_cursor(self) -> int | None:
        """Record the newest OwO DM id; DMs with a newer snowflake belong to this episode."""
        self.episode_start_message_id = None
        channel = await self._get_owo_dm_channel()
        if channel is None:
            return None
        last_id = getattr(channel, "last_message_id", None)
        try:
            if last_id is None:
                async for message in channel.history(limit=1):
                    last_id = message.id
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            return None
        self.episode_start_message_id = last_id
        await self.bot.log(
            f"[captcha] cursor={last_id}",
            "#888888",
            level="DEBUG",
            category="captcha",
        )
        return last_id

    async def _poll_owo_dm_for_confirmation(
        self, deadline_seconds: float = DM_POLL_DEADLINE_SECONDS
    ) -> str:
        """Poll OwO DM history for this episode's confirmation message.

        Returns one of: "success" | "failure" | "warning" | "timeout".
        """
        cursor = self.episode_start_message_id
        if cursor is None:
            cursor = await self._capture_episode_cursor()
        if cursor is None:
            return "timeout"

        channel = await self._get_owo_dm_channel()
        if channel is None:
            return "timeout"

        warning_seen = False
        poll_error_logged = False
        iteration = 0
        deadline = time.monotonic() + max(0.0, float(deadline_seconds))
        while True:
            classified = "none"
            last_content = ""
            try:
                async for message in channel.history(
                    after=discord.Object(id=cursor), oldest_first=True
                ):
                    if message.id <= cursor:
                        # A message from before this episode started
                        continue
                    kind = classify_owo_dm(message.content)
                    classified = kind or "none"
                    last_content = message.content or ""
                    if kind == "failure":
                        return "failure"
                    if kind == "success":
                        return "success"
                    if kind == "warning":
                        warning_seen = True
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if not poll_error_logged:
                    poll_error_logged = True
                    await self.bot.log(
                        f"[captcha] DM poll error ({type(error).__name__})",
                        "#e6e65a",
                    )
            iteration += 1
            raw = (
                f" | dm_raw={last_content[:500]}"
                if run_log.include_dm_raw() and last_content
                else ""
            )
            await self.bot.log(
                f"[captcha] DM poll iter={iteration} since={cursor} classified={classified}{raw}",
                "#888888",
                level="DEBUG",
                category="captcha_dm",
            )
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(DM_POLL_INTERVAL_SECONDS)
        return "warning" if warning_seen else "timeout"

    async def _confirm_episode_from_owo_dm(self):
        """Use the OwO DM as the success signal after a verified solve.

        This is the path that can clear the episode when the live listener never saw
        the DM, so it performs the listener's cleanup itself on success.
        """
        if self.bot.stopped:
            return
        if getattr(self.bot, "misc", {}).get("hostMode"):
            return
        result = await self._poll_owo_dm_for_confirmation()
        started = self.captcha_seen_at
        total_s = int(time.time() - started) if started else 0
        await self.bot.log(
            f"[captcha] episode total_s={total_s} outcome={result}",
            "#5fd700" if result == "success" else "#e6e65a",
            level="INFO",
            category="captcha",
        )
        if result == "success":
            self.bot.ch.command_handler_status.captcha = False
            await self.bot.log(
                "[captcha] state transition field=status.captcha old=True new=False",
                "#888888",
                level="DEBUG",
                category="captcha",
            )
            self.captcha_seen_at = None
            self.captcha_site_opened = False
            if self.deadline_task is not None and not self.deadline_task.done():
                self.deadline_task.cancel()
            self.episode_dm_confirmed_at = time.time()
            await self.bot.log("[captcha] DM confirmed solved", "#5fd700")
        elif result == "failure":
            await self.bot.log(
                "[captcha] OwO reported the captcha failed - stopping", "#d70000"
            )
            if self.webhook_settings.enabled:
                with suppress_and_log_block("Captcha OwO Failure Webhook"):
                    await self.bot.send_webhook(
                        "on_captcha_solve_fail",
                        webhook_url=await self.get_webhook(),
                        providers="owo_reported_failure",
                    )
            await self.stop_on_failure("OwO reported captcha failure")
        elif result == "warning":
            await self.bot.log(
                "[captcha] DM warning only, no confirmation", "#e6e65a"
            )
        else:
            await self.bot.log(
                "[captcha] DM poll timeout, deferring to deadline timer", "#e6e65a"
            )

    def _reset_episode_dm_state(self):
        """Forget this episode's cursor and confirmation; the channel id stays cached."""
        self.episode_start_message_id = None
        self.episode_dm_confirmed_at = None

    async def flag_captcha_timeout(self, timeout):
        """Cancel the in-flight solve, flag the account and report the timeout."""
        if self.bot.stopped or not self.bot.ch.command_handler_status.captcha:
            # The episode ended while the timer was sleeping
            return

        if self.episode_dm_confirmed_at is not None:
            # The DM poller already saw OwO's confirmation; the episode is over
            await self.bot.log(
                "[captcha] timeout fired but DM already confirmed; skipping stop",
                "#e6e65a",
            )
            return

        orchestrator = getattr(self.bot, "captcha_orchestrator", None)
        if orchestrator is not None:
            cancelled = orchestrator.cancel_in_flight()
            if cancelled is not None:
                # Let the cancelled solve unwind so its report and session cleanup land
                await asyncio.wait([cancelled], timeout=5)

        status = self.bot.ch.command_handler_status
        # stall_watchdog and level_check key off status.captcha, which stays True
        # for the whole pause. If a resume path ever resets status.captcha while
        # captcha_flag is still set, both consumers must read captcha_flag instead.
        status.captcha = True
        await self.bot.log(
            "[captcha] state transition field=status.captcha old=True new=True",
            "#888888",
            level="DEBUG",
            category="captcha",
        )
        providers = (
            orchestrator.reports_text() if orchestrator is not None else "no solver"
        )
        reason = f"captcha not solved within {timeout}s"
        started = self.captcha_seen_at
        total_s = int(time.time() - started) if started else 0
        await self.bot.log(
            f"[captcha] episode total_s={total_s} outcome=timeout",
            "#d70000",
            level="INFO",
            category="captcha",
        )

        if self.captcha_settings.flagOnTimeout:
            self.captcha_flag = True
            status.captcha_flag = True
            await self.bot.log(f"{reason} - account flagged", "#d70000")
            await self.bot.log(f"providers -> {providers}", "#d70000")
        else:
            await self.bot.log(f"{reason} - account paused", "#d70000")

        if self.webhook_settings.enabled and self.captcha_settings.flagOnTimeout:
            with suppress_and_log_block("Captcha Timeout Webhook"):
                await self.bot.send_webhook(
                    "on_captcha_timeout_flag",
                    webhook_url=await self.get_webhook(),
                    providers=providers,
                    elapsed=f"{timeout}s",
                    pingid=(
                        self.webhook_settings.pingUserId
                        if validate_snowflake(self.webhook_settings.pingUserId)
                        else None
                    ),
                )

        if self.captcha_settings.stopIfFailure:
            await self.stop_on_failure(reason)
        else:
            run_log.captcha_finish("FAILED", reason)
            await self.bot.log(
                "stopCodeIfFailedToSolve is off: account paused, process kept running",
                "#e6e65a",
            )

    async def report_solve_failure(self, message, providers):
        """Log, notify and stop after every provider failed to solve the captcha."""
        if self.deadline_task is not None and not self.deadline_task.done():
            self.deadline_task.cancel()
        self._reset_episode_dm_state()
        await self.bot.log("FAILED to solve hcaptcha", "#d70000")
        await self.bot.log(f"providers -> {providers}", "#d70000")
        self.captcha_handler(message.channel, "Link")
        print("stopping code.... Reason -> Failed Hcaptcha attempt")
        if self.webhook_settings.enabled:
            with suppress_and_log_block("Captcha Solve Fail Webhook"):
                await self.bot.send_webhook(
                    "on_captcha_solve_fail",
                    webhook_url=await self.get_webhook(),
                    providers=providers,
                    pingid=(
                        self.webhook_settings.pingUserId
                        if validate_snowflake(self.webhook_settings.pingUserId)
                        else None
                    ),
                )
        await self.stop_on_failure("FAILED to solve hcaptcha")

    async def handle_solve_success(self, orchestrator):
        """Log what a verified solve left behind and act on the balance."""
        balance_data = orchestrator.balance_summary()
        if balance_data is None:
            return
        balance, solves_left = balance_data
        await self.bot.log(
            f"solved, {solves_left} solves left (balance: {balance})",
            "#d70000",
        )
        await self.handle_credits_exhausted(balance)

    async def handle_credits_exhausted(self, balance):
        """Handle the balance a solve left behind.

        Called with the balance of the provider that solved. With a solve or
        more left it returns at once; when the balance cannot pay for another
        solve it pauses or stops, and stopScope governs that stop exactly as it
        governs the solve-failure path: "account" pauses this account (the
        process itself ends only when every account is stopped, see
        core/client.py stop_account), "process" ends the program.
        stopCodeIfFailedToSolve = false only pauses.
        """
        if balance // SOLVE_COST_POINTS >= 1:
            return
        self.bot.ch.command_handler_status.captcha = True
        await self.bot.log(
            f"credits exhausted - balance: {balance}, stopping...",
            "#d70000",
        )
        if self.webhook_settings.enabled:
            await self.bot.send_webhook(
                "on_captcha_solve_no_credits",
                webhook_url=await self.get_webhook(),
                pingid=(
                    self.webhook_settings.pingUserId
                    if validate_snowflake(self.webhook_settings.pingUserId)
                    else None
                ),
            )
        credits_reason = f"credits exhausted - balance: {balance}"
        if not self.captcha_settings.stopIfFailure:
            run_log.captcha_finish("FAILED", credits_reason)
            await self.bot.log(
                "stopCodeIfFailedToSolve is off: account paused, process kept running",
                "#e6e65a",
            )
        elif self.captcha_settings.stopScope == "account":
            await self.bot.stop_account(credits_reason)
        else:
            run_log.captcha_finish("FAILED", f"{credits_reason}, process stopping")
            run_log.exit_process(0)

    @tasks.loop()
    async def reccur_notifications(self):
        if self.content_to_notify:
            notify(self.content_to_notify, f"Captcha - {self.bot.username}!")
            self.reccured += 1

        times_to_reccur = self.notification_settings.reccur.timesToReccur

        if self.reccured == times_to_reccur:
            self.reccur_notifications.cancel()

        await asyncio.sleep(get_reccur_sleep_time(times_to_reccur))

    def captcha_handler(self, channel, captcha_type):
        if self.bot.misc["hostMode"]:
            return
        channel_name = get_channel_name(channel)
        content_type = (
            "captchaContent" if not captcha_type == "Ban" else "bannedContent"
        )
        url = "https://owobot.com/captcha"

        """Notifications"""
        if self.notification_settings.enabled:
            notification_content = getattr(
                self.notification_settings, content_type
            ).format(
                username=self.bot.username,
                channelname=channel_name,
                captchatype=captcha_type,
            )

            if self.notification_settings.reccur.enabled:
                self.reccured = 0
                self.content_to_notify = notification_content
                try:
                    self.reccur_notifications.start()
                except Exception:
                    # In case code sends one command after captcha, triggering captcha message twice.
                    pass
            else:
                notify(notification_content, f"Captcha - {self.bot.username}!")

        """Play audio file"""
        """
        TASK: add two checks, check the path for the file in both outside utils folder
        and in owo-dusk folder
        +
        better error handling for missing PATH
        """
        if self.captcha_settings.playAudio.enabled:
            with suppress_and_log_block("Captcha Play Audio"):
                path = get_path(self.captcha_settings.playAudio.path)
                if on_mobile:
                    run_system_command(
                        f"termux-media-player play {path}", timeout=5, retry=True
                    )
                else:
                    self.sound = playsound(path, block=False)

        """Toast/Popup"""
        if self.toast_settings.enabled:
            with suppress_and_log_block("Captcha Toast"):
                if on_mobile:
                    settings = self.toast_settings.termuxToast
                    run_system_command(
                        f"termux-toast -c {settings.textColour} -b {settings.backgroundColour} -g {settings.position} '{getattr(self.toast_settings, content_type).format(username=self.bot.username, channelname=channel_name, captchatype=captcha_type)}'",
                        timeout=5,
                        retry=True,
                    )
                else:
                    self.bot.add_popup_queue(
                        self.bot.user.name, channel_name, captcha_type
                    )

        if on_mobile:
            """Termux - Vibrate"""
            if self.termux_settings.vibrate.enabled:
                run_system_command(
                    f"termux-vibrate -f -d {self.termux_settings.vibrate.time * 1000}",
                    timeout=5,
                    retry=True,
                )

            """Termux - TTS"""
            if self.termux_settings.textToSpeech.enabled:
                run_system_command(
                    f"termux-tts-speak {getattr(self.termux_settings.textToSpeech, content_type)}",
                    timeout=7,
                    retry=False,
                )

        """Open captcha website"""
        if self.captcha_settings.openCaptchaWebsite and not self.captcha_site_opened:
            with suppress_and_log_block("Captcha Open Website"):
                if on_mobile:
                    run_system_command(f"termux-open {url}", timeout=5, retry=True)
                else:
                    if sys.platform.startswith("win"):
                        run_system_command(f"start {url}", timeout=5, retry=True)
                    elif sys.platform == "darwin":
                        # Macos
                        run_system_command(f"open {url}", timeout=5, retry=True)
                    else:
                        # Linux
                        run_system_command(f"xdg-open {url}", timeout=5, retry=True)
                self.captcha_site_opened = True

    def captcha_recent(self):
        """True when this account saw a captcha in the last CAPTCHA_RECENT_SECONDS and it is not solved yet"""
        return (
            self.captcha_seen_at is not None
            and time.time() - self.captcha_seen_at < CAPTCHA_RECENT_SECONDS
        )

    async def handle_solves(self):
        if self.bot.misc["hostMode"]:
            return

        """Play Audio"""
        if self.captcha_settings.playAudio.enabled:
            with suppress_and_log_block("Captcha Stop Audio"):
                if on_mobile:
                    run_system_command(
                        "termux-media-player stop", timeout=5, retry=True
                    )
                else:
                    if self.sound is not None:
                        if self.sound.is_alive():
                            self.sound.stop()

        """Reccurrring notification"""
        if (
            self.notification_settings.enabled
            and self.notification_settings.reccur.enabled
        ):
            try:
                self.reccur_notifications.cancel()
            except Exception:
                pass

        if self.deadline_task is not None and not self.deadline_task.done():
            self.deadline_task.cancel()
        self._reset_episode_dm_state()

    @commands.Cog.listener()
    async def on_message(self, message):
        self.last_msg = time.time()

        # This is likely a part of temporary fix, I forgot.
        # Doesn't hurt letting it stay!
        if not self.bot.dm:
            if message.author.id == self.bot.owo_bot_id:
                self.bot.dm = await message.author.create_dm()
            else:
                # Safe, since only owobot will send captcha messages.
                return

        if (
            message.channel.id == self.bot.dm.id
            and message.author.id == self.bot.owo_bot_id
        ):
            if "I have verified that you are human! Thank you! :3" in message.content:
                if self.episode_dm_confirmed_at is not None:
                    # The DM poller already handled this episode
                    return
                if self.solve_running:
                    # A copy of this verify message that is already being handled
                    return
                if not self.captcha_recent():
                    await self.bot.log(
                        f"{self.bot.username}: ignoring verify message without an active captcha",
                        "#e6e65a",
                    )
                    return
                self.solve_running = True
                # This captcha is over: a later copy of the verify message is not a new solve
                self.captcha_seen_at = None
                try:
                    time_to_sleep = self.bot.random_float(
                        self.bot.settings_dict.cooldowns.captchaRestart
                    )
                    await self.bot.log(
                        f"Captcha solved! - sleeping {time_to_sleep}s before restart.",
                        "#5fd700",
                    )
                    run_log.captcha_finish("RESOLVED", "owo bot confirmed the solve")
                    await asyncio.sleep(time_to_sleep)
                finally:
                    # The restart happens after the one sleep, whatever the logging or the wait raised
                    self.bot.ch.command_handler_status.captcha = False
                    self.solve_running = False
                self.bot.db.update_captcha_db()
                await self.handle_solves()
                self.captcha_site_opened = False
                if self.webhook_settings.enabled:
                    await self.bot.send_webhook(
                        "on_captcha_solve",
                        webhook_url=await self.get_webhook(),
                        captcha_url=message.jump_url,
                        pingid=(
                            self.webhook_settings.pingUserId
                            if validate_snowflake(self.webhook_settings.pingUserId)
                            else None
                        ),
                    )
                return

        channels = self.bot.active_channel_ids

        if message.channel.id in channels and message.author.id == self.bot.owo_bot_id:
            components = message.components
            content = clean(message.content)
            # Checks if `Verify` button exists
            has_verify_button = (
                components
                and components[0].children
                and getattr(components[0].children[0], "label", None) == "Verify"
            )

            has_warning_attachment = "⚠️" in message.content and message.attachments

            contains_captcha_word = any(word in content for word in list_captcha)

            if has_verify_button or has_warning_attachment or contains_captcha_word:
                if not get_channel_name(message.channel) == "owo DMs":
                    if not self.bot.names_us(message):
                        return
                self.bot.ch.command_handler_status.captcha = True
                self.captcha_seen_at = time.time()
                run_log.captcha_start()
                await self.bot.log("Captcha detected!", "#d70000")
                image_captcha = False
                if message.attachments:
                    image_captcha = True
                cap_dict = self.bot.captcha_settings_dict
                orchestrator = getattr(self.bot, "captcha_orchestrator", None)
                hcaptcha_on = hcaptcha_enabled(cap_dict)
                # Only a hosted hCaptcha solve has a deadline; an image captcha
                # and a notification-only pass do not
                hosted_solve = bool(
                    orchestrator is not None and hcaptcha_on and not image_captcha
                )
                if hosted_solve:
                    await self.start_solve_deadline()

                if cap_dict["notifications"]["notify_when_attempting_to_solve"] or not (
                    hcaptcha_on or cap_dict["image_solver"]["enabled"]
                ):
                    self.captcha_handler(message.channel, "Link")
                elif (image_captcha and not cap_dict["image_solver"]["enabled"]) or (
                    not image_captcha and not hcaptcha_on
                ):
                    self.captcha_handler(message.channel, "Link")

                if self.webhook_settings.enabled:
                    await self.bot.send_webhook(
                        "on_captcha",
                        webhook_url=await self.get_webhook(),
                        captcha_url=message.jump_url,
                        pingid=(
                            self.webhook_settings.pingUserId
                            if validate_snowflake(self.webhook_settings.pingUserId)
                            else None
                        ),
                    )
                console_handler(self.bot.global_settings_dict.console)

                if hosted_solve:
                    if not self.hcaptcha_solve_in_progress:
                        self.hcaptcha_solve_in_progress = True
                        await self.bot.log("Attempting to solve hcaptcha", "#656b66")
                        await self.bot.log(
                            f"[captcha] provider order={[getattr(p, 'name', '?') for p in getattr(orchestrator, 'providers', [])]}",
                            "#888888",
                            level="DEBUG",
                            category="captcha",
                        )
                        try:
                            await orchestrator.solve_with_fallback(
                                self.bot.local_headers, self.bot.user.id
                            )
                        except CaptchaTotalFailure as failure:
                            await self.report_solve_failure(message, failure.summary())
                            return
                        except asyncio.CancelledError:
                            raise
                        except Exception as error:
                            # A broken orchestrator must still end the episode
                            detail = f"{type(error).__name__}: {error}"
                            await self.report_solve_failure(message, detail)
                            return
                        finally:
                            self.hcaptcha_solve_in_progress = False

                        await self.handle_solve_success(orchestrator)
                        chosen = getattr(orchestrator, "last_provider", None)
                        await self.bot.log(
                            f"[captcha] solve returned provider={getattr(chosen, 'name', 'none')}",
                            "#888888",
                            level="DEBUG",
                            category="captcha",
                        )
                        if not self.bot.stopped:
                            await self._confirm_episode_from_owo_dm()

                elif cap_dict["image_solver"]["enabled"] and image_captcha:
                    await self.bot.log("Attempting to solve image captcha", "#656b66")
                    letters = int(
                        re.findall(
                            r"(\d+)(?=letterword)", clean(message.content.lower())
                        )[0]
                    )
                    ans = await solveImageCaptcha(
                        message.attachments[0].url, letters, self.bot.session
                    )
                    if ans and not self.bot.stopped:
                        await self.bot.log(
                            f"answer of image captcha -> {ans}", "#656b66"
                        )
                        await await_send_permission(
                            self.bot.account_index, stagger_first_send=False
                        )
                        try:
                            await message.author.send(ans)
                        except (
                            discord.HTTPException,
                            aiohttp.ClientError,
                            ConnectionError,
                            OSError,
                            discord.ConnectionClosed,
                            discord.GatewayNotFound,
                        ):
                            await asyncio.sleep(2.0)
                            return

            elif (
                "You have been banned for" in message.content
                and self.bot.names_us(message)
            ):
                self.bot.ch.command_handler_status.captcha = True
                await self.bot.log("Ban detected!", "#d70000")
                self.captcha_handler(message.channel, "Ban")
                console_handler(self.bot.global_settings_dict.console, captcha=False)
                if self.webhook_settings.enabled:
                    await self.bot.send_webhook(
                        "on_captcha_ban",
                        webhook_url=await self.get_webhook(),
                        captcha_url=message.jump_url,
                        pingid=(
                            self.webhook_settings.pingUserId
                            if validate_snowflake(self.webhook_settings.pingUserId)
                            else None
                        ),
                    )


async def setup(bot):
    await bot.add_cog(Captcha(bot))
