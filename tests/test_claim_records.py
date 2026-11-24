"""When the cookie and lottery cogs write their "claimed today" record.

Same rule as core/cogs/daily.py: queueing the command writes nothing. OwO's success reply writes the
record. A wait or refusal reply writes nothing.
"""

import asyncio
from types import SimpleNamespace

from core.cogs.cookie import Cookie
from core.cogs.lottery import Lottery

CHANNEL_ID = 2222
OWO_ID = 1111

COOKIE_SUCCESS = "You got a cookie from **Dusk**!"
COOKIE_WAIT = "Nu! You need to wait 3h before you can give another cookie."
LOTTERY_LIMIT = "You can only bet up to 250,000 cowoncy!"


class FakeDB:
    def __init__(self):
        self.writes = []

    async def fetch_cmd_lastran_time(self, cmd):
        return 0

    def update_cmd_lastran_time(self, cmd):
        self.writes.append(cmd)


class FakeCh:
    def __init__(self):
        self.queued = []
        self.removed = []

    async def put_queue(self, cmd_data, priority=False, quick=False):
        self.queued.append(cmd_data["id"])

    async def remove_queue(self, cmd_data=None, id=None):
        self.removed.append(id)


class FakeBot:
    def __init__(self):
        self.alias = {"cookie": {"normal": "cookie"}, "lottery": {"normal": "lottery"}}
        self.cm = SimpleNamespace(id=CHANNEL_ID)
        self.owo_bot_id = OWO_ID
        self.db = FakeDB()
        self.ch = FakeCh()
        self.settings_dict = SimpleNamespace(
            cooldowns=SimpleNamespace(moderateCooldown=[0, 0], shortCooldown=[0, 0]),
            commands=SimpleNamespace(
                cookie=SimpleNamespace(enabled=True, user_id=1, ping_user=False),
                lottery=SimpleNamespace(enabled=True, amount=100),
            ),
        )

    def should_run(self, last_timestamp):
        return True  # not claimed today yet, so the start path queues right away

    def calc_time(self):
        return 0  # no waiting for the next reset in these tests

    async def sleep_till(self, cooldown, cd_list=True, noise=3):
        return None

    def is_for_us(self, message):
        return True

    def name_phrase_in(self, text, template, message=None):
        return "Lottery Submission" in text

    async def log(self, text, color="#ffffff", **kwargs):
        return None


def owo_message(content, embeds=()):
    return SimpleNamespace(
        channel=SimpleNamespace(id=CHANNEL_ID),
        author=SimpleNamespace(id=OWO_ID),
        content=content,
        embeds=list(embeds),
    )


def lottery_submission_embed():
    return SimpleNamespace(author=SimpleNamespace(name="Dusk's Lottery Submission"))


def test_cookie_queued_at_start_writes_no_record():
    bot = FakeBot()
    asyncio.run(Cookie(bot).start_cookie())
    assert bot.ch.queued == ["cookie"]
    assert bot.db.writes == []


def test_cookie_success_reply_records_the_claim():
    bot = FakeBot()
    asyncio.run(Cookie(bot).on_message(owo_message(COOKIE_SUCCESS)))
    assert bot.db.writes == ["cookie"]
    assert bot.ch.queued == ["cookie"]  # the existing re-queue still happens


def test_cookie_wait_reply_writes_no_record():
    bot = FakeBot()
    asyncio.run(Cookie(bot).on_message(owo_message(COOKIE_WAIT)))
    assert bot.db.writes == []
    assert bot.ch.queued == ["cookie"]


def test_lottery_queued_at_start_writes_no_record():
    bot = FakeBot()
    asyncio.run(Lottery(bot).start_lottery())
    assert bot.ch.queued == ["lottery"]
    assert bot.db.writes == []


def test_lottery_submission_reply_records_the_bet():
    bot = FakeBot()
    asyncio.run(Lottery(bot).on_message(owo_message("", embeds=[lottery_submission_embed()])))
    assert bot.db.writes == ["lottery"]
    assert bot.ch.queued == ["lottery"]


def test_lottery_limit_reply_writes_no_record():
    bot = FakeBot()
    asyncio.run(Lottery(bot).on_message(owo_message(LOTTERY_LIMIT)))
    assert bot.db.writes == []
    assert bot.ch.queued == ["lottery"]
