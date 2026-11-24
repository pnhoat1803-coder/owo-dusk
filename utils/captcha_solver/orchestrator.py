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

"""Priority ordered hCaptcha solver providers with automatic fallback.

The orchestrator owns the decisions: which provider is asked first, how many
retries it gets, and whether the token it returned is accepted by OwO. A
provider only talks to its own API; the shared verify lives in `owo_verify`.

Configuration lives in config/captcha.toml. The modern form is an
array-of-tables:

    [[hcaptcha_solver.providers]]
    name = "captchaly"
    priority = 1

The legacy single-provider form (`[hcaptcha_solver]` with `api_key` and
`retries`) still loads: it is migrated to one YesCaptcha provider at priority 1.
"""

from __future__ import annotations

import asyncio
import random

import aiohttp

from utils import run_log
from utils.captcha_solver.base import (
    BaseCaptchaProvider,
    CaptchaTotalFailure,
    FailureKind,
)
from utils.captcha_solver.captchaly import (
    VARIANT_ENTERPRISE,
    VARIANT_STANDARD,
    CaptchalyProvider,
)
from utils.captcha_solver.owo_verify import close_session, verify_owo_token
from utils.captcha_solver.yescaptcha import SOLVE_COST_POINTS, YesCaptchaProvider

MIN_RETRIES = 3
MAX_RETRIES = 5
DEFAULT_RETRIES = MIN_RETRIES

KNOWN_PROVIDERS = ("captchaly", "yescaptcha")
LEGACY_PROVIDER_NAME = "yescaptcha"


class CaptchaConfigError(RuntimeError):
    """The captcha configuration cannot produce a usable provider list."""


def clamp_retries(value, *, default: int = DEFAULT_RETRIES, label: str = "retries") -> int:
    """Return a retry count inside [3, 5], warning when the configured value is outside."""
    try:
        if isinstance(value, bool):
            raise ValueError("a boolean is not a retry count")
        retries = int(value)
    except (TypeError, ValueError):
        run_log.write(f"[captcha] {label} {value!r} is not a number, using {default}", level="WARNING", category="captcha")
        return default

    if retries < MIN_RETRIES:
        run_log.write(
            f"[captcha] {label} {retries} is below the minimum, clamped to {MIN_RETRIES}"
        , level="WARNING", category="captcha")
        return MIN_RETRIES
    if retries > MAX_RETRIES:
        run_log.write(
            f"[captcha] {label} {retries} is above the maximum, clamped to {MAX_RETRIES}"
        , level="WARNING", category="captcha")
        return MAX_RETRIES
    return retries


def _positive_priority(value, fallback: int) -> int:
    try:
        if isinstance(value, bool):
            raise ValueError("a boolean is not a priority")
        priority = int(value)
    except (TypeError, ValueError):
        return fallback
    return priority if priority > 0 else fallback


def _parse_entry(entry: dict, position: int, notes: list[str]) -> dict | None:
    """Validate one provider entry and return it in the internal shape."""
    name = str(entry.get("name") or "").strip().lower()
    if not name:
        notes.append(f"an entry at position {position} has no name, ignored")
        return None
    if name not in KNOWN_PROVIDERS:
        message = (
            f"unknown provider {name!r} at position {position} rejected: only "
            "captchaly and yescaptcha are supported"
        )
        run_log.write(f"[captcha] warning: {message}", level="WARNING", category="captcha")
        notes.append(message)
        return None

    enabled = entry.get("enabled", True)
    if not isinstance(enabled, bool):
        notes.append(f"provider {name}: enabled is not a boolean, treated as true")
        enabled = True
    if not enabled:
        notes.append(f"provider {name} is disabled")
        return None

    api_key = entry.get("api_key")
    if not isinstance(api_key, str) or not api_key.strip():
        notes.append(f"provider {name} has no api_key, ignored")
        return None

    min_retries = clamp_retries(
        entry.get("min_retries", MIN_RETRIES),
        label=f"provider {name} min_retries",
    )
    max_retries = clamp_retries(
        entry.get("max_retries", MAX_RETRIES),
        label=f"provider {name} max_retries",
    )
    if min_retries > max_retries:
        notes.append(
            f"provider {name}: min_retries {min_retries} is above max_retries "
            f"{max_retries}, swapped"
        )
        min_retries, max_retries = max_retries, min_retries

    return {
        "name": name,
        "api_key": api_key.strip(),
        "priority": _positive_priority(entry.get("priority"), position),
        "min_retries": min_retries,
        "max_retries": max_retries,
        "_order": position,
    }


def _fixed_entries(solver: dict, notes: list[str]) -> list[dict]:
    """Format 1: [hcaptcha_solver.captchaly] and [hcaptcha_solver.yescaptcha]."""
    entries: list[dict] = []
    for position, name in enumerate(KNOWN_PROVIDERS, start=1):
        table = solver.get(name)
        if table is None:
            continue
        if not isinstance(table, dict):
            notes.append(f"[hcaptcha_solver.{name}] is not a table, ignored")
            continue
        parsed = _parse_entry({"name": name, **table}, position, notes)
        if parsed is not None:
            entries.append(parsed)

    # The fixed schema has only those two tables. A third one cannot be created
    # through a config edit, but a hand-written file could contain it: say so
    # loudly instead of dropping it without a trace.
    allowed = set(KNOWN_PROVIDERS) | {"auto_detect_enterprise", "captcha_type"}
    for name, value in solver.items():
        if name in allowed or not isinstance(value, dict):
            continue
        message = (
            f"unknown provider table [hcaptcha_solver.{name}] rejected: only "
            "captchaly and yescaptcha are supported"
        )
        run_log.write(f"[captcha] warning: {message}", level="WARNING", category="captcha")
        notes.append(message)

    return entries


def _array_entries(providers, notes: list[str]) -> list[dict]:
    """Format 2: the previous [[hcaptcha_solver.providers]] array of tables.

    A table of provider tables (providers.captchaly) is accepted too, because an
    earlier build of this file shipped that shape.
    """
    if isinstance(providers, dict):
        items = [
            {"name": name, **value}
            for name, value in providers.items()
            if isinstance(value, dict)
        ]
    else:
        items = []
        for position, item in enumerate(providers, start=1):
            if not isinstance(item, dict):
                notes.append(f"provider entry {position} is not a table, ignored")
                continue
            items.append(item)

    entries: list[dict] = []
    for position, item in enumerate(items, start=1):
        parsed = _parse_entry(item, position, notes)
        if parsed is not None:
            entries.append(parsed)
    return entries


def normalise_providers(captcha_config: dict) -> tuple[list[dict], list[str]]:
    """Validate the provider configuration and order it by priority.

    Three formats are accepted, detected in this order:
      1. [hcaptcha_solver.captchaly] / [hcaptcha_solver.yescaptcha] (shipped)
      2. [[hcaptcha_solver.providers]] array of tables (previous)
      3. [hcaptcha_solver] with only api_key / retries (legacy)
    """
    if not isinstance(captcha_config, dict):
        raise CaptchaConfigError("config/captcha.toml could not be read as a table")

    solver = captcha_config.get("hcaptcha_solver")
    if not isinstance(solver, dict):
        raise CaptchaConfigError("config/captcha.toml has no [hcaptcha_solver] table")

    notes: list[str] = []

    if "captchaly" in solver or "yescaptcha" in solver:
        entries = _fixed_entries(solver, notes)
    elif isinstance(solver.get("providers"), (list, dict)):
        entries = _array_entries(solver["providers"], notes)
    elif isinstance(solver.get("api_key"), str):
        return _legacy_entries(solver, notes), notes
    else:
        raise CaptchaConfigError(
            "config/captcha.toml configures no hcaptcha provider: add "
            "[hcaptcha_solver.captchaly] and/or [hcaptcha_solver.yescaptcha]"
        )

    if not entries:
        raise CaptchaConfigError(
            "config/captcha.toml has no enabled hcaptcha provider with an api_key"
        )

    # Priorities must be unique: keep the order, move later duplicates down
    entries.sort(key=lambda entry: (entry["priority"], entry["_order"]))
    seen: set[int] = set()
    for entry in entries:
        priority = entry["priority"]
        while priority in seen:
            notes.append(
                f"provider {entry['name']}: priority {priority} is already used, "
                f"moved to {priority + 1}"
            )
            priority += 1
        seen.add(priority)
        entry["priority"] = priority
        entry.pop("_order", None)

    return entries, notes


def _legacy_entries(solver: dict, notes: list[str]) -> list[dict]:
    """Migrate the old [hcaptcha_solver] api_key/retries pair to one YesCaptcha provider."""
    api_key = solver.get("api_key")
    if not isinstance(api_key, str) or not api_key.strip():
        raise CaptchaConfigError(
            "config/captcha.toml has no [[hcaptcha_solver.providers]] entry and no "
            "legacy api_key"
        )

    retries = clamp_retries(solver.get("retries", MIN_RETRIES), label="legacy retries")
    notes.append(
        "config/captcha.toml uses the deprecated single-provider keys "
        "(api_key/retries): migrated to one yescaptcha provider at priority 1. "
        "Move it to [[hcaptcha_solver.providers]] to configure more providers."
    )
    return [
        {
            "name": LEGACY_PROVIDER_NAME,
            "api_key": api_key.strip(),
            "priority": 1,
            "min_retries": retries,
            "max_retries": retries,
        }
    ]


def hcaptcha_enabled(captcha_config: dict) -> bool:
    """True when the file configures at least one enabled hCaptcha provider.

    The fixed schema has no [hcaptcha_solver].enabled switch: the provider
    tables themselves decide. The previous and legacy formats keep honoring
    their own `enabled` keys.
    """
    solver = (captcha_config or {}).get("hcaptcha_solver")
    if not isinstance(solver, dict):
        return False

    fixed = [solver.get("captchaly"), solver.get("yescaptcha")]
    if any(isinstance(table, dict) for table in fixed):
        return any(
            isinstance(table, dict) and bool(table.get("enabled", True))
            for table in fixed
        )

    providers = solver.get("providers")
    if isinstance(providers, dict):
        return any(
            isinstance(entry, dict) and bool(entry.get("enabled", True))
            for entry in providers.values()
        )
    if isinstance(providers, list):
        return any(
            isinstance(entry, dict) and bool(entry.get("enabled", True))
            for entry in providers
        )

    return bool(solver.get("enabled", True))


def build_orchestrator(
    captcha_config: dict,
    *,
    owo_auth=None,
    verify_fn=None,
    client_factory=None,
    captchaly_probe=None,
) -> "CaptchaSolverOrchestrator":
    """Build one provider per configured entry, in priority order.

    A provider whose balance probe fails is dropped with a warning instead of
    stopping the process, so a lower-priority provider can still solve. The
    Captchaly probe is informational: a missing or zero balance never disables
    it, only an auth rejection does. When nothing usable is left this is a
    configuration error.
    """
    entries, notes = normalise_providers(captcha_config)
    solver = captcha_config.get("hcaptcha_solver", {})

    auto_detect = solver.get("auto_detect_enterprise", True)
    auto_detect = auto_detect if isinstance(auto_detect, bool) else True
    captcha_type = str(solver.get("captcha_type", VARIANT_STANDARD)).strip().lower()
    if captcha_type not in (VARIANT_STANDARD, VARIANT_ENTERPRISE):
        notes.append(
            f"captcha_type {captcha_type!r} is not standard or enterprise, "
            f"using {VARIANT_STANDARD}"
        )
        captcha_type = VARIANT_STANDARD

    providers: list[BaseCaptchaProvider] = []
    unusable: list[str] = []
    for entry in entries:
        name = entry["name"]
        if name == "captchaly":
            probe = captchaly_probe(entry["api_key"]) if captchaly_probe else None
            if probe is not None and getattr(probe, "auth_failed", False):
                # A rejected key is a real configuration error, not a transient one
                message = "captchaly auth failed (check api_key)"
                run_log.write(f"[captcha] {message}", level="WARNING", category="captcha")
                notes.append(message)
                unusable.append(message)
                continue
            if probe is not None and getattr(probe, "balance", None) == 0:
                warning = (
                    "captchaly balance is 0; solves will fail and the orchestrator "
                    "will fall back to yescaptcha"
                )
                run_log.write(f"[captcha] {warning}", level="WARNING", category="captcha")
                notes.append(warning)
            provider = CaptchalyProvider(
                entry["api_key"],
                auto_detect_enterprise=auto_detect,
                captcha_type=captcha_type,
                owo_auth=owo_auth,
                priority=entry["priority"],
                min_retries=entry["min_retries"],
                max_retries=entry["max_retries"],
                balance_probe=probe,
            )
        else:
            client = client_factory(entry["api_key"]) if client_factory else None
            provider = YesCaptchaProvider(
                entry["api_key"],
                priority=entry["priority"],
                min_retries=entry["min_retries"],
                max_retries=entry["max_retries"],
                client=client,
            )

        if provider.has_balance:
            probe = getattr(provider, "balance_probe", None)
            failure = getattr(probe, "failure", None)
            if failure is not None:
                # The probe already knows why: network, auth or a real zero balance
                message = probe.startup_message()
            elif (provider.balance or 0) < SOLVE_COST_POINTS:
                message = (
                    f"provider {name} has no balance left "
                    f"({provider.balance}); it is disabled for this run"
                )
            else:
                message = None
            if message is not None:
                run_log.write(f"[captcha] {message}", level="WARNING", category="captcha")
                notes.append(message)
                unusable.append(message)
                # The dropped provider never opened a session, so it holds nothing
                continue

        providers.append(provider)

    if not providers:
        detail = " | ".join(unusable) if unusable else "no provider is enabled"
        raise CaptchaConfigError(f"no usable hcaptcha provider: {detail}")

    orchestrator = CaptchaSolverOrchestrator(
        providers, verify_fn=verify_fn, owo_auth=owo_auth
    )
    orchestrator.notes = notes
    return orchestrator


class CaptchaSolverOrchestrator:
    """Ask each provider in priority order until one returns a verified token."""

    def __init__(self, providers, verify_fn=None, owo_auth=None):
        self.providers = sorted(
            (provider for provider in providers if provider is not None),
            key=lambda provider: (provider.priority, provider.name),
        )
        self._verify_fn = verify_fn or verify_owo_token
        self.owo_auth = owo_auth
        self._solve_task = None
        self.last_reports = []
        self.last_provider = None
        self.notes: list[str] = []

    @property
    def enabled(self) -> bool:
        return bool(self.providers)

    def describe(self) -> str:
        return " -> ".join(
            f"{provider.priority}:{provider.name}" for provider in self.providers
        )

    def startup_lines(self) -> list[str]:
        lines = []
        for provider in self.providers:
            line = f"provider {provider.priority}: {provider.name}"
            balance = provider.balance_display()
            if balance is not None:
                line += f" (balance: {balance})"
            lines.append(line)
        return lines

    def reports_text(self) -> str:
        if not self.last_reports:
            return "no provider was attempted"
        return "; ".join(report.describe() for report in self.last_reports)

    def balance_summary(self) -> tuple[int, int] | None:
        """(balance, solves left) for the provider that solved, or None when it has no balance."""
        provider = self.last_provider
        if provider is None:
            # Pre-solve path only: nothing has run yet in this episode, so the
            # first provider with a balance answers "what is left?". Do not rely
            # on this while a solve is in flight: solve_with_fallback sets
            # last_provider as soon as one provider succeeds, and a mid-episode
            # caller must read the provider that actually solved.
            provider = next(
                (item for item in self.providers if item.has_balance), None
            )
        if provider is None or not provider.has_balance:
            return None
        balance = provider.balance
        if balance is None:
            return None
        return int(balance), int(balance) // SOLVE_COST_POINTS

    async def solve_with_fallback(self, discord_headers: dict, user_id) -> str:
        """Return a verified token, or raise CaptchaTotalFailure with one report per provider.

        Cancellation is propagated: `cancel_in_flight` cancels the task running
        this call, which unwinds provider sessions and re-raises CancelledError.
        """
        self.last_reports = []
        self.last_provider = None
        self._solve_task = asyncio.current_task()
        try:
            for provider in self.providers:
                retries = random.randint(provider.min_retries, provider.max_retries)
                provider.begin_episode(retries)
                try:
                    token = await provider.solve(discord_headers, user_id, retries)
                except asyncio.CancelledError:
                    provider.note_failure(
                        FailureKind.PROVIDER_ERROR, "cancelled by the solve deadline"
                    )
                    provider.end_episode()
                    self._record(provider)
                    raise
                except Exception as error:
                    provider.note_failure(
                        FailureKind.PROVIDER_ERROR,
                        f"error: {type(error).__name__}: {error}",
                    )
                    token = None

                if token:
                    if provider.token_already_verified:
                        verified = True
                    else:
                        verified = await self._verify(provider, token)
                    await provider.after_verify(token, verified)
                    provider.end_episode()
                    self._record(provider)
                    if verified:
                        self.last_provider = provider
                        run_log.write(f"provider={provider.name} result=solved", level="INFO", category="captcha")
                        return token
                    provider.note_failure(
                        FailureKind.VERIFY_FAILED, "OwO rejected the token"
                    )
                else:
                    provider.end_episode()
                    self._record(provider)

                await provider.release()

            raise CaptchaTotalFailure(self.last_reports)
        finally:
            self._solve_task = None
            for provider in self.providers:
                await provider.release()

    def _record(self, provider) -> None:
        if all(report is not provider.report for report in self.last_reports):
            self.last_reports.append(provider.report)

    async def _verify(self, provider, token: str) -> bool:
        session = provider.verify_session
        if session is not None:
            return await self._verify_fn(session, token)
        session = aiohttp.ClientSession()
        try:
            return await self._verify_fn(session, token)
        finally:
            await close_session(session)

    def cancel_in_flight(self):
        """Cancel the solve running right now, if any. Safe to call from another task.

        Returns the cancelled task so the caller can wait for it to unwind.
        """
        task = self._solve_task
        if task is not None and not task.done():
            run_log.write("provider=solver result=error: cancelled by the solve deadline", level="WARNING", category="captcha")
            task.cancel()
            return task
        return None

    async def close(self) -> None:
        for provider in self.providers:
            await provider.release()
