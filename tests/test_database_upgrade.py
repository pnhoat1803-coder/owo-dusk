import sqlite3

from utils import database
from utils.constants import database_version


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _old_database(path, version):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE meta_data (key TEXT PRIMARY KEY, value INTEGER)")
    conn.execute("INSERT INTO meta_data (key, value) VALUES ('version', ?)", (version,))
    # An old user_stats table: the boss_ticket column did not exist yet
    conn.execute(
        "CREATE TABLE user_stats (user_id TEXT PRIMARY KEY, daily REAL, lottery REAL, cookie REAL, "
        "giveaways REAL, captchas INTEGER, cowoncy INTEGER, boss REAL)"
    )
    conn.execute("INSERT INTO user_stats (user_id, daily, captchas) VALUES ('111', 2.5, 7)")
    conn.commit()
    conn.close()


def test_old_database_is_upgraded_in_place_and_keeps_its_rows(tmp_path):
    path = tmp_path / "db.sqlite"
    _old_database(path, "2.5.0")

    database.create_database(db_path=str(path))

    assert path.exists()
    conn = sqlite3.connect(path)
    try:
        assert "boss_ticket" in _columns(conn, "user_stats")
        row = conn.execute("SELECT daily, captchas, boss_ticket FROM user_stats WHERE user_id = '111'").fetchone()
        assert row == (2.5, 7, 0)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "pupiku" in tables
        stored = conn.execute("SELECT value FROM meta_data WHERE key = 'version'").fetchone()[0]
        assert str(stored) == database_version
    finally:
        conn.close()


def test_newer_database_is_kept_and_not_downgraded(tmp_path):
    path = tmp_path / "db.sqlite"
    _old_database(path, "9.9.9")

    database.create_database(db_path=str(path))

    assert path.exists()
    conn = sqlite3.connect(path)
    try:
        stored = conn.execute("SELECT value FROM meta_data WHERE key = 'version'").fetchone()[0]
        assert str(stored) == "9.9.9"
        assert conn.execute("SELECT COUNT(*) FROM user_stats").fetchone()[0] == 1
    finally:
        conn.close()
