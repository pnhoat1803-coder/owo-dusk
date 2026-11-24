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

import hmac
import logging
import os
import queue
import sqlite3
import sys
import threading
import time
from datetime import date, timedelta
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from utils import lan
from utils.runtime_handler import load_weekly_runtime

app = Flask(__name__)
config_updated = None


class ConsoleLogWriter:
    def __init__(self, path="utils/data/web_console.sqlite", max_queue=2000):
        self.path = Path(path)
        self.queue = queue.Queue(maxsize=max_queue)
        self.stop_event = threading.Event()
        self.thread = None
        self.warned = False

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS logs ("
                "id INTEGER PRIMARY KEY, ts REAL, html TEXT)"
            )
            conn.commit()
        finally:
            conn.close()
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._run, name="dashboard-console-writer", daemon=True
        )
        self.thread.start()

    def append(self, html):
        item = (time.time(), str(html))
        while True:
            try:
                self.queue.put_nowait(item)
                return
            except queue.Full:
                try:
                    self.queue.get_nowait()
                except queue.Empty:
                    continue

    def _drain_batch(self):
        batch = []
        while True:
            try:
                batch.append(self.queue.get_nowait())
            except queue.Empty:
                return batch

    def _flush(self, conn):
        batch = self._drain_batch()
        if not batch:
            return
        conn.executemany("INSERT INTO logs(ts, html) VALUES (?, ?)", batch)
        conn.execute(
            "DELETE FROM logs WHERE id NOT IN "
            "(SELECT id FROM logs ORDER BY id DESC LIMIT 1000)"
        )
        conn.commit()

    def _run(self):
        conn = None
        try:
            conn = sqlite3.connect(self.path, timeout=2)
            while not self.stop_event.wait(1):
                self._flush(conn)
            self._flush(conn)
        except Exception as error:
            if not self.warned:
                self.warned = True
                print(
                    f"Warning: dashboard console log write failed "
                    f"({type(error).__name__})."
                )
        finally:
            if conn is not None:
                conn.close()

    def stop(self, timeout=2):
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout)


_console_log_writer = ConsoleLogWriter()


def start_console_log_writer(path="utils/data/web_console.sqlite"):
    if Path(path) != _console_log_writer.path:
        if _console_log_writer.thread and _console_log_writer.thread.is_alive():
            raise RuntimeError("dashboard console log writer already uses another path")
        _console_log_writer.path = Path(path)
    _console_log_writer.start()
    return _console_log_writer

# these will be updated
password = ""
version = ""
refresh_interval = 15


def merge_dicts(main, small):
    for key, value in small.items():
        if key in main and isinstance(main[key], dict) and isinstance(value, dict):
            merge_dicts(main[key], value)
        else:
            main[key] = value


def _read_only_uri(path):
    return Path(path).resolve().as_uri() + "?mode=ro"


def get_from_db(command):
    conn = sqlite3.connect(_read_only_uri("utils/data/db.sqlite"), uri=True)
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(command)
        return cur.fetchall()
    finally:
        conn.close()


def get_console_rows():
    conn = sqlite3.connect(
        _read_only_uri("utils/data/web_console.sqlite"), uri=True
    )
    try:
        rows = conn.execute(
            "SELECT html FROM logs ORDER BY id DESC LIMIT 300"
        ).fetchall()
        return [row[0] for row in reversed(rows)]
    finally:
        conn.close()


def weekly_runtime_snapshot(data, today=None):
    today = today or date.today()
    stored_days = data.get("days", {}) if isinstance(data, dict) else {}
    dates = [today - timedelta(days=offset) for offset in range(6, -1, -1)]
    values = []
    for day in dates:
        try:
            seconds = max(0.0, float(stored_days.get(day.isoformat(), 0)))
        except (TypeError, ValueError):
            seconds = 0.0
        values.append(seconds / 3600)
    try:
        current_uptime = max(0.0, float(stored_days.get(today.isoformat(), 0)))
    except (TypeError, ValueError):
        current_uptime = 0.0
    return {
        "runtime_labels": [day.strftime("%a %m/%d") for day in dates],
        "runtime_data": values,
        "current_uptime": current_uptime,
    }


def is_authorized():
    supplied = request.headers.get("password", "")
    # compare_digest takes the same time whatever part of the password matches
    return bool(password) and hmac.compare_digest(
        supplied.encode("utf-8"), password.encode("utf-8")
    )


@app.route("/legacy")
def home():
    return render_template(
        "index.html", version=version, refresh_interval=refresh_interval
    )


@app.route("/api/console", methods=["GET"])
def get_console_logs():
    if not is_authorized():
        return "Invalid Password", 401
    try:
        return "\n".join(get_console_rows())
    except Exception as e:
        print(f"Error fetching logs: {e}")
        return jsonify(
            {"status": "error", "message": "An error occurred while fetching logs"}
        ), 500


@app.route("/api/fetch_gamble_data", methods=["GET"])
def fetch_gamble_data():
    if not is_authorized():
        return "Invalid Password", 401
    try:
        # Fetch table data
        rows = get_from_db(
            "SELECT hour, wins, losses FROM gamble_winrate ORDER BY hour"
        )

        # Extract columns as lists
        win_data = [row["wins"] for row in rows]
        lose_data = [row["losses"] for row in rows]

        # Return Data
        return jsonify(
            {"status": "success", "win_data": win_data, "lose_data": lose_data}
        )

    except Exception as e:
        print(f"Error fetching gamble data: {e}")
        return jsonify(
            {
                "status": "error",
                "message": "An error occurred while fetching gamble data",
            }
        ), 500


@app.route("/api/fetch_cowoncy_data", methods=["GET"])
def fetch_cowoncy_data():
    if not is_authorized():
        return "Invalid Password", 401

    try:
        rows = get_from_db(
            "SELECT user_id, hour, earnings FROM cowoncy_earnings ORDER BY hour"
        )
        user_data = {}
        for row in rows:
            user_id = row["user_id"]
            hour = row["hour"]
            earnings = row["earnings"]

            if user_id not in user_data:
                # Create dummy data
                user_data[user_id] = {i: 0 for i in range(24)}
            # populate
            user_data[user_id][hour] = earnings

        # Base data
        base_data = {"labels": [f"Hour {i}" for i in range(24)], "datasets": []}

        # Top 10 accounts by total earnings, everyone else is merged into one "Còn lại" line
        ranked = sorted(
            user_data.items(), key=lambda item: sum(item[1].values()), reverse=True
        )
        series = ranked[:10]
        if len(ranked) > 10:
            rest = [hourly for _, hourly in ranked[10:]]
            series.append(
                ("Còn lại", {i: sum(hourly[i] for hourly in rest) for i in range(24)})
            )

        for index, (label, hourly_data) in enumerate(series):
            # fixed hues, so the colours stay the same on every refresh
            color_hue = (index * 137) % 360
            dataset = {
                "label": label,
                "data": [hourly_data[i] for i in range(24)],
                "borderColor": f"hsl({color_hue}, 100%, 50%)",
                "backgroundColor": f"hsl({color_hue}, 100%, 70%)",
                "fill": False,
                "tension": 0.4,
                "pointRadius": 0,
            }
            base_data["datasets"].append(dataset)

        rows = get_from_db("SELECT cowoncy, captchas FROM user_stats")
        total_cowoncy = sum(row["cowoncy"] for row in rows)
        # I understand this area is for cowoncy, but accessing thro here since lazy lol.
        total_captchas = sum(row["captchas"] for row in rows)

        return jsonify(
            {
                "status": "success",
                "data": base_data,
                "total_cash": total_cowoncy,
                "total_captchas": total_captchas,
            }
        ), 200

    except Exception as e:
        print(f"Error fetching cowoncy data: {e}")
        return jsonify(
            {
                "status": "error",
                "message": "An error occurred while fetching cowoncy data",
            }
        ), 500


@app.route("/api/fetch_cmd_data", methods=["GET"])
def fetch_cmd_data():
    if not is_authorized():
        return "Invalid Password", 401
    try:
        rows = get_from_db("SELECT * FROM commands")

        filtered_rows = [row for row in rows if row["count"] != 0]

        command_names = [row["name"] for row in filtered_rows]
        count = [row["count"] for row in filtered_rows]

        for idx, item in enumerate(count):
            if item == 0:
                command_names.pop(idx)
                count.pop(idx)

        # Return Data
        return jsonify(
            {"status": "success", "command_names": command_names, "count": count}
        )

    except Exception as e:
        print(f"Error fetching command data: {e}")
        return jsonify(
            {
                "status": "error",
                "message": "An error occurred while fetching command data",
            }
        ), 500


@app.route("/api/fetch_weekly_runtime", methods=["GET"])
def fetch_weekly_runtime():
    if not is_authorized():
        return "Invalid Password", 401
    try:
        data_dict = load_weekly_runtime("utils/data/weekly_runtime.json")
        runtime_data = weekly_runtime_snapshot(data_dict)
        runtime_data["status"] = "success"
        return jsonify(runtime_data)

    except Exception as e:
        print(f"Error fetching weekly runtime: {e}")
        return jsonify(
            {
                "status": "error",
                "message": "An error occurred while fetching weekly runtime",
            }
        ), 500


def dashboard_password_notice(paswd):
    """One console line about the dashboard password, or None when it is long enough (lan.MIN_PASSWORD characters).
    No password: api_v2.authorized() lets only this computer in (website/api_v2.py)."""
    if not paswd:
        return "Dashboard chưa đặt mật khẩu: chỉ mở được trên máy này."
    if len(paswd) < lan.MIN_PASSWORD:
        return f"Mật khẩu dashboard quá ngắn (nên từ {lan.MIN_PASSWORD} ký tự trở lên)."
    return None


def configure_dashboard(ver, paswd, refresh_secs=15, port=None, host="127.0.0.1", lan_urls=None, password_banner=False):
    """host: the address the server listens on, already checked by utils/lan.py (a LAN address needs a password).
    lan_urls: the URLs other devices can open. password_banner: True when the dashboard fell back to this machine"""
    global password, version, refresh_interval
    password = paswd
    version = ver
    refresh_interval = refresh_secs
    # The v2 read-only dashboard (website/api_v2.py): password, session key, snapshot thread
    api_v2.configure(
        paswd, ver, refresh_secs, port=port, host=host, app=app,
        lan_urls=lan_urls, password_banner=password_banner,
    )


def web_start(port, host, ver, paswd, refresh_secs=15):
    """The dashboard inside the bot's process (separateProcess false). host is the address asked for; it is checked
    here the same way as in website/server.py (utils/lan.py)"""
    bind_host, reason = lan.effective_bind(host, paswd)
    configure_dashboard(
        ver, paswd, refresh_secs, port=port, host=bind_host,
        lan_urls=lan.lan_urls(bind_host, port), password_banner=lan.shows_password_banner(reason),
    )
    # Start the webserver
    flask_log = logging.getLogger("werkzeug")
    flask_log.disabled = True
    cli = sys.modules["flask.cli"]
    setattr(cli, "show_server_banner", lambda *x: None)
    app.run(
        debug=False,
        use_reloader=False,
        port=port,
        host=bind_host,
    )


def website_append(content):
    _console_log_writer.append(content)


# The new read-only dashboard: routes "/", "/login", "/api/v2/*", the auth check and the security headers.
# The page that used to be "/" is now "/legacy"; the old /api/* routes keep their paths.
from . import api_v2  # noqa: E402

api_v2.init_app(app)
