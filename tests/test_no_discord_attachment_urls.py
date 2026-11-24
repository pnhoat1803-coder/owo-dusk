"""Shipped text must not link to Discord attachments: those links carry channel ids and signed query strings.
The check reads the project's text files only (not the captcha folders, not this file)."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".git", "captcha_solver", "captcha_resolver"}
PATTERNS = ("media.discordapp.net/attachments", "cdn.discordapp.com/attachments")
TEXT_SUFFIXES = (".py", ".js", ".html", ".css", ".md", ".txt", ".toml", ".json", ".bat", ".sh", ".svg")


def _shipped_text_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name.endswith(TEXT_SUFFIXES) and name != Path(__file__).name:
                yield Path(dirpath) / name


def test_no_shipped_file_links_to_a_discord_attachment():
    found = []
    for path in _shipped_text_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if any(pattern in text for pattern in PATTERNS):
            found.append(str(path.relative_to(ROOT)))
    assert found == []
