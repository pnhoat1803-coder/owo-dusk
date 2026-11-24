"""OwO DM poller: message classification, snowflake cursor, poll loop and guards."""

import asyncio
import time
from types import SimpleNamespace

import core.cogs.captcha as captcha_module
from core.cogs.captcha import Captcha, classify_owo_dm

OWO_ID = 408785106942164992
SUCCESS = "I have verified that you are human! Thank you! :3"
FAILURE = "Captchas failed to solve within 10 minutes"
WARNING = "Please make sure to solve captchas within 10 minutes"


class FakeStatus:
    def __init__(self):
        self.captcha = False
        self.captcha_flag = False


class FakeChannelHandler:
    def __init__(self):
        self.command_handler_status = FakeStatus()


class FakeCaptchaSettings:
    stopIfFailure = True
    stopScope = "account"
    timeoutSeconds = 600
    flagOnTimeout = True


class FakeWebhookSettings:
    enabled = False
    webhookCaptchaUrl = None
    pingUserId = ""


class FakeGlobalSettings:
    captcha = FakeCaptchaSettings()
    webhook = FakeWebhookSettings()


class FakeBot:
    def __init__(self):
        self.username = "tester"
        self.global_settings_dict = FakeGlobalSettings()
        self.ch = FakeChannelHandler()
        self.stopped = False
        self.misc = {"hostMode": False}
        self.dm = None
        self.owo_bot_id = OWO_ID
        self.logged = []

    async def log(self, text, color=None, **kwargs):
        self.logged.append(text)


class FakeMessage:
    def __init__(self, message_id, content):
        self.id = message_id
        self.content = content


class _History:
    def __init__(self, items):
        self.items = items

    def __aiter__(self):
        return self._generate()

    async def _generate(self):
        for item in self.items:
            yield item


class FakeChannel:
    """Returns one batch of messages per history() call, repeating the last batch."""

    def __init__(self, batches, last_message_id=None):
        self.batches = batches
        self.last_message_id = last_message_id
        self.calls = 0

    def history(self, *, limit=100, after=None, oldest_first=None):
        index = min(self.calls, len(self.batches) - 1)
        self.calls += 1
        return _History(self.batches[index])


def _build(monkeypatch, channel=None):
    bot = FakeBot()
    cog = Captcha(bot)
    if channel is not None:
        async def _get_channel():
            return channel

        monkeypatch.setattr(cog, "_get_owo_dm_channel", _get_channel)
    return cog, bot


def _fake_clock(monkeypatch):
    clock = {"now": 0.0}

    async def _sleep(seconds):
        clock["now"] += seconds

    monkeypatch.setattr(captcha_module, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    monkeypatch.setattr(
        captcha_module,
        "asyncio",
        SimpleNamespace(sleep=_sleep, CancelledError=asyncio.CancelledError),
    )
    return clock


# 3.1 pattern classification


def test_classify_success_message():
    assert classify_owo_dm(SUCCESS) == "success"


def test_classify_failure_message():
    assert classify_owo_dm(FAILURE) == "failure"


def test_classify_warning_message():
    assert classify_owo_dm(WARNING) == "warning"


def test_classify_unrelated_message_returns_none():
    assert classify_owo_dm("just a normal DM") is None


def test_classify_success_beats_warning():
    assert classify_owo_dm(f"{SUCCESS} {WARNING}") == "success"


def test_classify_success_beats_failure():
    assert classify_owo_dm(f"{FAILURE} {SUCCESS}") == "success"


def test_classify_user_provided_multi_pattern_message():
    message = (
        "**👍 |** I have verified that you are human! Thank you! :3\n"
        "**⚠️ |** Please make sure to solve captchas within 10 minutes! "
        "Failing to do so will result in a ban!\n"
        "<:blank:100000000000000001> **|** Captchas failed to solve within "
        "10 minutes: <:cross_box:100000000000000002> "
        "<:cross_box:100000000000000002> <:blank_box:100000000000000003>"
    )
    assert classify_owo_dm(message) == "success"


def test_classify_failure_only_message_returns_failure():
    assert classify_owo_dm("Captchas failed to solve within 10 minutes: ❌❌") == "failure"


# 3.2 cursor logic


def test_message_at_cursor_is_ignored(monkeypatch):
    channel = FakeChannel([[FakeMessage(100, SUCCESS)]])
    cog, _ = _build(monkeypatch, channel)
    cog.episode_start_message_id = 100

    assert asyncio.run(cog._poll_owo_dm_for_confirmation(deadline_seconds=0)) == "timeout"


def test_message_before_cursor_is_ignored(monkeypatch):
    channel = FakeChannel([[FakeMessage(99, SUCCESS)]])
    cog, _ = _build(monkeypatch, channel)
    cog.episode_start_message_id = 100

    assert asyncio.run(cog._poll_owo_dm_for_confirmation(deadline_seconds=0)) == "timeout"


def test_message_after_cursor_is_examined(monkeypatch):
    channel = FakeChannel([[FakeMessage(101, SUCCESS)]])
    cog, _ = _build(monkeypatch, channel)
    cog.episode_start_message_id = 100

    assert asyncio.run(cog._poll_owo_dm_for_confirmation(deadline_seconds=0)) == "success"


def test_missing_cursor_that_cannot_be_captured_returns_timeout(monkeypatch):
    channel = FakeChannel([[FakeMessage(101, SUCCESS)]])
    cog, _ = _build(monkeypatch, channel)
    cog.episode_start_message_id = None

    async def _no_cursor():
        return None

    monkeypatch.setattr(cog, "_capture_episode_cursor", _no_cursor)

    assert asyncio.run(cog._poll_owo_dm_for_confirmation(deadline_seconds=90)) == "timeout"


# 3.3 poll loop


def test_poll_finds_success_after_empty_rounds(monkeypatch):
    channel = FakeChannel(
        [[], [], [], [FakeMessage(101, SUCCESS)]]
    )
    cog, _ = _build(monkeypatch, channel)
    cog.episode_start_message_id = 100
    clock = _fake_clock(monkeypatch)

    result = asyncio.run(cog._poll_owo_dm_for_confirmation(deadline_seconds=15))

    assert result == "success"
    assert channel.calls == 4
    assert clock["now"] == 15.0


def test_poll_times_out_after_the_deadline(monkeypatch):
    channel = FakeChannel([[]])
    cog, _ = _build(monkeypatch, channel)
    cog.episode_start_message_id = 100
    clock = _fake_clock(monkeypatch)

    result = asyncio.run(cog._poll_owo_dm_for_confirmation(deadline_seconds=95))

    assert result == "timeout"
    assert channel.calls == 20
    assert clock["now"] == 95.0


def test_poll_returns_warning_when_only_the_warning_arrives(monkeypatch):
    channel = FakeChannel([[FakeMessage(101, WARNING)]])
    cog, _ = _build(monkeypatch, channel)
    cog.episode_start_message_id = 100

    assert asyncio.run(cog._poll_owo_dm_for_confirmation(deadline_seconds=0)) == "warning"


# 3.4 integration with flag_captcha_timeout


def _build_timeout(monkeypatch):
    bot = FakeBot()
    cog = Captcha(bot)
    bot.ch.command_handler_status.captcha = True
    calls = []

    async def _stop(reason):
        calls.append(reason)

    monkeypatch.setattr(cog, "stop_on_failure", _stop)
    return cog, bot, calls


def test_timeout_does_not_stop_a_confirmed_episode(monkeypatch):
    cog, bot, calls = _build_timeout(monkeypatch)
    cog.episode_dm_confirmed_at = time.time()

    asyncio.run(cog.flag_captcha_timeout(600))

    assert calls == []
    assert any("skipping stop" in line for line in bot.logged)


def test_timeout_still_stops_an_unconfirmed_episode(monkeypatch):
    cog, _bot, calls = _build_timeout(monkeypatch)
    cog.episode_dm_confirmed_at = None

    asyncio.run(cog.flag_captcha_timeout(600))

    assert calls == ["captcha not solved within 600s"]


# 3.5 double-processing guard


def test_listener_ignores_verify_dm_when_poller_confirmed(monkeypatch):
    bot = FakeBot()
    bot.dm = SimpleNamespace(id=900)
    cog = Captcha(bot)
    bot.ch.command_handler_status.captcha = True
    cog.captcha_seen_at = time.time()
    cog.episode_dm_confirmed_at = time.time()
    message = SimpleNamespace(
        author=SimpleNamespace(id=OWO_ID),
        channel=SimpleNamespace(id=900),
        content=SUCCESS,
    )

    asyncio.run(cog.on_message(message))

    assert bot.ch.command_handler_status.captcha is True
    assert cog.captcha_seen_at is not None
    assert cog.solve_running is False


# confirmation path


def test_confirmation_clears_the_pause_and_cancels_the_deadline(monkeypatch):
    bot = FakeBot()
    cog = Captcha(bot)
    bot.ch.command_handler_status.captcha = True
    cog.captcha_seen_at = time.time()

    async def _poll(deadline_seconds=90.0):
        return "success"

    monkeypatch.setattr(cog, "_poll_owo_dm_for_confirmation", _poll)

    async def _scenario():
        cog.deadline_task = asyncio.create_task(asyncio.sleep(1000))
        await cog._confirm_episode_from_owo_dm()
        await asyncio.sleep(0)
        return cog.deadline_task

    task = asyncio.run(_scenario())

    assert bot.ch.command_handler_status.captcha is False
    assert cog.captcha_seen_at is None
    assert cog.episode_dm_confirmed_at is not None
    assert task.cancelled()
    assert any("DM confirmed solved" in line for line in bot.logged)


def test_owo_reported_failure_stops_the_account(monkeypatch):
    bot = FakeBot()
    cog = Captcha(bot)
    calls = []

    async def _poll(deadline_seconds=90.0):
        return "failure"

    async def _stop(reason):
        calls.append(reason)

    monkeypatch.setattr(cog, "_poll_owo_dm_for_confirmation", _poll)
    monkeypatch.setattr(cog, "stop_on_failure", _stop)

    asyncio.run(cog._confirm_episode_from_owo_dm())

    assert calls == ["OwO reported captcha failure"]
    assert any("OwO reported the captcha failed" in line for line in bot.logged)


def test_poll_timeout_is_logged_and_defers_to_the_deadline(monkeypatch):
    bot = FakeBot()
    cog = Captcha(bot)
    bot.ch.command_handler_status.captcha = True

    async def _poll(deadline_seconds=90.0):
        return "timeout"

    monkeypatch.setattr(cog, "_poll_owo_dm_for_confirmation", _poll)

    asyncio.run(cog._confirm_episode_from_owo_dm())

    assert bot.ch.command_handler_status.captcha is True
    assert cog.episode_dm_confirmed_at is None
    assert any("deferring to deadline timer" in line for line in bot.logged)
