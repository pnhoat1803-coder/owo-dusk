"""The local level-grind lines of config/level_lines.txt (phase 4, item L). Read once, shared by all accounts.

Moved here unchanged from core/cogs/looper.py (_local_level_lines), so utils/quote_source.py can use the same file
without importing the cog.
"""

import threading

LEVEL_LINES_PATH = "config/level_lines.txt"

_lock = threading.Lock()
_lines = None


def local_level_lines():
    """The non-empty lines of the file, read once. Empty when the file cannot be read"""
    global _lines
    with _lock:
        if _lines is None:
            try:
                with open(LEVEL_LINES_PATH, "r", encoding="utf-8") as file:
                    _lines = [line.strip() for line in file if line.strip()]
            except OSError:
                _lines = []
        return _lines
