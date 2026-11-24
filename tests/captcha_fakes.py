"""Fakes shared by the captcha solver tests.

Nothing here talks to the network: the fake aiohttp layer only replays canned
responses, and the scripted providers record what the orchestrator asked them.
"""

import asyncio
import json

from utils.captcha_solver.base import BaseCaptchaProvider, FailureKind


class FakeResponse:
    def __init__(self, status=200, body="", delay=0.0):
        self.status = status
        self._body = body
        self.delay = delay

    async def __aenter__(self):
        if self.delay:
            await asyncio.sleep(self.delay)
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def text(self):
        return self._body

    async def json(self):
        return json.loads(self._body)


class FakeErrorResponse:
    """A context manager that raises on enter, like a failed aiohttp request."""

    def __init__(self, error):
        self._error = error

    async def __aenter__(self):
        raise self._error

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeSession:
    def __init__(self, responses=None, delay=0.0):
        self._queue = list(responses or [])
        self.delay = delay
        self.closed = False
        self.requests = []
        self.default_response = FakeResponse(200, '{"token": "default"}')

    def _take(self):
        if not self._queue:
            return self.default_response
        item = self._queue.pop(0)
        if isinstance(item, BaseException):
            return FakeErrorResponse(item)
        return item

    def get(self, url, **kwargs):
        self.requests.append({"method": "GET", "url": url, "kwargs": kwargs})
        return self._take()

    def post(self, url, **kwargs):
        self.requests.append({"method": "POST", "url": url, "kwargs": kwargs})
        return self._take()

    async def close(self):
        self.closed = True


class FakeSessionFactory:
    def __init__(self, session):
        self.session = session
        self.kwargs = None
        self.calls = 0

    def __call__(self, **kwargs):
        self.calls += 1
        self.kwargs = kwargs
        return self.session


class FakeOwoAuth:
    def __init__(self, session):
        self.session = session
        self.calls = 0

    async def open_session(self, discord_headers, user_id):
        self.calls += 1
        return self.session


class ScriptedProvider(BaseCaptchaProvider):
    """A provider whose answers are decided by the test."""

    def __init__(
        self,
        name,
        *,
        tokens=None,
        priority=1,
        min_retries=3,
        max_retries=5,
        error=None,
    ):
        self.name = name
        super().__init__(
            priority=priority, min_retries=min_retries, max_retries=max_retries
        )
        self.report.name = name
        self._tokens = list(tokens or [])
        self.error = error
        self.calls = []
        self.retries_seen = []
        self.released = 0

    async def solve(self, discord_headers, user_id, retries):
        self.calls.append({"headers": discord_headers, "user_id": user_id, "retries": retries})
        self.retries_seen.append(retries)
        self.report.attempts += 1
        # A real provider opens its OwO session while solving
        self.verify_session = object()
        if self.error is not None:
            raise self.error
        if not self._tokens:
            self.note_failure(FailureKind.PROVIDER_ERROR, "scripted failure")
            return None
        self.report.tokens += 1
        return self._tokens.pop(0)

    async def release(self):
        self.released += 1
        await super().release()


class HangingProvider(BaseCaptchaProvider):
    """Stays inside solve until the test cancels it."""

    def __init__(self):
        self.name = "hanging"
        super().__init__(priority=1, min_retries=3, max_retries=3)
        self.started = asyncio.Event()
        self.released = 0

    async def solve(self, discord_headers, user_id, retries):
        self.started.set()
        await asyncio.sleep(3600)

    async def release(self):
        self.released += 1
        await super().release()


class FakeYesCaptchaClient:
    """Stand-in for captchaClient, without its blocking balance request."""

    def __init__(self, balance=100, balance_probe=None):
        self.balance = balance
        self.balance_probe = balance_probe
        self.last_error = None
        self.update_calls = 0

    async def update_balance(self):
        self.update_calls += 1
