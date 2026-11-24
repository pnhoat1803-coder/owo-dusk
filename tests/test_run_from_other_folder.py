"""Every entry point changes to the project folder before it imports any project module."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRY_POINTS = ("uwu.py", "setup.py", "website/__init__.py", "website/server.py")
PROJECT_IMPORTS = (
    "from core", "from utils", "from website", "from .",
    "import core", "import utils", "import website",
)
CHDIR = "os.chdir(PROJECT_DIR)"


def _first_line(lines, starts):
    for number, line in enumerate(lines):
        if line.startswith(starts):
            return number
    return None


def test_every_entry_point_changes_folder_before_its_project_imports():
    for rel in ENTRY_POINTS:
        lines = (ROOT / rel).read_text(encoding="utf-8").splitlines()
        chdir_at = _first_line(lines, (CHDIR,))
        assert chdir_at is not None, f"{rel}: no module-level line {CHDIR!r}"
        import_at = _first_line(lines, PROJECT_IMPORTS)
        assert import_at is None or chdir_at < import_at, (
            f"{rel}: a project import (line {import_at + 1}) comes before the folder change (line {chdir_at + 1})"
        )


def test_importing_the_dashboard_from_another_folder_uses_the_project_folder(tmp_path):
    # The project must be importable (PYTHONPATH), but the process starts in another folder
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    code = "import os, website; print(os.getcwd())"
    result = subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert result.returncode == 0, result.stderr[-500:]
    printed = result.stdout.strip().splitlines()[-1]
    assert Path(printed).resolve() == ROOT.resolve()
