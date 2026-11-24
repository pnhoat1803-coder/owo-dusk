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

import json
import logging
import math
import random
import time
from threading import Thread

import discord
import requests

from core import live_status
from core.account_registry import registry
from core.auto_stop import (
    STOPPED_REASON,
    register_client,
    register_thread,
    stop_requested,
    wait_for_shutdown,
)
from core.gateway_stats import writer as stats_writer
from core.global_rate_limiter import rate_limiter
from core.loop_watchdog import loop_watchdog
from core.reply_trace import reply_trace
import utils.run_log as run_log
import utils.system as syst
from utils.account_identity import expected_key
from utils import quote_source
from utils.loader import danger_settings_dict, global_settings_dict


def level_quotes_wanted():
    """True when the level grind will use the quote API: allow_quotes in danger.toml, and in config/settings.json
    lvlGrind enabled, useQuoteInstead on and quoteSource "api" (the default). Any read problem means no"""
    if not danger_settings_dict.get("allow_quotes"):
        return False
    try:
        with open("config/settings.json", "r", encoding="utf-8") as handle:
            grind = json.load(handle)["commands"]["lvlGrind"]
        return bool(grind.get("enabled")) and bool(grind.get("useQuoteInstead")) and grind.get("quoteSource", "api") == "api"
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def start_level_quotes():
    """Starts the quote refill only when the level grind will use the API. Returns True when it was started"""
    if not level_quotes_wanted():
        return False
    quote_source.quotes.start()
    return True


def fetch_json(url, description="data"):
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        syst.system.print_box(f"Failed to fetch {description}: {e}", "bold red")
        return {}


def _classify_login_error(error) -> str:
    if isinstance(error, discord.LoginFailure):
        return "token invalid or unauthorized"
    if isinstance(error, discord.NotFound):
        return "channel_id not found"
    if isinstance(error, discord.Forbidden):
        return "channel_id forbidden"

    error_text = str(error).lower()
    if any(
        phrase in error_text
        for phrase in ("cloudflare ban", "retry-after", "rate limit")
    ):
        return "rate limited by Discord"

    # Do not include exception text in reports; it may contain sensitive data.
    return f"{type(error).__name__}: login attempt failed"[:200]


def _finite_setting(value, default, minimum=0.0):
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(parsed) or parsed < minimum:
        return default
    return parsed


def _integer_setting(value, default, minimum=1):
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if parsed >= minimum else default


def _report_failure_and_exit(registry, wave_indices):
    if stop_requested():
        return  # autoStop stopped these accounts on purpose: they are not login failures
    failed_indices = [
        index
        for index in wave_indices
        if registry.get(index)["status"] in {"failed", "stopped"}
    ]
    if not failed_indices:
        return

    failed_index = failed_indices[0]
    account = registry.get(failed_index)
    reason = account["error"] or "login failed"
    userid = account["userid"] if account["userid"] is not None else "unknown"
    message = (
        "[OwO-Dusk] Account login failure; continuing with other accounts.\n"
        f"Failed account index: {failed_index}\n"
        f"Reason: {reason}\n"
        f"User ID (if known): {userid}\n"
        f"Wave: {wave_indices}"
    )

    try:
        from core.client import webhook_handler

        if webhook_handler is None:
            logging.error("Failure webhook handler is unavailable")
        else:
            while not rate_limiter.try_acquire():
                time.sleep(1.0 / rate_limiter.rate_per_second)
            webhook_handler.send({"content": message})

            queue = getattr(webhook_handler, "queue", None)
            if queue is not None:
                waiter = Thread(target=queue.join, daemon=True)
                waiter.start()
                waiter.join(timeout=10.0)
                if waiter.is_alive():
                    logging.error("Failure webhook delivery did not complete in time")
    except Exception as error:
        logging.error("Failure webhook send failed (%s)", type(error).__name__)

    if registry.all_stopped():
        logging.error("Every account failed or stopped; exiting")
        run_log.exit_process(0)  # writes the queued run log lines first, then ends the process


def _mark_login_failed(index, reason, client=None):
    registry.signal_stopped(index, reason)
    account_name = getattr(client, "username", None) or f"account #{index}"
    run_log.write(f"STOPPED - {account_name} - login failed: {reason}",
                  account=account_name, level="INFO", category="captcha")


def setup_discord_logging():
    """Sets up the discord library's logging once for the whole process. Client.run() would add a handler every
    time it is called, so the accounts call it with log_handler=None and this sets the logger up instead"""
    library_logger = logging.getLogger("discord")
    if any(not isinstance(handler, logging.NullHandler) for handler in library_logger.handlers):
        return  # already set up: a second handler would print every error once more
    for handler in list(library_logger.handlers):
        library_logger.removeHandler(handler)  # discord.py's NullHandler, replaced by the one below
    handler = logging.StreamHandler()  # sys.stderr, which the run log has replaced while it is on
    handler.setLevel(logging.ERROR)  # the console shows errors only, as before
    discord.utils.setup_logging(handler=handler, level=logging.ERROR, root=False)


def run_bot(
    token,
    channel_id,
    global_settings_dict,
    token_len,
    index,
    max_retries,
):
    last_reason = "client.run returned before ready"

    client = None
    for attempt in range(1, max_retries + 1):
        if stop_requested():
            registry.signal_stopped(index, STOPPED_REASON)  # autoStop has started: this account does not log in
            return
        try:
            from core.client import MyClient

            client = MyClient(
                token,
                channel_id,
                global_settings_dict,
                token_len,
                account_index=index,
            )
            if not register_client(index, client):
                registry.signal_stopped(index, STOPPED_REASON)
                return
            client.run(token, log_handler=None, reconnect=False)

            state = registry.get(index)
            if state["status"] == "failed":
                _mark_login_failed(index, state["error"] or "login failed", client)
                return
            if state["status"] in {"ready", "stopped"}:
                return
            last_reason = "client.run returned before ready"
        except Exception as error:
            # Keep the real traceback in the run log: _classify_login_error only
            # reports the exception type, which hides what actually broke
            logging.getLogger(__name__).exception(
                "account #%s login attempt raised", index
            )
            state = registry.get(index)
            if state["status"] == "failed":
                _mark_login_failed(index, state["error"] or "login failed", client)
                return
            if state["status"] in {"ready", "stopped"}:
                return
            last_reason = _classify_login_error(error)

        if stop_requested():
            return  # autoStop has started: no new login attempt, and no failure is reported for it
        if attempt < max_retries:
            time.sleep(2.0)

    _mark_login_failed(index, last_reason, client)


def run_bots(tokens_and_channels):
    setup_discord_logging()
    reply_trace.configure_from_settings(global_settings_dict)
    if reply_trace.enabled:
        # Both the console and the run log get the line, so a run with the trace on says so
        syst.system.console.print("replyTrace is ON, this costs a lot of CPU", style="tan")
        run_log.write("replyTrace is ON, this costs a lot of CPU")
    trace_path = reply_trace.start()
    if trace_path:
        syst.system.console.print(f"Reply trace is writing to {trace_path}", style="tan")
    stats_writer.configure_from_settings(global_settings_dict)
    stats_path = stats_writer.start()
    if stats_path:
        syst.system.console.print(f"Gateway stats are writing to {stats_path}", style="tan")
    # Stacks of account loops that stopped running are traced too, see core/loop_watchdog.py
    loop_watchdog.configure_from_settings(global_settings_dict)
    if reply_trace.active():
        loop_watchdog.start()

    threads = []
    total = len(tokens_and_channels)
    if isinstance(global_settings_dict, dict):
        account_settings = global_settings_dict.get("account", {})
    else:
        account_settings = getattr(global_settings_dict, "account", {})

    if isinstance(account_settings, dict):
        wave_size = account_settings.get("waveSize", 5)
        wave_delay = account_settings.get("waveDelay", [3.0, 6.0])
        login_max_retries = account_settings.get("loginMaxRetries", 3)
        global_rate = account_settings.get("globalRatePerSecond", 3.0)
        stagger_step = account_settings.get("firstSendStaggerStep", 5.0)
        stagger_cap = account_settings.get("firstSendStaggerCap", 600.0)
        reconnect_min_interval = account_settings.get("reconnectMinInterval", 3.0)
        mass_disconnect_threshold = account_settings.get(
            "massDisconnectThreshold", 10
        )
        mass_disconnect_window = account_settings.get("massDisconnectWindow", 5.0)
        reconnect_backoff_base = account_settings.get("reconnectBackoffBase", 30.0)
        reconnect_backoff_jitter = account_settings.get(
            "reconnectBackoffJitter", 90.0
        )
        reconnect_backoff_duration = account_settings.get(
            "reconnectBackoffDuration", 120.0
        )
        presence_loop_seconds = account_settings.get("presenceLoopSeconds", 90)
        safety_check_loop_seconds = account_settings.get(
            "safetyCheckLoopSeconds", 30
        )
    else:
        wave_size = getattr(account_settings, "waveSize", 5)
        wave_delay = getattr(account_settings, "waveDelay", [3.0, 6.0])
        login_max_retries = getattr(account_settings, "loginMaxRetries", 3)
        global_rate = getattr(account_settings, "globalRatePerSecond", 3.0)
        stagger_step = getattr(account_settings, "firstSendStaggerStep", 5.0)
        stagger_cap = getattr(account_settings, "firstSendStaggerCap", 600.0)
        reconnect_min_interval = getattr(
            account_settings, "reconnectMinInterval", 3.0
        )
        mass_disconnect_threshold = getattr(
            account_settings, "massDisconnectThreshold", 10
        )
        mass_disconnect_window = getattr(
            account_settings, "massDisconnectWindow", 5.0
        )
        reconnect_backoff_base = getattr(
            account_settings, "reconnectBackoffBase", 30.0
        )
        reconnect_backoff_jitter = getattr(
            account_settings, "reconnectBackoffJitter", 90.0
        )
        reconnect_backoff_duration = getattr(
            account_settings, "reconnectBackoffDuration", 120.0
        )
        presence_loop_seconds = getattr(
            account_settings, "presenceLoopSeconds", 90
        )
        safety_check_loop_seconds = getattr(
            account_settings, "safetyCheckLoopSeconds", 30
        )

    try:
        wave_size = max(1, int(wave_size))
    except (TypeError, ValueError, OverflowError):
        wave_size = 5
    if not isinstance(wave_delay, (list, tuple)) or len(wave_delay) < 2:
        wave_delay = [3.0, 6.0]
    try:
        wave_delay = [float(wave_delay[0]), float(wave_delay[1])]
    except (TypeError, ValueError, OverflowError):
        wave_delay = [3.0, 6.0]
    if (
        any(not math.isfinite(delay) or delay < 0.0 for delay in wave_delay)
        or wave_delay[0] > wave_delay[1]
    ):
        wave_delay = [3.0, 6.0]
    try:
        login_max_retries = max(1, int(login_max_retries))
    except (TypeError, ValueError, OverflowError):
        login_max_retries = 3
    try:
        rate_limiter.set_rate(float(global_rate))
    except (TypeError, ValueError, OverflowError):
        rate_limiter.set_rate(3.0)
    try:
        registry.stagger_step = max(0.0, float(stagger_step))
    except (TypeError, ValueError, OverflowError):
        registry.stagger_step = 5.0
    try:
        registry.stagger_cap = max(0.0, float(stagger_cap))
    except (TypeError, ValueError, OverflowError):
        registry.stagger_cap = 600.0

    reconnect_min_interval = _finite_setting(
        reconnect_min_interval, 3.0, minimum=0.01
    )
    mass_disconnect_threshold = _integer_setting(mass_disconnect_threshold, 10)
    mass_disconnect_window = _finite_setting(
        mass_disconnect_window, 5.0, minimum=0.01
    )
    reconnect_backoff_base = _finite_setting(reconnect_backoff_base, 30.0)
    reconnect_backoff_jitter = _finite_setting(reconnect_backoff_jitter, 90.0)
    reconnect_backoff_duration = _finite_setting(
        reconnect_backoff_duration, 120.0
    )
    presence_loop_seconds = max(
        90, _integer_setting(presence_loop_seconds, 90)
    )
    safety_check_loop_seconds = max(
        30, _integer_setting(safety_check_loop_seconds, 30)
    )

    from core.client import configure_task_loop_intervals
    from core.reconnect_governor import reconnect_governor

    reconnect_governor.set_min_interval(reconnect_min_interval)
    reconnect_governor.configure_detection(
        mass_threshold=mass_disconnect_threshold,
        mass_window_seconds=mass_disconnect_window,
        backoff_base_seconds=reconnect_backoff_base,
        backoff_jitter_seconds=reconnect_backoff_jitter,
        backoff_duration_seconds=reconnect_backoff_duration,
    )
    configure_task_loop_intervals(
        presence_seconds=presence_loop_seconds,
        safety_check_seconds=safety_check_loop_seconds,
    )

    indexed = list(enumerate(tokens_and_channels, start=1))
    for index, pair in indexed:
        # The key the account is listed under from now on: the user ID its token shows (utils/account_identity.py)
        registry.register(index, expected_key=expected_key(pair[0], index))
    live_status.write_now()  # the first live status file, before any login: every account is listed under its key
    # Level quotes: the pool fills while the accounts log in, not at the first level line (utils/quote_source.py)
    # Only when the level grind will use the API: otherwise the bot never contacts it
    start_level_quotes()

    for offset in range(0, total, wave_size):
        if stop_requested():
            break  # autoStop has started: no more accounts are started
        wave = indexed[offset : offset + wave_size]
        wave_indices = [index for index, _ in wave]

        for index, (token, channel_id) in wave:
            thread = Thread(
                target=run_bot,
                args=(
                    token,
                    channel_id,
                    global_settings_dict,
                    total,
                    index,
                    login_max_retries,
                ),
            )
            register_thread(index, thread)
            thread.start()
            threads.append(thread)

        completed = registry.wait_for_wave(wave_indices, timeout=180.0)
        if not completed:
            for index in wave_indices:
                if registry.get(index)["status"] == "pending":
                    registry.signal_stopped(index, "wave timeout")
                    logging.warning(
                        "STOPPED - account #%s - login failed: wave timeout", index
                    )
            _report_failure_and_exit(registry, wave_indices)

        if any(
            registry.get(index)["status"] in {"failed", "stopped"}
            for index in wave_indices
        ):
            _report_failure_and_exit(registry, wave_indices)

        if offset + wave_size < total:
            time.sleep(random.uniform(float(wave_delay[0]), float(wave_delay[1])))

    registry.set_all_ready()
    print(
        f"logging handlers: discord={len(logging.getLogger('discord').handlers)} "
        f"root={len(logging.getLogger().handlers)}"
    )

    for index, account in sorted(registry.snapshot().items()):
        print(
            f"Account {index}: status={account['status']} "
            f"userid={account['userid']} error={account['error']}"
        )

    for thread in threads:
        thread.join()
    wait_for_shutdown()  # when autoStop has stopped the accounts, its exit ends the process
