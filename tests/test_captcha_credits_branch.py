"""Credits-exhausted branch: stopScope decides pause, account stop or process exit."""

import asyncio

import core.cogs.captcha as captcha_module
from core.cogs.captcha import Captcha


class FakeStatus:
    def __init__(self):
        self.captcha = False
        self.captcha_flag = False


class FakeChannelHandler:
    def __init__(self):
        self.command_handler_status = FakeStatus()


class FakeCaptchaSettings:
    def __init__(self, stop_if_failure, stop_scope):
        self.stopIfFailure = stop_if_failure
        self.stopScope = stop_scope
        self.timeoutSeconds = 600
        self.flagOnTimeout = True


class FakeWebhookSettings:
    enabled = False
    webhookCaptchaUrl = None
    pingUserId = ""


class FakeGlobalSettings:
    def __init__(self, stop_if_failure, stop_scope):
        self.captcha = FakeCaptchaSettings(stop_if_failure, stop_scope)
        self.webhook = FakeWebhookSettings()


class FakeBot:
    def __init__(self, stop_if_failure, stop_scope):
        self.username = "tester"
        self.global_settings_dict = FakeGlobalSettings(stop_if_failure, stop_scope)
        self.ch = FakeChannelHandler()
        self.stopped = False
        self.logged = []
        self.stop_calls = []

    async def log(self, text, color=None, **kwargs):
        self.logged.append(text)

    async def stop_account(self, reason):
        self.stop_calls.append(reason)


class FakeOrchestrator:
    def __init__(self, balance_data):
        self.balance_data = balance_data
        self.summary_calls = 0

    def balance_summary(self):
        self.summary_calls += 1
        return self.balance_data


def _build(stop_if_failure, stop_scope, monkeypatch):
    bot = FakeBot(stop_if_failure, stop_scope)
    cog = Captcha(bot)
    exit_calls = []
    finish_calls = []
    monkeypatch.setattr(
        captcha_module.run_log,
        "exit_process",
        lambda code=0: exit_calls.append(code),
    )
    monkeypatch.setattr(
        captcha_module.run_log,
        "captcha_finish",
        lambda result, reason: finish_calls.append((result, reason)),
    )
    return cog, bot, exit_calls, finish_calls


def test_stop_disabled_pauses_without_stopping_or_exiting(monkeypatch):
    cog, bot, exit_calls, finish_calls = _build(False, "account", monkeypatch)

    asyncio.run(cog.handle_credits_exhausted(12))

    assert exit_calls == []
    assert bot.stop_calls == []
    assert finish_calls == [("FAILED", "credits exhausted - balance: 12")]
    assert bot.ch.command_handler_status.captcha is True
    assert any("account paused" in line for line in bot.logged)


def test_account_scope_stops_the_account_only(monkeypatch):
    cog, bot, exit_calls, _finish_calls = _build(True, "account", monkeypatch)

    asyncio.run(cog.handle_credits_exhausted(12))

    assert bot.stop_calls == ["credits exhausted - balance: 12"]
    assert exit_calls == []


def test_process_scope_exits_the_process(monkeypatch):
    cog, bot, exit_calls, finish_calls = _build(True, "process", monkeypatch)

    asyncio.run(cog.handle_credits_exhausted(12))

    assert exit_calls == [0]
    assert bot.stop_calls == []
    assert finish_calls == [
        ("FAILED", "credits exhausted - balance: 12, process stopping")
    ]


def test_stop_disabled_with_process_scope_still_does_not_exit(monkeypatch):
    # `if not stopIfFailure` must short-circuit before the scope check
    cog, bot, exit_calls, finish_calls = _build(False, "process", monkeypatch)

    asyncio.run(cog.handle_credits_exhausted(12))

    assert bot.stop_calls == []
    assert exit_calls == []
    assert finish_calls == [("FAILED", "credits exhausted - balance: 12")]
    assert bot.ch.command_handler_status.captcha is True


def test_handle_credits_exhausted_is_called_with_current_balance(monkeypatch):
    cog, bot, exit_calls, _finish_calls = _build(True, "account", monkeypatch)
    orchestrator = FakeOrchestrator((60, 2))
    calls = []
    real_handler = cog.handle_credits_exhausted

    async def spy(balance):
        calls.append(balance)
        return await real_handler(balance)

    monkeypatch.setattr(cog, "handle_credits_exhausted", spy)

    asyncio.run(cog.handle_solve_success(orchestrator))

    assert orchestrator.summary_calls == 1
    assert calls == [60]
    assert bot.stop_calls == []
    assert exit_calls == []
