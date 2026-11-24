"""Q5: with no ocrApi set, a quest image is never sent to ocr.space and the public demo key 'helloworld' is never
used. Tesseract is the only reader then, and one line says so per process."""

import asyncio

import pytest

import utils.image_to_text.get_quest_details as get_quest_details
import utils.image_to_text.image_to_text as image_to_text

URL = "https://cdn.example.invalid/quest.png"


def _no_tesseract(monkeypatch):
    async def fake_tesseract(image_url, session):
        return None

    monkeypatch.setattr(image_to_text, "get_text_by_tesseract", fake_tesseract)


def _spy_ocr_space(monkeypatch):
    calls = []

    async def fake_once(image_url, session, api_key):
        calls.append(api_key)
        return 200, {}

    monkeypatch.setattr(image_to_text, "_ocr_space_once", fake_once)
    return calls


def _fresh_note(monkeypatch):
    monkeypatch.setattr(image_to_text, "_no_key_noted", False)


@pytest.mark.parametrize("key", ["", "   ", "helloworld", "HelloWorld"])
def test_no_key_never_calls_ocr_space(monkeypatch, key):
    _fresh_note(monkeypatch)
    _no_tesseract(monkeypatch)
    calls = _spy_ocr_space(monkeypatch)
    assert asyncio.run(get_quest_details.get_quest_details(URL, object(), key)) == []
    assert calls == []


def test_a_key_the_owner_set_is_still_used(monkeypatch):
    _fresh_note(monkeypatch)
    _no_tesseract(monkeypatch)
    calls = _spy_ocr_space(monkeypatch)
    asyncio.run(get_quest_details.get_quest_details(URL, object(), "owner-key-123"))
    assert calls == ["owner-key-123"]


def test_the_no_key_line_is_printed_once(monkeypatch, capsys):
    _fresh_note(monkeypatch)
    _no_tesseract(monkeypatch)
    _spy_ocr_space(monkeypatch)
    for _ in range(3):
        asyncio.run(get_quest_details.get_quest_details(URL, object(), ""))
    assert capsys.readouterr().out.count(image_to_text.OCR_NO_KEY_TEXT) == 1
