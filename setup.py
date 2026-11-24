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

"""One-click, repeatable setup. Windows: double-click setup.bat. Termux/Linux: python setup.py

It only adds or checks, it never deletes anything and never overwrites an existing file:
  1. checks the Python that runs it          2. checks pip
  3. installs requirements.txt into THIS Python, then imports every module to prove it works
  4. portable Tesseract OCR in third_party/tesseract (asks first, ~50-80 MB download from GitHub)
  5. creates missing runtime folders         6. checks that config files parse (names only, no contents)
  7. self-test: OCR libraries, Tesseract and a tiny OCR smoke test
`python setup.py --check` only checks the required imports (no prompts, no pip, no download) and exits 0 or 1.
It does not touch the account files and does not run git. The only network use is the
optional Tesseract download in step 4. Standard library only, so it runs before any package is installed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import site
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.request
from pathlib import Path

# Run from the project folder wherever this file is started from. This must come before any project import
PROJECT_DIR = Path(__file__).resolve().parent
os.chdir(PROJECT_DIR)
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

try:  # Windows consoles with a legacy code page must not crash on a non-ASCII folder name
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))
MIN_PYTHON = (3, 12)  # same as the ruff target-version in pyproject.toml
TOTAL_STEPS = 7

TESSERACT_API = "https://api.github.com/repos/UB-Mannheim/tesseract/releases/latest"
# Used only when the GitHub API lookup fails. Pinned release of the official UB-Mannheim Windows 64-bit installer.
TESSERACT_FALLBACK_URL = (
    "https://github.com/UB-Mannheim/tesseract/releases/download/v5.5.0.20241111/"
    "tesseract-ocr-w64-setup-5.5.0.20241111.exe"
)
TESSERACT_ASSET_RE = re.compile(r"^tesseract-ocr-w64-setup-[\d.]+\.exe$", re.I)
DOWNLOAD_AGENT = "owo-dusk-setup"

# pip name (lower case) -> (import name, kind). kind: "required" | "desktop" (not on Termux) | "optional"
PACKAGE_MODULES = {
    "discord.py-self": ("discord", "required"),
    "flask": ("flask", "required"),
    "aiosqlite": ("aiosqlite", "required"),
    "rich": ("rich", "required"),
    "requests": ("requests", "required"),
    "aiohttp": ("aiohttp", "required"),
    "pytz": ("pytz", "required"),
    "numpy": ("numpy", "required"),
    "pillow": ("PIL", "required"),
    "onnxruntime": ("onnxruntime", "required"),
    "packaging": ("packaging", "required"),
    "playsound3": ("playsound3", "desktop"),
    "plyer": ("plyer", "desktop"),
    "psutil": ("psutil", "optional"),
}
# Termux: pip cannot build these, the Termux package manager provides some of them (see step 3)
TERMUX_SKIP_PIP = {"numpy", "pillow", "onnxruntime", "psutil", "playsound3", "plyer"}
TERMUX_UNUSED = {"playsound3", "plyer", "psutil"}  # desktop-only helpers, not used on Termux
TERMUX_PKG = (("python-numpy", "numpy"), ("python-pillow", "PIL"), ("python-onnxruntime", "onnxruntime"))

REQUIRED_CONFIG = (
    "settings.json",
    "global_settings.json",
    "misc.json",
    "webhookContent.json",
    "captcha.toml",
    "danger.toml",
)
TEMPLATE_SUFFIXES = (".example", ".default", ".template", ".sample")
RUNTIME_FOLDERS = ("logs", os.path.join("utils", "data"))

SMOKE_EXPECTED = (18, 11351, 22689)

SELF_TEST_CODE = r'''
import io, json, os, sys
sys.path.insert(0, ".")
import core.level_ocr as m
ok, why = m.ocr_available()
print("ocr_available:", ok, why)
configured = ""
try:
    with open(os.path.join("config", "global_settings.json"), encoding="utf-8") as handle:
        configured = json.load(handle).get("levelCheck", {}).get("tesseractPath", "") or ""
except Exception:
    pass
path = m.find_tesseract(configured)
print("find_tesseract:", path)
if ok and path:
    kind, version = m.tesseract_info(path)
    print("tesseract:", kind, version)
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new("RGB", (640, 240), (38, 20, 70))
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.load_default(size=48)
    except Exception:
        font = ImageFont.load_default()
    draw.text((30, 30), "LVL 18", fill=(255, 255, 255), font=font)
    draw.text((30, 120), "XP: 11,351/22,689", fill=(255, 255, 255), font=font)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    result = m.read_level_card(buffer.getvalue())
    print("smoke:", result["ok"], result["level"], result["xp_cur"], result["xp_need"], result["error"])
sys.stdout.flush()
os._exit(0)
'''


# ------------------------------------------------------------------------------------------------ small helpers
def say(text=""):
    print(text, flush=True)


def is_interactive():
    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except Exception:
        return False


def is_termux():
    prefix, home = os.environ.get("PREFIX", ""), os.environ.get("HOME", "")
    return "com.termux" in prefix or "com.termux" in home or os.path.isdir("/data/data/com.termux")


def ask(question, default):
    """True/False. Not interactive (or EOF): the answer is False and nothing is asked"""
    if not is_interactive():
        return False
    hint = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"      {question} {hint} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    if not answer:
        return default
    return answer in ("y", "yes")


def wait_for_key():
    """'Press any key' on a real console, nothing when the input is not a terminal"""
    if not is_interactive():
        say("Setup finished.")
        return
    try:
        if os.name == "nt":
            import msvcrt

            say("Setup finished. Press any key to exit...")
            msvcrt.getch()
        else:
            input("Setup finished. Press Enter to exit...")
    except (EOFError, KeyboardInterrupt, OSError):
        pass


def run_cmd(cmd, **kwargs):
    """The only place setup starts programs (pip, tesseract, installers, package managers). Tests replace it"""
    return subprocess.run(cmd, **kwargs)


def portable_dir():
    return os.path.join(ROOT, "third_party", "tesseract")


def portable_exe():
    return os.path.join(portable_dir(), "tesseract.exe" if os.name == "nt" else "tesseract")


def tesseract_version(path):
    """'5.5.0' from `tesseract --version`, '' when it prints no version, None when it cannot be started"""
    try:
        run = run_cmd([path, "--version"], capture_output=True, timeout=15, stdin=subprocess.DEVNULL)
    except Exception:
        return None
    text = (run.stdout or b"").decode("utf-8", "replace") + " " + (run.stderr or b"").decode("utf-8", "replace")
    match = re.search(r"tesseract\s+v?(\d+(?:\.\d+)+)", text, re.I)
    if run.returncode != 0 and not match:
        return None
    return match.group(1) if match else ""


# ------------------------------------------------------------------------------------------------ step 1: Python
def step_python():
    version = sys.version_info
    text = f"Python {version.major}.{version.minor}.{version.micro} ({sys.executable})"
    say(f"      {text}")
    if tuple(version[:2]) < MIN_PYTHON:
        return "FAIL", f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer is needed. Install it from https://www.python.org/downloads/"
    return "OK", text


# ------------------------------------------------------------------------------------------------ step 2: pip
def pip_ok():
    try:
        return run_cmd([sys.executable, "-m", "pip", "--version"], capture_output=True, timeout=60).returncode == 0
    except Exception:
        return False


def step_pip():
    if pip_ok():
        return "OK", "pip is available"
    say("      pip is missing, running ensurepip...")
    try:
        run_cmd([sys.executable, "-m", "ensurepip", "--upgrade"], timeout=300)
    except Exception as error:
        return "FAIL", f"ensurepip failed ({type(error).__name__}). Install pip for this Python by hand"
    if pip_ok():
        return "OK", "pip installed with ensurepip"
    return "FAIL", "pip is still not available (on Linux: install the python3-pip package)"


# ------------------------------------------------------------------------------------------------ step 3: packages
def read_requirements():
    """[(package name, original line)] from requirements.txt, comments and blank lines skipped"""
    path = os.path.join(ROOT, "requirements.txt")
    result = []
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            match = re.match(r"[A-Za-z0-9][A-Za-z0-9_.\-]*", line)
            if match:
                result.append((match.group(0).lower().replace("_", "-"), line))
    return result


def site_needs_user_flag():
    """True when pip would fail to write into this Python's site-packages (and it is not a virtual environment)"""
    if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        return False
    try:
        folders = site.getsitepackages()
    except Exception:
        return False
    return not any(os.path.isdir(folder) and os.access(folder, os.W_OK) for folder in folders)


def import_check(module, dist):
    """(ok, version) by importing in a fresh process, so a broken package cannot hurt setup itself"""
    code = (
        "import importlib, importlib.metadata as md, sys\n"
        "importlib.import_module(sys.argv[1])\n"
        "try:\n    print(md.version(sys.argv[2]))\n"
        "except Exception:\n    print('?')\n"
    )
    try:
        run = run_cmd(
            [sys.executable, "-B", "-c", code, module, dist], capture_output=True, timeout=120, cwd=ROOT
        )
    except Exception:
        return False, "-"
    if run.returncode != 0:
        return False, "-"
    lines = (run.stdout or b"").decode("utf-8", "replace").strip().splitlines()
    return True, (lines[-1] if lines else "?")


def termux_requirements_file(requirements):
    """A temporary requirements file without the packages pip cannot build on Termux"""
    handle = tempfile.NamedTemporaryFile("w", suffix="-requirements.txt", delete=False, encoding="utf-8")
    with handle:
        for name, line in requirements:
            if name not in TERMUX_SKIP_PIP:
                handle.write(line + "\n")
    return handle.name


def step_packages():
    requirements = read_requirements()
    termux = is_termux()
    notes = []

    if any("git+" in line for _name, line in requirements) and shutil.which("git") is None:
        say("      NOTE: discord.py-self is installed from GitHub, so pip needs Git on this PC.")
        say("            Windows: https://git-scm.com/download/win   Termux: pkg install git   Linux: apt install git")
        notes.append("git not found")

    temp_file = None
    target = os.path.join(ROOT, "requirements.txt")
    if termux:
        temp_file = termux_requirements_file(requirements)
        target = temp_file
        say("      Termux: numpy / pillow / onnxruntime come from the Termux package manager, not pip.")
    command = [sys.executable, "-m", "pip", "install", "-r", "requirements.txt" if not termux else target]
    if site_needs_user_flag():
        command.append("--user")
        say("      site-packages is not writable, using --user")
    say("      " + " ".join(command))
    pip_failed = False
    try:
        pip_failed = run_cmd(command, cwd=ROOT).returncode != 0  # output is shown live
    except Exception as error:
        pip_failed = True
        say(f"      pip could not be started ({type(error).__name__})")
    finally:
        if temp_file:
            try:
                os.remove(temp_file)
            except OSError:
                pass

    if termux:
        missing = [(pkg, mod) for pkg, mod in TERMUX_PKG if not import_check(mod, mod)[0]]
        if missing and ask("Install " + ", ".join(p for p, _m in missing) + " with pkg now?", True):
            for pkg, _mod in missing:
                try:
                    run_cmd(["pkg", "install", "-y", pkg])
                except Exception as error:
                    say(f"      pkg install {pkg} failed ({type(error).__name__})")

    say("      module              version                result")
    failed_required, failed_optional = [], []
    for name, _line in requirements:
        module, kind = PACKAGE_MODULES.get(name, (name.replace("-", "_"), "required"))
        if termux and name in TERMUX_UNUSED:
            say(f"      {module:<19} {'-':<22} SKIP (not used on Termux)")
            continue
        ok, version = import_check(module, name)
        say(f"      {module:<19} {version:<22} {'OK' if ok else 'FAIL'}")
        if not ok:
            (failed_optional if kind == "optional" else failed_required).append(name)
    if failed_required:
        hint = " Git is missing, install it first." if "git not found" in notes else ""
        return "FAIL", "cannot import: " + ", ".join(failed_required) + ("; pip reported an error." if pip_failed else ".") + hint
    if failed_optional:
        return "WARN", "optional package missing: " + ", ".join(failed_optional)
    if pip_failed:
        return "WARN", "pip reported an error, but every module imports"
    return "OK", "all modules import"


# ------------------------------------------------------------------------------------------------ step 4: Tesseract
def http_get_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": DOWNLOAD_AGENT, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def download_file(url, destination, expected_sha256=None):
    """Streams `url` to `destination` with a progress line. Raises on any problem, including a wrong checksum"""
    request = urllib.request.Request(url, headers={"User-Agent": DOWNLOAD_AGENT})
    digest = hashlib.sha256()
    with urllib.request.urlopen(request, timeout=60) as response, open(destination, "wb") as handle:
        total = int(response.headers.get("Content-Length") or 0)
        done, shown = 0, -1
        while True:
            chunk = response.read(256 * 1024)
            if not chunk:
                break
            handle.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            if total:
                percent = done * 100 // total
                if percent // 10 != shown // 10:
                    shown = percent
                    say(f"      downloading... {percent}% ({done // 1048576} / {total // 1048576} MB)")
    if expected_sha256 and digest.hexdigest().lower() != expected_sha256.lower():
        raise RuntimeError("checksum of the downloaded file does not match")


def find_installer():
    """(url, file name, sha256 or None): the latest official installer from the GitHub API, else the pinned URL"""
    try:
        release = http_get_json(TESSERACT_API)
        for asset in release.get("assets", []):
            if TESSERACT_ASSET_RE.match(asset.get("name", "")):
                digest = str(asset.get("digest") or "")
                sha = digest.split(":", 1)[1] if digest.startswith("sha256:") else None
                return asset["browser_download_url"], asset["name"], sha
        say("      GitHub release has no matching installer, using the pinned download URL")
    except Exception as error:
        say(f"      GitHub API lookup failed ({type(error).__name__}), using the pinned download URL")
    return TESSERACT_FALLBACK_URL, TESSERACT_FALLBACK_URL.rsplit("/", 1)[1], None


def silent_install(installer, target):
    """Runs the NSIS installer silently. /D= must be the last argument and must NOT be quoted, so the command line is
    built by hand (a list would be quoted by Python because of spaces in the path)"""
    return run_cmd(f'"{installer}" /S /D={target}', timeout=600).returncode


def manual_steps():
    say("      Manual way: download the installer from https://github.com/UB-Mannheim/tesseract/releases")
    say("        1. run tesseract-ocr-w64-setup-*.exe (choose 'Install for current user' if it asks)")
    say(f"        2. set the destination folder to: {portable_dir()}")
    say("        3. keep 'English' (eng) language data ticked, finish the installer")
    say("        4. run setup.bat again to check it")


def portable_status():
    """('OK'|'WARN', text) when the portable program works, else None"""
    exe = portable_exe()
    if not os.path.isfile(exe):
        return None
    version = tesseract_version(exe)
    if version is None:
        return None
    eng = os.path.join(portable_dir(), "tessdata", "eng.traineddata")
    label = f"portable Tesseract {version}".strip()
    if not os.path.isfile(eng):
        return "WARN", f"{label} works, but tessdata\\eng.traineddata is missing (reinstall with English data)"
    return "OK", f"{label} in third_party\\tesseract"


def step_tesseract_windows():
    found = portable_status()
    if found:
        return found
    if os.path.isfile(portable_exe()):
        say("      third_party\\tesseract\\tesseract.exe exists but does not start")
    system = shutil.which("tesseract")
    if system:
        say(f"      A Tesseract is already on PATH ({system}); the bot would use it as a fallback.")
    if not is_interactive():
        if system:
            return "OK", "system Tesseract on PATH (portable copy skipped: no terminal to ask)"
        return "WARN", "portable Tesseract not installed (no terminal to ask). Run setup.bat by hand to install it"
    if not ask("Download portable Tesseract (~50-80 MB) into this project?", True):
        return ("OK", "system Tesseract on PATH (portable copy not requested)") if system else (
            "WARN", "portable Tesseract not installed; the level check stays off")
    try:
        url, name, sha = find_installer()
        say(f"      installer: {name}")
        folder = os.path.join(tempfile.gettempdir(), "owo_dusk_setup")
        os.makedirs(folder, exist_ok=True)
        installer = os.path.join(folder, name)
        download_file(url, installer, sha)
    except Exception as error:
        say(f"      download failed ({type(error).__name__}: {error})")
        manual_steps()
        return "WARN", "download failed, install Tesseract by hand (steps above)"
    say("      installing silently into third_party\\tesseract (can take a minute)...")
    try:
        os.makedirs(os.path.dirname(portable_dir()), exist_ok=True)
        code = silent_install(installer, portable_dir())
    except Exception as error:  # e.g. WinError 740: the installer wants administrator rights
        say(f"      the installer could not run ({type(error).__name__}: {error})")
        manual_steps()
        return "WARN", "silent install failed (needs admin?), install by hand (steps above)"
    # some installers hand over to a child process: wait a little for the program to appear
    for _ in range(60):
        if os.path.isfile(portable_exe()):
            break
        time.sleep(1)
    found = portable_status()
    if found:
        try:
            os.remove(installer)
        except OSError:
            pass
        return found
    say(f"      installer exit code {code}, and tesseract.exe does not work yet")
    manual_steps()
    return "WARN", "silent install did not produce a working tesseract.exe, install by hand (steps above)"


def step_tesseract_other():
    program = shutil.which("tesseract") or (portable_exe() if os.path.isfile(portable_exe()) else None)
    if program:
        version = tesseract_version(program)
        if version is not None:
            return "OK", f"Tesseract {version} at {program}".replace("Tesseract  ", "Tesseract ")
    if sys.platform == "darwin":
        say("      Install it with: brew install tesseract")
        return "WARN", "Tesseract not found; the level check stays off"
    if is_termux():
        command, shown = ["pkg", "install", "-y", "tesseract"], "pkg install tesseract"
    elif shutil.which("apt-get") or shutil.which("apt"):
        prefix = [] if (hasattr(os, "geteuid") and os.geteuid() == 0) else ["sudo"]
        command = prefix + ["apt-get", "install", "-y", "tesseract-ocr"]
        shown = "sudo apt install tesseract-ocr"
    else:
        say("      Install the 'tesseract' package with your package manager.")
        return "WARN", "Tesseract not found; the level check stays off"
    say(f"      Tesseract is not installed. Command: {shown}")
    if ask(f"Run '{shown}' now?", False):
        try:
            run_cmd(command)
        except Exception as error:
            say(f"      failed ({type(error).__name__})")
        program = shutil.which("tesseract")
        if program and tesseract_version(program) is not None:
            return "OK", f"Tesseract installed at {program}"
        return "WARN", "install did not finish; run it by hand and re-run setup"
    return "WARN", f"Tesseract not installed. Run: {shown}"


def step_tesseract():
    return step_tesseract_windows() if os.name == "nt" else step_tesseract_other()


# ------------------------------------------------------------------------------------------------ step 5: folders
def step_folders():
    created = []
    for relative in RUNTIME_FOLDERS:
        folder = os.path.join(ROOT, relative)
        if not os.path.isdir(folder):
            os.makedirs(folder, exist_ok=True)
            created.append(relative)
    if created:
        return "OK", "created: " + ", ".join(created)
    return "OK", "all runtime folders exist"


# ------------------------------------------------------------------------------------------------ step 6: config
def parse_config(path):
    """None when the file parses, else a short reason (position only, never file contents)"""
    try:
        if path.endswith(".toml"):
            try:
                import tomllib
            except ImportError:
                return None
            with open(path, "rb") as handle:
                tomllib.load(handle)
        else:
            with open(path, encoding="utf-8") as handle:
                json.load(handle)
    except json.JSONDecodeError as error:
        return f"invalid JSON at line {error.lineno} column {error.colno}"
    except UnicodeDecodeError:
        return "not valid UTF-8"
    except Exception as error:
        return f"cannot parse ({type(error).__name__})"
    return None


def step_config():
    folder = os.path.join(ROOT, "config")
    if not os.path.isdir(folder):
        return "FAIL", "config folder is missing"
    copied, missing = [], []
    for name in REQUIRED_CONFIG:
        target = os.path.join(folder, name)
        if os.path.exists(target):
            continue
        template = next((target + s for s in TEMPLATE_SUFFIXES if os.path.isfile(target + s)), None)
        if template:
            shutil.copyfile(template, target)  # the target does not exist, so nothing is overwritten
            copied.append(name)
        else:
            missing.append(name)
    bad = []
    names = sorted(n for n in os.listdir(folder) if n.endswith((".json", ".toml")))
    for name in names:
        problem = parse_config(os.path.join(folder, name))
        say(f"      {name:<28} {'OK' if problem is None else 'FAIL (' + problem + ')'}")
        if problem is not None:
            bad.append(name)
    for name in copied:
        say(f"      {name}: was missing, copied from its template")
    for name in missing:
        say(f"      {name}: MISSING and no template found")
    if bad:
        return "FAIL", "does not parse: " + ", ".join(bad)
    if missing:
        return "WARN", "missing config: " + ", ".join(missing)
    return "OK", f"{len(names)} config file(s) parse" + (f", copied {len(copied)} from templates" if copied else "")


# ------------------------------------------------------------------------------------------------ step 7: self-test
def step_selftest():
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        run = run_cmd(
            [sys.executable, "-B", "-c", SELF_TEST_CODE], capture_output=True, timeout=180, cwd=ROOT, env=env
        )
    except Exception as error:
        return "WARN", f"self-test could not run ({type(error).__name__})"
    out = (run.stdout or b"").decode("utf-8", "replace")
    values = {}
    for line in out.splitlines():
        if ":" in line:
            key, _sep, rest = line.partition(":")
            values[key.strip()] = rest.strip()
    if run.returncode != 0 or "ocr_available" not in values:
        return "WARN", "self-test failed to import core.level_ocr (see step 3)"
    for line in out.splitlines():
        say("      " + line)
    if not values["ocr_available"].startswith("True"):
        return "WARN", "OCR libraries unavailable: " + values["ocr_available"].partition(" ")[2]
    if values.get("find_tesseract", "None") == "None":
        return "WARN", "Tesseract program not found; the level check stays off"
    smoke = values.get("smoke", "").split()
    if len(smoke) >= 4 and smoke[0] == "True":
        got = tuple(int(x) if x.isdigit() else None for x in smoke[1:4])
        if got == SMOKE_EXPECTED:
            return "OK", f"OCR works: level {got[0]}, XP {got[1]}/{got[2]}"
        return "WARN", f"OCR ran but read {got} instead of {SMOKE_EXPECTED}"
    return "WARN", "OCR smoke test did not read the numbers: " + (" ".join(smoke[4:]) or "no result")


# ------------------------------------------------------------------------------------------------ --check
CHECK_CODE = (
    "import importlib, sys\n"
    "bad = []\n"
    "for name in sys.argv[1:]:\n"
    "    try:\n        importlib.import_module(name)\n"
    "    except BaseException:\n        bad.append(name)\n"
    "print(' '.join(bad))\n"
    "sys.stdout.flush()\n"
    "import os\nos._exit(1 if bad else 0)\n"
)


def check_only():
    """`setup.py --check`, used by the launchers on every start: no prompts, no pip, no download, no key press.
    0 when this Python is new enough and every required module imports, else 1 (the launcher then runs the full setup)"""
    os.chdir(ROOT)
    if tuple(sys.version_info[:2]) < MIN_PYTHON:
        say(f"[setup check] Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer is needed")
        return 1
    modules = [module for module, kind in PACKAGE_MODULES.values() if kind == "required"]
    try:
        run = run_cmd(
            [sys.executable, "-B", "-c", CHECK_CODE, *modules], capture_output=True, timeout=120, cwd=ROOT,
            stdin=subprocess.DEVNULL,
        )
    except Exception as error:
        say(f"[setup check] could not run ({type(error).__name__}), full setup needed")
        return 1
    if run.returncode == 0:
        say("[setup check] OK: all required packages import")
        return 0
    missing = (run.stdout or b"").decode("utf-8", "replace").strip().splitlines()
    say("[setup check] missing or broken: " + (missing[-1] if missing and missing[-1] else "unknown") + ", full setup needed")
    return 1


# ------------------------------------------------------------------------------------------------ runner
STEPS = (
    ("Python", step_python),
    ("pip", step_pip),
    ("Python packages (requirements.txt)", step_packages),
    ("Portable Tesseract OCR", step_tesseract),
    ("Project folders", step_folders),
    ("Config files (read only)", step_config),
    ("Self-test", step_selftest),
)


def main():
    os.chdir(ROOT)
    say("OwO-Dusk setup. Safe to run again. It never touches account files or existing config.")
    say(f"Project: {ROOT}")
    say()
    results = []
    python_failed = False
    for number, (title, function) in enumerate(STEPS, start=1):
        say(f"[{number}/{TOTAL_STEPS}] {title}")
        if python_failed and number in (2, 3, 7):
            status, note = "SKIP", "needs a newer Python (step 1)"
        else:
            try:
                status, note = function()
            except KeyboardInterrupt:
                status, note = "FAIL", "interrupted"
            except Exception as error:
                say(traceback.format_exc())
                status, note = "FAIL", f"unexpected error: {type(error).__name__}: {error}"
        if number == 1 and status == "FAIL":
            python_failed = True
        results.append((title, status, note))
        say(f"      -> {status}: {note}")
        say()

    say("Summary")
    say("-------")
    for number, (title, status, note) in enumerate(results, start=1):
        say(f"[{number}/{TOTAL_STEPS}] {status:<5} {title}")
    say()
    failed = any(status == "FAIL" for _t, status, _n in results)
    if failed:
        say("Some steps FAILED, see the lines above.")
    say("Tokens are not managed by setup. Add accounts by editing tokens.txt yourself.")
    return 1 if failed else 0


def run():
    code = 1
    try:
        code = main()
    except BaseException:
        say(traceback.format_exc())
        say("Setup stopped because of an unexpected error (see above).")
    wait_for_key()
    return code


if __name__ == "__main__":
    sys.exit(check_only() if "--check" in sys.argv[1:] else run())
