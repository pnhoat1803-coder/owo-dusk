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
 
# Standard Library
import argparse
import atexit
import os
import signal
import sys
import threading
import time
from pathlib import Path

# Run from the project folder wherever this file is started from. This must come before any project import
PROJECT_DIR = Path(__file__).resolve().parent
os.chdir(PROJECT_DIR)
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

try:  # Vietnamese text must come out right in consoles, pipes and log files alike, and must not crash the start-up
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Taken before the imports below, which take a few seconds: the auto-stop timer counts from here
STARTED_AT = time.monotonic()

# Third-Party Libraries
import discord
from discord import SyncWebhook

# First of all, so that everything printed from here on, imports included, is kept in logs/
import utils.run_log as run_log

run_log.start()

import core.auto_stop as auto_stop
import core.client as client
import core.live_status as live_status
import utils.bot_state as bot_state
import utils.lan as lan

# Local
import utils.system as syst
from core.bot_runner import fetch_json, run_bots
from utils.accounts_file import NO_ACCOUNTS_MESSAGE, read_accounts
from utils.captcha_solver.captchaly import probe_captchaly_balance_sync
from utils.captcha_solver.orchestrator import (
    CaptchaConfigError,
    build_orchestrator,
    hcaptcha_enabled,
)
from utils.captcha_solver.owo_verify import OwoAuth
from utils.constants import owo_dusk_api, owo_panel, version
from utils.database import create_database
from utils.errors import suppress_and_log, suppress_and_log_block
from utils.loader import (
    captcha_settings_dict,
    global_settings_dict,
    misc_dict,
)
from utils.quest_helper.quest import QuestHandler
from utils.runtime_handler import start_runtime_loop
from utils.webhook import webhookSender
from website import start_console_log_writer, web_start
from website.process_manager import DashboardProcessManager


def handle_sigint(signal_number, frame):
    print("\nCtrl+C detected. stopping code!")
    auto_stop.cancel_timer()  # the auto-stop timer, if it runs, ends with the process
    bot_state.set_exit_reason(bot_state.REASON_USER_STOP)  # last_exit.json says the user stopped the bot
    try:
        live_status.stop()  # last dashboard status file, running=false
    except Exception:
        pass
    run_log.exit_process(0)


@suppress_and_log("Setup Service")
def setup_and_start_services(no_dashboard=False):
    # clear console
    syst.system.clear()

    if not misc_dict["console"]["compactMode"]:
        syst.system.console.print(owo_panel)
        syst.system.console.rule(f"[bold blue1]version - {version}", style="navy_blue")

    # Sets up CTRL+C detection
    signal.signal(signal.SIGINT, handle_sigint)

    # Start battery check service
    syst.battery.start_battery_check()

    # Create database or modify if required
    create_database()

    # Weekly runtime thread
    start_runtime_loop()

    # Start website if enabled
    if global_settings_dict.website.enabled:
        try:
            live_status.start()  # utils/data/live_status.json, read by the dashboard process
        except Exception as error:
            print(f"Warning: dashboard live status is unavailable ({type(error).__name__}).")
        try:
            start_console_log_writer()
        except Exception as error:
            print(f"Warning: dashboard console logging is unavailable ({type(error).__name__}).")

        if no_dashboard:
            return  # the dashboard runs in its own process, and that process started this bot (website/bot_supervisor.py)
        if lan.dashboard_listening(global_settings_dict.website.port):
            print(f"Dashboard already running on port {global_settings_dict.website.port}")
            return
        # The address to listen on comes from website.host only. utils/lan.py makes the one bind decision: the configured
        # address is handed on, and the process that binds makes the same decision and gives the same reason
        site_host = global_settings_dict.website.host
        site_password = global_settings_dict.website.password
        dashboard_port = global_settings_dict.website.port
        dashboard_host, dashboard_reason = lan.effective_bind(site_host, site_password)
        dashboard_urls = lan.lan_urls(dashboard_host, dashboard_port)
        if global_settings_dict.website.separateProcess:
            DashboardProcessManager(
                port=dashboard_port,
                host=site_host,
                refresh_interval=global_settings_dict.website.refreshInterval,
                version=version,
            ).start()
        else:
            web_thread = threading.Thread(
                target=web_start,
                args=(
                    global_settings_dict.website.port,
                    site_host,
                    version,
                    site_password,
                    global_settings_dict.website.refreshInterval,
                ),
                daemon=True,
            )
            web_thread.start()
        # The same block the dashboard prints on its own: the LAN addresses, or the reason it stays on this machine
        syst.system.print_box(
            "\n".join(lan.startup_lines(dashboard_host, dashboard_port, dashboard_urls, dashboard_reason)),
            "dark_magenta",
        )


@suppress_and_log("Version Check")
def notify_version_changes():
    # Detect updates and notify
    version_json = fetch_json(f"{owo_dusk_api}/version.json", "version info")
    if "version" not in version_json:
        # No network or a bad answer: fetch_json has already said so, and there is nothing to compare
        return
    if syst.system.compare_versions(version, version_json["version"]):
        changelog = version_json.get("changelog", "")
        syst.system.print_box(
            f"""Update Detected - {version_json["version"]}
    Changelog:
        {changelog}""",
            "bold gold3",
        )
        syst.notification.notify(
            f"Content: {changelog}",
            f"New Update detected: {version_json['version']}",
        )
        if version_json.get("important_update"):
            syst.system.print_box(
                "It is recommended to update....", "bold light_yellow3"
            )


def _stop_from_dashboard(_reason):
    """A stop request from the dashboard (utils/bot_state.py): the same shutdown autoStop uses, ending with exit code 0"""
    auto_stop.request_stop(
        "stop request from the dashboard",
        global_settings_dict.autoStop.graceSeconds,
        exit_reason=bot_state.REASON_USER_STOP,
    )


def _note_crash(previous_hook):
    """Remembers an exception that ends the main thread, so last_exit.json says crash (utils/bot_state.py)"""

    def hook(exc_type, exc_value, exc_traceback):
        if not issubclass(exc_type, KeyboardInterrupt):
            bot_state.mark_crashed()
        previous_hook(exc_type, exc_value, exc_traceback)

    return hook


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="OwO-Dusk")
    parser.add_argument(
        "--max-runtime",
        type=auto_stop.parse_minutes,
        metavar="MINUTES",
        help="stop all accounts after this many minutes and exit; turns autoStop on for this run",
    )
    parser.add_argument(
        "--no-dashboard",
        action="store_true",
        help="do not start the dashboard: the dashboard process (python -m website.server) runs elsewhere",
    )
    # Unknown arguments are ignored, so launch commands written before this option keep working
    args, _unknown = parser.parse_known_args(argv)
    return args


def start_owodusk(max_runtime=None, no_dashboard=False):
    # Checked before anything starts: no accounts is a message and a clean exit, not a crash
    tokens_and_channels = read_accounts("tokens.txt")
    if not tokens_and_channels:
        print(NO_ACCOUNTS_MESSAGE)
        return
    supervised = os.environ.get(bot_state.SUPERVISED_ENV) == "1"  # set when the dashboard started this bot
    no_dashboard = no_dashboard or supervised
    run_id = os.environ.get(bot_state.RUN_ID_ENV, "")
    if not bot_state.valid_run_id(run_id):
        run_id = bot_state.new_run_id()
    try:
        bot_state.claim_pid_file(run_id, supervised)  # one bot per project: utils/data/bot.pid
    except bot_state.BotAlreadyRunning as error:
        print(f"{error}. Stop it first: with Stop on the dashboard, or with Ctrl+C in its window.")
        run_log.exit_process(2)
    bot_state.begin_run(run_id)
    run_log.add_exit_hook(bot_state.on_exit)  # last_exit.json and bot.pid, on every exit that ends the process
    atexit.register(bot_state.on_exit)  # a normal end of the program
    sys.excepthook = _note_crash(sys.excepthook)
    bot_state.start_stop_watch(run_id, _stop_from_dashboard)  # a stop request from the dashboard, checked every 2 s

    syst.notification.notify(
        "OwO-Dusk starting... If an issue arises, see README.md",
        "Starting OwO-Dusk! :>",
    )
    # Start up tasks and services
    setup_and_start_services(no_dashboard)
    # Auto-stop, see core/auto_stop.py. Started after the console is cleared, so its first line stays on screen
    auto_stop.start_timer(global_settings_dict.autoStop, max_runtime, started_at=STARTED_AT)

    # Version Check
    notify_version_changes()

    token_len = len(tokens_and_channels)
    live_status.begin_run(token_len, run_id)  # the live status file carries this count from its first write

    syst.system.print_box(
        f"-Received {token_len} tokens.".center(syst.system.console_width - 2),
        "bold magenta",
    )

    if misc_dict["news"]:
        with suppress_and_log_block("Fetch News"):
            news_json = fetch_json(f"{owo_dusk_api}/news.json", "news")
            if news_json.get("available"):
                syst.system.print_box(
                    f"{news_json.get('content', 'no content found..? this is an error! should be safe to ignore')}".center(
                        syst.system.console_width - 2
                    ),
                    f"bold {news_json.get('color', 'white')}",
                    title=news_json.get("title", "???"),
                )

    syst.system.console.rule(style="navy_blue")

    client.webhook_handler = webhookSender(global_settings_dict.webhook.webhookUrl)
    client.global_quest_handler = QuestHandler(api=global_settings_dict.ocrApi)
    client.captcha_orchestrator = None
    # Deprecated alias, kept for code that still reads the old name
    client.hcaptcha_solver = None
    hcaptcha_on = hcaptcha_enabled(captcha_settings_dict)
    if captcha_settings_dict["image_solver"]["enabled"] or hcaptcha_on:
        syst.system.console.print(
            "Be Warned, Captcha solving is not well tested.. You are using on your own risk..",
            style="orange_red1",
        )
        if hcaptcha_on:
            try:
                orchestrator = build_orchestrator(
                    captcha_settings_dict,
                    owo_auth=OwoAuth(),
                    captchaly_probe=probe_captchaly_balance_sync,
                )
            except CaptchaConfigError as error:
                syst.system.console.print(
                    f"Captcha configuration error - {error}", style="bold red"
                )
                syst.system.console.print(
                    "No hcaptcha provider is usable, so the captcha cog cannot run.",
                    style="orange_red1",
                )
                run_log.exit_process(1)

            for note in orchestrator.notes:
                syst.system.console.print(f"[captcha] {note}", style="tan")
            for line in orchestrator.startup_lines():
                syst.system.console.print(line, style="tan")
            syst.system.console.print(
                f"Captcha provider order - {orchestrator.describe()}", style="tan"
            )
            client.captcha_orchestrator = orchestrator
            # Deprecated alias: a zero balance no longer stops the process, the
            # provider is dropped while building the orchestrator
            client.hcaptcha_solver = orchestrator

    if (
        global_settings_dict.captcha.toastOrPopup
        and not syst.system.on_mobile
        and not misc_dict["hostMode"]
    ):
        # In MacOS, for Popup to work correctly, it must be ran through
        # The main loop. For that reason, `run_bots` are ran through a thread
        from utils.system.popup import popup_main_loop

        bot_threads = threading.Thread(target=run_bots, args=(tokens_and_channels,))
        bot_threads.daemon = True
        bot_threads.start()

        popup_main_loop()
    else:
        run_bots(tokens_and_channels)


if __name__ == "__main__":
    _args = parse_args()
    start_owodusk(_args.max_runtime, no_dashboard=_args.no_dashboard)
