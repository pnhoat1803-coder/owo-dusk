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

"""Contract every hCaptcha solver backend implements.

A provider produces a hCaptcha solution token. Everything that talks to OwO
(the Discord OAuth login and the /api/captcha/verify POST) lives in
`utils.captcha_solver.owo_verify`, so both backends share one verify path.

A provider may verify a token itself while it probes the Captchaly
standard/enterprise endpoint; it then sets `token_already_verified` so the
orchestrator does not spend a second verify on the same token.
"""

from __future__ import annotations

import enum
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass


class FailureKind(str, enum.Enum):
    """Why a provider did not hand back a token that OwO accepted."""

    PROVIDER_ERROR = "provider_error"
    RATE_LIMITED = "rate_limited"
    NO_CREDITS = "no_credits"
    VERIFY_FAILED = "verify_failed"
    TIMEOUT = "timeout"
    # Startup balance probe: a transient network problem, not a configuration error
    NETWORK = "network"
    # The provider rejected the api_key
    AUTH = "auth"


@dataclass
class ProviderReport:
    """What one provider did during a single captcha episode, for the user report."""

    name: str
    attempts: int = 0
    retries: int = 0
    tokens: int = 0
    error: str | None = None
    failure: FailureKind | None = None
    elapsed: float = 0.0
    started_at: float | None = None

    def start(self, retries: int) -> None:
        self.attempts = 0
        self.retries = int(retries)
        self.tokens = 0
        self.error = None
        self.failure = None
        self.elapsed = 0.0
        self.started_at = time.monotonic()

    def finish(self) -> None:
        if self.started_at is not None:
            self.elapsed = time.monotonic() - self.started_at

    def describe(self) -> str:
        text = f"{self.name} (attempts={self.attempts}/{self.retries}"
        if self.tokens:
            text += f", tokens={self.tokens}"
        if self.failure is not None:
            text += f", failure={self.failure.value}"
        if self.error:
            text += f", last_error={self.error}"
        if self.elapsed:
            text += f", elapsed={self.elapsed:.1f}s"
        return text + ")"


class BaseCaptchaProvider(ABC):
    """A hCaptcha solver backend.

    Subclasses implement `solve`, which returns a solution token or None and
    leaves a human readable reason in `last_error` / `last_failure`.
    """

    name: str = "provider"
    # True when the provider bills per solve and exposes a balance, see balance_summary
    has_balance = False

    def __init__(self, *, priority: int = 1, min_retries: int = 3, max_retries: int = 5):
        self.priority = int(priority)
        self.min_retries = int(min_retries)
        self.max_retries = int(max_retries)
        self.report = ProviderReport(name=self.name)
        # Session the shared verify must use, set by solve when the provider opened one
        self.verify_session = None
        # True when solve already verified the token it returns
        self.token_already_verified = False
        self.last_error = None
        self.last_failure = None

    @abstractmethod
    async def solve(self, discord_headers: dict, user_id: int, retries: int) -> str | None:
        """Return a hCaptcha token, or None when this provider could not produce one."""

    @property
    def balance(self):
        """Points left, for providers that bill per solve. None otherwise."""
        return None

    def balance_display(self) -> str | None:
        """Text shown next to the provider in the startup log, or None for no entry."""
        if self.has_balance and self.balance is not None:
            return str(self.balance)
        return None

    def begin_episode(self, retries: int) -> None:
        self.last_error = None
        self.last_failure = None
        self.token_already_verified = False
        self.verify_session = None
        self.report.start(retries)

    def end_episode(self) -> None:
        self.report.finish()

    def note_failure(self, kind: FailureKind, message: str) -> None:
        self.last_failure = kind
        self.last_error = message
        self.report.failure = kind
        self.report.error = message

    async def after_verify(self, token: str, success: bool) -> None:
        """Bookkeeping once the shared verify has decided, e.g. balance accounting."""

    async def release(self) -> None:
        """Drop anything the episode still holds. Must be safe to call twice."""
        self.verify_session = None


class CaptchaTotalFailure(RuntimeError):
    """Every enabled provider failed; `reports` holds one entry per provider tried."""

    def __init__(self, reports, reason: str = "all providers failed"):
        self.reports = list(reports)
        self.reason = reason
        super().__init__(self.summary())

    @property
    def provider_names(self) -> list[str]:
        return [report.name for report in self.reports]

    def summary(self) -> str:
        if not self.reports:
            return self.reason
        return "; ".join(report.describe() for report in self.reports)
