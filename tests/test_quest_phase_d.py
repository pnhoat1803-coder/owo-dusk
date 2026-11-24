"""Autoquest phase D (prompt 29): one OCR limit for all accounts, a 429 backoff, no OCR when it is not needed, a spread
first check, a 90 s Quest Log wait, quest state across restarts (utils/data/quests.sqlite, in tmp_path here), the real
Quest Log format, the dashboard quest block, and the local Tesseract reader.

Fakes only: no network, no Discord, no Tesseract program is run (the level helper is replaced). Time is faked for the
cog and the OCR limiter, like tests/test_quest_safety.py. The cog fakes (FakeBot, make_cog, ...) come from that file.
"""

import asyncio
import base64
import io
import json
import sqlite3
import time
from types import SimpleNamespace

import aiohttp
import pytest
from multidict import CIMultiDict, CIMultiDictProxy
from PIL import Image
from yarl import URL

from core import level_ocr
from core.cogs import quest as quest_mod
from core.live_status import LiveStatusWriter, _quest_block
from utils.account_identity import expected_key
from utils.image_to_text import get_quest_details as quest_details_mod
from utils.image_to_text import image_to_text
from utils.quest_helper import quest as qh_module
from utils.quest_helper import quest_store
from utils.quest_helper.quest import LocalQuestHandler, QuestHandler
from utils.quest_helper.quest_store import QuestStore
from website import data_v2

from test_quest_safety import (  # the phase A fakes: the same cog, the same message shapes
    BOT_ID,
    CHANNEL_ID,
    GUILD_ID,
    OWO_ID,
    QUEST_URL,
    START,
    FakeBot,
    gallery,
    make_cog,
    make_message,
    quest_components,
    text,
)

_real_sleep = asyncio.sleep
ACC = str(BOT_ID)
NEXT = int(START) + 3600  # a next-quest time one hour ahead
REAL_NEXT = 1795075200  # 2026-11-19 08:00 UTC: the value on the real Quest Log
OCR_URL = "https://api.ocr.space/parse/image"


class Clock:
    def __init__(self):
        self.now = START

    def monotonic(self):
        return self.now

    def wall(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    clk = Clock()
    monkeypatch.setattr(quest_mod, "_monotonic", clk.monotonic)
    monkeypatch.setattr(quest_mod, "_wall_now", clk.wall)
    monkeypatch.setattr(image_to_text, "_monotonic", clk.monotonic)

    async def fake_sleep(seconds, *args, **kwargs):
        clk.now += max(0.0, float(seconds))
        await _real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return clk


@pytest.fixture(autouse=True)
def no_other_accounts_and_no_autostop(monkeypatch, tmp_path):
    monkeypatch.setattr(quest_mod, "clients_snapshot", lambda: {})
    monkeypatch.setattr(quest_mod, "stop_requested", lambda: False)
    # every state file of this file lives in tmp_path. The real utils/data/quests.sqlite is never touched
    monkeypatch.setattr(quest_store, "DEFAULT_PATH", tmp_path / "default_quests.sqlite")


def fresh_ocr_gate(monkeypatch):
    gate = image_to_text._Gate(limit=1, gap=image_to_text.OCR_MIN_GAP_SECONDS)
    monkeypatch.setattr(image_to_text, "_OCR_SPACE_GATE", gate)
    return gate


def sends(bot, command_id="quest"):
    return [time_ for time_, cmd in bot.ch.put if cmd["id"] == command_id]


# ----------------------------------------------------------------------------------------------- fake aiohttp sessions
OK_JSON = {"ParsedResults": [{"TextOverlay": {"Lines": [
    {"LineText": "Catch 3 animals", "Words": [{"Left": 1, "Top": 2}]}]}}]}


def _request_info():
    url = URL(OCR_URL)
    return aiohttp.RequestInfo(url, "POST", CIMultiDictProxy(CIMultiDict()), url)


class _OcrCall:
    def __init__(self, session):
        self.session = session
        self.answer = None

    async def __aenter__(self):
        s = self.session
        s.in_flight += 1
        s.max_in_flight = max(s.max_in_flight, s.in_flight)
        s.starts.append(s.now())
        await asyncio.sleep(s.latency)
        self.answer = s.answers.pop(0) if s.answers else OK_JSON
        if self.answer == "timeout":
            raise asyncio.TimeoutError()
        return self

    async def __aexit__(self, *exc_info):
        self.session.in_flight -= 1
        return False

    def raise_for_status(self):
        if self.answer == "429":
            raise aiohttp.ClientResponseError(_request_info(), (), status=429, message="Too Many Requests")

    async def json(self):
        return self.answer


class FakeOcr:
    """Stands in for the account's aiohttp session. post() is one ocr.space request that takes `latency` seconds"""

    def __init__(self, clock=None, answers=None, latency=0.5):
        self.clock = clock
        self.answers = list(answers or [])
        self.latency = latency
        self.starts = []
        self.in_flight = 0
        self.max_in_flight = 0

    def now(self):
        return self.clock.now if self.clock else time.monotonic()

    def post(self, url, data=None, headers=None, timeout=None):
        return _OcrCall(self)


class _ImageResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body
        self.content_length = len(body)

    async def read(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class FakeImageSession:
    """GET serves image bytes. No POST: this session never reaches ocr.space"""

    def __init__(self, body):
        self.body = body
        self.gets = 0

    def get(self, url, timeout=None):
        self.gets += 1
        return _ImageResponse(200, self.body)


class FakeBothSession(FakeImageSession, FakeOcr):
    """GET serves the image, POST is the ocr.space fake: one object for the tests that use both"""

    def __init__(self, body, answers=None, latency=0.0):
        FakeImageSession.__init__(self, body)
        FakeOcr.__init__(self, None, answers=answers, latency=latency)


def png_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (12, 12), (40, 40, 44)).save(buf, format="PNG")
    return buf.getvalue()


# -------------------------------------------------------------------------------------------- D1: the OCR limit
# D-T1 20 concurrent OCR calls: one in flight at a time, the starts at least 2 s apart (fake clock)
def test_d_t1_twenty_ocr_calls_one_at_a_time_two_seconds_apart(clock, monkeypatch):
    fresh_ocr_gate(monkeypatch)
    monkeypatch.setattr(image_to_text, "QUEST_OCR_ENGINE", "ocrspace")
    session = FakeOcr(clock, latency=0.5, answers=[OK_JSON] * 20)

    async def scenario():
        await asyncio.gather(*(image_to_text.get_text_from_url(f"{QUEST_URL}?n={i}", session, "key") for i in range(20)))

    asyncio.run(scenario())
    assert len(session.starts) == 20
    assert session.max_in_flight == 1
    gaps = [later - earlier for earlier, later in zip(session.starts, session.starts[1:])]
    assert min(gaps) >= image_to_text.OCR_MIN_GAP_SECONDS


# D-T2 HTTP 429: one shared 30 s backoff, one retry, then None; the log line is printed once
def test_d_t2_429_backs_off_30_seconds_retries_once_then_gives_up(clock, monkeypatch, capsys):
    fresh_ocr_gate(monkeypatch)
    session = FakeOcr(clock, latency=0.5, answers=["429", "429"])

    async def scenario():
        return await image_to_text.get_text_from_url(QUEST_URL, session, "key")

    assert asyncio.run(scenario()) is None
    assert len(session.starts) == 2  # the first try and one retry, no more
    assert session.starts[1] - session.starts[0] >= image_to_text.OCR_429_BACKOFF_SECONDS
    assert capsys.readouterr().out.count("[quest] OCR rate limited, backing off 30s") == 1


def test_d_t2b_the_backoff_holds_the_other_accounts_too(clock, monkeypatch):
    fresh_ocr_gate(monkeypatch)
    session = FakeOcr(clock, latency=0.5, answers=["429", OK_JSON, OK_JSON])

    async def scenario():
        first = asyncio.create_task(image_to_text.get_text_from_url(QUEST_URL + "?a", session, "key"))
        await _real_sleep(0)
        second = asyncio.create_task(image_to_text.get_text_from_url(QUEST_URL + "?b", session, "key"))
        return await first, await second

    first_result, second_result = asyncio.run(scenario())
    assert first_result is not None and second_result is not None  # the retry and the other account both got text
    first_429_at = session.starts[0]
    # nothing was sent while the backoff held: every later request starts after the 30 s
    assert all(start >= first_429_at + image_to_text.OCR_429_BACKOFF_SECONDS for start in session.starts[1:])


# D-T3 a timeout is an OCR failure: None from the reader, "OCR failed" from the cog, no "unexpected error"
def test_d_t3_ocr_timeout_is_an_ocr_failure_for_the_cog(clock, monkeypatch):
    fresh_ocr_gate(monkeypatch)
    monkeypatch.setattr(image_to_text, "QUEST_OCR_ENGINE", "ocrspace")
    bot = FakeBot(clock)
    cog = make_cog(bot)
    bot.quest_handler.session = FakeOcr(clock, answers=["timeout"], latency=0.1)

    async def scenario():
        await cog.on_component_message(
            make_message(quest_components(text("Your quests are listed below"), gallery(QUEST_URL)))
        )

    asyncio.run(scenario())
    assert "[quest] OCR failed, keeping previous quest list" in bot.logs
    assert not any("unexpected error" in line for line in bot.logs)


# -------------------------------------------------------------------------------- D3: no OCR when it is not needed
def _spy_ocr(monkeypatch):
    calls = []

    async def spy_space(url, session, key="helloworld"):
        calls.append(url)
        return None

    async def spy_tesseract(url, session):
        calls.append(url)
        return None

    monkeypatch.setattr(image_to_text, "get_text_from_url", spy_space)
    monkeypatch.setattr(image_to_text, "get_text_by_tesseract", spy_tesseract)
    return calls


# D-T4 the all-done message is read without any OCR
def test_d_t4_all_done_message_does_not_call_ocr(clock, monkeypatch):
    calls = _spy_ocr(monkeypatch)
    bot = FakeBot(clock)
    cog = make_cog(bot)
    done = make_message(quest_components(
        text(f"{quest_mod.QUEST_ALL_DONE_TEXT}\n-# ⏰ Next quest: **<t:{NEXT}:R>**")
    ))

    asyncio.run(cog.on_component_message(done))
    assert calls == []
    assert cog.all_quests_done is True


# D-T5 same next-quest time, a good read 10 minutes ago, nothing to claim: no OCR. A claim button: OCR again
def test_d_t5_recent_read_for_the_same_quest_log_is_reused_unless_a_claim_waits(clock, monkeypatch):
    calls = _spy_ocr(monkeypatch)
    monkeypatch.setattr(image_to_text, "QUEST_OCR_ENGINE", "ocrspace")
    bot = FakeBot(clock)
    cog = make_cog(bot)
    cog._last_ocr_ok_at = START - 600
    cog._last_ocr_next_at = NEXT
    body = [text(f"-# ⏰ Next quest: **<t:{NEXT}:R>**"), gallery(QUEST_URL)]

    asyncio.run(cog.on_component_message(make_message(quest_components(*body))))
    assert calls == []  # reused

    async def claim_click(*args, **kwargs):
        return None

    claim = SimpleNamespace(custom_id="quests:claim", disabled=False, click=claim_click)
    asyncio.run(cog.on_component_message(make_message(quest_components(*body), buttons=[claim])))
    assert len(calls) == 1  # a claim is waiting: the list is read again
    assert "Claimed a finished quest!" in bot.logs


# ---------------------------------------------------------------------------------- D4: the spread and the reply wait
# D-T6 the first check: checkCooldown (20 s in the fake) plus a spread in [0, 900]
def test_d_t6_first_check_is_delayed_by_checkcooldown_plus_a_spread_up_to_900(clock):
    bot = FakeBot(clock)
    cog = make_cog(bot)
    asyncio.run(cog._quest_loop_once())
    first = sends(bot)[0]
    assert 20.0 <= first - START <= 20.0 + quest_mod.QUEST_FIRST_CHECK_SPREAD_SECONDS
    assert quest_mod.QUEST_LOG_REPLY_SECONDS == 90


# D-T7 a Quest Log that arrives 60 s after "owo quest" is used; one that arrives after 90 s is not
@pytest.mark.parametrize("reply_after, accepted", [(60, True), (100, False)])
def test_d_t7_quest_log_reply_wait_is_90_seconds(clock, monkeypatch, reply_after, accepted):
    bot = FakeBot(clock)
    cog = make_cog(bot)
    base_sleep = asyncio.sleep
    state = {"fired": False}

    async def reply_sleep(seconds, *args, **kwargs):
        if not state["fired"] and sends(bot) and clock.now - sends(bot)[0] >= reply_after:
            state["fired"] = True
            cog._quest_log_event.set()  # the Quest Log of this account was processed
        await base_sleep(seconds, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", reply_sleep)
    asyncio.run(cog._quest_loop_once())
    skipped = "[quest] no quest log reply, skipping this round" in bot.logs
    assert skipped is (not accepted)


# ----------------------------------------------------------------------------------------- D5: state across restarts
def _store_row(**overrides):
    row = {
        "next_quest_at": NEXT, "all_done": 0, "seals": 3,
        "quests_json": json.dumps([{"quest": "receive cookies", "quest_id": "cookie", "current": 0, "total": 1,
                                    "complete": False, "helpable": True}]),
        "last_log_at": START - 300, "last_ocr_ok_at": START - 300, "last_claim_at": None,
        "claims_total": 0, "ocr_next_at": NEXT,
    }
    row.update(overrides)
    return row


# D-T8 store round trip: write, open a new store, the same values come back; a read never creates the file
def test_d_t8_store_round_trip_with_a_new_instance(tmp_path):
    path = tmp_path / "quests.sqlite"
    row = _store_row(all_done=1, last_claim_at=START - 60, claims_total=2)
    QuestStore(path).save(ACC, row)
    got = QuestStore(path).load(ACC)
    assert {k: got[k] for k in row} == row
    assert QuestStore(path).load("999") is None
    missing = tmp_path / "none.sqlite"
    assert QuestStore(missing).load(ACC) is None
    assert not missing.exists()


# D-T9 restart with all_done=1 and a next-quest time ahead: nothing is sent before that time
def test_d_t9_restart_all_done_sends_nothing_before_the_next_quest_time(clock, tmp_path):
    path = tmp_path / "quests.sqlite"
    QuestStore(path).save(ACC, _store_row(all_done=1, next_quest_at=int(START) + 5000, quests_json="[]"))
    bot = FakeBot(clock)
    cog = make_cog(bot)
    cog.enable_state_store(path)

    async def scenario():
        await cog.restore_quest_state()
        await cog._quest_loop_once()

    asyncio.run(scenario())
    assert sends(bot)[0] >= int(START) + 5000
    assert any("all quests were done before the restart" in line for line in bot.logs)


# D-T10 restart with a next-quest time already past: a normal start (a check after the spread)
def test_d_t10_restart_after_the_next_quest_time_starts_normally(clock, tmp_path):
    path = tmp_path / "quests.sqlite"
    QuestStore(path).save(ACC, _store_row(all_done=1, next_quest_at=int(START) - 100, quests_json="[]"))
    bot = FakeBot(clock)
    cog = make_cog(bot)
    cog.enable_state_store(path)

    async def scenario():
        await cog.restore_quest_state()
        await cog._quest_loop_once()

    asyncio.run(scenario())
    assert cog.all_quests_done is False
    assert 20.0 <= sends(bot)[0] - START <= 20.0 + quest_mod.QUEST_FIRST_CHECK_SPREAD_SECONDS


def test_d_t10b_open_list_is_restored_and_the_check_waits_for_the_last_log(clock, tmp_path):
    path = tmp_path / "quests.sqlite"
    QuestStore(path).save(ACC, _store_row(last_log_at=START - 120))  # the last Quest Log was 2 minutes ago
    bot = FakeBot(clock)
    cog = make_cog(bot)
    cog.enable_state_store(path)

    async def scenario():
        await cog.restore_quest_state()

    asyncio.run(scenario())
    assert [q["quest_id"] for q in bot.quest_handler.quests] == ["cookie"]
    assert bot.quest_handler.seals == 3
    assert bot.quest_handler.next_quest_timestamp == NEXT
    # the Quest Log was 120 s ago: the next check may not come before QUEST_MIN_CHECK_SECONDS after it
    assert cog._last_quest_check == pytest.approx(clock.monotonic() - 120)


# D-T14 a failing store: logged once, never raised
def test_d_t14_a_failing_store_is_logged_once_and_never_raises(clock, tmp_path, monkeypatch):
    path = tmp_path / "quests.sqlite"
    QuestStore(path).save(ACC, _store_row())  # the file exists, so the next read goes through sqlite3.connect
    bot = FakeBot(clock)
    cog = make_cog(bot)
    cog.enable_state_store(path)

    def broken_connect(*args, **kwargs):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(quest_store.sqlite3, "connect", broken_connect)

    async def scenario():
        await cog.restore_quest_state()
        await cog._persist_state()
        await cog._persist_state()

    asyncio.run(scenario())
    assert sum("quest state store failed" in line for line in bot.logs) == 1


# D-T22 a restored helpable quest is registered with the shared list again at the next good read
def test_d_t22_restored_helpable_quest_is_registered_again(monkeypatch):
    shared = QuestHandler()
    local = LocalQuestHandler(shared, BOT_ID, None)
    local.restore_quests([{"quest": "receive prayers", "quest_id": "pray", "current": 0, "total": 3,
                           "complete": False, "helpable": True}])

    async def fresh(url, session, key="helloworld"):
        return [{"quest": "receive prayers", "progress": "1/3", "current": 1, "total": 3, "complete": False}]

    # update_quests imports get_quest_details by name, so the fake goes into the module that calls it
    monkeypatch.setattr(qh_module, "get_quest_details", fresh)
    assert asyncio.run(local.update_quests(QUEST_URL, CHANNEL_ID, GUILD_ID, NEXT)) is True
    assert [q["quest_id"] for q in shared.quests] == ["pray"]


# ----------------------------------------------------------------------------------------------- D6: the real format
REAL_LOG = [
    f"### 📜 <@{BOT_ID}>'s Quest Log\n-# Complete quests to earn rewards!\n-# Balance: **0** <:seal:1234567890123456789> Quest Seals",
    f"-# ⏰ Next quest: **<t:{REAL_NEXT}:R>**",
]


# D-T11 the real text: seals 0 and 10, next quest 1795075200, with and without bold
def test_d_t11_real_quest_log_text_seals_and_next_quest():
    assert quest_mod._seals_from(REAL_LOG) == 0
    assert quest_mod._next_quest_from_log(REAL_LOG) == REAL_NEXT
    bold = ["-# Balance: **10** <:seal:1234567890123456789> Quest Seals", "-# ⏰ Next quest: **<t:1795075200:R>**"]
    assert quest_mod._seals_from(bold) == 10
    assert quest_mod._next_quest_from_log(bold) == REAL_NEXT
    plain = ["-# Balance: 10 <:seal:1234567890123456789> Quest Seals", "-# Next quest: <t:1795075200:R>"]
    assert quest_mod._seals_from(plain) == 10
    assert quest_mod._next_quest_from_log(plain) == REAL_NEXT
    assert quest_mod._seals_from(["no balance here"]) is None


def test_d_t11b_the_cog_reads_seals_and_next_quest_from_the_message(clock):
    cog = make_cog(FakeBot(clock))
    details = cog.get_available_quest_details(components=[], buttons=[], texts=REAL_LOG)
    assert details["seals"] == 0
    assert details["next_quest_timestamp"] == REAL_NEXT
    assert details["questDone"] is False


# -------------------------------------------------------------------------------------- D7: the dashboard quest block
def test_d_t12_live_status_quest_block_shape_and_null_without_the_cog(tmp_path):
    cog = SimpleNamespace(all_quests_done=False, _last_claim_at=START - 60)
    handler = SimpleNamespace(
        quests=[
            {"quest_id": "cookie", "current": 0, "total": 1, "complete": False},
            {"quest_id": "hunt", "current": 5, "total": 5, "complete": True},
        ],
        next_quest_timestamp=REAL_NEXT,
        seals=10,
    )
    client = SimpleNamespace(get_cog=lambda name: cog if name == "Quest" else None, quest_handler=handler)
    assert _quest_block(client) == {
        "enabled": True,
        "all_done": False,
        "next_quest_at": REAL_NEXT,
        "seals": 10,
        "open_quests": [{"id": "cookie", "current": 0, "total": 1}],
        "last_claim_at": round(START - 60, 1),
    }
    assert _quest_block(SimpleNamespace(get_cog=lambda name: None, quest_handler=handler)) is None
    assert _quest_block(SimpleNamespace()) is None

    writer = LiveStatusWriter(path=tmp_path / "live.json", commands_path=tmp_path / "commands.json")
    record = {"status": "running", "userid": ACC}
    out = writer._account(1, record, SimpleNamespace(get_cog=lambda name: None, username="x"), START, START)
    assert "quest" in out and out["quest"] is None


def _write_live(root, user_ids):
    """The live status file of a bot that has just started, written by the real LiveStatusWriter (no quest block)"""
    records = {}
    for index, user_id in enumerate(user_ids, start=1):
        token = base64.urlsafe_b64encode(user_id.encode("ascii")).decode("ascii").rstrip("=") + ".AAAAAA.DUSKTEST"
        records[index] = {"status": "pending", "userid": None, "error": None,
                          "expected_key": expected_key(token, index)}
    writer = LiveStatusWriter(
        path=root / "utils" / "data" / "live_status.json",
        clients=lambda: {},
        accounts=lambda: dict(records),
        autostop=lambda wall: {"enabled": False, "stopping": False},
        commands_path=root / "utils" / "data" / "web_commands.json",
    )
    writer.write_once(running=False)
    return root / "utils" / "data" / "live_status.json"


# D-T13 the dashboard falls back to quests.sqlite when the live status has no quest block (the bot is stopped)
def test_d_t13_dashboard_falls_back_to_the_saved_quest_state(tmp_path):
    dash_id = "111111111111110001"  # 18 digits: the live status keys an account by a token id of 17-20 digits
    root = tmp_path / "bot"
    (root / "utils" / "data").mkdir(parents=True)
    QuestStore(root / "utils" / "data" / "quests.sqlite").save(dash_id, _store_row(seals=7))
    _write_live(root, [dash_id])

    store = data_v2.SnapshotStore(root=str(root))
    store.refresh()
    item = next(i for i in store.accounts_page(1, 50, "name", "asc", "", "", "")["items"] if i["id"] == dash_id)
    assert item["quest"]["saved"] is True
    assert item["quest"]["enabled"] is None
    assert item["quest"]["seals"] == 7
    assert item["quest"]["open_quests"] == [{"id": "cookie", "current": 0, "total": 1}]
    assert store.sources["quests"].ok is True
    assert store.snapshot.summary["quest_seals"] == 7


def test_d_t13b_missing_quests_file_is_not_an_error(tmp_path):
    root = tmp_path / "bot"
    (root / "utils" / "data").mkdir(parents=True)
    _write_live(root, [ACC])
    store = data_v2.SnapshotStore(root=str(root))
    store.refresh()
    assert store.sources["quests"].ok is False
    assert store.sources["quests"].error == "not created yet"


# ------------------------------------------------------------------------------------------- D8: the local Tesseract
TSV_HEADER = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"


def _tsv_word(block, line, word, left, top, value):
    """A word row as Tesseract writes it. The coordinates are in the enlarged image (x3, see level_ocr.UPSCALE)"""
    s = level_ocr.UPSCALE
    return f"5\t1\t{block}\t1\t{line}\t{word}\t{left * s}\t{top * s}\t20\t12\t95\t{value}"


def _quest_tsv():
    """Three quest rows as Tesseract reads them: name lines, progress lines, the blacklisted "Completion rewards" line"""
    rows = [
        _tsv_word(1, 1, 1, 17, 18, "1."), _tsv_word(1, 1, 2, 68, 16, "Receive"), _tsv_word(1, 1, 3, 130, 16, "cookies"),
        _tsv_word(2, 1, 1, 330, 44, "0/1"),
        _tsv_word(3, 1, 1, 18, 72, "Completion"), _tsv_word(3, 1, 2, 90, 72, "rewards"),
        _tsv_word(4, 1, 1, 18, 124, "2."), _tsv_word(4, 1, 2, 70, 122, "Send"), _tsv_word(4, 1, 3, 112, 126, "an"),
        _tsv_word(4, 1, 4, 136, 122, "action"), _tsv_word(4, 1, 5, 186, 123, "to"), _tsv_word(4, 1, 6, 208, 126, "a"),
        _tsv_word(4, 1, 7, 222, 122, "friend"),
        _tsv_word(5, 1, 1, 328, 150, "0/3"),
        _tsv_word(6, 1, 1, 18, 230, "3."), _tsv_word(6, 1, 2, 68, 228, "Manually"), _tsv_word(6, 1, 3, 150, 228, "hunt"),
        _tsv_word(7, 1, 1, 328, 256, "3/5"),
    ]
    return "\n".join([TSV_HEADER, *rows]) + "\n"


OCR_ROWS = {"ParsedResults": [{"TextOverlay": {"Lines": [
    {"LineText": "1. Receive cookies", "Words": [{"Left": 17, "Top": 18}]},
    {"LineText": "0/1", "Words": [{"Left": 330, "Top": 44}]},
    {"LineText": "Completion rewards", "Words": [{"Left": 18, "Top": 72}]},
    {"LineText": "2. Send an action to a friend", "Words": [{"Left": 18, "Top": 124}]},
    {"LineText": "0/3", "Words": [{"Left": 328, "Top": 150}]},
    {"LineText": "3. Manually hunt", "Words": [{"Left": 18, "Top": 230}]},
    {"LineText": "3/5", "Words": [{"Left": 328, "Top": 256}]},
]}}]}


def _use_fake_tesseract(monkeypatch, tsv):
    monkeypatch.setattr(level_ocr, "ocr_available", lambda: (True, ""))
    monkeypatch.setattr(level_ocr, "current_program", lambda: "fake-tesseract")
    monkeypatch.setattr(level_ocr, "preprocess", lambda image: image)
    monkeypatch.setattr(level_ocr, "run_text_pass", lambda image, psm, extra="": tsv)


# D-T16 the Tesseract read gives the same quests as the ocr.space read of the same text
def test_d_t16_tesseract_and_ocr_space_give_the_same_quests(monkeypatch):
    fresh_ocr_gate(monkeypatch)
    _use_fake_tesseract(monkeypatch, _quest_tsv())

    monkeypatch.setattr(image_to_text, "QUEST_OCR_ENGINE", "tesseract")
    tess_session = FakeBothSession(png_bytes(), answers=[])
    tess = asyncio.run(quest_details_mod.get_quest_details(QUEST_URL, tess_session, "key"))
    assert tess_session.starts == []  # the Tesseract read made no ocr.space request

    monkeypatch.setattr(image_to_text, "QUEST_OCR_ENGINE", "ocrspace")
    ocr_session = FakeBothSession(png_bytes(), answers=[OCR_ROWS])
    ocr = asyncio.run(quest_details_mod.get_quest_details(QUEST_URL, ocr_session, "key"))

    assert tess == ocr
    assert [q["quest"] for q in tess] == ["receive cookies", "send an action to a friend", "manually hunt"]
    assert [(q["current"], q["total"]) for q in tess] == [(0, 1), (0, 3), (3, 5)]
    assert len(tess_session.starts) == 0  # the Tesseract read needed no ocr.space request


# D-T17 Tesseract missing: ocr.space is the fallback, one request, through the limiter
def test_d_t17_tesseract_missing_falls_back_to_ocr_space_once_through_the_limiter(monkeypatch):
    class CountingGate(image_to_text._Gate):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.acquired = 0

        async def acquire(self):
            self.acquired += 1
            await super().acquire()

    gate = CountingGate(limit=1, gap=image_to_text.OCR_MIN_GAP_SECONDS)
    monkeypatch.setattr(image_to_text, "_OCR_SPACE_GATE", gate)
    monkeypatch.setattr(level_ocr, "ocr_available", lambda: (True, ""))
    monkeypatch.setattr(level_ocr, "current_program", lambda: None)
    monkeypatch.setattr(level_ocr, "find_tesseract", lambda *args, **kwargs: None)
    monkeypatch.setattr(image_to_text, "QUEST_OCR_ENGINE", "tesseract")

    session = FakeBothSession(png_bytes(), answers=[OCR_ROWS])
    quests = asyncio.run(quest_details_mod.get_quest_details(QUEST_URL, session, "key"))
    assert [q["quest"] for q in quests] == ["receive cookies", "send an action to a friend", "manually hunt"]
    assert len(session.starts) == 1
    assert gate.acquired == 1


# D-T23 a Tesseract read with a name the bot does not know is not used: ocr.space answers instead
def test_d_t23_unknown_quest_name_in_the_tesseract_read_goes_to_ocr_space(monkeypatch):
    fresh_ocr_gate(monkeypatch)
    _use_fake_tesseract(monkeypatch, _quest_tsv().replace("Manually", "Manualy"))
    monkeypatch.setattr(image_to_text, "QUEST_OCR_ENGINE", "tesseract")
    session = FakeBothSession(png_bytes(), answers=[OCR_ROWS])
    quests = asyncio.run(quest_details_mod.get_quest_details(QUEST_URL, session, "key"))
    assert len(session.starts) == 1  # the fallback request
    assert "manually hunt" in [q["quest"] for q in quests]


# D-T18 the Tesseract run is off the event loop: a slow run does not stop another coroutine
def test_d_t18_tesseract_runs_off_the_event_loop(monkeypatch):
    def slow_run(data):
        time.sleep(0.4)  # a blocking run, like a real Tesseract process
        return [{"text": "Receive cookies", "x_axis": 0, "y_axis": 0}]

    monkeypatch.setattr(image_to_text, "_tesseract_lines", slow_run)
    session = FakeImageSession(png_bytes())

    async def scenario():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await _real_sleep(0.01)
                ticks += 1

        task = asyncio.create_task(ticker())
        lines = await image_to_text.get_text_by_tesseract(QUEST_URL, session)
        task.cancel()
        return ticks, lines

    ticks, lines = asyncio.run(scenario())
    assert lines == [{"text": "Receive cookies", "x_axis": 0, "y_axis": 0}]
    assert ticks >= 10  # the loop kept running while the run was going on


# D-T20 the TSV conversion: the lines, the O->0 fix of a progress line, the coordinates back in the original size
def test_d_t20_tsv_becomes_the_ocr_shape_with_the_progress_fixed():
    lines = image_to_text.tsv_to_lines(_quest_tsv(), level_ocr.UPSCALE)
    by_text = {line["text"]: line for line in lines}
    assert by_text["1 Receive cookies"] == {"text": "1 Receive cookies", "x_axis": 17, "y_axis": 18}
    assert by_text["0/1"] == {"text": "0/1", "x_axis": 330, "y_axis": 44}
    assert image_to_text.tsv_to_lines("not a tsv table\n", 3) == []
    letters = _quest_tsv().replace("0/1", "o/1")
    assert {"text": "0/1", "x_axis": 330, "y_axis": 44} in image_to_text.tsv_to_lines(letters, level_ocr.UPSCALE)


# D-T21 the quest name clean-up: row numbers, the blacklist, a line that is paired once, no crash on two phrases
def test_d_t21_quest_name_clean_up_and_pairing():
    items = [
        {"text": "2 2 Send an action to a friend", "x_axis": 18, "y_axis": 124},
        {"text": "1 Receive cookies", "x_axis": 17, "y_axis": 18},
        {"text": "Completion rewards", "x_axis": 18, "y_axis": 72},
        {"text": "you can claim a new quest free a slot to receive it", "x_axis": 18, "y_axis": 90},
        {"text": "0/1", "x_axis": 330, "y_axis": 44},
        {"text": "0/3", "x_axis": 328, "y_axis": 150},
    ]
    quests = quest_details_mod.extract_quests(items)
    assert [q["quest"] for q in quests] == ["receive cookies", "send an action to a friend"]
    assert [(q["current"], q["total"], q["complete"]) for q in quests] == [(0, 1, False), (0, 3, False)]


# ------------------------------------------------------------------------------------------------------- the engine note
def test_d_engine_note_is_printed_once_an_hour_per_account(monkeypatch, capsys):
    monkeypatch.setattr(quest_details_mod, "_engine_notes", {})
    session = object()
    quest_details_mod._note_engine(session, "tesseract")
    quest_details_mod._note_engine(session, "tesseract")
    assert capsys.readouterr().out.count("[quest] OCR engine: tesseract") == 1
