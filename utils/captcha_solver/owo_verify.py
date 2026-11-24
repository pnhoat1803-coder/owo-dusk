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

"""Everything that talks to owobot.com for a captcha solve.

`OwoAuth` performs the Discord OAuth login and caches the resulting cookies per
Discord user id. `verify_owo_token` is the one verify POST every provider uses,
so the success criterion cannot drift between backends. OwO is asked to verify
a token, so the session that did the OAuth login is the session that verifies.
"""

from __future__ import annotations

import asyncio
import json

import aiohttp

from utils import run_log

OWO_BASE_URL = "https://owobot.com"
OWO_VERIFY_URL = f"{OWO_BASE_URL}/api/captcha/verify"
OWO_CAPTCHA_PAGE_URL = f"{OWO_BASE_URL}/captcha"
OWO_AUTH_CHECK_URL = f"{OWO_BASE_URL}/api/auth"

DISCORD_AUTHORIZE_URL = (
    "https://discord.com/api/v9/oauth2/authorize"
    "?client_id=408785106942164992"
    "&response_type=code"
    "&redirect_uri=https://owobot.com/api/auth/discord/redirect"
    "&scope=identify guilds"
)

# Body of the Discord authorize POST, as OwO-Dusk has always sent it
OAUTH_PAYLOAD = {
    "authorize": True,
    "integration_type": 0,
    "permissions": "0",
    "location_context": {
        "guild_id": "10000",
        "channel_id": "10000",
        "channel_type": 10000,
    },
}

VERIFY_HEADERS = {
    "Referer": f"{OWO_BASE_URL}/captcha",
    "Origin": OWO_BASE_URL,
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
}

# The OAuth login and the verify POST are small JSON requests, not solves
OAUTH_TIMEOUT = aiohttp.ClientTimeout(total=30)
VERIFY_TIMEOUT = aiohttp.ClientTimeout(total=30)


class OwoAuthError(RuntimeError):
    """The Discord OAuth login for owobot.com did not complete."""


async def close_session(session) -> None:
    """Close an aiohttp session unless it is already gone. Never raises."""
    if session is None:
        return
    try:
        if not session.closed:
            await session.close()
    except Exception:
        pass


class OwoAuth:
    """Discord OAuth login for owobot.com, with an in-memory cookie cache.

    The cache is keyed by Discord user id and shared by every provider, which
    is safe because OwO-Dusk runs one process per set of accounts and the
    cookies belong to the account, not to the solver.
    """

    def __init__(self, auth_url: str = DISCORD_AUTHORIZE_URL, payload: dict | None = None):
        self._auth_url = auth_url
        self._payload = dict(payload or OAUTH_PAYLOAD)
        self._cookie_cache: dict[int, object] = {}

    @property
    def auth_url(self) -> str:
        return self._auth_url

    @property
    def cookie_cache(self) -> dict:
        """The live per-user cookie map, so a caller can inspect or clear it."""
        return self._cookie_cache

    def forget(self, user_id) -> None:
        self._cookie_cache.pop(user_id, None)

    async def open_session(self, discord_headers: dict, user_id):
        """Return an aiohttp session logged in to owobot.com for this user.

        The caller owns the session and must close it. Raises OwoAuthError when
        the login cannot be completed; cancellation is propagated as-is.
        """
        cookie = self._cookie_cache.get(user_id)
        session = aiohttp.ClientSession(cookies=cookie)
        try:
            if cookie:
                if await self._is_authenticated(session):
                    run_log.write("Using cached OAuth session!", level="INFO", category="captcha")
                    return session
                # Cookie expired or invalid
                self._cookie_cache.pop(user_id, None)

            # https://docs.aiohttp.org/en/stable/abc.html#aiohttp.abc.AbstractCookieJar.clear
            session.cookie_jar.clear()
            await self._authorize(session, discord_headers)
            await self._require_captcha_page(session)
            if not await self._require_auth(session):
                raise OwoAuthError("auth data was empty after the OAuth login")

            # https://docs.aiohttp.org/en/stable/abc.html#aiohttp.abc.AbstractCookieJar.filter_cookies
            self._cookie_cache[user_id] = session.cookie_jar.filter_cookies(OWO_BASE_URL)
            return session
        except BaseException:
            # Cancellation and every login failure must not leak the session
            await close_session(session)
            raise

    async def _is_authenticated(self, session) -> bool:
        try:
            async with session.get(OWO_AUTH_CHECK_URL, timeout=OAUTH_TIMEOUT) as resp:
                if resp.status == 200 and await resp.json():
                    return True
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        return False

    async def _authorize(self, session, discord_headers: dict) -> None:
        async with session.post(
            self._auth_url,
            json=self._payload,
            headers=discord_headers,
            allow_redirects=True,
            timeout=OAUTH_TIMEOUT,
        ) as oauth_resp:
            if oauth_resp.status != 200:
                raise OwoAuthError(f"authorize returned HTTP {oauth_resp.status}")
            oauth_text = await oauth_resp.text()

        try:
            redirect_url = json.loads(oauth_text).get("location")
        except Exception as error:
            raise OwoAuthError(f"authorize response could not be parsed: {error}") from error

        if redirect_url:
            async with session.get(redirect_url, timeout=OAUTH_TIMEOUT) as redirect_resp:
                if redirect_resp.status != 200:
                    raise OwoAuthError(
                        f"OAuth redirect returned HTTP {redirect_resp.status}"
                    )

    async def _require_captcha_page(self, session) -> None:
        async with session.get(OWO_CAPTCHA_PAGE_URL, timeout=OAUTH_TIMEOUT) as captcha_resp:
            if captcha_resp.status != 200:
                raise OwoAuthError(f"captcha page returned HTTP {captcha_resp.status}")

    async def _require_auth(self, session) -> bool:
        async with session.get(OWO_AUTH_CHECK_URL, timeout=OAUTH_TIMEOUT) as auth_resp:
            if auth_resp.status != 200:
                raise OwoAuthError(f"auth check returned HTTP {auth_resp.status}")
            return bool(await auth_resp.json())


async def verify_owo_token(session, token: str) -> bool:
    """POST one hCaptcha token to OwO and return whether it was accepted.

    The success criterion matches the original implementation: HTTP 200 only,
    the response body is not parsed. Failure details are printed for the run log.
    """
    if not token:
        return False
    try:
        async with session.post(
            OWO_VERIFY_URL,
            json={"token": token},
            headers=VERIFY_HEADERS,
            timeout=VERIFY_TIMEOUT,
        ) as verify_resp:
            if verify_resp.status == 200:
                return True
            error_text = await verify_resp.text()
            run_log.write(f"Verification failed (Status {verify_resp.status})", level="WARNING", category="captcha")
            run_log.write(f"Server Response: {error_text[:300]}", level="WARNING", category="captcha")
            return False
    except asyncio.CancelledError:
        raise
    except (asyncio.TimeoutError, TimeoutError):
        run_log.write("Verification timed out after 30 seconds", level="WARNING", category="captcha")
        return False
    except aiohttp.ClientError as error:
        run_log.write(f"Verification request failed: {error}", level="WARNING", category="captcha")
        return False
    except Exception as error:
        run_log.write(f"Verification failed with an unexpected error: {error}", level="WARNING", category="captcha")
        return False
