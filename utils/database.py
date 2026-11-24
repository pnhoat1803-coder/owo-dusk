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

import os
import sqlite3

from utils.constants import database_version
from utils.errors import suppress_and_log
from utils.loader import misc_dict
from utils.system.system import compare_versions, console


# The tables the bot uses. Every statement is CREATE TABLE IF NOT EXISTS: running them on an old
# database only creates what is missing, it never drops or empties a table.
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS commands (name TEXT PRIMARY KEY, count INTEGER)",
    "CREATE TABLE IF NOT EXISTS cowoncy_earnings (user_id TEXT, hour INTEGER, earnings INTEGER, PRIMARY KEY (user_id, hour))",
    "CREATE TABLE IF NOT EXISTS gamble_winrate (hour INTEGER PRIMARY KEY, wins INTEGER, losses INTEGER, net INTEGER)",
    "CREATE TABLE IF NOT EXISTS user_stats (user_id TEXT PRIMARY KEY, daily REAL, lottery REAL, cookie REAL, giveaways REAL, captchas INTEGER, cowoncy INTEGER, boss REAL, boss_ticket INTEGER)",
    "CREATE TABLE IF NOT EXISTS meta_data (key TEXT PRIMARY KEY, value INTEGER)",
    "CREATE TABLE IF NOT EXISTS command_priority (user_id TEXT, command_name TEXT, priority INTEGER, PRIMARY KEY (user_id, command_name))",
    "CREATE TABLE IF NOT EXISTS pupiku (user_id TEXT, cmd TEXT, times_to_run INTEGER, last_ran REAL, PRIMARY KEY (user_id, cmd))",
)


def _default_for(column_type):
    """Default for a column that an old database does not have yet (the schema sets none)"""
    t = (column_type or "").upper()
    if "INT" in t or "REAL" in t or "FLOA" in t or "DOUB" in t:
        return "0"
    if "CHAR" in t or "TEXT" in t or "CLOB" in t:
        return "''"
    return "NULL"


def _version_is_older(stored):
    """True when the stored version is older than database_version (or cannot be read as a version)"""
    try:
        return bool(compare_versions(str(stored), database_version))
    except (ValueError, TypeError):
        return True


def upgrade_database(db_path):
    """Upgrades an existing database in place. The file is never deleted, renamed or copied and no row is removed:
    missing tables are created and missing columns are added with ALTER TABLE ... ADD COLUMN.
    Returns the version that was stored before (None when there was none)."""
    conn = sqlite3.connect(db_path)
    try:
        c = conn.cursor()
        try:
            c.execute("SELECT value FROM meta_data WHERE key = 'version'")
            row = c.fetchone()
            old_version = row[0] if row else None
        except sqlite3.OperationalError:
            old_version = None  # meta_data does not exist yet
        for statement in SCHEMA:
            c.execute(statement)
        expected = sqlite3.connect(":memory:")
        try:
            for statement in SCHEMA:
                expected.execute(statement)
            tables = [row[0] for row in expected.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
            for table in tables:
                present = {info[1] for info in c.execute(f"PRAGMA table_info({table})").fetchall()}
                wanted = expected.execute(f"PRAGMA table_info({table})").fetchall()
                for _cid, name, column_type, _notnull, _default, _pk in wanted:
                    if name in present:
                        continue
                    try:
                        c.execute(
                            f"ALTER TABLE {table} ADD COLUMN {name} {column_type} DEFAULT {_default_for(column_type)}"
                        )
                    except sqlite3.Error as error:
                        console.print(
                            f"Database upgrade: could not add {table}.{name} ({error}); kept going with the old database."
                        )
        finally:
            expected.close()
        if old_version is None or _version_is_older(old_version):
            c.execute("INSERT OR REPLACE INTO meta_data (key, value) VALUES ('version', ?)", (database_version,))
            console.print(f"Database upgraded from {old_version or 'unknown'} to {database_version} (data kept)")
        conn.commit()
        return old_version
    finally:
        conn.close()


@suppress_and_log("Database Initialisation")
def create_database(db_path="utils/data/db.sqlite"):
    if os.path.exists(db_path):
        # Upgrade in place: the file, the rows and the stats are kept (no more deleting on a version change)
        upgrade_database(db_path)

    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    for statement in SCHEMA:
        c.execute(statement)
    # Switch to WAL mode.
    c.execute("PRAGMA journal_mode=WAL;")

    # Populate
    # -- gamble_winrate
    for hr in range(24):
        # hour does not have 24 in 24 hr format!!
        c.execute(
            "INSERT OR IGNORE INTO gamble_winrate (hour, wins, losses, net) VALUES (?, ?, ?, ?)",
            (hr, 0, 0, 0),
        )

    # -- meta data
    c.execute(
        "INSERT OR IGNORE INTO meta_data (key, value) VALUES (?, ?)",
        ("gamble_winrate_last_checked", 0),
    )
    c.execute(
        "INSERT OR IGNORE INTO meta_data (key, value) VALUES (?, ?)",
        ("cowoncy_earnings_last_checked", 0),
    )

    # `INSERT OR UPDATE` is not used since we will be comparing old value (if any) ------ (check!!)
    c.execute(
        "INSERT OR IGNORE INTO meta_data (key, value) VALUES (?, ?)",
        ("version", database_version),
    )

    c.execute(
        "INSERT OR IGNORE INTO meta_data (key, value) VALUES (?, ?)",
        ("event_till_timestamp", 0),
    )

    # -- command priority
    c.execute("SELECT * FROM command_priority WHERE user_id = ?", ("default",))
    rows = c.fetchall()
    populate = False
    if not rows:
        populate = True

    if not populate:
        # 0 -> user_id
        # 1 -> command_name
        # 2 -> priority
        temp_list = [(row[1], int(row[2])) for row in rows]
        for key, value in misc_dict["command_info"].items():
            if (key, value["priority"]) not in temp_list:
                c.execute("DELETE FROM command_priority")
                populate = True
                break

    if populate:
        for key, value in misc_dict["command_info"].items():
            # We will be putting a `DEFAULT` value here to make it easier to compare to misc.json.
            # This is to ensure we do update in two cases:
            # 1) when priority is changed
            # 3) when a new item is added to priority
            c.execute(
                "INSERT OR IGNORE INTO command_priority (user_id, command_name, priority) VALUES (?, ?, ?)",
                ("default", key, value.get("priority")),
            )

    # -- commands
    for cmd in misc_dict["command_info"].keys():
        c.execute(
            "INSERT OR IGNORE INTO commands (name, count) VALUES (?, ?)", (cmd, 0)
        )

    # -- end --#
    conn.commit()
    conn.close()
