"""Health rules of the read-only dashboard.

Thresholds come from config/dashboard_view.json, a file people edit by hand. The web never writes it.
Every rule adds a short human-readable reason. health is the worst level reached: good < warn < bad.
"unknown" is used when the bot gives no live data for the account.
"""

import json
from pathlib import Path

DEFAULTS = {
    "lastHuntWarnSeconds": 300,
    "lastHuntBadSeconds": 900,
    "inboundWarnKbMin": 300,
    "inboundBadKbMin": 700,
    "captchaWarnPerDay": 1,
    "captchaBadPerDay": 3,
    "errorsWarnPerHour": 3,
    "errorsBadPerHour": 10,
    "huntsSinceStartWarn": 250,
    # Captcha waves were seen near 280 hunts after a clean start. The chip is shown between these two numbers
    "huntsSinceStartWarnUntil": 330,
}

CONFIG_PATH = Path("config") / "dashboard_view.json"
ORDER = {"good": 0, "unknown": 1, "warn": 2, "bad": 3}
# States where "no hunt for a while" is normal
QUIET_STATES = {"sleeping", "paused", "captcha", "stopped", "offline", "starting", "connecting", "reconnecting"}


def load_thresholds(path=CONFIG_PATH):
    """Defaults, overridden by the numbers in the file. A missing or broken file keeps the defaults"""
    values = dict(DEFAULTS)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return values
    if isinstance(data, dict):
        # refreshSeconds is not a threshold: how often the dashboard rebuilds its snapshot (at least 2).
        # When the key is missing, website.refreshInterval of global_settings.json is used
        for key in list(DEFAULTS) + ["refreshSeconds"]:
            value = data.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                values[key] = value
    return values


def _worse(current, new):
    return new if ORDER[new] > ORDER[current] else current


def _minutes(seconds):
    return f"{seconds / 60:.0f} min" if seconds >= 120 else f"{seconds:.0f} s"


def evaluate(entry, cfg, run_age_s=None):
    """entry: dict with state, hunts_total, last_hunt_ago_s, inbound_kb_min, captcha_today, errors_1h, reconnects_1h.
    Returns (health, reasons). run_age_s: seconds since the bot started, None when unknown."""
    state = entry.get("state")
    if state in (None, "offline", "unknown"):
        return "unknown", ["No live data from the bot"]
    health = "good"
    reasons = []

    if state == "captcha":
        health = _worse(health, "bad")
        reasons.append("Captcha is waiting to be solved")
        if entry.get("captcha_flag"):
            reasons.append("Captcha deadline passed: account is flagged")
    if state == "stopped":
        reason = entry.get("stopped_reason")
        if reason and str(reason).startswith("autoStop"):
            return "unknown", ["Stopped by autoStop"]
        reasons = ["Account is stopped" + (f": {reason}" if reason else "")]
        if entry.get("captcha_flag"):
            reasons.append("Captcha deadline passed: account is flagged")
        return "bad", reasons
    if state == "reconnecting":
        health = _worse(health, "warn")
        reasons.append("Gateway is disconnected, reconnecting")

    # No hunt for a long time matters only while the account is meant to hunt
    ago = entry.get("last_hunt_ago_s")
    hunt_on = entry.get("hunt_on", True)
    if state not in QUIET_STATES and hunt_on:
        if ago is not None:
            if ago >= cfg["lastHuntBadSeconds"]:
                health = _worse(health, "bad")
                reasons.append(f"No hunt for {_minutes(ago)}")
            elif ago >= cfg["lastHuntWarnSeconds"]:
                health = _worse(health, "warn")
                reasons.append(f"No hunt for {_minutes(ago)}")
        elif run_age_s is not None and run_age_s >= cfg["lastHuntBadSeconds"]:
            health = _worse(health, "warn")
            reasons.append("No hunt seen yet")

    inbound = entry.get("inbound_kb_min")
    if inbound is not None:
        if inbound >= cfg["inboundBadKbMin"]:
            health = _worse(health, "bad")
            reasons.append(f"Inbound {inbound:.0f} KB/min (limit {cfg['inboundBadKbMin']:.0f})")
        elif inbound >= cfg["inboundWarnKbMin"]:
            health = _worse(health, "warn")
            reasons.append(f"Inbound {inbound:.0f} KB/min (high)")

    # The busiest server that is not this account's farm server, see website/data_v2.py _noisy_for
    noisy = entry.get("noisy_guild")
    if noisy:
        kb = noisy["kb_min"]
        if kb >= cfg["inboundWarnKbMin"]:
            health = _worse(health, "bad" if kb >= cfg["inboundBadKbMin"] else "warn")
            reasons.append(f"Noisy server {noisy['guild_id']}: {kb:.0f} KB/min")

    captcha = entry.get("captcha_today")
    if captcha:
        if captcha >= cfg["captchaBadPerDay"]:
            health = _worse(health, "bad")
            reasons.append(f"{captcha} captchas today")
        elif captcha >= cfg["captchaWarnPerDay"]:
            health = _worse(health, "warn")
            reasons.append(f"{captcha} captcha today")

    errors = entry.get("errors_1h")
    if errors:
        if errors >= cfg["errorsBadPerHour"]:
            health = _worse(health, "bad")
            reasons.append(f"{errors} errors in the last hour")
        elif errors >= cfg["errorsWarnPerHour"]:
            health = _worse(health, "warn")
            reasons.append(f"{errors} errors in the last hour")

    hunts = entry.get("hunts_total")
    if hunts is not None and cfg["huntsSinceStartWarn"] <= hunts <= cfg["huntsSinceStartWarnUntil"]:
        health = _worse(health, "warn")
        reasons.append(f"{hunts} hunts since start: near captcha zone")

    return health, reasons


LEVEL_STALE_SECONDS = 24 * 3600


def level_info(checked_ago_s, check_active, stale_after=LEVEL_STALE_SECONDS):
    """Info lines about the level check. They never change an account's health. Only when the check is on (the bot says
    so in levels.sqlite) and the last good read is older than stale_after"""
    if check_active and checked_ago_s is not None and checked_ago_s > stale_after:
        return ["Level not checked for more than 24 h"]
    return []
