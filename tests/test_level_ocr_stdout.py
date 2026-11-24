"""OCR reads Tesseract's stdout (prompt 24, follow-up A): no TXT output file is needed.

The fake-program tests check the command line, the card file that exists only while tesseract reads it, the extra
options, the error paths and the ASCII card folder. The real-card tests run the three fixtures in
tests/fixtures/level_cards through the Tesseract on this machine, and one of them goes through the queue worker into the
level store. Without a Tesseract program those tests are skipped. Only tmp_path is written.
"""

import io
import subprocess
from pathlib import Path

import pytest

from core import level_ocr
from core.level_check import LevelQueue, OcrWorker, QueueItem
from core.level_store import LevelStore

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "level_cards"
REAL_CARDS = {  # card file -> (level, xp_cur, xp_need), as the file names say
    "lvl1_xp55_5196.png": (1, 55, 5196),
    "lvl0_xp1136_5049.png": (0, 1136, 5049),
    "lvl18_xp18376_22689.png": (18, 18376, 22689),
}
ACC = "111111111111110001"  # a fake Discord user id
NOW = 1_700_000_000.0

needs_ocr_libraries = pytest.mark.skipif(not level_ocr.ocr_available()[0], reason="Pillow or pytesseract is not installed")


def _blank_image():
    return level_ocr.Image.new("L", (40, 20), 255)


def _png_bytes():
    buffer = io.BytesIO()
    _blank_image().save(buffer, format="PNG")
    return buffer.getvalue()


def _fake_run(monkeypatch, *, stdout=b"", returncode=0, raises=None, seen=None):
    """Replaces subprocess.run for one test. `seen` collects (args, kwargs, whether the card file exists during the run)"""

    def fake(args, **kwargs):
        if seen is not None:
            seen.append((list(args), kwargs, Path(args[1]).exists()))
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=b"")

    monkeypatch.setattr(level_ocr.subprocess, "run", fake)


@needs_ocr_libraries
def test_the_text_comes_back_on_stdout_with_the_same_page_mode(monkeypatch):
    seen = []
    _fake_run(monkeypatch, stdout=b"LVL 1\r\nXP 55/5196\r\n", seen=seen)
    text = level_ocr._ocr(_blank_image(), 6)
    assert text == "LVL 1\nXP 55/5196\n"  # the console's CRLF becomes LF, as the old TXT file had it
    args, kwargs, card_existed = seen[0]
    assert args[1].endswith("card.png") and args[2] == "stdout"
    assert args[3:] == ["--psm", "6"]
    assert card_existed is True  # the card image is on disk while tesseract reads it
    assert not Path(args[1]).exists() and not Path(args[1]).parent.exists()  # and its folder is removed afterwards
    assert kwargs["timeout"] == level_ocr.OCR_TIMEOUT_SECONDS
    assert kwargs["stdin"] == subprocess.DEVNULL


@needs_ocr_libraries
def test_the_extra_options_are_passed_on_as_before(monkeypatch):
    seen = []
    _fake_run(monkeypatch, stdout=b"12", seen=seen)
    level_ocr._ocr(_blank_image(), 7, "-c tessedit_char_whitelist=0123456789")
    assert seen[0][0][3:] == ["--psm", "7", "-c", "tessedit_char_whitelist=0123456789"]


@needs_ocr_libraries
def test_a_tesseract_error_code_is_an_ocr_error_not_a_crash(monkeypatch):
    _fake_run(monkeypatch, returncode=1)
    result = level_ocr.read_level_card(_png_bytes())
    assert result["ok"] is False
    assert result["error"] == "ocr_error:TesseractRunError"


@needs_ocr_libraries
@pytest.mark.parametrize("problem", [subprocess.TimeoutExpired(cmd="tesseract", timeout=25), FileNotFoundError(2, "missing")],
                         ids=["timeout", "missing-program"])
def test_a_timeout_or_a_missing_program_is_an_ocr_error(monkeypatch, problem):
    _fake_run(monkeypatch, raises=problem)
    result = level_ocr.read_level_card(_png_bytes())
    assert result["ok"] is False
    assert result["error"] == "ocr_error:" + type(problem).__name__


@needs_ocr_libraries
def test_the_card_folder_has_an_ascii_path_even_when_the_temp_folder_does_not(monkeypatch, tmp_path):
    non_ascii = str(tmp_path / "Dương")  # a temp folder whose name has a Vietnamese letter
    monkeypatch.setattr(level_ocr.tempfile, "gettempdir", lambda: non_ascii)
    folder = level_ocr._ocr_scratch_folder()
    assert folder is None or level_ocr._is_ascii(folder)
    assert folder != non_ascii


@pytest.fixture
def installed_tesseract():
    if not level_ocr.ocr_available()[0]:
        pytest.skip("Pillow or pytesseract is not installed")
    if level_ocr.find_tesseract("") is None:
        pytest.skip("no Tesseract program on this machine")


@pytest.mark.parametrize("name", sorted(REAL_CARDS))
def test_each_real_card_gives_the_same_numbers_through_stdout(installed_tesseract, name):
    result = level_ocr.read_level_card((FIXTURES / name).read_bytes())
    assert result["ok"] is True, result.get("error")
    level, xp_cur, xp_need = REAL_CARDS[name]
    assert (result["level"], result["xp_cur"], result["xp_need"]) == (level, xp_cur, xp_need)


def test_a_real_card_read_by_the_queue_worker_is_stored_for_the_account(installed_tesseract, tmp_path):
    folder = tmp_path / "level_queue"
    folder.mkdir()
    card = folder / (ACC + "_1.png")
    sidecar = folder / (ACC + "_1.json")
    card.write_bytes((FIXTURES / "lvl1_xp55_5196.png").read_bytes())
    sidecar.write_text('{"account_id": "%s"}' % ACC, encoding="utf-8")
    store_path = tmp_path / "levels.sqlite"
    worker = OcrWorker(LevelQueue(folder=folder), LevelStore(store_path), clock=lambda: NOW)
    worker.process(QueueItem(card, sidecar, ACC, 1, "tester", NOW))
    row = LevelStore(store_path).get(ACC)  # a new store object: the value is in the file
    assert row is not None and row["last_ok"] == 1
    assert (row["level"], row["xp_cur"], row["xp_need"]) == (1, 55, 5196)
    assert not card.exists() and not sidecar.exists()  # the worker deletes the card and its sidecar
