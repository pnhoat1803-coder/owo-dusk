# This file is part of owo-dusk.
#
# Copyright (c) 2024-present EchoQuill
#
# Portions of this file are based on code by EchoQuill, licensed under the
# GNU General Public License v3.0 (GPL-3.0).
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

import re
import time

from utils.image_to_text import image_to_text
from utils.quest_helper.quest_types import QUEST_IDS

ENGINE_NOTE_SECONDS = 3600  # D8: the engine used for an account is printed at most once an hour
_engine_notes = {}  # id(session) -> monotonic time of the last note. One session per account

# The row number of a Quest Log row: "1. <emoji> Receive cookies" reads as "1 Receive cookies". An emoji that OCR reads as
# a digit adds one more: "2 2 Send an action" (seen on the real sample). A quest name never starts with a number
_ROW_NUMBER = re.compile(r"^(?:\d{1,2}\s+)+")
# Lines that are never a quest name (checked on the real sample: "Completion rewards" sits under every row)
_BLACKLIST = (
    "claimed",
    "rewards earned",
    "you can claim a new quest",
    "free a slot to receive it",
    "completion rewards",
)


def int_gap(x: int, x2: int, mingap: int, maxgap: int) -> bool:
    diff = abs(x - x2)
    return diff >= mingap and diff <= maxgap


def _quest_name(text: str) -> str:
    """The quest name as QUEST_IDS spells it: single spaces, lower case, no row number"""
    name = " ".join(str(text).split())
    return _ROW_NUMBER.sub("", name).strip().lower()


def extract_quests(ocr_results: list) -> list[dict]:
    if not ocr_results or not isinstance(ocr_results, list):
        # OCR failed or returned nothing usable (None, empty, not a list)
        return []

    progress_pattern = re.compile(r"^\d+/\d+$")

    # Identify potential quest text and progress items
    progress_items = []
    quest_candidates = []

    for item in ocr_results:
        text = item["text"]  # unwrap the set

        if progress_pattern.match(text.replace(",", "")):
            progress_items.append(item)
        elif len(text) > 4:
            quest_candidates.append(item)

    quests = []

    # remove for sure not quest items
    quest_candidates = [
        candidate
        for candidate in quest_candidates
        if not any(phrase in candidate["text"].strip().lower() for phrase in _BLACKLIST)
    ]

    # sort both to be at their close y axis
    quest_candidates.sort(key=lambda d: d["y_axis"])
    progress_items.sort(key=lambda d: d["y_axis"])

    for progress_item in progress_items.copy():
        for candidate in quest_candidates.copy():
            # Y axis difference is exactly 25 on every images
            # Noticed some have 23 though
            # to be safe keeping range somewhat lenient
            if int_gap(candidate["y_axis"], progress_item["y_axis"], 22, 27):
                current, total = progress_item["text"].strip().replace(",", "").split("/")

                quests.append(
                    {
                        "quest": _quest_name(candidate["text"]),
                        "progress": progress_item["text"].strip(),
                        "current": int(current),
                        "total": int(total),
                        "complete": int(current) == int(total),
                    }
                )
                quest_candidates.remove(candidate)
                break  # one progress line is one quest

    return quests


def _clean_tesseract_read(lines, quests):
    """A Tesseract read is used only when it has quests, every progress line belongs to a quest, and every quest name is
    one the bot knows (QUEST_IDS). Anything else goes to ocr.space"""
    if not quests:
        return False
    progress_lines = [line for line in lines if re.match(r"^\d+/\d+$", line["text"].replace(",", ""))]
    if len(progress_lines) != len(quests):
        return False
    return all(quest["quest"] in QUEST_IDS for quest in quests)


def _note_engine(session, engine):
    """Prints the engine used for this account at most once an hour (the account's session is the key)"""
    key = id(session)
    now = time.monotonic()
    last = _engine_notes.get(key)
    if last is not None and now - last < ENGINE_NOTE_SECONDS:
        return
    _engine_notes[key] = now
    print(f"[quest] OCR engine: {engine}")


async def get_quest_details(image_url: str, session, api_key: str = ""):
    """The quests of a Quest Log image. With QUEST_OCR_ENGINE "tesseract" the local Tesseract reads it first (D8), and its
    read is used only when it is clean. Otherwise, or when Tesseract cannot read it, ocr.space is used through its limiter
    (D1). [] when nothing can be read. Never raises for an OCR failure"""
    if image_to_text.QUEST_OCR_ENGINE == "tesseract":
        lines = await image_to_text.get_text_by_tesseract(image_url, session)
        if lines:
            quests = extract_quests(lines)
            if _clean_tesseract_read(lines, quests):
                _note_engine(session, "tesseract")
                return quests
            print("[quest] Tesseract read is not clean (progress or quest names)"
                  + (", using ocr.space" if image_to_text.ocr_space_key_usable(api_key) else ""))
        else:
            print("[quest] Tesseract could not read the image"
                  + (", using ocr.space" if image_to_text.ocr_space_key_usable(api_key) else ""))

    ocr_results = await image_to_text.get_text_from_url(image_url, session, api_key)
    if not ocr_results or not isinstance(ocr_results, list):
        return []
    _note_engine(session, "ocr.space")
    return extract_quests(ocr_results)
