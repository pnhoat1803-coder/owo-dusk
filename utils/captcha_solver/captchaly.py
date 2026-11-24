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

"""Captchaly hCaptcha provider.

Captchaly solves in a single GET whose response already carries the token, so
there is no task/poll round trip like YesCaptcha. The docs warn that a solve
can take a long time and that clients must not abort early; the request timeout
is therefore 120 seconds and a 503 is Captchaly's own timeout/unsolvable answer.

Enterprise detection: when `auto_detect_enterprise` is on, the retry budget is
spent on both endpoints, standard first. The token is sent to the shared OwO
verify after every attempt; the first endpoint whose token OwO accepts is
remembered per sitekey and tried first on later episodes.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import aiohttp

from utils import run_log
from utils.captcha_solver.base import BaseCaptchaProvider, FailureKind
from utils.captcha_solver.owo_verify import (
    OwoAuthError,
    close_session,
    verify_owo_token,
)

CAPTCHALY_STANDARD_URL = "https://v1.captchaly.com/hcaptcha"
CAPTCHALY_ENTERPRISE_URL = "https://v1.captchaly.com/hcaptcha-enterprise"
CAPTCHALY_TIMEOUT_SECONDS = 120
CAPTCHALY_TIMEOUT = aiohttp.ClientTimeout(total=CAPTCHALY_TIMEOUT_SECONDS)

OWO_SITEKEY = "a6a1d5ce-612d-472d-8e37-7601408fbc09"
OWO_SITE_URL = "https://owobot.com"

# A 429 means the subscription concurrency limit is reached: retrying at once
# cannot help, so the provider waits briefly and lets the orchestrator fail over
CONCURRENCY_BACKOFF_SECONDS = 2.0

VARIANT_STANDARD = "standard"
VARIANT_ENTERPRISE = "enterprise"
VALID_VARIANTS = (VARIANT_STANDARD, VARIANT_ENTERPRISE)

# GET /account is documented in the Captchaly OpenAPI spec
# ("Account Information" -> User { id, email, balance, join_at }). The rest are
# fallbacks in case the endpoint moves; the probe stops at the first 200 that
# carries a numeric balance.
CAPTCHALY_ACCOUNT_URL = "https://v1.captchaly.com/account"
CAPTCHALY_BALANCE_CANDIDATES = (
    CAPTCHALY_ACCOUNT_URL,
    "https://v1.captchaly.com/balance",
    "https://v1.captchaly.com/user",
    "https://v1.captchaly.com/me",
    "https://v1.captchaly.com/api/user",
    "https://v1.captchaly.com/api/balance",
)
CAPTCHALY_BALANCE_TIMEOUT = aiohttp.ClientTimeout(total=10)
CAPTCHALY_BALANCE_RETRY_BACKOFF_SECONDS = 2.0
BALANCE_KEYS = ("balance", "credits", "funds", "amount")
NESTED_BALANCE_PATHS = (("account", "balance"), ("data", "balance"), ("data", "credits"))


@dataclass
class CaptchalyBalance:
    """Startup-only balance information, in USD. Never used to disable the provider."""

    balance: float | None = None
    endpoint: str | None = None
    auth_failed: bool = False
    detail: str = ""


def _is_number(value) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        try:
            float(value)
        except ValueError:
            return False
        return True
    return False


def _find_balance(data):
    if not isinstance(data, dict):
        return None
    lowered = {str(key).lower(): value for key, value in data.items()}
    for key in BALANCE_KEYS:
        if key in lowered and _is_number(lowered[key]):
            return lowered[key]
    for parent, child in NESTED_BALANCE_PATHS:
        nested = lowered.get(parent)
        if isinstance(nested, dict):
            nested_lower = {str(key).lower(): value for key, value in nested.items()}
            if child in nested_lower and _is_number(nested_lower[child]):
                return nested_lower[child]
    return None


def _extract_balance(body) -> float | None:
    """The account balance as a float: Captchaly reports USD, e.g. 0.1 = $0.10."""
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return None
    value = _find_balance(data)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def _fetch_candidate(session, url, api_key, sleep):
    """GET one candidate, retrying once on a timeout or connection error."""
    last_error = None
    for attempt in (1, 2):
        try:
            async with session.get(
                url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "application/json",
                },
                timeout=CAPTCHALY_BALANCE_TIMEOUT,
            ) as response:
                return response.status, await response.text()
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, TimeoutError, aiohttp.ClientError) as error:
            last_error = error
            if attempt == 1:
                await sleep(CAPTCHALY_BALANCE_RETRY_BACKOFF_SECONDS)
    return None, f"network error: {last_error}"


async def probe_captchaly_balance(
    api_key: str,
    *,
    candidates=None,
    session=None,
    sleep=None,
) -> CaptchalyBalance:
    """Ask Captchaly for the balance without ever making the provider unusable.

    Tries the documented account endpoint first, then the fallbacks. A 401/403
    is final (bad key); everything else just moves to the next candidate. A
    missing balance is informational only.
    """
    urls = tuple(candidates or CAPTCHALY_BALANCE_CANDIDATES)
    sleep = sleep or asyncio.sleep
    owns_session = session is None
    if owns_session:
        session = aiohttp.ClientSession(timeout=CAPTCHALY_BALANCE_TIMEOUT)
    try:
        for url in urls:
            status, body = await _fetch_candidate(session, url, api_key, sleep)
            if status in (401, 403):
                run_log.write("captchaly auth failed (check api_key)", level="WARNING", category="captcha")
                return CaptchalyBalance(
                    endpoint=url, auth_failed=True, detail=f"HTTP {status}"
                )
            if status == 200:
                balance = _extract_balance(body)
                if balance is not None:
                    run_log.write(
                        f"[captcha] captchaly balance is ${balance:.2f} (from {url})"
                    , level="INFO", category="captcha")
                    return CaptchalyBalance(balance=balance, endpoint=url)
                run_log.write(f"[captcha] captchaly endpoint {url} returned no balance field", level="WARNING", category="captcha")
        run_log.write("captchaly balance endpoint not found; balance will show as unknown", level="WARNING", category="captcha")
        return CaptchalyBalance()
    finally:
        if owns_session:
            await close_session(session)


def probe_captchaly_balance_sync(api_key: str, **kwargs) -> CaptchalyBalance:
    """Startup helper: run the async probe from synchronous startup code."""
    try:
        return asyncio.run(probe_captchaly_balance(api_key, **kwargs))
    except RuntimeError:
        # A loop is already running in this thread: use a private one
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                probe_captchaly_balance(api_key, **kwargs)
            )
        finally:
            loop.close()


class CaptchalyProvider(BaseCaptchaProvider):
    name = "captchaly"
    has_balance = False

    # sitekey -> variant that last verified, shared by every instance so a
    # restart of the cog does not re-probe the wrong endpoint
    _variant_cache: dict[str, str] = {}

    def __init__(
        self,
        api_key: str,
        *,
        auto_detect_enterprise: bool = True,
        captcha_type: str = VARIANT_STANDARD,
        owo_auth=None,
        priority: int = 1,
        min_retries: int = 3,
        max_retries: int = 5,
        session_factory=None,
        balance_probe: CaptchalyBalance | None = None,
    ):
        super().__init__(
            priority=priority, min_retries=min_retries, max_retries=max_retries
        )
        self._api_key = (api_key or "").strip()
        self.auto_detect_enterprise = bool(auto_detect_enterprise)
        chosen = str(captcha_type or VARIANT_STANDARD).strip().lower()
        self.captcha_type = chosen if chosen in VALID_VARIANTS else VARIANT_STANDARD
        self._owo_auth = owo_auth
        # Injectable so tests can hand in a fake session
        self._session_factory = session_factory or aiohttp.ClientSession
        # Startup information only: a missing or zero balance never disables it
        self.balance_probe = balance_probe

    @property
    def balance(self):
        if self.balance_probe is None:
            return None
        return self.balance_probe.balance

    def balance_display(self) -> str | None:
        if self.balance_probe is None:
            return None
        if self.balance_probe.balance is None:
            return "unknown — endpoint not found"
        # Captchaly bills in USD: show it the way the dashboard does
        return f"${self.balance_probe.balance:.2f}"

    def endpoint_for(self, variant: str) -> str:
        return (
            CAPTCHALY_ENTERPRISE_URL
            if variant == VARIANT_ENTERPRISE
            else CAPTCHALY_STANDARD_URL
        )

    def _variant_plan(self, retries: int) -> list[str]:
        """The ordered endpoints to try, one entry per attempt.

        With auto detection off, every attempt uses the configured variant.
        With a remembered variant, the first half of the budget uses it and the
        rest switches to the other endpoint in case OwO changed its page. With
        no memory, standard gets ceil(retries / 2) attempts (at least one) and
        enterprise gets the rest.
        """
        total = max(1, int(retries))
        if not self.auto_detect_enterprise:
            return [self.captcha_type] * total

        known = self._variant_cache.get(OWO_SITEKEY)
        if known in VALID_VARIANTS:
            other = (
                VARIANT_ENTERPRISE if known == VARIANT_STANDARD else VARIANT_STANDARD
            )
            first = max(1, (total + 1) // 2)
            return [known] * first + [other] * (total - first)

        standard_tries = max(1, (total + 1) // 2)
        return [VARIANT_STANDARD] * standard_tries + [VARIANT_ENTERPRISE] * (
            total - standard_tries
        )

    def _remember_variant(self, variant: str) -> None:
        type(self)._variant_cache[OWO_SITEKEY] = variant

    @classmethod
    def known_variant(cls) -> str | None:
        return cls._variant_cache.get(OWO_SITEKEY)

    @classmethod
    def forget_variant(cls) -> None:
        cls._variant_cache.pop(OWO_SITEKEY, None)

    async def solve(self, discord_headers: dict, user_id: int, retries: int) -> str | None:
        """Try the planned endpoints and return a token, verifying when possible."""
        self.token_already_verified = False
        verify_session = None
        http_session = None
        try:
            if self._owo_auth is not None:
                # OwO only accepts a token for a logged in session, so the
                # account login happens before the paid request
                try:
                    verify_session = await self._owo_auth.open_session(
                        discord_headers, user_id
                    )
                except OwoAuthError as error:
                    self.note_failure(
                        FailureKind.PROVIDER_ERROR, f"OwO login failed: {error}"
                    )
                    run_log.write(f"provider={self.name} login failed: {error}", level="WARNING", category="captcha")
                    return None

            http_session = self._session_factory(timeout=CAPTCHALY_TIMEOUT)
            plan = self._variant_plan(retries)
            for number, variant in enumerate(plan, start=1):
                token = await self._request_token(http_session, variant, number, len(plan))
                if token is None:
                    if self.last_failure is FailureKind.RATE_LIMITED:
                        # Do not hammer the same provider; let the orchestrator fail over
                        await asyncio.sleep(CONCURRENCY_BACKOFF_SECONDS)
                        return None
                    continue

                self.report.tokens += 1
                if verify_session is None:
                    # No OwO session available: the orchestrator verifies with its own
                    run_log.write(
                        f"provider={self.name} attempt={number}/{len(plan)} "
                        f"variant={variant} result=token_received"
                    , level="DEBUG", category="captcha_provider")
                    return token

                verified = await verify_owo_token(verify_session, token)
                run_log.write(
                    f"provider={self.name} attempt={number}/{len(plan)} "
                    f"variant={variant} "
                    f"result={'token_received' if verified else 'verify_failed'}"
                , level="DEBUG", category="captcha_provider")
                if verified:
                    self._remember_variant(variant)
                    self.last_error = None
                    self.last_failure = None
                    self.report.error = None
                    self.report.failure = None
                    self.token_already_verified = True
                    return token
                self.note_failure(
                    FailureKind.VERIFY_FAILED,
                    f"{variant} token was rejected by OwO",
                )

            if self.last_failure is None:
                self.note_failure(
                    FailureKind.PROVIDER_ERROR, "no token from any endpoint"
                )
            return None
        except asyncio.CancelledError:
            raise
        finally:
            try:
                await close_session(verify_session)
            finally:
                await close_session(http_session)

    async def _request_token(
        self, session, variant: str, number: int, total: int
    ) -> str | None:
        url = self.endpoint_for(variant)
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        params = {"sitekey": OWO_SITEKEY, "url": OWO_SITE_URL}
        self.report.attempts = number

        try:
            async with session.get(
                url, params=params, headers=headers, timeout=CAPTCHALY_TIMEOUT
            ) as resp:
                body = await resp.text()

                if resp.status == 429:
                    self.note_failure(
                        FailureKind.RATE_LIMITED, "HTTP 429 concurrency limit reached"
                    )
                    run_log.write(
                        f"provider={self.name} attempt={number}/{total} "
                        f"variant={variant} result=error: HTTP 429 concurrency limit"
                    , level="DEBUG", category="captcha_provider")
                    return None

                if resp.status == 503:
                    self.note_failure(
                        FailureKind.PROVIDER_ERROR, "HTTP 503 CAPTCHA_UNSOLVABLE"
                    )
                    run_log.write(
                        f"provider={self.name} attempt={number}/{total} "
                        f"variant={variant} result=error: HTTP 503 CAPTCHA_UNSOLVABLE"
                    , level="DEBUG", category="captcha_provider")
                    return None

                if resp.status != 200:
                    self.note_failure(FailureKind.PROVIDER_ERROR, f"HTTP {resp.status}")
                    run_log.write(
                        f"provider={self.name} attempt={number}/{total} "
                        f"variant={variant} result=error: HTTP {resp.status}: "
                        f"{self._redact(body)}"
                    , level="DEBUG", category="captcha_provider")
                    return None

                token = self._extract_token(body)
                if not token:
                    self.note_failure(
                        FailureKind.PROVIDER_ERROR, "HTTP 200 without a token field"
                    )
                    run_log.write(
                        f"provider={self.name} attempt={number}/{total} "
                        f"variant={variant} result=error: HTTP 200 without a token "
                        f"field: {self._redact(body)}"
                    , level="DEBUG", category="captcha_provider")
                    return None
                return token
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, TimeoutError):
            self.note_failure(
                FailureKind.TIMEOUT,
                f"request timed out after {CAPTCHALY_TIMEOUT_SECONDS}s",
            )
            run_log.write(
                f"provider={self.name} attempt={number}/{total} variant={variant} "
                f"result=error: timed out after {CAPTCHALY_TIMEOUT_SECONDS}s"
            , level="DEBUG", category="captcha_provider")
            return None
        except aiohttp.ClientError as error:
            self.note_failure(FailureKind.PROVIDER_ERROR, f"network error: {error}")
            run_log.write(
                f"provider={self.name} attempt={number}/{total} variant={variant} "
                f"result=error: network error: {error}"
            , level="DEBUG", category="captcha_provider")
            return None

    @staticmethod
    def _extract_token(body: str) -> str | None:
        try:
            data = json.loads(body)
        except (TypeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        token = data.get("token")
        if isinstance(token, str) and token.strip():
            return token.strip()
        return None

    def _redact(self, text: str) -> str:
        """Keep the key out of logs even if a proxy echoes it back."""
        if not text:
            return ""
        if self._api_key:
            text = text.replace(self._api_key, "***")
        return " ".join(str(text).split())[:300]

    async def release(self) -> None:
        await super().release()
