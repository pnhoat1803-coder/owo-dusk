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

# Most errors should be caught by frontend. Just rough checks.
# I like things named this way in code. So might be named entirely differently
# in the code compared to the settings.json file.


"""
Task :
Consider actually erroring instead of defaulting to values for non-cooldown parts
Also confirm all settings are updating.

also ensure types aren't none, and values actually exist
"""

from utils import lan
from utils.config_models import validators
from utils.config_models.helpers import expected_fetch


class GlobalSettings:
    def __init__(self, d: dict):
        self.typingIndicator = expected_fetch(d, "typingIndicator", bool)
        self.silentMessage = expected_fetch(d, "silentMessages", bool)
        self.offlineStatus = expected_fetch(d, "offlineStatus", bool)
        self.ocrApi = expected_fetch(d, "ocrApi", str)

        # Website Dashboard
        self.website = Website(expected_fetch(d, "website", dict))

        # Database
        self.database = Database(expected_fetch(d, "database", dict))

        # Account Delays
        self.account = Account(expected_fetch(d, "account", dict))

        # Text Commands
        self.textCommands = TextCommands(expected_fetch(d, "textCommands", dict))

        # Webhook Logging
        self.webhook = Webhook(expected_fetch(d, "webhook", dict))

        # Console commands
        self.console = Console(expected_fetch(d, "console", dict))

        # Captcha
        self.captcha = Captcha(expected_fetch(d, "captcha", dict))

        # Battery Check
        self.batteryCheck = BatteryCheck(expected_fetch(d, "batteryCheck", dict))

        # Channel Switcher
        self.channelSwitcher = ChannelSwitcher(
            expected_fetch(d, "channelSwitcher", dict)
        )

        # Diagnostics. Optional, so an older global_settings.json still loads
        self.debug = Debug(d["debug"] if isinstance(d.get("debug"), dict) else {})

        # Reply fallback, reads channel history for OwO replies the gateway missed. Optional, like debug
        self.replyFallback = ReplyFallback(
            d["replyFallback"] if isinstance(d.get("replyFallback"), dict) else {}
        )

        # Auto-stop after a run time, see core/auto_stop.py. Optional, a bad value keeps its default
        self.autoStop = AutoStop(d.get("autoStop"))

        # Level check: reads each account's OwO level from the owolvl card, see core/level_check.py. Optional
        self.levelCheck = LevelCheck(d.get("levelCheck"))

        # Gateway: what the client asks Discord for at login, see core/gateway_options.py. Optional, like debug
        self.gateway = Gateway(d.get("gateway"))


class Website:
    def __init__(self, d: dict):
        self.enabled = expected_fetch(d, "enabled", bool)
        self.port = expected_fetch(d, "port", int)
        self.refreshInterval = expected_fetch(d, "refreshInterval", int)
        self.password = expected_fetch(d, "password", str)
        self.enableHost = expected_fetch(d, "enableHost", bool)
        separate_process = d.get("separateProcess", True)
        self.separateProcess = separate_process if isinstance(separate_process, bool) else True
        # The IPv4 address the dashboard listens on, see utils/lan.py. A missing key takes the default 0.0.0.0
        # takes the default 0.0.0.0, enableHost is ignored
        self.host = lan.configured_host(d)
        # Dashboard-controlled bot, see website/bot_supervisor.py. All default to off
        self.restartOnCrash = validators.validate_flag(d.get("restartOnCrash", False), "website.restartOnCrash", False)
        self.startBotOnLaunch = validators.validate_flag(
            d.get("startBotOnLaunch", False), "website.startBotOnLaunch", False
        )
        self.stopBotWithDashboard = validators.validate_flag(
            d.get("stopBotWithDashboard", False), "website.stopBotWithDashboard", False
        )


class Database:
    def __init__(self, d: dict):
        self.logStatistics = expected_fetch(d, "logStatistics", bool)


class Account:
    def __init__(self, d: dict):
        self.startupDelay = expected_fetch(d, "startupDelay", list, float)
        self.reconnectMinInterval = (
            expected_fetch(d, "reconnectMinInterval", float)
            if "reconnectMinInterval" in d
            else 3.0
        )
        self.pauseTimeoutSeconds = validators.validate_seconds(
            d.get("pauseTimeoutSeconds", 60), "pauseTimeoutSeconds", 60
        )
        self.stallThresholdSeconds = validators.validate_seconds(
            d.get("stallThresholdSeconds", 600), "stallThresholdSeconds", 600
        )
        self.massDisconnectThreshold = (
            expected_fetch(d, "massDisconnectThreshold", int)
            if "massDisconnectThreshold" in d
            else 10
        )
        self.massDisconnectWindow = (
            expected_fetch(d, "massDisconnectWindow", float)
            if "massDisconnectWindow" in d
            else 5.0
        )
        self.reconnectBackoffBase = (
            expected_fetch(d, "reconnectBackoffBase", float)
            if "reconnectBackoffBase" in d
            else 30.0
        )
        self.reconnectBackoffJitter = (
            expected_fetch(d, "reconnectBackoffJitter", float)
            if "reconnectBackoffJitter" in d
            else 90.0
        )
        self.reconnectBackoffDuration = (
            expected_fetch(d, "reconnectBackoffDuration", float)
            if "reconnectBackoffDuration" in d
            else 120.0
        )
        self.presenceLoopSeconds = (
            expected_fetch(d, "presenceLoopSeconds", int)
            if "presenceLoopSeconds" in d
            else 90
        )
        self.safetyCheckLoopSeconds = (
            expected_fetch(d, "safetyCheckLoopSeconds", int)
            if "safetyCheckLoopSeconds" in d
            else 30
        )
        self.waveSize = (
            expected_fetch(d, "waveSize", int) if "waveSize" in d else 5
        )
        self.waveDelay = (
            expected_fetch(d, "waveDelay", list, float)
            if "waveDelay" in d
            else [3.0, 6.0]
        )
        self.loginMaxRetries = (
            expected_fetch(d, "loginMaxRetries", int)
            if "loginMaxRetries" in d
            else 3
        )
        self.globalRatePerSecond = (
            expected_fetch(d, "globalRatePerSecond", float)
            if "globalRatePerSecond" in d
            else 3.0
        )
        self.firstSendStaggerStep = (
            expected_fetch(d, "firstSendStaggerStep", float)
            if "firstSendStaggerStep" in d
            else 5.0
        )
        self.firstSendStaggerCap = (
            expected_fetch(d, "firstSendStaggerCap", float)
            if "firstSendStaggerCap" in d
            else 600.0
        )
        self.commandsStart = expected_fetch(d, "commandsStartDelay", list, float)
        self.commandsHandlerStart = expected_fetch(
            d, "commandsHandlerStartDelay", list, float
        )

        # Ensures all cooldowns are valid:
        # May want to consider namings in global_settings.json file.
        validators.validate_cooldown(self.startupDelay)
        validators.validate_cooldown(self.commandsStart)
        validators.validate_cooldown(self.commandsHandlerStart)


class TextCommands:
    def __init__(self, d: dict):
        self.prefix = expected_fetch(d, "prefix", str)
        self.stopCommand = expected_fetch(d, "commandToStopUser", str)
        self.startCommand = expected_fetch(d, "commandToStartUser", str)
        self.restartCommand = expected_fetch(d, "commandToRestartAfterCaptcha", str)
        self.switchChannelCommand = expected_fetch(d, "CommandToSwitchChannel", str)
        self.sleepCommand = expected_fetch(d, "commandToSleep", str)
        self.defaultSleepDuration = expected_fetch(d, "defaultSleepDuration", int)
        self.allowedUsers = expected_fetch(d, "allowedUsers", list, int)


class Webhook:
    def __init__(self, d: dict):
        self.enabled = expected_fetch(d, "enabled", bool)
        self.logCommandSend = expected_fetch(d, "logCommandSend", bool)
        self.webhookUrl = d.get("webhookUrl") or ""
        self.webhookCaptchaUrl = d.get("webhookCaptchaUrl") or ""
        self.pingUserId = d.get("webhookUserIdToPingOnCaptcha") or ""

        self.animalLog = AnimalLog(expected_fetch(d, "animalLog", dict))

        self.others = WebhookOthers(expected_fetch(d, "others", dict))


class AnimalLog:
    # Part of `Webhook` class
    def __init__(self, d: dict):
        self.enabled = expected_fetch(d, "enabled", bool)
        self.rank = AnimalLogRank(expected_fetch(d, "rank", dict))


class AnimalLogRank:
    # Part of `AnimalLog` class, defines what all animals can be logged.
    def __init__(self, d: dict):
        self._rank = [
            "common",
            "uncommon",
            "rare",
            "epic",
            "mythical",
            "gem",
            "legendary",
            "fabled",
            "hidden",
            "distorted",
        ]

        for rank in self._rank:
            setattr(self, rank, expected_fetch(d, rank, bool))


class WebhookOthers:
    # Part of `Webhook` class
    def __init__(self, d: dict):
        self.lootbox = expected_fetch(d, "logLootbox", bool)
        self.crate = expected_fetch(d, "logCrate", bool)
        self.logChannelSwitch = expected_fetch(d, "logChannelSwitch", bool)


class Console:
    # Console commands on captcha.
    def __init__(self, d: dict):
        self.onCaptcha = expected_fetch(d, "runConsoleCommandOnCaptcha", bool)
        self.onBan = expected_fetch(d, "runConsoleCommandOnBan", bool)

        self.captchaCommand = expected_fetch(d, "commandToRunOnCaptcha", str)
        self.banCommand = expected_fetch(d, "commandToRunOnBan", str)


class Captcha:
    def __init__(self, d: dict):
        self.openCaptchaWebsite = expected_fetch(d, "openCaptchaWebsite", bool)
        self.stopIfFailure = expected_fetch(d, "stopCodeIfFailedToSolve", bool)
        # Hard deadline per captcha episode, counted from the moment it is detected
        self.timeoutSeconds = validators.validate_positive_count(
            d.get("timeoutSeconds", 600), "captcha.timeoutSeconds", 600
        )
        # When true, a deadline that fires flags and reports the account
        flag_on_timeout = d.get("flagOnTimeout", True)
        self.flagOnTimeout = flag_on_timeout if isinstance(flag_on_timeout, bool) else True
        # "account": only the account that failed stops. "process": the whole program stops
        self.stopScope = (
            expected_fetch(d, "stopScope", str) if "stopScope" in d else "account"
        )
        validators.validate_stop_scope(self.stopScope)

        # Notifications
        self.notifications = Notifications(expected_fetch(d, "notifications", dict))

        # Play Audio
        self.playAudio = PlayAudio(expected_fetch(d, "playAudio", dict))

        # Toast and Popup
        self.toastOrPopup = ToastOrPopup(expected_fetch(d, "toastOrPopup", dict))

        # Termux specific
        self.termux = Termux(expected_fetch(d, "termux", dict))


class Notifications:
    # Part of `Captcha` class
    def __init__(self, d: dict):
        self.enabled = expected_fetch(d, "enabled", bool)
        self.captchaContent = expected_fetch(d, "captchaContent", str)
        self.bannedContent = expected_fetch(d, "bannedContent", str)

        self.reccur = Reccur(expected_fetch(d, "reccur", dict))


class Reccur:
    def __init__(self, d: dict):
        self.enabled = expected_fetch(d, "enabled", bool)
        self.timesToReccur = expected_fetch(d, "timesToReccur", int)


class PlayAudio:
    # Part of `Captcha` class
    def __init__(self, d: dict):
        self.enabled = expected_fetch(d, "enabled", bool)
        self.path = expected_fetch(d, "path", str)


class ToastOrPopup:
    def __init__(self, d: dict):
        self.enabled = expected_fetch(d, "enabled", bool)
        self.captchaContent = expected_fetch(d, "captchaContent", str)
        self.bannedContent = expected_fetch(d, "bannedContent", str)
        self.termuxToast = TermuxToast(expected_fetch(d, "termuxToast", dict))


class TermuxToast:
    def __init__(self, d: dict):
        self.backgroundColour = expected_fetch(d, "backgroundColour", str)
        self.textColour = expected_fetch(d, "textColour", str)
        self.position = expected_fetch(d, "position", str)


class Termux:
    # Part of `Captcha` class
    # Termux only part
    def __init__(self, d: dict):
        self.vibrate = Vibrate(expected_fetch(d, "vibrate", dict))
        self.textToSpeech = TextToSpeech(expected_fetch(d, "textToSpeech", dict))


class Vibrate:
    def __init__(self, d: dict):
        self.enabled = expected_fetch(d, "enabled", bool)
        self.time = expected_fetch(d, "time", int)


class TextToSpeech:
    def __init__(self, d: dict):
        self.enabled = expected_fetch(d, "enabled", bool)
        self.captchaContent = expected_fetch(d, "captchaContent", str)
        self.bannedContent = expected_fetch(d, "bannedContent", str)


class BatteryCheck:
    def __init__(self, d: dict):
        self.enabled = expected_fetch(d, "enabled", bool)
        self.minPercentage = expected_fetch(d, "minPercentage", int)
        self.refreshInterval = expected_fetch(d, "refreshInterval", int)

        validators.validate_frequency(self.minPercentage)


class ChannelSwitcher:
    def __init__(self, d: dict):
        self.enabled = expected_fetch(d, "enabled", bool)

        self.allUsers = expected_fetch(
            expected_fetch(d, "allUsers", dict), "channels", list, int
        )
        self.users = []
        for user_dict in expected_fetch(d, "users", list, dict):
            self.users.append(User(user_dict))

        self.interval = expected_fetch(d, "interval", list, float)
        self.delayBeforeSwitch = expected_fetch(d, "delayBeforeSwitch", list, float)

        validators.validate_cooldown(self.interval)
        validators.validate_cooldown(self.delayBeforeSwitch)


class User:
    def __init__(self, d: dict):
        self.userid = expected_fetch(d, "userid", int)
        self.channels = expected_fetch(d, "channels", list, int)


class ReplyFallback:
    # Part of `GlobalSettings` class: finds OwO replies the gateway did not deliver
    def __init__(self, d: dict):
        enabled = d.get("enabled", True)
        self.enabled = enabled if isinstance(enabled, bool) else True
        self.firstCheckAfter = validators.validate_positive_seconds(
            d.get("firstCheckAfter", 15), "replyFallback.firstCheckAfter", 15
        )
        self.checkEvery = validators.validate_positive_seconds(
            d.get("checkEvery", 15), "replyFallback.checkEvery", 15
        )
        self.giveUpAfter = validators.validate_positive_seconds(
            d.get("giveUpAfter", 60), "replyFallback.giveUpAfter", 60
        )
        self.historyLimit = validators.validate_positive_count(
            d.get("historyLimit", 15), "replyFallback.historyLimit", 15
        )
        self.maxConcurrentFetches = validators.validate_positive_count(
            d.get("maxConcurrentFetches", 4), "replyFallback.maxConcurrentFetches", 4
        )
        hold = d.get("holdWhileGatewayDown", False)
        self.holdWhileGatewayDown = hold if isinstance(hold, bool) else False
        # While the gateway is down, the OwO DM channel is read this often for captcha warnings. 0 means off
        self.dmCaptchaCheckEvery = validators.validate_seconds_or_off(
            d.get("dmCaptchaCheckEvery", 60), "replyFallback.dmCaptchaCheckEvery", 60
        )


class Debug:
    # Part of `GlobalSettings` class, diagnostics only
    def __init__(self, d: dict):
        reply_trace = d.get("replyTrace", True)
        self.replyTrace = reply_trace if isinstance(reply_trace, bool) else True
        self.replyTraceMaxMB = validators.validate_megabytes(
            d.get("replyTraceMaxMB", 20), "replyTraceMaxMB", 20
        )
        # Seconds an account's loop may stay unrun before its thread's stack is traced. 0 means off, see core/loop_watchdog.py
        self.blockedLoopStackAfter = validators.validate_seconds_or_off(
            d.get("blockedLoopStackAfter", 5), "debug.blockedLoopStackAfter", 5
        )
        # Frame counts per account and the READY line, see core/gateway_stats.py
        self.gatewayStats = validators.validate_flag(
            d.get("gatewayStats", True), "debug.gatewayStats", True
        )


class AutoStop:
    # Part of `GlobalSettings` class: stops the whole bot after a run time, see core/auto_stop.py
    def __init__(self, d):
        import logging

        if not isinstance(d, dict):
            logging.getLogger(__name__).warning(
                "autoStop is missing or not an object in config/global_settings.json; autoStop stays off"
            )
            d = {}
        self.enabled = validators.validate_flag(d.get("enabled", False), "autoStop.enabled", False)
        self.afterMinutes = validators.validate_whole_number(
            d.get("afterMinutes", 180), "autoStop.afterMinutes", 180, 1, 10080
        )
        self.graceSeconds = validators.validate_whole_number(
            d.get("graceSeconds", 90), "autoStop.graceSeconds", 90, 10, 600
        )
        self.logEveryMinutes = validators.validate_whole_number(
            d.get("logEveryMinutes", 30), "autoStop.logEveryMinutes", 30, 0, 1440
        )


class LevelCheck:
    # Part of `GlobalSettings` class: a low-frequency `owolvl` per account, read with OCR. Missing keys take their
    # defaults and bad values are clamped, so an old or hand-edited file never stops the bot
    DEFAULTS = {
        "enabled": True,
        "intervalMinutes": 360,
        "firstCheckMinDelayMinutes": 15,
        "firstCheckMaxDelayMinutes": 75,
        "maxChecksPerMinuteAllAccounts": 2,
        "replyTimeoutSeconds": 45,
        "command": "lvl",
        "tesseractPath": "third_party/tesseract/tesseract.exe",
        "saveFailedCards": False,
        "debug": True,
    }

    @staticmethod
    def _number(value, default, low, high):
        if isinstance(value, bool):
            return default
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return default
        if number != number or number in (float("inf"), float("-inf")):
            return default
        return min(max(number, low), high)

    def __init__(self, d):
        d = d if isinstance(d, dict) else {}
        defaults = self.DEFAULTS
        flag = d.get("enabled", defaults["enabled"])
        self.enabled = flag if isinstance(flag, bool) else defaults["enabled"]
        self.intervalMinutes = self._number(d.get("intervalMinutes"), defaults["intervalMinutes"], 60, 10080)
        low = self._number(d.get("firstCheckMinDelayMinutes"), defaults["firstCheckMinDelayMinutes"], 0, 10080)
        high = self._number(d.get("firstCheckMaxDelayMinutes"), defaults["firstCheckMaxDelayMinutes"], 0, 10080)
        self.firstCheckMinDelayMinutes, self.firstCheckMaxDelayMinutes = min(low, high), max(low, high)
        self.maxChecksPerMinuteAllAccounts = int(
            self._number(d.get("maxChecksPerMinuteAllAccounts"), defaults["maxChecksPerMinuteAllAccounts"], 1, 60)
        )
        self.replyTimeoutSeconds = self._number(d.get("replyTimeoutSeconds"), defaults["replyTimeoutSeconds"], 10, 120)
        command = d.get("command", defaults["command"])
        command = command.strip() if isinstance(command, str) else ""
        self.command = command if command and command.replace("_", "").isalnum() else defaults["command"]
        path = d.get("tesseractPath", defaults["tesseractPath"])
        self.tesseractPath = path.strip() if isinstance(path, str) else ""
        save = d.get("saveFailedCards", defaults["saveFailedCards"])
        self.saveFailedCards = save if isinstance(save, bool) else defaults["saveFailedCards"]
        debug = d.get("debug", defaults["debug"])
        self.debug = debug if isinstance(debug, bool) else defaults["debug"]


class Gateway:
    # Part of `GlobalSettings` class: what the client asks Discord for at login, see core/gateway_options.py.
    # Missing keys take their defaults, a bad value keeps its default with one warning. lean false restores the
    # library defaults (the bot's behaviour before this block existed).
    def __init__(self, d):
        d = d if isinstance(d, dict) else {}
        self.lean = validators.validate_flag(d.get("lean", True), "gateway.lean", True)
        self.chunkGuilds = validators.validate_flag(d.get("chunkGuilds", False), "gateway.chunkGuilds", False)
        self.guildSubscriptions = validators.validate_flag(
            d.get("guildSubscriptions", False), "gateway.guildSubscriptions", False
        )
        # Message cache size. 0 turns it off, and then the on_message_edit events of blackjack, slots and coinflip
        # never fire, because the library only dispatches an edit for a cached message (state.py parse_message_update)
        self.maxMessages = validators.validate_whole_number(d.get("maxMessages", 200), "gateway.maxMessages", 200, 0, 1000)
        # Experimental: the old name of guildSubscriptions. Only used by a library that does not have guildSubscriptions
        self.requestGuilds = validators.validate_flag(d.get("requestGuilds", True), "gateway.requestGuilds", True)
