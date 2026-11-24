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

"""YesCaptcha hCaptcha provider.

`captchaClient` keeps the public surface it has always had
(`solve_owo_bot_captcha` returns True/False and performs the whole flow), but
the OwO login and the verify POST now come from `owo_verify`, the same code the
Captchaly provider uses. `YesCaptchaProvider` is the adapter the orchestrator
drives: it verifies the token with the shared helper and returns it, or None.
"""

import asyncio
import time
from dataclasses import dataclass

import aiohttp
import requests

from utils import run_log
from utils.captcha_solver.base import BaseCaptchaProvider, FailureKind
from utils.captcha_solver.owo_verify import (
    DISCORD_AUTHORIZE_URL,
    OwoAuth,
    OwoAuthError,
    close_session,
    verify_owo_token,
)

YESCAPTCHA_BALANCE_URL = "https://api.yescaptcha.com/getBalance"
YESCAPTCHA_CREATE_TASK_URL = "https://api.yescaptcha.com/createTask"
YESCAPTCHA_RESULT_URL = "https://api.yescaptcha.com/getTaskResult"
YESCAPTCHA_TASK_TYPE = "HCaptchaTaskProxyless"
YESCAPTCHA_SOFT_ID = 94493

OWO_SITEKEY = "a6a1d5ce-612d-472d-8e37-7601408fbc09"
OWO_SITE_URL = "https://owobot.com"

# Points YesCaptcha charges for one hCaptcha solve
SOLVE_COST_POINTS = 30

BALANCE_TIMEOUT_SECONDS = 10
# The startup probe retries a transient network failure before the provider is
# called unusable: three attempts, waiting 2 s then 4 s between them (the 8 s
# entry applies if BALANCE_PROBE_ATTEMPTS is ever raised to four).
BALANCE_PROBE_ATTEMPTS = 3
BALANCE_PROBE_BACKOFF_SECONDS = (2.0, 4.0, 8.0)
# A solve is a createTask call plus up to POLL_ATTEMPTS result calls
CREATE_TASK_TIMEOUT = aiohttp.ClientTimeout(total=30)
POLL_TIMEOUT = aiohttp.ClientTimeout(total=120)
POLL_INTERVAL_SECONDS = 2
POLL_ATTEMPTS = 60


@dataclass
class BalanceProbe:
    """Result of the startup balance probe, with the reason a provider is unusable."""

    balance: int | None = None
    failure: FailureKind | None = None
    detail: str = ""
    attempts: int = 0

    @property
    def usable(self) -> bool:
        return self.failure is None and self.balance is not None

    def startup_message(self) -> str:
        """One line that names the real cause, used by the startup report."""
        if self.failure is FailureKind.NETWORK:
            return (
                f"YesCaptcha API unreachable after {self.attempts} attempts "
                f"(network): {self.detail}"
            )
        if self.failure is FailureKind.AUTH:
            return "YesCaptcha auth failed (check api_key)"
        if self.failure is FailureKind.NO_CREDITS:
            return "YesCaptcha balance is zero (top up to continue)"
        if self.failure is not None:
            return f"YesCaptcha balance probe failed: {self.detail}"
        return f"YesCaptcha balance is {self.balance}"


def _balance_from_response(response, attempt: int) -> BalanceProbe:
    if response.status_code in (401, 403):
        run_log.write(f"[YesCaptcha Error] auth failed (HTTP {response.status_code})", level="WARNING", category="captcha")
        return BalanceProbe(
            failure=FailureKind.AUTH,
            detail=f"HTTP {response.status_code}",
            attempts=attempt,
        )
    if response.status_code != 200:
        detail = f"HTTP {response.status_code}"
        run_log.write(f"[YesCaptcha Error] balance request failed with {detail}", level="WARNING", category="captcha")
        return BalanceProbe(
            failure=FailureKind.PROVIDER_ERROR, detail=detail, attempts=attempt
        )

    try:
        data = response.json()
    except ValueError as error:
        detail = f"invalid JSON: {error}"
        run_log.write(f"[YesCaptcha Error] {detail}", level="WARNING", category="captcha")
        return BalanceProbe(
            failure=FailureKind.PROVIDER_ERROR, detail=detail, attempts=attempt
        )

    if not isinstance(data, dict):
        detail = "unexpected response body"
        run_log.write(f"[YesCaptcha Error] {detail}", level="WARNING", category="captcha")
        return BalanceProbe(
            failure=FailureKind.PROVIDER_ERROR, detail=detail, attempts=attempt
        )

    if data.get("errorId") != 0:
        code = str(data.get("errorCode", ""))
        description = str(data.get("errorDescription", ""))
        upper = code.upper()
        detail = f"{code}: {description}" if (code or description) else "API error"
        if "KEY" in upper or "AUTH" in upper or "ACCESS" in upper:
            run_log.write(f"[YesCaptcha Error] auth failed ({detail})", level="WARNING", category="captcha")
            return BalanceProbe(
                failure=FailureKind.AUTH, detail=detail, attempts=attempt
            )
        run_log.write(f"[YesCaptcha Error] balance request failed ({detail})", level="WARNING", category="captcha")
        return BalanceProbe(
            failure=FailureKind.PROVIDER_ERROR, detail=detail, attempts=attempt
        )

    try:
        balance = int(data.get("balance", 0) or 0)
    except (TypeError, ValueError):
        detail = "balance was not a number"
        run_log.write(f"[YesCaptcha Error] {detail}", level="WARNING", category="captcha")
        return BalanceProbe(
            failure=FailureKind.PROVIDER_ERROR, detail=detail, attempts=attempt
        )

    if balance < SOLVE_COST_POINTS:
        run_log.write(f"[YesCaptcha Error] balance is {balance}, too low for another solve", level="WARNING", category="captcha")
        return BalanceProbe(
            balance=balance,
            failure=FailureKind.NO_CREDITS,
            detail=f"balance {balance}",
            attempts=attempt,
        )
    return BalanceProbe(balance=balance, attempts=attempt)


def probe_yescaptcha_balance(
    api_key,
    *,
    attempts: int = BALANCE_PROBE_ATTEMPTS,
    post=None,
    sleep=None,
) -> BalanceProbe:
    """Ask YesCaptcha for the balance, retrying only transient network failures.

    A timeout or connection error is retried with the configured backoff. HTTP
    errors, an auth rejection and a real zero balance are final answers and are
    reported as such, so startup never tells the user to edit the config for a
    network problem.
    """
    post = post or requests.post
    sleep = sleep or time.sleep
    last_detail = ""

    for attempt in range(1, attempts + 1):
        try:
            response = post(
                YESCAPTCHA_BALANCE_URL,
                json={"clientKey": api_key},
                timeout=BALANCE_TIMEOUT_SECONDS,
            )
        except requests.exceptions.Timeout:
            last_detail = f"request timed out after {BALANCE_TIMEOUT_SECONDS}s"
            run_log.write(f"[YesCaptcha Error] {last_detail} (attempt {attempt}/{attempts})", level="WARNING", category="captcha")
        except requests.exceptions.ConnectionError as error:
            last_detail = f"connection error: {error}"
            run_log.write(f"[YesCaptcha Error] {last_detail} (attempt {attempt}/{attempts})", level="WARNING", category="captcha")
        except requests.exceptions.RequestException as error:
            detail = f"HTTP request failed: {error}"
            run_log.write(f"[YesCaptcha Error] {detail}", level="WARNING", category="captcha")
            return BalanceProbe(
                failure=FailureKind.PROVIDER_ERROR, detail=detail, attempts=attempt
            )
        else:
            return _balance_from_response(response, attempt)

        if attempt < attempts:
            delay = BALANCE_PROBE_BACKOFF_SECONDS[
                min(attempt - 1, len(BALANCE_PROBE_BACKOFF_SECONDS) - 1)
            ]
            run_log.write(f"[YesCaptcha Error] retrying the balance probe in {delay:.0f}s", level="INFO", category="captcha")
            sleep(delay)

    return BalanceProbe(
        failure=FailureKind.NETWORK, detail=last_detail, attempts=attempts
    )


class captchaClient:
    def __init__(self, api, *, probe: BalanceProbe | None = None):
        self.api = api
        # One probe at startup; its cause decides whether the provider is usable
        self.balance_probe = (
            probe if probe is not None else probe_yescaptcha_balance(api)
        )
        self.balance = self.balance_probe.balance or 0
        self._auth_url = DISCORD_AUTHORIZE_URL
        self._owo_auth = OwoAuth(auth_url=self._auth_url)
        # Kept as a name because older code may inspect it; it is the same map
        self._cookie_cache = self._owo_auth.cookie_cache
        self.last_error = None

    # We aren't supposed to use sync copies for this.. There must be a better solution
    # Double check - for the time being it works!
    def get_yescaptcha_balance_sync(self):
        """The balance, or 0 when the probe failed. Kept for older callers."""
        return probe_yescaptcha_balance(self.api).balance or 0

    async def get_yescaptcha_balance(self, session: aiohttp.ClientSession) -> int:
        url = YESCAPTCHA_BALANCE_URL
        timeout = aiohttp.ClientTimeout(total=BALANCE_TIMEOUT_SECONDS)

        try:
            async with session.post(
                url, json={"clientKey": self.api}, timeout=timeout
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    error_id = data.get("errorId")

                    if error_id == 0:
                        balance = int(data.get("balance", 0))
                        self.balance = balance
                        return balance

                    # Otherwise error_id would be 0 (error)
                    error_code = data.get(
                        "errorCode", "Error field (errorCode) missing"
                    )
                    error_desc = data.get(
                        "errorDescription", "Error field (errorDescription) missing"
                    )
                    run_log.write(
                        f"[YesCaptcha Error] API returned error (Code: {error_code}): {error_desc}"
                    , level="WARNING", category="captcha")
                else:
                    run_log.write(
                        f"[YesCaptcha Error] HTTP request failed with status {response.status}"
                    , level="WARNING", category="captcha")

        except (asyncio.TimeoutError, TimeoutError):
            run_log.write(
                f"[YesCaptcha Error] Request timed out after {BALANCE_TIMEOUT_SECONDS} seconds"
            , level="WARNING", category="captcha")
        except aiohttp.ClientError as e:
            run_log.write(f"[YesCaptcha Error] Network connection issue: {e}", level="WARNING", category="captcha")
        except Exception as e:
            run_log.write(f"[YesCaptcha Error] Unexpected error occurred: {e}", level="WARNING", category="captcha")

        # fallback
        return self.balance

    async def update_balance(self):
        async with aiohttp.ClientSession() as session:
            self.balance = await self.get_yescaptcha_balance(session)

    async def solve_hcaptcha_logic(self, retries=3):
        """
        Attempts to solve the captcha using Yescaptcha.
        Retries creating a new task 'retries' times upon failure.
        """
        create_url = YESCAPTCHA_CREATE_TASK_URL
        result_url = YESCAPTCHA_RESULT_URL

        try:
            retries = max(1, int(retries))
        except (TypeError, ValueError):
            retries = 1

        payload = {
            "clientKey": self.api,
            "task": {
                "type": YESCAPTCHA_TASK_TYPE,
                "websiteKey": OWO_SITEKEY,
                "websiteURL": OWO_SITE_URL,
            },
            "softID": YESCAPTCHA_SOFT_ID,
        }

        async with aiohttp.ClientSession(timeout=CREATE_TASK_TIMEOUT) as session:
            for attempt in range(retries):
                try:
                    run_log.write(f"Solving captcha... Attempt {attempt + 1}/{retries}", level="DEBUG", category="captcha_provider")

                    async with session.post(
                        create_url, json=payload, timeout=CREATE_TASK_TIMEOUT
                    ) as resp:
                        if resp.status != 200:
                            raise Exception(
                                f"Create task failed with HTTP {resp.status}"
                            )
                        data = await resp.json()

                    if data.get("errorId") != 0:
                        raise Exception(f"API Error: {data.get('errorDescription')}")

                    task_id = data.get("taskId")
                    if not task_id:
                        raise Exception("No taskId returned from API")

                    for _ in range(POLL_ATTEMPTS):
                        await asyncio.sleep(POLL_INTERVAL_SECONDS)

                        async with session.post(
                            result_url,
                            json={"clientKey": self.api, "taskId": task_id},
                            timeout=POLL_TIMEOUT,
                        ) as result_resp:
                            if result_resp.status != 200:
                                raise Exception(
                                    f"Result check failed with HTTP {result_resp.status}"
                                )

                            res = await result_resp.json()

                        if res.get("errorId") != 0:
                            # Logic error from solver side
                            raise Exception(
                                f"Polling API Error: {res.get('errorDescription')}"
                            )

                        if res.get("status") == "ready":
                            return res["solution"]["gRecaptchaResponse"]

                    raise Exception("Task timed out without becoming 'ready'")

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    run_log.write(f"Attempt {attempt + 1} failed: {e}", level="DEBUG", category="captcha_provider")
                    self.last_error = str(e)
                    if attempt + 1 < retries:
                        run_log.write("Retrying with a new task...", level="DEBUG", category="captcha_provider")
                        await asyncio.sleep(1)
                    else:
                        run_log.write("All retry attempts exhausted.", level="DEBUG", category="captcha_provider")

            return None

    async def _prepare_solve(self, discord_headers, user_id, tries):
        """Log in to OwO and buy a token. Returns (session, token); (None, None) on failure.

        The session is deliberately left open: the verify POST needs the cookies
        this login produced, and the caller owns closing it.
        """
        discord_headers["Referer"] = self._auth_url
        self.last_error = None

        # 30 points is required by Yescaptcha for Hcaptcha solving.
        if self.balance < SOLVE_COST_POINTS:
            run_log.write("Not enough balance!", level="WARNING", category="captcha")
            self.last_error = "Not enough balance"
            return None, None

        try:
            session = await self._owo_auth.open_session(discord_headers, user_id)
        except asyncio.CancelledError:
            raise
        except OwoAuthError as error:
            run_log.write(f"OAuth failed: {error}", level="WARNING", category="captcha")
            self.last_error = f"OAuth failed: {error}"
            return None, None
        except Exception as error:
            run_log.write(f"OAuth failed with an unexpected error: {error}", level="WARNING", category="captcha")
            self.last_error = f"OAuth failed: {error}"
            return None, None

        try:
            solution = await self.solve_hcaptcha_logic(tries)
        except asyncio.CancelledError:
            await close_session(session)
            raise
        except Exception as error:
            run_log.write(f"Solver Error: {error}", level="WARNING", category="captcha")
            self.last_error = f"Solver Error: {error}"
            await close_session(session)
            return None, None

        if not solution:
            run_log.write("No solution result found for Hcaptcha", level="WARNING", category="captcha")
            self.last_error = self.last_error or "No solution result found for Hcaptcha"
            await close_session(session)
            return None, None

        return session, solution

    async def verify_token(self, session, token) -> bool:
        """Send the token to OwO and bill the solve when it is accepted."""
        verified = await verify_owo_token(session, token)
        if verified:
            # Update balance may sometimes return `self.balance` as fallback if request fails
            # Hence we deduct 30 in advance here to take in consideration the points used for the solve.
            self.balance -= SOLVE_COST_POINTS
            # We still attempt to fetch balance since some times if multiple tries was made,
            # Failed attempts may temporarily consume points, which will take some time to be refunded.
            await self.update_balance()
        return verified

    async def solve_owo_bot_captcha(self, discord_headers, user_id, tries):
        """Unchanged public entry point: solve, verify, return True/False."""
        session, solution = await self._prepare_solve(
            discord_headers, user_id, tries
        )
        if solution is None:
            return False
        try:
            return await self.verify_token(session, solution)
        finally:
            await close_session(session)


class YesCaptchaProvider(BaseCaptchaProvider):
    """Adapter that lets the orchestrator drive YesCaptcha through `captchaClient`."""

    name = "yescaptcha"
    has_balance = True

    def __init__(
        self,
        api_key: str,
        *,
        priority: int = 1,
        min_retries: int = 3,
        max_retries: int = 5,
        client=None,
    ):
        super().__init__(
            priority=priority, min_retries=min_retries, max_retries=max_retries
        )
        self.client = client if client is not None else captchaClient(api_key)

    @property
    def balance(self):
        return self.client.balance

    @property
    def balance_probe(self):
        """The startup probe result, or None for a client that never probed."""
        return getattr(self.client, "balance_probe", None)

    async def solve(self, discord_headers: dict, user_id: int, retries: int) -> str | None:
        self.token_already_verified = False
        session = None
        try:
            session, token = await self.client._prepare_solve(
                discord_headers, user_id, retries
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.note_failure(
                FailureKind.PROVIDER_ERROR, f"error: {type(error).__name__}: {error}"
            )
            return None

        if token is None:
            error = self.client.last_error or "provider returned no token"
            kind = (
                FailureKind.NO_CREDITS
                if "balance" in error.lower()
                else FailureKind.PROVIDER_ERROR
            )
            self.note_failure(kind, error)
            run_log.write(
                f"provider={self.name} attempt={retries}/{retries} "
                f"variant=standard result=error: {error}"
            , level="DEBUG", category="captcha_provider")
            return None

        self.report.tokens += 1
        try:
            verified = await verify_owo_token(session, token)
            run_log.write(
                f"provider={self.name} attempt=1/{retries} "
                f"variant=standard "
                f"result={'token_received' if verified else 'verify_failed'}"
            , level="DEBUG", category="captcha_provider")
            if verified:
                self.token_already_verified = True
                self.last_error = None
                self.last_failure = None
                self.report.error = None
                self.report.failure = None
                return token
            self.note_failure(
                FailureKind.VERIFY_FAILED, "token was rejected by OwO"
            )
            return None
        finally:
            await close_session(session)

    async def after_verify(self, token: str, success: bool) -> None:
        if not success:
            return
        self.client.balance -= SOLVE_COST_POINTS
        await self.client.update_balance()

    async def release(self) -> None:
        await super().release()
