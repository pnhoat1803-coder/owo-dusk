"""Storage of the autoquest state (prompt 29, D5): utils/data/quests.sqlite, a NEW file (utils/data/db.sqlite is never touched).

One row per account, keyed by the Discord user id as text. The row is written after each parsed Quest Log, each claim and
each all-done, and read once when the quest cog starts, so a restart does not send "owo quest" for every account again.

WAL mode, busy_timeout 3000 ms, one module-level write lock, short-lived connections (same pattern as core/level_store.py).
The functions are synchronous: the quest cog runs them with asyncio.to_thread. A read never creates the file. Errors are
raised as sqlite3.Error / OSError; the caller decides what to log (the quest cog logs once and goes on).
"""

import sqlite3
import threading
from contextlib import closing
from pathlib import Path

# Anchored to the project folder (like the quest debug file), not to the working directory: the bot and the dashboard
# must meet at the same file wherever they were started from
DEFAULT_PATH = Path(__file__).resolve().parents[2] / "utils" / "data" / "quests.sqlite"

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS quest_state (
         user_id TEXT PRIMARY KEY,
         next_quest_at INTEGER, all_done INTEGER, seals INTEGER, quests_json TEXT,
         last_log_at INTEGER, last_ocr_ok_at INTEGER, last_claim_at INTEGER, claims_total INTEGER,
         ocr_next_at INTEGER
       )""",
)

# Every column except user_id. ocr_next_at is the next-quest time of the last good OCR: the one column added to the
# first design, because the OCR reuse rule must compare it with the next-quest time of the current quest log
COLUMNS = (
    "next_quest_at",
    "all_done",
    "seals",
    "quests_json",
    "last_log_at",
    "last_ocr_ok_at",
    "last_claim_at",
    "claims_total",
    "ocr_next_at",
)

_write_lock = threading.Lock()  # one writer at a time, process wide


class QuestStore:
    def __init__(self, path=None):
        # The default is read when the store is made, so a test can point DEFAULT_PATH somewhere else
        self.path = Path(path) if path is not None else Path(DEFAULT_PATH)
        self._ready = False

    def _open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=3.0)
        conn.execute("PRAGMA busy_timeout=3000")
        return conn

    def _prepare(self, conn):
        """Creates the table once per store and sets WAL. Called with the write lock held or by a read"""
        if not self._ready:
            conn.execute("PRAGMA journal_mode=WAL")
            for statement in SCHEMA:
                conn.execute(statement)
            conn.commit()
            self._ready = True

    def load(self, user_id):
        """The row of one account as a dict (COLUMNS as keys), or None. A missing file is None: nothing is created"""
        if not self.path.exists():
            return None
        with closing(self._open()) as conn:
            self._prepare(conn)
            row = conn.execute(
                f"SELECT {', '.join(COLUMNS)} FROM quest_state WHERE user_id = ?",
                (str(user_id),),
            ).fetchone()
        if row is None:
            return None
        return dict(zip(COLUMNS, row))

    def save(self, user_id, values):
        """Writes the whole row of one account. values: a dict with the COLUMNS keys (missing keys are stored as NULL)"""
        params = [str(user_id)] + [values.get(column) for column in COLUMNS]
        placeholders = ", ".join("?" * (len(COLUMNS) + 1))
        updates = ", ".join(f"{column} = excluded.{column}" for column in COLUMNS)
        with _write_lock:
            with closing(self._open()) as conn:
                self._prepare(conn)
                conn.execute(
                    f"INSERT INTO quest_state (user_id, {', '.join(COLUMNS)}) VALUES ({placeholders}) "
                    f"ON CONFLICT(user_id) DO UPDATE SET {updates}",
                    params,
                )
                conn.commit()
