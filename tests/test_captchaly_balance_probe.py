"""Captchaly startup balance probe: documented /account first, then fallbacks.

A missing balance is informational only: it must never disable the provider.
Only a 401/403 (rejected api_key) makes captchaly unusable.
"""

import asyncio

from utils.captcha_solver.captchaly import (
    CAPTCHALY_ACCOUNT_URL,
    CAPTCHALY_BALANCE_CANDIDATES,
    CAPTCHALY_BALANCE_TIMEOUT,
    CaptchalyBalance,
    probe_captchaly_balance,
)
from utils.captcha_solver.orchestrator import build_orchestrator

from captcha_fakes import FakeYesCaptchaClient


class FakeBalanceResponse:
    def __init__(self, status, body=""):
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def text(self):
        return self._body


class FakeBalanceSession:
    def __init__(self, responses):
        self._queue = list(responses)
        self.requests = []
        self.closed = False

    def get(self, url, **kwargs):
        self.requests.append({"url": url, "kwargs": kwargs})
        item = self._queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self):
        self.closed = True


def _no_sleep(seconds):
    async def _sleep():
        return None

    return _sleep()


def _new_config():
    return {
        "hcaptcha_solver": {
            "auto_detect_enterprise": True,
            "captchaly": {
                "enabled": True,
                "priority": 1,
                "api_key": "CP_KEY",
                "min_retries": 3,
                "max_retries": 5,
            },
            "yescaptcha": {
                "enabled": True,
                "priority": 2,
                "api_key": "YES_KEY",
                "min_retries": 3,
                "max_retries": 5,
            },
        }
    }


def _build(probe, yes_balance=100):
    return build_orchestrator(
        _new_config(),
        captchaly_probe=lambda key: probe,
        client_factory=lambda key: FakeYesCaptchaClient(balance=yes_balance),
    )


def test_captchaly_balance_first_candidate_returns_balance():
    session = FakeBalanceSession(
        [FakeBalanceResponse(200, '{"id": "u", "email": "e", "balance": 42, "join_at": 1}')]
    )

    result = asyncio.run(
        probe_captchaly_balance("CP_KEY", session=session, sleep=_no_sleep)
    )

    assert result.balance == 42
    assert result.endpoint == CAPTCHALY_ACCOUNT_URL
    assert result.auth_failed is False
    request = session.requests[0]
    assert request["url"] == CAPTCHALY_ACCOUNT_URL
    assert request["kwargs"]["headers"]["Authorization"] == "Bearer CP_KEY"
    assert request["kwargs"]["timeout"].total == 10
    assert session.requests[0]["kwargs"]["timeout"] is CAPTCHALY_BALANCE_TIMEOUT


def test_captchaly_balance_falls_through_to_later_candidate():
    session = FakeBalanceSession(
        [
            FakeBalanceResponse(404, ""),
            FakeBalanceResponse(200, '{"credits": 7}'),
        ]
    )

    result = asyncio.run(
        probe_captchaly_balance("CP_KEY", session=session, sleep=_no_sleep)
    )

    assert result.balance == 7
    assert result.endpoint == CAPTCHALY_BALANCE_CANDIDATES[1]
    assert [request["url"] for request in session.requests] == [
        CAPTCHALY_BALANCE_CANDIDATES[0],
        CAPTCHALY_BALANCE_CANDIDATES[1],
    ]


def test_captchaly_balance_all_endpoints_404_returns_None():
    session = FakeBalanceSession(
        [FakeBalanceResponse(404, "") for _ in CAPTCHALY_BALANCE_CANDIDATES]
    )

    result = asyncio.run(
        probe_captchaly_balance("CP_KEY", session=session, sleep=_no_sleep)
    )

    assert result.balance is None
    assert result.auth_failed is False
    assert result.endpoint is None
    assert len(session.requests) == len(CAPTCHALY_BALANCE_CANDIDATES)


def test_captchaly_balance_401_is_auth_failure():
    session = FakeBalanceSession([FakeBalanceResponse(401, '{"detail": "Unauthenticated!"}')])

    result = asyncio.run(
        probe_captchaly_balance("CP_KEY", session=session, sleep=_no_sleep)
    )

    assert result.auth_failed is True
    assert result.balance is None
    assert result.endpoint == CAPTCHALY_ACCOUNT_URL
    # A rejected key is final: no later candidate is tried
    assert len(session.requests) == 1

    orchestrator = _build(result)
    assert [provider.name for provider in orchestrator.providers] == ["yescaptcha"]
    assert any("captchaly auth failed" in note for note in orchestrator.notes)


def test_captchaly_balance_returns_float():
    session = FakeBalanceSession([FakeBalanceResponse(200, '{"balance": 0.15}')])

    result = asyncio.run(
        probe_captchaly_balance("CP_KEY", session=session, sleep=_no_sleep)
    )

    assert result.balance == 0.15
    assert isinstance(result.balance, float)

    orchestrator = _build(result)
    # Captchaly is USD with 2 decimals; YesCaptcha stays integer points
    assert orchestrator.providers[0].balance_display() == "$0.15"
    assert orchestrator.providers[1].balance_display() == "100"


def test_captchaly_balance_zero_keeps_provider_enabled():
    probe = CaptchalyBalance(balance=0.0, endpoint=CAPTCHALY_ACCOUNT_URL)

    orchestrator = _build(probe)

    assert [provider.name for provider in orchestrator.providers] == [
        "captchaly",
        "yescaptcha",
    ]
    assert orchestrator.providers[0].balance_display() == "$0.00"
    assert any("captchaly balance is 0" in note for note in orchestrator.notes)


def test_captchaly_balance_none_keeps_provider_enabled():
    probe = CaptchalyBalance()

    orchestrator = _build(probe)

    assert [provider.name for provider in orchestrator.providers] == [
        "captchaly",
        "yescaptcha",
    ]
    assert orchestrator.providers[0].balance_display() == "unknown — endpoint not found"
