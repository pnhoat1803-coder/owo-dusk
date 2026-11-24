"""Orchestrator: priority order, retry budget, fallback, failure report, cancellation."""

import asyncio

import pytest

from utils.captcha_solver.base import CaptchaTotalFailure, FailureKind
from utils.captcha_solver.orchestrator import (
    CaptchaSolverOrchestrator,
    clamp_retries,
    normalise_providers,
)

from captcha_fakes import HangingProvider, ScriptedProvider


async def _always_true(session, token):
    return True


async def _always_false(session, token):
    return False


def test_priority_order_asks_lowest_number_first():
    async def run():
        later = ScriptedProvider("later", tokens=["late"], priority=2)
        earlier = ScriptedProvider("earlier", tokens=["early"], priority=1)
        verified = []

        async def verify(session, token):
            verified.append(token)
            return True

        orchestrator = CaptchaSolverOrchestrator([later, earlier], verify_fn=verify)
        assert orchestrator.describe() == "1:earlier -> 2:later"

        token = await orchestrator.solve_with_fallback({}, 7)

        assert token == "early"
        assert later.calls == []
        assert verified == ["early"]
        assert orchestrator.last_provider is earlier

    asyncio.run(run())


def test_retry_budget_is_clamped():
    config = {
        "hcaptcha_solver": {
            "enabled": True,
            "providers": [
                {
                    "name": "yescaptcha",
                    "enabled": True,
                    "priority": 1,
                    "api_key": "key",
                    "min_retries": 0,
                    "max_retries": 42,
                }
            ],
        }
    }
    entries, _notes = normalise_providers(config)
    assert entries[0]["min_retries"] == 3
    assert entries[0]["max_retries"] == 5

    assert clamp_retries(1) == 3
    assert clamp_retries(9) == 5
    assert clamp_retries(4) == 4

    async def run():
        provider = ScriptedProvider("yescaptcha", tokens=["token"], min_retries=3, max_retries=5)
        orchestrator = CaptchaSolverOrchestrator([provider], verify_fn=_always_true)

        await orchestrator.solve_with_fallback({}, 1)

        assert 3 <= provider.retries_seen[0] <= 5
        assert 3 <= orchestrator.last_reports[0].retries <= 5

    asyncio.run(run())


def test_fallback_to_the_next_provider():
    async def run():
        first = ScriptedProvider("first", tokens=[], priority=1)
        second = ScriptedProvider("second", tokens=["good"], priority=2)
        orchestrator = CaptchaSolverOrchestrator(
            [first, second], verify_fn=_always_true
        )

        token = await orchestrator.solve_with_fallback({}, 5)

        assert token == "good"
        assert len(first.calls) == 1
        assert len(second.calls) == 1
        assert first.report.failure is FailureKind.PROVIDER_ERROR
        assert [report.name for report in orchestrator.last_reports] == [
            "first",
            "second",
        ]

    asyncio.run(run())


def test_verify_failure_moves_to_the_next_provider():
    async def run():
        first = ScriptedProvider("first", tokens=["rejected"], priority=1)
        second = ScriptedProvider("second", tokens=["accepted"], priority=2)

        async def verify(session, token):
            return token == "accepted"

        orchestrator = CaptchaSolverOrchestrator([first, second], verify_fn=verify)

        token = await orchestrator.solve_with_fallback({}, 5)

        assert token == "accepted"
        assert first.report.failure is FailureKind.VERIFY_FAILED
        assert second.report.tokens == 1

    asyncio.run(run())


def test_total_failure_raises_with_a_report_per_provider():
    async def run():
        first = ScriptedProvider("first", tokens=[], priority=1)
        second = ScriptedProvider("second", tokens=[], priority=2)
        orchestrator = CaptchaSolverOrchestrator(
            [first, second], verify_fn=_always_true
        )

        with pytest.raises(CaptchaTotalFailure) as raised:
            await orchestrator.solve_with_fallback({}, 5)

        assert raised.value.provider_names == ["first", "second"]
        assert "first" in raised.value.summary()
        assert "second" in raised.value.summary()
        assert orchestrator.reports_text() == raised.value.summary()

    asyncio.run(run())


def test_provider_exception_is_tolerated():
    async def run():
        broken = ScriptedProvider("broken", error=RuntimeError("boom"), priority=1)
        backup = ScriptedProvider("backup", tokens=["token"], priority=2)
        orchestrator = CaptchaSolverOrchestrator(
            [broken, backup], verify_fn=_always_true
        )

        token = await orchestrator.solve_with_fallback({}, 1)

        assert token == "token"
        assert "boom" in broken.report.error

    asyncio.run(run())


def test_cancellation_stops_the_in_flight_solve_and_releases():
    async def run():
        provider = HangingProvider()
        orchestrator = CaptchaSolverOrchestrator([provider], verify_fn=_always_true)

        task = asyncio.create_task(orchestrator.solve_with_fallback({}, 1))
        await provider.started.wait()
        orchestrator.cancel_in_flight()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert provider.released >= 1
        assert orchestrator.last_reports[0].name == "hanging"
        assert orchestrator.last_reports[0].failure is not None
        # The slot is free again, so a later deadline cannot cancel an unrelated task
        orchestrator.cancel_in_flight()

    asyncio.run(run())


def test_after_verify_is_called_only_on_success():
    class RecordingProvider(ScriptedProvider):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.after = []

        async def after_verify(self, token, success):
            self.after.append((token, success))

    async def run():
        provider = RecordingProvider("yescaptcha", tokens=["token"])
        orchestrator = CaptchaSolverOrchestrator([provider], verify_fn=_always_true)

        await orchestrator.solve_with_fallback({}, 1)

        assert provider.after == [("token", True)]

    asyncio.run(run())


def test_balance_summary_follows_the_solving_provider():
    class BalanceProvider(ScriptedProvider):
        has_balance = True

        def __init__(self, *args, balance=90, **kwargs):
            super().__init__(*args, **kwargs)
            self._balance = balance

        @property
        def balance(self):
            return self._balance

    async def run():
        provider = BalanceProvider("yescaptcha", tokens=["token"])
        orchestrator = CaptchaSolverOrchestrator([provider], verify_fn=_always_true)

        assert orchestrator.balance_summary() == (90, 3)

        await orchestrator.solve_with_fallback({}, 1)

        assert orchestrator.balance_summary() == (90, 3)

    asyncio.run(run())


def test_solve_without_balance_provider_returns_none():
    async def run():
        provider = ScriptedProvider("captchaly", tokens=["token"])
        orchestrator = CaptchaSolverOrchestrator([provider], verify_fn=_always_true)

        await orchestrator.solve_with_fallback({}, 1)

        assert orchestrator.balance_summary() is None

    asyncio.run(run())
