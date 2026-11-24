"""Test setup for the whole suite.

1. Every test runs with the bot's log folder set to a temp folder (DUSK_LOG_DIR, see utils/log_paths.py). Nothing a test
   does can write to, rotate or delete the real logs/ folder next to uwu.py. A session-wide temp folder is set before any
   test module is imported, so module-level code is covered as well.
2. The real logs/ folder is listed when the session starts and again when it ends: names, sizes and times, never the
   contents. If anything in it was removed, added or changed while the tests ran, the run is marked as failed.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ENV = "DUSK_LOG_DIR"
REAL_LOGS = ROOT / "logs"
_SESSION_LOGS = Path(tempfile.mkdtemp(prefix="dusk_test_logs_"))
os.environ[ENV] = str(_SESSION_LOGS)
_BEFORE = None


def _listing(folder):
    """Names, sizes and modification times of everything under folder. No file is opened"""
    rows = {}
    if not folder.is_dir():
        return rows
    for dirpath, dirnames, filenames in os.walk(folder):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            rows[os.path.relpath(full, folder)] = (st.st_size, st.st_mtime_ns, os.path.isdir(full))
    return rows


def pytest_sessionstart(session):
    global _BEFORE
    _BEFORE = _listing(REAL_LOGS)


@pytest.fixture(autouse=True)
def _bot_logs_in_a_temp_folder(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, str(tmp_path / "dusk_logs"))
    yield


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_SESSION_LOGS, ignore_errors=True)
    if _BEFORE is None:
        return
    after = _listing(REAL_LOGS)
    if after == _BEFORE:
        return
    gone = sorted(set(_BEFORE) - set(after))
    new = sorted(set(after) - set(_BEFORE))
    changed = sorted(path for path in set(_BEFORE) & set(after) if _BEFORE[path] != after[path])
    lines = [
        "",
        "!" * 78,
        "!!! The real logs/ folder CHANGED while the tests ran. The run is marked as failed.",
        "!!! removed: %d   added: %d   changed: %d   (names only)" % (len(gone), len(new), len(changed)),
    ]
    for label, names in (("removed", gone), ("added", new), ("changed", changed)):
        for name in names[:20]:
            lines.append("!!!   %s: %s" % (label, name))
    lines.append("!" * 78)
    sys.stderr.write("\n".join(lines) + "\n")
    session.exitstatus = 1
