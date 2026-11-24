"""YesCaptcha startup balance probe: network vs auth vs a real zero balance."""

import pytest
import requests

from utils.captcha_solver.base import FailureKind
from utils.captcha_solver.orchestrator import CaptchaConfigError, build_orchestrator
from utils.captcha_solver.yescaptcha import BalanceProbe, probe_yescaptcha_balance

from captcha_fakes import FakeYesCaptchaClient


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def _post_sequence(*results):
    results = list(results)
    calls = []

    def post(url, **kwargs):
        calls.append(kwargs)
        item = results.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    return post, calls


def _provider_config(*priorities):
    return {
        "hcaptcha_solver": {
            "enabled": True,
            "providers": [
                {
                    "name": "yescaptcha",
                    "enabled": True,
                    "priority": priority,
                    "api_key": f"KEY{priority}",
                    "min_retries": 3,
                    "max_retries": 5,
                }
                for priority in priorities
            ],
        }
    }


def test_three_timeouts_drop_the_provider_with_a_network_message():
    post, calls = _post_sequence(
        requests.exceptions.Timeout(),
        requests.exceptions.Timeout(),
        requests.exceptions.Timeout(),
    )
    sleeps = []

    probe = probe_yescaptcha_balance("KEY", post=post, sleep=sleeps.append)

    assert len(calls) == 3
    assert sleeps == [2.0, 4.0]
    assert probe.failure is FailureKind.NETWORK
    assert probe.attempts == 3
    assert "unreachable after 3 attempts (network)" in probe.startup_message()

    client = FakeYesCaptchaClient(balance=0, balance_probe=probe)
    with pytest.raises(CaptchaConfigError) as raised:
        build_orchestrator(_provider_config(1), client_factory=lambda key: client)

    message = str(raised.value)
    assert "unreachable after 3 attempts (network)" in message
    assert "request timed out after 10s" in message
    assert "Fix config/captcha.toml" not in message


def test_timeout_then_success_keeps_the_provider():
    post, calls = _post_sequence(
        requests.exceptions.Timeout(),
        FakeResponse(200, {"errorId": 0, "balance": 120}),
    )
    sleeps = []

    probe = probe_yescaptcha_balance("KEY", post=post, sleep=sleeps.append)

    assert probe.failure is None
    assert probe.balance == 120
    assert probe.usable
    assert sleeps == [2.0]
    assert len(calls) == 2

    client = FakeYesCaptchaClient(balance=120, balance_probe=probe)
    orchestrator = build_orchestrator(
        _provider_config(1), client_factory=lambda key: client
    )

    assert [provider.name for provider in orchestrator.providers] == ["yescaptcha"]
    assert orchestrator.providers[0].balance == 120
    assert orchestrator.notes == []


def test_http_401_is_an_auth_failure():
    post, calls = _post_sequence(FakeResponse(401))

    probe = probe_yescaptcha_balance(
        "KEY", post=post, sleep=lambda seconds: pytest.fail("auth must not retry")
    )

    assert probe.failure is FailureKind.AUTH
    assert probe.attempts == 1
    assert probe.startup_message() == "YesCaptcha auth failed (check api_key)"
    assert len(calls) == 1


def test_real_zero_balance_is_reported_as_balance():
    post, _calls = _post_sequence(FakeResponse(200, {"errorId": 0, "balance": 0}))

    probe = probe_yescaptcha_balance(
        "KEY", post=post, sleep=lambda seconds: pytest.fail("zero balance must not retry")
    )

    assert probe.failure is FailureKind.NO_CREDITS
    assert probe.startup_message() == (
        "YesCaptcha balance is zero (top up to continue)"
    )


def test_unreachable_priority_one_falls_back_to_healthy_priority_two():
    unreachable = BalanceProbe(
        failure=FailureKind.NETWORK,
        detail="request timed out after 10s",
        attempts=3,
    )
    clients = [
        FakeYesCaptchaClient(balance=0, balance_probe=unreachable),
        FakeYesCaptchaClient(balance=65846, balance_probe=BalanceProbe(balance=65846, attempts=1)),
    ]

    orchestrator = build_orchestrator(
        _provider_config(1, 2), client_factory=lambda key: clients.pop(0)
    )

    assert [provider.priority for provider in orchestrator.providers] == [2]
    assert orchestrator.providers[0].balance == 65846
    assert any(
        "unreachable after 3 attempts (network)" in note
        for note in orchestrator.notes
    )


def test_all_providers_unusable_aborts_with_the_real_cause():
    auth = BalanceProbe(failure=FailureKind.AUTH, detail="HTTP 401", attempts=1)
    client = FakeYesCaptchaClient(balance=0, balance_probe=auth)

    with pytest.raises(CaptchaConfigError) as raised:
        build_orchestrator(_provider_config(1), client_factory=lambda key: client)

    message = str(raised.value)
    assert "YesCaptcha auth failed (check api_key)" in message
    assert "Fix config/captcha.toml" not in message
