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

import asyncio
import io
import re
import threading
import time

import aiohttp

from core import level_ocr

OCR_SPACE_URL = "https://api.ocr.space/parse/image"

# Q5: ocr.space is called only with a key the owner set (ocrApi in config/global_settings.json). Without one the
# quest image is read by the local Tesseract only, and the public demo key "helloworld" is never sent
OCR_NO_KEY_TEXT = (
    "[quest] ocrApi chưa đặt, nên không gọi ocr.space: chỉ dùng Tesseract. "
    "Muốn dùng ocr.space, đặt khóa trong config/global_settings.json (ocrApi)."
)
_no_key_noted = False


def ocr_space_key_usable(api_key):
    """True only for a key the owner set: an empty key and the public demo key 'helloworld' are not usable"""
    return isinstance(api_key, str) and api_key.strip().lower() not in ("", "helloworld")

# "tesseract": the local Tesseract reads the quest image first, ocr.space is used only when that read is not usable
# (see utils/image_to_text/get_quest_details.py). "ocrspace": the old behaviour, ocr.space only
QUEST_OCR_ENGINE = "tesseract"

# D1: one process-wide limit for ocr.space. Every account shares it, so the requests never come all at once
OCR_MIN_GAP_SECONDS = 2.0  # the least time between two requests (their starts)
OCR_429_BACKOFF_SECONDS = 30  # after HTTP 429 every account waits this long; the caller retries once
OCR_SPACE_TIMEOUT_SECONDS = 15

# D8: the local Tesseract, process-wide, never on the event loop
TESSERACT_MAX_IN_FLIGHT = 2
TESSERACT_PSM = 4  # a single column of rows of different size: checked on a real Quest Log sample
# "tsv" as a config word is not found: the ASCII copy of the language data has no configs folder. The variable is built in
TESSERACT_TSV_OPTION = "-c tessedit_create_tsv=1"
IMAGE_DOWNLOAD_TIMEOUT_SECONDS = 15
IMAGE_MAX_BYTES = 4 * 1024 * 1024

# Looked up at call time, so tests can replace the clock
_monotonic = time.monotonic

_PROGRESS_SHAPE = re.compile(r"^[0-9OoIl|]+\s*/\s*[0-9OoIl|]+$")  # a progress read with letters for digits
_PROGRESS_FIX = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1", "|": "1"})


class _Gate:
    """A limit shared by every event loop of the process. Each account runs its own event loop in its own thread, so an
    asyncio.Semaphore would not be shared: this gate is a threading lock, and a waiting caller polls with asyncio.sleep.
    gap: the least time between two starts. A backoff holds every caller until it ends"""

    def __init__(self, limit, gap=0.0):
        self.limit = limit
        self.gap = gap
        self._lock = threading.Lock()
        self._in_flight = 0
        self._next_start = 0.0
        self._backoff_until = 0.0

    async def acquire(self):
        while True:
            with self._lock:
                now = _monotonic()
                if self._in_flight < self.limit:
                    ready_at = max(self._next_start, self._backoff_until)
                    if now >= ready_at:
                        self._in_flight += 1
                        self._next_start = now + self.gap
                        return
                    delay = ready_at - now
                else:
                    delay = 0.2
            await asyncio.sleep(min(max(delay, 0.05), 0.5))

    def release(self):
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)

    def back_off(self, seconds):
        """Every caller waits until now + seconds. True only when this call started a new backoff (one log line for it)"""
        with self._lock:
            now = _monotonic()
            if self._backoff_until > now:
                return False
            self._backoff_until = now + seconds
            return True


_OCR_SPACE_GATE = _Gate(limit=1, gap=OCR_MIN_GAP_SECONDS)
_TESSERACT_GATE = _Gate(limit=TESSERACT_MAX_IN_FLIGHT)


# ---------------------------------------------------------------- ocr.space (the fallback)
async def _ocr_space_once(image_url, session, api_key):
    """One request to ocr.space. Returns (429, None) when it is rate limited, else (200, the JSON answer)"""
    url = OCR_SPACE_URL
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://ocr.space/",
        "Origin": "https://ocr.space",
    }
    timeout = aiohttp.ClientTimeout(total=OCR_SPACE_TIMEOUT_SECONDS)
    data = aiohttp.FormData()
    data.add_field("apikey", api_key)
    data.add_field("url", image_url)
    data.add_field("language", "eng")
    data.add_field("isOverlayRequired", "true")
    data.add_field("ocrengine", "2")
    async with session.post(url, data=data, headers=headers, timeout=timeout) as response:
        try:
            response.raise_for_status()
        except aiohttp.ClientResponseError as error:
            if error.status == 429:
                return 429, None
            raise
        return 200, await response.json()


def _lines_from_ocr_space(result, api_key):
    """The lines of an ocr.space answer, in the shape extract_quests reads. None when there is no usable text"""
    if not isinstance(result, dict):
        print("OCR returned an unexpected response.")
        return None

    if result.get("IsErroredOnProcessing"):
        error_msg = result.get("ErrorMessage")
        print(f"API Error: {error_msg}")

        if "helloworld" in api_key.lower():
            print(
                "The default demo key could be rate-limited. Please get your own free key at https://ocr.space/ocrapi"
            )
            if error_msg:
                print(f"Error : {error_msg}")
        return None

    parsed_results = result.get("ParsedResults", [])
    if not parsed_results:
        print("No text found or image could not be read.")
        return None

    lines = []
    for page in parsed_results:
        overlay = page.get("TextOverlay")
        if not overlay or not overlay.get("Lines"):
            print("No positional overlay data returned for this page.")
            continue
        for line in overlay["Lines"]:
            words = line.get("Words", [])
            x_coord = 0
            y_coord = 0
            if words:
                x_coord = words[0].get("Left", 0)
                y_coord = words[0].get("Top", 0)
            text_portion = line.get("LineText") or ""
            text = re.sub(r"[^a-zA-Z0-9 /]", "", text_portion)
            lines.append({"text": text, "x_axis": int(x_coord), "y_axis": int(y_coord)})

    if not lines:
        # no page had positional text
        print("No positional text found in the OCR response.")
        return None
    return lines


async def get_text_from_url(
    image_url: str, session: aiohttp.ClientSession, api_key: str = "helloworld"
):
    """The ocr.space reader: the lines in the shape extract_quests reads, or None. At most one request at a time for the
    whole process and OCR_MIN_GAP_SECONDS between two starts (D1). HTTP 429: every account waits OCR_429_BACKOFF_SECONDS,
    then the call retries once and gives up with None. A timeout or a client error is None too (D2), never an exception"""
    print(f" Attempting {image_url} url")
    global _no_key_noted
    if not ocr_space_key_usable(api_key):
        # Q5: no key set by the owner: ocr.space is not called and the demo key is never sent
        if not _no_key_noted:
            _no_key_noted = True
            print(OCR_NO_KEY_TEXT)
        return None

    for attempt in (1, 2):
        await _OCR_SPACE_GATE.acquire()
        try:
            status, result = await _ocr_space_once(image_url, session, api_key)
        except (asyncio.TimeoutError, aiohttp.ClientError) as error:
            print(f"HTTP Request failed: {error}")
            return None
        finally:
            _OCR_SPACE_GATE.release()

        if status != 429:
            return _lines_from_ocr_space(result, api_key)
        if attempt == 1 and _OCR_SPACE_GATE.back_off(OCR_429_BACKOFF_SECONDS):
            print(f"[quest] OCR rate limited, backing off {OCR_429_BACKOFF_SECONDS}s")
    return None


# ---------------------------------------------------------------- local Tesseract (D8)
def tsv_to_lines(tsv_text, scale):
    """Tesseract TSV (its word rows, level 5) in the ocr.space line shape. A line is the words that share block, paragraph
    and line number. Its position is the left and top of its first word: the same as ocr.space's Words[0]. The coordinates
    are divided by scale (the image was enlarged before OCR). A progress line read with letters (o/1) gets its digits back.
    [] when the text is not TSV"""
    rows = (tsv_text or "").splitlines()
    if not rows:
        return []
    header = rows[0].split("\t")
    if "text" not in header or "line_num" not in header:
        return []
    col = {name: index for index, name in enumerate(header)}
    lines = {}
    order = []
    for raw in rows[1:]:
        cells = raw.split("\t")
        if len(cells) < len(header) or cells[col["level"]] != "5":
            continue
        word = "\t".join(cells[col["text"]:]).strip()
        if not word:
            continue
        key = (cells[col["block_num"]], cells[col["par_num"]], cells[col["line_num"]])
        if key not in lines:
            lines[key] = {
                "x": float(cells[col["left"]]),
                "y": float(cells[col["top"]]),
                "words": [],
            }
            order.append(key)
        lines[key]["words"].append(word)

    result = []
    for key in order:
        item = lines[key]
        text = " ".join(item["words"]).strip()
        if _PROGRESS_SHAPE.match(text):
            text = text.replace(" ", "").translate(_PROGRESS_FIX)
        text = " ".join(re.sub(r"[^a-zA-Z0-9 /]", "", text).split())  # the same clean-up as the ocr.space path
        if not text:
            continue
        result.append(
            {
                "text": text,
                "x_axis": int(round(item["x"] / scale)),
                "y_axis": int(round(item["y"] / scale)),
            }
        )
    return result


def _tesseract_lines(data):
    """Runs in a worker thread. The lines of the quest image read by the local Tesseract: the same program, page mode
    and private environment as the level check (core/level_ocr.py). Raises when Tesseract cannot be used"""
    ok, reason = level_ocr.ocr_available()
    if not ok:
        raise RuntimeError(f"OCR libraries missing: {reason}")
    if level_ocr.current_program() is None and level_ocr.find_tesseract("") is None:
        raise RuntimeError("no Tesseract program")
    image = level_ocr.Image.open(io.BytesIO(data))
    prepared = level_ocr.preprocess(image)  # 3x, light text on dark becomes dark on light, Otsu binary
    raw = level_ocr.run_text_pass(prepared, TESSERACT_PSM, TESSERACT_TSV_OPTION)
    return tsv_to_lines(raw, level_ocr.UPSCALE)


async def _download_image(image_url, session):
    """The bytes of the image, or None (not 200, too big, or no answer in time)"""
    timeout = aiohttp.ClientTimeout(total=IMAGE_DOWNLOAD_TIMEOUT_SECONDS)
    async with session.get(image_url, timeout=timeout) as response:
        if response.status != 200:
            return None
        if (response.content_length or 0) > IMAGE_MAX_BYTES:
            return None
        data = await response.read()
    return data if len(data) <= IMAGE_MAX_BYTES else None


async def get_text_by_tesseract(image_url: str, session):
    """D8: the lines read by the local Tesseract, in the shape of get_text_from_url, or None when it cannot be used
    (no program, no image, a failure or a timeout). The OCR runs in a thread, at most TESSERACT_MAX_IN_FLIGHT at a time"""
    try:
        data = await _download_image(image_url, session)
    except Exception as error:
        print(f"[quest] image download for Tesseract failed: {type(error).__name__}")
        return None
    if not data:
        return None

    await _TESSERACT_GATE.acquire()
    try:
        lines = await asyncio.to_thread(_tesseract_lines, data)
    except Exception as error:
        # the type only: the message of a failed run could hold text from the image
        print(f"[quest] Tesseract read failed: {type(error).__name__}")
        return None
    finally:
        _TESSERACT_GATE.release()
    return lines or None


async def main():
    my_api_key = ""
    target_image_url = "https://example.invalid/quest.png"

    async with aiohttp.ClientSession() as session:
        await get_text_from_url(target_image_url, session, api_key=my_api_key)


if __name__ == "__main__":
    asyncio.run(main())
