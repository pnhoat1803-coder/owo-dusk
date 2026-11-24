"""Reads the OwO level card (the image `owolvl` answers with) into numbers, with OCR.

Pure functions, no Discord. The module imports and works without Pillow or pytesseract: ocr_available() then says why
OCR is off and read_level_card() returns ok=False. It never logs and never returns a name, only numbers and an error.

The card (800x240 in the real fixtures): purple space photo, light text. Name, `LVL 18` with a big number, a row with
`Rank: #3,732,588` and `XP:18,376/22,689`, a progress bar. The rank and the XP label overlap on the card (`#3,732,58XP:`),
so the rank is only trusted when it is clean (rank_ok) and is never needed for ok.

Every number that decides `ok` is read more than once (the whole card, and two crops) and is accepted only when at least
two reads agree and no other value ties with it. A card that cannot be read twice the same way is ok=False, never a guess.
"""

import io
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

try:  # optional: the feature turns itself off when one of them is missing
    from PIL import Image, ImageOps

    _PIL_ERROR = None
except Exception as _error:  # depends on the machine
    Image = ImageOps = None
    _PIL_ERROR = f"Pillow is not installed ({type(_error).__name__})"

# The folder that holds uwu.py. Relative paths from the config and the portable Tesseract are resolved against it,
# never against the current working directory (the project lives on a portable drive and is started from anywhere).
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR_DIR = os.path.join("third_party", "python")  # relative to PROJECT_ROOT: a vendored copy of pytesseract
PORTABLE_REL = os.path.join("third_party", "tesseract")  # relative to PROJECT_ROOT: the portable Tesseract folder


def _import_pytesseract():
    """(module, None) or (None, reason). An installed pytesseract wins; the copy in third_party/python is the fallback
    and its folder is added at the END of sys.path, so it can never shadow a real package"""
    try:
        import pytesseract as module

        return module, None
    except Exception as first_error:  # depends on the machine
        reason = f"pytesseract is not installed ({type(first_error).__name__})"
        vendor = os.path.join(PROJECT_ROOT, VENDOR_DIR)
        if not os.path.isdir(os.path.join(vendor, "pytesseract")):
            return None, reason
        added = vendor not in sys.path
        if added:
            sys.path.append(vendor)
        try:
            import pytesseract as module

            return module, None
        except Exception as second_error:  # most likely `packaging` or Pillow is missing
            if added:
                sys.path.remove(vendor)
            sys.modules.pop("pytesseract", None)
            return None, f"pytesseract (bundled copy) failed to import ({type(second_error).__name__}: {second_error})"


pytesseract, _TESS_ERROR = _import_pytesseract()

MAX_BYTES = 3 * 1024 * 1024
UPSCALE = 3
CROP_PAD = 24
RAW_LIMIT = 300
OCR_TIMEOUT_SECONDS = 25
LEVEL_MIN, LEVEL_MAX = 0, 500  # new OwO accounts show LVL 0
XP_NEED_MIN, XP_NEED_MAX = 100, 10_000_000
XP_BASE, XP_FACTOR = 5000, 49  # the XP goal of a level: XP_BASE + XP_FACTOR * (level + 1)^2, see level_from_need

# Crops, as fractions (left, top, right, bottom) of the card. Measured on the real 800x240 owolvl cards in
# tests\fixtures\level_cards\ (level 0, 1 and 18): the big level number starts at x=308 under the name and stops before
# the `Rank:` label (x=390); the rank/XP row is at y=145..172 from x=390 to the right edge of the bar (x=750).
REGION_LEVEL = (0.375, 0.60, 0.485, 0.88)
REGION_LEVEL_ALT = (0.370, 0.58, 0.483, 0.90)  # the same number with a little more margin: an independent second read
REGION_STATS = (0.48, 0.58, 0.95, 0.74)
REGION_STATS_ALT = (0.48, 0.57, 0.96, 0.76)
DIGITS = "0123456789"
STATS_WHITELIST = "0123456789,.:/#XP"
# (region, scale, binarise) of the crop reads. Measured on the real fixtures and on cards drawn in the same layout: the big
# level digits (about 40 px tall) read best at their own size (x1) or x2; more upscaling made 7 and 1 swap. The small
# rank/XP row reads best at x2 and x3 with the character whitelist
LEVEL_READS = ((REGION_LEVEL, 1, True), (REGION_LEVEL_ALT, 2, False))
STATS_READS = ((REGION_STATS, 2, True), (REGION_STATS_ALT, 3, True), (REGION_STATS, 4, True))

WINDOWS_PATHS = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Tesseract-OCR", "tesseract.exe"),
)

_tesseract_cmd = None  # the path set on pytesseract, found once
_tesseract_kind = None  # "portable" (inside the project) or "system"


def ocr_available():
    """(True, "") when Pillow and pytesseract can be imported, else (False, reason). Says nothing about the Tesseract program"""
    if Image is None or pytesseract is None:
        return False, _PIL_ERROR or _TESS_ERROR or "OCR libraries are missing"
    return True, ""


def portable_dir():
    return os.path.join(PROJECT_ROOT, PORTABLE_REL)


def _portable_binary():
    return os.path.join(portable_dir(), "tesseract.exe" if os.name == "nt" else "tesseract")


def _inside(path, folder):
    path, folder = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(folder))
    return path == folder or path.startswith(folder + os.sep)


_ascii_tessdata_cache = None


def _is_ascii(text):
    try:
        text.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def _ascii_tessdata(source):
    """A folder with only ASCII letters in its path that holds a copy of the *.traineddata files of `source`.
    Found on a real machine: the Windows build of Tesseract fails with `filesystem error: Cannot convert character
    sequence: Illegal byte sequence` when TESSDATA_PREFIX (or any other way to name its data folder) has a letter such
    as `â` in it, and a project folder can have one. The copy goes to the temp folder (or ProgramData / Public when
    that one is not ASCII either) and is made once. None when no ASCII place can be written"""
    global _ascii_tessdata_cache
    if _ascii_tessdata_cache and os.path.isdir(_ascii_tessdata_cache):
        return _ascii_tessdata_cache
    bases = [tempfile.gettempdir(), os.environ.get("ProgramData", ""), os.environ.get("PUBLIC", ""), r"C:\Temp"]
    try:
        names = [n for n in os.listdir(source) if n.lower().endswith(".traineddata")]
    except OSError:
        return None
    if not names:
        return None
    for base in bases:
        if not base or not _is_ascii(base):
            continue
        target = os.path.join(base, "owo_dusk_tessdata")
        try:
            os.makedirs(target, exist_ok=True)
            for name in names:
                src, dst = os.path.join(source, name), os.path.join(target, name)
                if not os.path.isfile(dst) or os.path.getsize(dst) != os.path.getsize(src):
                    partial = dst + ".part"
                    shutil.copyfile(src, partial)
                    os.replace(partial, dst)
            _ascii_tessdata_cache = target
            return target
        except OSError:
            continue
    return None


def _private_env(kind):
    """The environment pytesseract gives to the tesseract process. A copy: os.environ itself is never changed. The
    portable program gets TESSDATA_PREFIX (its own tessdata folder; a path with spaces is safe here, unlike a quoted
    --tessdata-dir argument which pytesseract passes on with the quotes on Windows). When that folder has a non-ASCII
    letter in its path, an ASCII copy of the language files is used instead (see _ascii_tessdata)"""
    env = dict(os.environ)
    # One OpenMP thread per Tesseract run: the bot has many accounts, the OCR must stay small
    env.setdefault("OMP_THREAD_LIMIT", "1")
    if kind == "portable":
        tessdata = os.path.join(portable_dir(), "tessdata")
        if os.path.isdir(tessdata):
            env["TESSDATA_PREFIX"] = tessdata if _is_ascii(tessdata) else (_ascii_tessdata(tessdata) or tessdata)
    return env


def find_tesseract(config_path=""):
    """Path of the Tesseract program, first hit of: the configured path (a relative one is relative to the project
    folder), <project>/third_party/tesseract, PATH, the usual Windows folders. Sets pytesseract's command and its
    private environment once. None when there is no program"""
    global _tesseract_cmd, _tesseract_kind
    candidates = []
    if config_path:
        configured = os.path.expandvars(os.path.expanduser(str(config_path)))
        if not os.path.isabs(configured):
            configured = os.path.join(PROJECT_ROOT, configured)
        candidates.append(os.path.normpath(configured))
    candidates.append(_portable_binary())
    found = shutil.which("tesseract")
    if found:
        candidates.append(found)
    candidates.extend(path for path in WINDOWS_PATHS if path)
    for path in candidates:
        if os.path.isfile(path):
            kind = "portable" if _inside(path, portable_dir()) else "system"
            if pytesseract is not None and (_tesseract_cmd != path or _tesseract_kind != kind):
                pytesseract.pytesseract.tesseract_cmd = path
                pytesseract.pytesseract.environ = _private_env(kind)
            _tesseract_cmd, _tesseract_kind = path, kind
            return path
    return None


def tesseract_info(path):
    """(kind, version) of the program `path` that find_tesseract returned: kind is "portable" or "system", version is
    a string like "5.5.0" or None when `tesseract --version` does not answer. Runs the program once, 5 s at most"""
    kind = "portable" if _inside(path, portable_dir()) else "system"
    version = None
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        run = subprocess.run(
            [path, "--version"], capture_output=True, timeout=5, stdin=subprocess.DEVNULL, env=_private_env(kind),
            creationflags=flags,
        )
        text = (run.stdout or b"").decode("utf-8", "replace") + " " + (run.stderr or b"").decode("utf-8", "replace")
        match = re.search(r"tesseract\s+v?(\d+(?:\.\d+)+)", text, re.I)
        version = match.group(1) if match else None
    except Exception:  # a missing or broken program is reported as "version unknown", never raised
        pass
    return kind, version


def describe_tesseract(path):
    """The one start-up line: `[levelCheck] tesseract: portable (third_party\tesseract, v5.5.0)` or
    `system (<path of tesseract.exe>, v5.5.0)`. `version unknown` when the program does not answer `--version` like
    Tesseract (for example a levelCheck.tesseractPath that points at the wrong file)"""
    kind, version = tesseract_info(path)
    shown = f"v{version}" if version else "version unknown"
    if kind == "portable":
        return f"[levelCheck] tesseract: portable ({PORTABLE_REL}, {shown})"
    return f"[levelCheck] tesseract: system ({path}, {shown})"


# ---------------------------------------------------------------------------------------------------- parsing
_NUMBER_RUN = re.compile(r"[0-9OolI][0-9OolI,\.]*")
_LEVEL_RE = re.compile(r"\b(?:LVL|LEVEL)[ \t]*[:.]?[ \t]*(\d{1,3})(?![\d/,\.]|[ 	]+\d)", re.I)  # same line; `2/7` and `2 4 4` (split digits) are not a level
_LEVEL_WORD = re.compile(r"\bLEVEL\b", re.I)
_LONE_NUMBER = re.compile(r"^\s*(\d{1,3})\s*$")
# The label is XP, but on the card the X is often eaten by the rank next to it (`KP:`, `P:`), so P (or `XP`/`KP`), a separator and
# `cur/need` is the anchor. A separator (a colon or a space) is required: `XP248,352` could be a colon misread as a 2.
# Bare `a/b` without the label is never accepted: it could be the end of the rank
_XP_RE = re.compile(r"(?:[XK]P|P)(?:[ 	]*[:;.#*][ 	]*|[ 	]+)([\d,\.]{1,12})[ 	]*/[ 	]*([\d,\.]{1,12})(?![\d,\.]*[ 	]*/)", re.I)
_RANK_RE = re.compile(r"#\s*([\d,\.]{1,12})")
_PROFILE_WORDS = ("cookies", "my team", "about me")
_DIGIT_FIX = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1"})


def _fix_run(match, line):
    run = match.group(0)
    start, end = match.span()
    before = line[start - 1] if start > 0 else " "
    after = line[end] if end < len(line) else " "
    # no real digit: a lone O, l or I is a letter. Glued to letters (`Rank`, `Level`, `colour`): part of a word
    if not any(ch.isdigit() for ch in run) or before.isalpha() or after.isalpha():
        return run
    return run.translate(_DIGIT_FIX)


def normalise(text):
    """The OCR text with letters that look like digits (O, o, l, I) fixed, but only inside numbers, and the spaces
    inside numbers (`11, 351`) removed. Words such as LVL, Level and Rank are not touched"""
    text = (text or "").replace("\r", "\n")
    text = re.sub(r"(?<=\d)[ \t]*([,\.])[ \t]+(?=\d)", r"\1", text)
    return "\n".join(_NUMBER_RUN.sub(lambda m, ln=line: _fix_run(m, ln), line) for line in text.split("\n"))


def _to_int(text):
    """'11,351' -> 11351; '3.877.026' -> 3877026. None when it is not clean digits and separators"""
    digits = re.sub(r"[,\.]", "", text or "")
    return int(digits) if digits.isdigit() else None


def _xp_pair(match):
    """(cur, need) from an XP match when both numbers are written cleanly (`18,376`, `5049`), else (None, None)"""
    if not match:
        return None, None
    first, second = match.group(1), match.group(2)
    clean = r"\d{1,3}(?:[,\.]\d{3})+|\d+"
    if not (re.fullmatch(clean, first) and re.fullmatch(clean, second)):
        return None, None
    return _to_int(first), _to_int(second)


def _clean_group(text):
    """True for a number written as plain digits or in thousand groups (`3,877,026`), False for a cut one (`3,877,32`)"""
    return bool(re.fullmatch(r"\d{1,3}(?:[,\.]\d{3})+|\d+", (text or "").strip(",.")))


def detect_kind(text):
    low = (text or "").lower()
    if any(word in low for word in _PROFILE_WORDS):
        return "profile"
    if "lvl" in low:
        return "lvl"
    return "unknown"


def _blank():
    return {
        "ok": False, "level": None, "xp_cur": None, "xp_need": None, "rank": None, "rank_ok": False,
        "kind": "unknown", "error": None, "raw": "",
    }


def _find_level(text):
    found = _LEVEL_RE.search(text)
    if found:
        return int(found.group(1))
    # The profile card: the word Level and its number may be split across lines
    lines = text.split("\n")
    for index, line in enumerate(lines):
        word = _LEVEL_WORD.search(line)
        if not word:
            continue
        for follow in lines[index + 1 : index + 3]:
            lone = _LONE_NUMBER.match(follow)
            if lone:
                return int(lone.group(1))
        for follow in lines[index + 1 : index + 3]:
            lead = re.match(r"^\s*(\d{1,3})(?=\s|$)", follow)  # `2/7` is a misread 277, not level 2
            if lead:
                return int(lead.group(1))
    return None


def _find_rank(text, kind=None):
    """(rank, rank_ok): ok only for a clean number of 1 to 9 digits that is not glued to the XP label (the label can
    hide the last digits, and the X is often read as part of the number's neighbour)"""
    found = _RANK_RE.search(text)
    if not found:
        return None, False
    raw = found.group(1)
    value = _to_int(raw)
    if value is None:
        return None, False
    glued = bool(re.search(r"#\s*[\d,\.]{1,12}[XKP]", text, re.I))  # no space between the digits and the XP label
    if _clean_group(raw) and 1 <= len(str(value)) <= 9 and not glued:
        return value, True
    return None, False


def level_from_need(need):
    """The level that an XP goal belongs to, or None when the goal does not fit the formula. Measured on three real cards
    (level 0: 5,049, level 1: 5,196, level 18: 22,689): need = 5000 + 49 * (level + 1)^2. It is only a cross-check, never
    the only evidence: a goal that does not fit gives None and changes nothing"""
    if not isinstance(need, int) or need <= XP_BASE:
        return None
    rest, remainder = divmod(need - XP_BASE, XP_FACTOR)
    root = math.isqrt(rest)
    if remainder or root * root != rest or root < 1:
        return None
    return root - 1


def finish(result):
    """Validation, in place. ok needs 0<=level<=500, 0<=xp_cur<xp_need and 100<=xp_need<=10_000_000, and a level that
    does not contradict the XP goal (see level_from_need). A value that fails its rule is dropped, so a wrong number is
    never carried as if it were good"""
    level, cur, need = result.get("level"), result.get("xp_cur"), result.get("xp_need")
    level_ok = level is not None and LEVEL_MIN <= level <= LEVEL_MAX
    xp_ok = cur is not None and need is not None and 0 <= cur < need and XP_NEED_MIN <= need <= XP_NEED_MAX
    if level_ok and xp_ok:
        implied = level_from_need(need)
        if implied is not None and implied != level:
            level_ok = False
    if not level_ok:
        result["level"] = None
    if not xp_ok:
        result["xp_cur"] = result["xp_need"] = None
    result["ok"] = bool(level_ok and xp_ok)
    if result["ok"]:
        result["error"] = None
    elif level is not None and not level_ok:
        result["error"] = "bad_level"
    elif cur is not None and need is not None and not xp_ok:
        result["error"] = "bad_xp"
    else:
        result["error"] = "no_numbers"
    if not result.get("rank_ok"):
        result["rank"] = None
    return result


def parse_text(raw_text):
    """Numbers from OCR text, no image work. Returns the same dict as read_level_card"""
    result = _blank()
    result["kind"] = detect_kind(raw_text)
    text = normalise(raw_text)
    result["level"] = _find_level(text)
    result["xp_cur"], result["xp_need"] = _xp_pair(_XP_RE.search(text))
    result["rank"], result["rank_ok"] = _find_rank(text, result["kind"])
    result["raw"] = (raw_text or "")[:RAW_LIMIT]
    return finish(result)


# ---------------------------------------------------------------------------------------------------- image steps
def otsu_threshold(histogram):
    """Otsu threshold of a 256-bin histogram, plain Python"""
    total = sum(histogram)
    if not total:
        return 128
    weighted = sum(i * count for i, count in enumerate(histogram))
    sum_back = weight_back = 0
    best, threshold = -1.0, 128
    for i, count in enumerate(histogram):
        weight_back += count
        if not weight_back:
            continue
        weight_front = total - weight_back
        if not weight_front:
            break
        sum_back += i * count
        mean_back = sum_back / weight_back
        mean_front = (weighted - sum_back) / weight_front
        between = weight_back * weight_front * (mean_back - mean_front) ** 2
        if between > best:
            best, threshold = between, i
    return threshold


def preprocess(image):
    """RGB -> 3x LANCZOS -> grayscale -> invert (light text on a dark card becomes dark on light) -> auto-contrast ->
    binary by an Otsu threshold. Returns a mode 'L' image"""
    image = image.convert("RGB")
    width, height = image.size
    image = image.resize((max(1, width * UPSCALE), max(1, height * UPSCALE)), Image.LANCZOS)
    gray = ImageOps.autocontrast(ImageOps.invert(ImageOps.grayscale(image)))
    threshold = otsu_threshold(gray.histogram())
    return gray.point(lambda value: 255 if value > threshold else 0)


class TesseractRunError(RuntimeError):
    """Tesseract ran and exited with an error code. The message never holds card text (a card can show a name)"""


def _ocr_scratch_folder():
    """The folder for the card image that tesseract reads: an ASCII path, because Tesseract cannot open a non-ASCII path
    on Windows (`filesystem error: Cannot convert character sequence`). None when no such folder exists, and then the
    default temp folder is used"""
    for base in (tempfile.gettempdir(), os.environ.get("ProgramData", ""), os.environ.get("PUBLIC", ""), r"C:\Temp"):
        if base and _is_ascii(base) and os.path.isdir(base):
            return base
    return None


def _ocr(image, psm, extra=""):
    """The text of one OCR pass. Tesseract writes it to stdout, not to a TXT file: on some Windows machines Tesseract
    cannot create its output file (`Error, could not create TXT output file: Permission denied`, exit code 0), so the
    file pytesseract reads never appears. The program and its private environment are the ones find_tesseract() set on
    pytesseract, and the page mode and extra options are the same as before. Raises on any failure; read_level_card turns
    that into ocr_error:<name>"""
    config = f"--psm {psm} {extra}".strip()
    folder = tempfile.mkdtemp(prefix="owo_dusk_ocr_", dir=_ocr_scratch_folder())
    try:
        card = os.path.join(folder, "card.png")
        image.save(card, format="PNG")
        args = [pytesseract.pytesseract.tesseract_cmd, card, "stdout"] + shlex.split(config, posix=sys.platform != "win32")
        done = subprocess.run(
            args, env=pytesseract.pytesseract.environ, stdin=subprocess.DEVNULL, capture_output=True,
            timeout=OCR_TIMEOUT_SECONDS, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    if done.returncode != 0:
        raise TesseractRunError(f"tesseract exit code {done.returncode}")
    return (done.stdout or b"").decode("utf-8", "replace").replace("\r\n", "\n")


def current_program():
    """The Tesseract program find_tesseract() chose last (the level check's choice when it ran), or None. Read only"""
    return _tesseract_cmd


def run_text_pass(image, psm, extra=""):
    """Public, thin wrapper of _ocr for the quest reader (prompt 29, D8): the stdout text of one Tesseract pass on a PIL
    image. Raises like _ocr: TesseractRunError on a non-zero exit, subprocess.TimeoutExpired after OCR_TIMEOUT_SECONDS"""
    return _ocr(image, psm, extra)


def _crop_image(image, region, scale, binarise):
    """(crop, clipped). The region of the ORIGINAL card, scaled, auto-contrasted, light text made dark, with a white
    margin. The real card has a photo background, so a threshold is worked out for the crop alone (Otsu), used for the
    picture when `binarise`, and always to find out whether text runs into the left or right edge of the crop: then the
    number is cut by the crop and what Tesseract reads is only a part of it (`clipped` is True)"""
    width, height = image.size
    left, top, right, bottom = region
    x0, y0 = int(width * left), int(height * top)
    crop = image.convert("RGB").crop((x0, y0, max(x0 + 1, int(width * right)), max(y0 + 1, int(height * bottom))))
    if scale != 1:
        crop = crop.resize((crop.width * scale, crop.height * scale), Image.LANCZOS)
    gray = ImageOps.autocontrast(ImageOps.invert(ImageOps.grayscale(crop)))
    threshold = otsu_threshold(gray.histogram())
    binary = gray.point(lambda value: 255 if value > threshold else 0)
    ink_edge = binary.crop((0, 0, 1, binary.height)).histogram()[0] + binary.crop((binary.width - 1, 0, binary.width, binary.height)).histogram()[0]
    shown = binary if binarise else gray
    return ImageOps.expand(shown, border=CROP_PAD, fill=255), ink_edge > 0  # Tesseract reads a number better with a white margin


def _vote(values):
    """The value at least two reads agree on, when no other value has as many votes. None otherwise. `None` entries
    (a read that found nothing) do not vote"""
    counts = {}
    for value in values:
        if value is not None:
            counts[value] = counts.get(value, 0) + 1
    if not counts:
        return None
    best = max(counts.values())
    winners = [value for value, count in counts.items() if count == best]
    return winners[0] if best >= 2 and len(winners) == 1 else None


def _level_from_digits(text):
    digits = re.sub(r"\D", "", normalise(text))
    return int(digits) if 1 <= len(digits) <= 3 else None


def _read_lvl_card(image, processed, first):
    """The owolvl layout. `first` is the parse of the whole-card read. Level from the whole card and two crops, XP from the
    whole card and three crops of the rank/XP row; each is accepted only by vote (see _vote). When the crops did not give
    a winner for the level or the XP (the card layout changed, or it is not the owolvl card) a second whole-card read in
    another page mode votes too, so a card with the numbers somewhere else can still be read, only if two reads agree"""
    texts = []
    level_whole = [first["level"]]  # reads of the whole card
    level_crops = []  # (value, clipped) of the level crops
    for region, scale, binarise in LEVEL_READS:
        crop, clipped = _crop_image(image, region, scale, binarise)
        text = _ocr(crop, 7, f"-c tessedit_char_whitelist={DIGITS}")
        texts.append(text)
        level_crops.append((_level_from_digits(text), clipped))

    xp_reads = [(first["xp_cur"], first["xp_need"]) if first["xp_cur"] is not None else None]
    rank_reads = [first["rank"] if first["rank_ok"] else None]
    for region, scale, binarise in STATS_READS:
        text = _ocr(_crop_image(image, region, scale, binarise)[0], 7, f"-c tessedit_char_whitelist={STATS_WHITELIST}")
        texts.append(text)
        normal = normalise(text)
        pair = _xp_pair(_XP_RE.search(normal))
        xp_reads.append(pair if pair[0] is not None else None)
        value, good = _find_rank(normal)
        rank_reads.append(value if good else None)

    def decide():
        pair = _vote(xp_reads)
        # the XP goal implies a level (level_from_need): one more, independent read of it
        implied = level_from_need(pair[1]) if pair else None
        others = level_whole + [implied]
        # A crop whose text runs into its edge may show only a part of the number (another layout, or a wide number):
        # it only counts when a read of another kind (whole card, XP goal) says the same
        crops = [value for value, clipped in level_crops if value is not None and (not clipped or value in others)]
        return _vote(others + crops), pair

    level, pair = decide()
    if level is None or pair is None:
        second_text = _ocr(processed, 11)
        texts.append(second_text)
        second = parse_text(second_text)
        level_whole.append(second["level"])
        xp_reads.append((second["xp_cur"], second["xp_need"]) if second["xp_cur"] is not None else None)
        rank_reads.append(second["rank"] if second["rank_ok"] else None)
        level, pair = decide()

    result = dict(first)
    result["kind"] = "lvl" if first["kind"] == "unknown" else first["kind"]
    result["level"] = level
    result["xp_cur"], result["xp_need"] = pair if pair else (None, None)
    # The rank is cosmetic and often cut by the XP label: it needs two reads that agree (a read where it touches the label
    # does not count, see _find_rank). Never needed for ok
    rank = _vote(rank_reads)
    result["rank"], result["rank_ok"] = (rank, True) if rank is not None else (None, False)
    return result, texts


def read_level_card(image_bytes):
    """Numbers of a level or profile card image. Never raises: a problem comes back as ok=False and an error word"""
    result = _blank()
    try:
        if not isinstance(image_bytes, (bytes, bytearray)) or not image_bytes:
            result["error"] = "not_image"
            return result
        if len(image_bytes) > MAX_BYTES:
            result["error"] = "not_image"
            return result
        available, _reason = ocr_available()
        if not available:
            result["error"] = "ocr_unavailable"
            return result
        try:
            image = Image.open(io.BytesIO(bytes(image_bytes)))
            image.load()
        except Exception:
            result["error"] = "not_image"
            return result
        processed = preprocess(image)

        # Pass A: the whole card
        text = _ocr(processed, 6)
        texts = [text]
        parsed = parse_text(text)
        if parsed["kind"] == "profile":
            # A profile card has another layout than the crops below. Whole-card reads only: a second page mode when
            # the first one did not give everything, the first value wins
            if not parsed["ok"]:
                second_text = _ocr(processed, 11)
                texts.append(second_text)
                second = parse_text(second_text)
                for key in ("level", "xp_cur", "xp_need"):
                    if parsed.get(key) is None:
                        parsed[key] = second.get(key)
                if parsed["xp_cur"] is None or parsed["xp_need"] is None:
                    parsed["xp_cur"] = parsed["xp_need"] = None
                if not parsed["rank_ok"] and second["rank_ok"]:
                    parsed["rank"], parsed["rank_ok"] = second["rank"], True
            merged = finish(parsed)
        else:
            # Pass B: the owolvl card (its kind is often not recognised: `LVL` is a small word). Crops, by vote
            patch, crop_texts = _read_lvl_card(image, processed, parsed)
            texts.extend(crop_texts)
            merged = finish(patch)
        merged["raw"] = " | ".join(t.strip() for t in texts if t.strip())[:RAW_LIMIT]
        return merged
    except Exception as error:  # the checker must never see an exception from here
        result["error"] = f"ocr_error:{type(error).__name__}"
        return result
