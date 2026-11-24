"""Where the bot's log files live, and the only way the code deletes or renames them.

The folder is read when it is used, never stored when a module is imported. When the environment variable DUSK_LOG_DIR
is set, the logs go there: the tests set it to a temp folder for every test (tests/conftest.py). Otherwise the logs go to
logs/ next to uwu.py, as before.

Deleting and renaming a log file goes through remove_old, safe_unlink, safe_rename and rotate_to_part. Each of them:
  - touches only a plain file that is a direct child of the folder it is given (links and folders are refused);
  - touches only names that match the pattern it is given, or the exact .1 part of the file being rotated;
  - never deletes the newest `keep` files of a pattern, and never fewer than one is kept.
A refusal is written to the log as a warning, and nothing is deleted.
"""

import fnmatch
import glob
import logging
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV = "DUSK_LOG_DIR"
DEFAULT_KEEP = 20
_log = logging.getLogger(__name__)


def folder_for(name):
    """The folder for a configured name such as "logs": DUSK_LOG_DIR when it is set, else the name under the project"""
    override = os.environ.get(ENV, "").strip()
    if override:
        return Path(override).resolve()
    path = Path(name)
    return path if path.is_absolute() else PROJECT_ROOT / path


def log_dir():
    """The bot's log folder, read now (not at import time)"""
    return folder_for("logs")


def log_file(name, root=None):
    """A log file of a component that was given a root folder: <root>/logs/<name>, or inside DUSK_LOG_DIR when it is set"""
    override = os.environ.get(ENV, "").strip()
    if override:
        return Path(override).resolve() / name
    base = Path(root) if root is not None else PROJECT_ROOT
    return base / "logs" / name


def _direct_child(path, folder):
    """True when path sits directly inside folder: no deeper folder and no link on the way"""
    try:
        return Path(path).parent.resolve() == Path(folder).resolve()
    except OSError:
        return False


def _plain_file(path):
    """True for a regular file that is not a link"""
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def _refuse(action, path, folder, why):
    _log.warning("log cleanup refused: cannot %s %s in %s (%s)", action, path, folder, why)


def safe_unlink(path, folder, pattern):
    """Deletes one plain file that is a direct child of folder and whose name matches pattern. Returns True if deleted"""
    path = Path(path)
    if not _direct_child(path, folder):
        _refuse("delete", path, folder, "not directly inside the log folder")
        return False
    if not fnmatch.fnmatchcase(path.name, pattern):
        _refuse("delete", path, folder, "the name does not match " + pattern)
        return False
    if path.is_symlink() or path.is_dir():
        _refuse("delete", path, folder, "a link or a folder")
        return False
    if not _plain_file(path):
        return False  # already gone
    try:
        path.unlink()
        return True
    except OSError as error:
        _log.warning("log cleanup could not delete %s: %s", path, error)
        return False


def remove_old(folder, pattern, keep=None):
    """Deletes the oldest plain files named pattern in folder and keeps the newest `keep` of them (DEFAULT_KEEP when
    None, and never fewer than one). Names carry their time stamp, so the name decides which file is older.
    Returns the removed paths."""
    folder = Path(folder)
    try:
        matches = [path for path in folder.iterdir() if fnmatch.fnmatchcase(path.name, pattern) and _plain_file(path)]
    except OSError:
        return []
    keep = DEFAULT_KEEP if keep is None else max(1, int(keep))
    matches.sort(key=lambda path: path.name)
    removed = []
    for path in matches[: max(0, len(matches) - keep)]:
        if safe_unlink(path, folder, pattern):
            removed.append(path)
    return removed


def safe_rename(src, dst, folder, dst_pattern):
    """Renames one plain file of folder to dst, both direct children of folder. dst may already exist only as a plain
    file whose name matches dst_pattern; it is then replaced (an older rotated part). Returns True if renamed."""
    src, dst = Path(src), Path(dst)
    if not (_direct_child(src, folder) and _direct_child(dst, folder)):
        _refuse("rename", src, folder, "not directly inside the log folder")
        return False
    if not fnmatch.fnmatchcase(dst.name, dst_pattern):
        _refuse("rename", src, folder, "the new name does not match " + dst_pattern)
        return False
    if not _plain_file(src):
        return False
    if dst.exists() and not _plain_file(dst):
        _refuse("rename", src, folder, "the new name is taken by a folder or a link")
        return False
    try:
        os.replace(src, dst)
        return True
    except OSError as error:
        _log.warning("log rotation could not rename %s: %s", src, error)
        return False


def rotate_to_part(path, folder=None):
    """Moves path to path.1 for a rotation. An older path.1 is replaced, and nothing else is touched. Returns True if moved"""
    path = Path(path)
    folder = path.parent if folder is None else Path(folder)
    part = path.with_name(path.name + ".1")
    return safe_rename(path, part, folder, glob.escape(part.name))
