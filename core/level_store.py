"""Storage of the level check: utils/data/levels.sqlite, a NEW file (utils/data/db.sqlite is never touched).

WAL mode, busy_timeout 3000 ms. Every write takes one module-level lock and uses a short-lived connection, so the
accounts (one thread and event loop each) never write at the same moment. The functions are synchronous: the callers
run them with loop.run_in_executor, never on an event loop directly. The dashboard process reads the file read-only.

account_level keeps the last GOOD numbers; a failed try only updates last_try_at, last_ok and last_error.
"""

import os
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

SCHEMA_VERSION = "1"
HISTORY_DAYS = 90
DEFAULT_PATH = Path("utils") / "data" / "levels.sqlite"

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
    """CREATE TABLE IF NOT EXISTS account_level (
         account_id TEXT PRIMARY KEY,
         level INTEGER, xp_cur INTEGER, xp_need INTEGER, rank INTEGER,
         checked_at REAL,
         last_try_at REAL, last_ok INTEGER, last_error TEXT
       )""",
    """CREATE TABLE IF NOT EXISTS level_history (
         account_id TEXT, ts REAL, level INTEGER, xp_cur INTEGER, xp_need INTEGER, rank INTEGER
       )""",
    "CREATE INDEX IF NOT EXISTS ix_level_history_acc_ts ON level_history(account_id, ts)",
)

_write_lock = threading.Lock()  # one writer at a time, process wide


class LevelStore:
    def __init__(self, path=DEFAULT_PATH):
        self.path = Path(path)
        self._ready = False
        self._purged = False

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=3.0)
        conn.execute("PRAGMA busy_timeout=3000")
        return conn

    def init(self):
        """Creates the file and tables on first use, sets WAL, and drops history older than 90 days once per process"""
        with _write_lock:
            with closing(self._connect()) as conn:
                if not self._ready:
                    conn.execute("PRAGMA journal_mode=WAL")
                    for statement in SCHEMA:
                        conn.execute(statement)
                    conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
                    self._ready = True
                if not self._purged:
                    conn.execute("DELETE FROM level_history WHERE ts < ?", (time.time() - HISTORY_DAYS * 86400,))
                    self._purged = True
                conn.commit()

    def _ensure(self):
        if not self._ready:
            self.init()

    # ---- reads (WAL: they do not wait for a writer) ----------------------
    def get(self, account_id):
        """The row of one account as a dict, or None"""
        self._ensure()
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT level, xp_cur, xp_need, rank, checked_at, last_try_at, last_ok, last_error "
                "FROM account_level WHERE account_id = ?",
                (str(account_id),),
            ).fetchone()
        if row is None:
            return None
        keys = ("level", "xp_cur", "xp_need", "rank", "checked_at", "last_try_at", "last_ok", "last_error")
        return dict(zip(keys, row))

    def get_meta(self, key):
        self._ensure()
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    # ---- writes ----------------------------------------------------------
    def set_meta(self, key, value):
        self._ensure()
        with _write_lock:
            with closing(self._connect()) as conn:
                conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))
                conn.commit()

    def record_good(self, account_id, level, xp_cur, xp_need, rank, now=None):
        """A good read: updates level, XP and time, and appends one history row. rank is None when the card had no clean
        rank: then the last clean rank stays in account_level (it is never replaced by NULL)"""
        self._ensure()
        now = time.time() if now is None else now
        with _write_lock:
            with closing(self._connect()) as conn:
                conn.execute(
                    """INSERT INTO account_level
                         (account_id, level, xp_cur, xp_need, rank, checked_at, last_try_at, last_ok, last_error)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 1, NULL)
                       ON CONFLICT(account_id) DO UPDATE SET
                         level = excluded.level, xp_cur = excluded.xp_cur, xp_need = excluded.xp_need,
                         rank = COALESCE(excluded.rank, account_level.rank), checked_at = excluded.checked_at,
                         last_try_at = excluded.last_try_at, last_ok = 1, last_error = NULL""",
                    (str(account_id), level, xp_cur, xp_need, rank, now, now),
                )
                conn.execute(
                    "INSERT INTO level_history (account_id, ts, level, xp_cur, xp_need, rank) VALUES (?, ?, ?, ?, ?, ?)",
                    (str(account_id), now, level, xp_cur, xp_need, rank),
                )
                conn.commit()

    def record_fail(self, account_id, error, now=None):
        """A failed try: the last good numbers stay, only last_try_at, last_ok=0 and last_error change"""
        self._ensure()
        now = time.time() if now is None else now
        with _write_lock:
            with closing(self._connect()) as conn:
                conn.execute(
                    """INSERT INTO account_level (account_id, last_try_at, last_ok, last_error)
                       VALUES (?, ?, 0, ?)
                       ON CONFLICT(account_id) DO UPDATE SET
                         last_try_at = excluded.last_try_at, last_ok = 0, last_error = excluded.last_error""",
                    (str(account_id), now, str(error)[:60]),
                )
                conn.commit()


def failed_card_folder():
    return Path("utils") / "data" / "level_cards"


def save_failed_card(account_id, data, now=None, keep=20, folder=None):
    """Saves a card image that could not be read and keeps only the newest `keep` files. Synchronous, run it in an executor"""
    folder = Path(folder) if folder is not None else failed_card_folder()
    folder.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time() if now is None else now)
    safe_id = "".join(ch for ch in str(account_id) if ch.isalnum())[:30] or "x"
    (folder / f"{safe_id}_{stamp}.png").write_bytes(data)
    files = sorted(folder.glob("*.png"), key=lambda item: (item.stat().st_mtime, item.name))
    for old in files[:-keep] if keep > 0 else files:
        try:
            os.remove(old)
        except OSError:
            pass
