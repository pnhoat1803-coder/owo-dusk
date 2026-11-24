"""The remote safety check pauses the bot with its own flag and clears only that flag.

The remote files are answered by a fake session, so no request leaves the test."""

import asyncio

import pytest

import core.client as client_module
from utils.loader import global_settings_dict

UNSAFE = {"version": "999.0.0", "reason": "fake reason for tests", "author": "tests"}
SAFE = {"version": "0.0.1", "reason": "", "author": ""}
LATEST = {"version": "999.0.1"}
PAUSED = "Safety check: paused (remote flag)"
RESUMED = "Safety check: resumed"


class FakeResponse:
    def __init__(self, data):
        self._data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._data


class FakeSession:
    def __init__(self, safety):
        self.safety = safety

    def get(self, url, timeout=None):
        if url.endswith("/safety_check.json"):
            return FakeResponse(self.safety)
        if url.endswith("/version.json"):
            return FakeResponse(LATEST)
        raise AssertionError("the test session only answers the two remote files")


class StubOrchestrator:
    balance = 100

    def describe(self):
        return "stub"

    def balance_summary(self):
        return None


@pytest.fixture
def bot(monkeypatch):
    stub = StubOrchestrator()
    monkeypatch.setattr(client_module, "captcha_orchestrator", stub)
    monkeypatch.setattr(client_module, "hcaptcha_solver", stub)
    client = client_module.MyClient(
        "FAKE.TOKEN.VALUE",
        123456789,
        global_settings_dict,
        1,
        account_index=1,
    )
    client.test_log_lines = []

    async def fake_log(text, color=None):
        client.test_log_lines.append(text)

    client.log = fake_log
    return client


def _run_check(bot, remote_result):
    bot.session = FakeSession(remote_result)
    asyncio.run(client_module.MyClient.safety_check_loop.coro(bot))


def _lines(bot, prefix):
    return [line for line in bot.test_log_lines if line.startswith(prefix)]


def test_unsafe_result_pauses_with_the_safety_flag_only(bot):
    _run_check(bot, UNSAFE)

    status = bot.ch.command_handler_status
    assert status.safety_check is True
    assert status.captcha is False
    assert len(_lines(bot, PAUSED)) == 1


def test_repeated_unsafe_results_log_the_pause_once(bot):
    _run_check(bot, UNSAFE)
    _run_check(bot, UNSAFE)

    assert len(_lines(bot, PAUSED)) == 1
    assert bot.ch.command_handler_status.safety_check is True


def test_safe_result_resumes_and_logs_once(bot):
    _run_check(bot, UNSAFE)
    _run_check(bot, SAFE)
    _run_check(bot, SAFE)

    status = bot.ch.command_handler_status
    assert status.safety_check is False
    assert status.captcha is False
    assert len(_lines(bot, RESUMED)) == 1


def test_a_real_captcha_pause_survives_a_safe_result(bot):
    status = bot.ch.command_handler_status
    status.captcha = True

    _run_check(bot, SAFE)

    assert status.captcha is True
    assert status.safety_check is False
    assert _lines(bot, RESUMED) == []


def test_captcha_and_safety_together_only_the_safety_flag_is_cleared(bot):
    status = bot.ch.command_handler_status
    status.captcha = True

    _run_check(bot, UNSAFE)
    _run_check(bot, SAFE)

    assert status.captcha is True
    assert status.safety_check is False


def test_the_send_gates_follow_the_safety_flag(bot):
    handler = bot.ch
    assert handler.should_not_send is False
    assert handler.can_send_if_priority is True

    _run_check(bot, UNSAFE)
    assert handler.should_not_send is True
    assert handler.can_send_if_priority is False

    _run_check(bot, SAFE)
    assert handler.should_not_send is False
    assert handler.can_send_if_priority is True
