"""Captchaly provider: single GET flow, enterprise ladder, timeout discipline."""

import asyncio

import pytest

from utils.captcha_solver import captchaly as captchaly_module
from utils.captcha_solver.base import FailureKind
from utils.captcha_solver.captchaly import (
    CAPTCHALY_ENTERPRISE_URL,
    CAPTCHALY_STANDARD_URL,
    OWO_SITEKEY,
    OWO_SITE_URL,
    CaptchalyProvider,
)

from captcha_fakes import (
    FakeOwoAuth,
    FakeResponse,
    FakeSession,
    FakeSessionFactory,
)

API_KEY = "CP_test_key"


@pytest.fixture(autouse=True)
def _clear_variant_cache():
    CaptchalyProvider.forget_variant()
    yield
    CaptchalyProvider.forget_variant()


def _provider(session, **kwargs):
    factory = FakeSessionFactory(session)
    kwargs.setdefault("owo_auth", None)
    provider = CaptchalyProvider(
        API_KEY, session_factory=factory, **kwargs
    )
    return provider, factory


async def _always_true(session, token):
    return True


async def _always_false(session, token):
    return False


def test_returns_token_on_200(monkeypatch):
    async def run():
        session = FakeSession([FakeResponse(200, '{"token": "abc123"}')])
        provider, factory = _provider(session)

        token = await provider.solve({}, 1, 3)

        assert token == "abc123"
        assert factory.calls == 1
        assert factory.kwargs["timeout"].total == 120
        assert session.closed is True

        request = session.requests[0]
        assert request["method"] == "GET"
        assert request["url"] == CAPTCHALY_STANDARD_URL
        assert request["kwargs"]["params"] == {"sitekey": OWO_SITEKEY, "url": OWO_SITE_URL}
        assert request["kwargs"]["headers"]["Authorization"] == f"Bearer {API_KEY}"
        assert request["kwargs"]["timeout"].total == 120
        # Without an OwO session the orchestrator does the verify
        assert provider.token_already_verified is False

    asyncio.run(run())


def test_503_returns_none():
    async def run():
        session = FakeSession([FakeResponse(503, "") for _ in range(3)])
        provider, _ = _provider(session)

        token = await provider.solve({}, 1, 3)

        assert token is None
        assert provider.last_failure is FailureKind.PROVIDER_ERROR
        assert "503" in provider.last_error
        assert len(session.requests) == 3
        assert session.closed is True

    asyncio.run(run())


def test_429_returns_none_and_stops_retrying(monkeypatch):
    monkeypatch.setattr(captchaly_module, "CONCURRENCY_BACKOFF_SECONDS", 0)

    async def run():
        session = FakeSession([FakeResponse(429, "")])
        provider, _ = _provider(session)

        token = await provider.solve({}, 1, 5)

        assert token is None
        assert provider.last_failure is FailureKind.RATE_LIMITED
        # No immediate retry of the same provider
        assert len(session.requests) == 1

    asyncio.run(run())


def test_timeout_returns_none():
    async def run():
        session = FakeSession([asyncio.TimeoutError() for _ in range(3)])
        provider, _ = _provider(session)

        token = await provider.solve({}, 1, 3)

        assert token is None
        assert provider.last_failure is FailureKind.TIMEOUT
        assert "120" in provider.last_error

    asyncio.run(run())


def test_200_without_token_field_returns_none():
    async def run():
        session = FakeSession([FakeResponse(200, '{"success": false}') for _ in range(3)])
        provider, _ = _provider(session)

        token = await provider.solve({}, 1, 3)

        assert token is None
        assert provider.last_failure is FailureKind.PROVIDER_ERROR
        assert "token" in provider.last_error

    asyncio.run(run())


def test_enterprise_ladder_probes_both_endpoints(monkeypatch):
    """Three retries on standard fail, then enterprise verifies: cached for next time."""
    monkeypatch.setattr(captchaly_module, "verify_owo_token", _always_true)

    async def run():
        responses = [
            FakeResponse(503, ""),
            FakeResponse(503, ""),
            FakeResponse(200, '{"token": "enterprise-token"}'),
        ]
        session = FakeSession(responses)
        verify_session = FakeSession()
        provider, _ = _provider(
            session,
            auto_detect_enterprise=True,
            owo_auth=FakeOwoAuth(verify_session),
        )

        # ceil(3 / 2) = 2 standard attempts, then 1 enterprise attempt
        token = await provider.solve({}, 1, 3)

        assert token == "enterprise-token"
        assert [request["url"] for request in session.requests] == [
            CAPTCHALY_STANDARD_URL,
            CAPTCHALY_STANDARD_URL,
            CAPTCHALY_ENTERPRISE_URL,
        ]
        assert provider.token_already_verified is True
        assert CaptchalyProvider.known_variant() == "enterprise"

    asyncio.run(run())


def test_cached_variant_is_tried_first(monkeypatch):
    monkeypatch.setattr(captchaly_module, "verify_owo_token", _always_true)
    CaptchalyProvider._variant_cache[OWO_SITEKEY] = "enterprise"

    async def run():
        session = FakeSession([FakeResponse(200, '{"token": "tok"}')])
        provider, _ = _provider(
            session,
            auto_detect_enterprise=True,
            owo_auth=FakeOwoAuth(FakeSession()),
        )

        token = await provider.solve({}, 1, 4)

        assert token == "tok"
        assert session.requests[0]["url"] == CAPTCHALY_ENTERPRISE_URL

    asyncio.run(run())


def test_auto_detect_off_uses_configured_endpoint_only():
    async def run():
        session = FakeSession([FakeResponse(200, '{"token": "tok"}')])
        provider, _ = _provider(
            session,
            auto_detect_enterprise=False,
            captcha_type="enterprise",
        )

        token = await provider.solve({}, 1, 3)

        assert token == "tok"
        assert [request["url"] for request in session.requests] == [
            CAPTCHALY_ENTERPRISE_URL
        ]

    asyncio.run(run())


def test_verify_failure_exhausts_the_budget(monkeypatch):
    monkeypatch.setattr(captchaly_module, "verify_owo_token", _always_false)

    async def run():
        session = FakeSession([FakeResponse(200, '{"token": "bad"}') for _ in range(2)])
        provider, _ = _provider(
            session,
            auto_detect_enterprise=False,
            captcha_type="standard",
            owo_auth=FakeOwoAuth(FakeSession()),
        )

        token = await provider.solve({}, 1, 2)

        assert token is None
        assert provider.last_failure is FailureKind.VERIFY_FAILED
        assert len(session.requests) == 2
        # The OwO verify session is owned by the orchestrator, not closed here
        assert session.closed is True
        assert CaptchalyProvider.known_variant() is None

    asyncio.run(run())


def test_release_closes_both_sessions():
    async def run():
        http = FakeSession([FakeResponse(503, "")])
        verify = FakeSession()
        provider, _ = _provider(
            http, auto_detect_enterprise=False, owo_auth=FakeOwoAuth(verify)
        )

        await provider.solve({}, 1, 1)
        await provider.release()

        assert http.closed is True
        assert verify.closed is True
        assert provider.verify_session is None

    asyncio.run(run())


def test_cancellation_is_propagated_and_session_closed():
    async def run():
        session = FakeSession([FakeResponse(200, '{"token": "slow"}', delay=30)])
        provider, _ = _provider(session)

        task = asyncio.create_task(provider.solve({}, 1, 1))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert session.closed is True

    asyncio.run(run())
