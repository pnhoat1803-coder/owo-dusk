"""Live state of every account, written to utils/data/live_status.json for the read-only dashboard.

The dashboard runs in another process and cannot see the memory of the bot. One daemon thread wakes up every
INTERVAL_SECONDS, reads PLAIN ATTRIBUTES of each account (state flags, last command time, balance, the time of
each command the account ran last) and writes one small JSON file. The file is written to a .tmp file and moved
over the real one with os.replace, so a reader never sees half a file.

Accounts never wait for this thread: they only set the attributes they already set. The thread takes no lock that
an account holds, and it holds none while it writes. A failed write is logged once per WARN_EVERY_SECONDS and the
thread goes on. Nothing here sends a command, changes a setting or touches the captcha, hunt, gem or sleep code.

Command counters: the bot stores the time each command id ran last (CommandHandler.cmds_state[id]["last_ran"]).
This thread counts every change of that time, so "hunt": 120 means 120 runs seen since the bot started. A command
that runs twice in less than one interval is counted once, hunt and battle never do (cooldown is above 15 s).

Remote commands: the dashboard appends commands to utils/data/web_commands.json (website/api_v2.py). This thread runs
each new one once: sleep, wake, captcha reset and channel switch, by setting the same flags and calling the same
methods as the Discord text commands. Only this thread reads that file. The dashboard prunes it, so nothing here
rewrites it and a command queued at the same moment cannot be lost.
"""

import asyncio
import atexit
import json
import logging
import os
import threading
import time
from pathlib import Path

from utils.account_identity import slot_key
from utils.quote_source import quotes as quote_source

INTERVAL_SECONDS = 5.0
WARN_EVERY_SECONDS = 600.0
SCHEMA = 1
DEFAULT_PATH = Path("utils") / "data" / "live_status.json"
COMMANDS_PATH = Path("utils") / "data" / "web_commands.json"
# Command ids counted by watching cmds_state[id]["last_ran"]
COUNTED = ("hunt", "battle", "owo", "pray", "curse")
# Remote commands the bot runs. The dashboard refuses any other action (website/api_v2.py CONTROL_ACTIONS)
COMMAND_ACTIONS = ("sleep", "wake", "restart_captcha", "switch_channel")
COMMAND_TTL_S = 60  # older commands are ignored
REPLACE_TRIES = 4
REPLACE_WAIT = 0.03

_log = logging.getLogger(__name__)


def _short(text, limit=80):
    if text is None:
        return None
    text = str(text).replace("\n", " ").strip()
    return text[:limit]


class _Tracker:
    """What the writer remembers about one account between two samples"""

    def __init__(self):
        self.seen = {}  # command id -> last_ran value seen
        self.count = {}  # command id -> runs counted
        self.back_seen = 0.0  # last gateway_back_at seen
        self.reconnects = 0


class LiveStatusWriter:
    def __init__(self, path=DEFAULT_PATH, interval=INTERVAL_SECONDS, clients=None, accounts=None, autostop=None, commands_path=COMMANDS_PATH):
        self.path = Path(path)
        self.interval = float(interval)
        self.commands_path = Path(commands_path)
        # Providers, so tests can feed fake accounts. Defaults read the real bot
        self._clients = clients or _real_clients
        self._accounts = accounts or _real_accounts
        self._autostop = autostop or _real_autostop
        self._trackers = {}
        self._processed_cmd_ids = set()
        self._stop = threading.Event()
        self._thread = None
        self._seq = 0
        self._started_at = time.time()
        self._last_warn = 0.0
        self._stopped_written = False
        self.write_errors = 0
        # Set by begin_run() once tokens.txt is read. No file is written before that
        self._configured_total = None
        self._run_id = None
        self._run_ready = False
        self._write_lock = threading.Lock()
        self._warned_ids = set()

    # ---- lifecycle -------------------------------------------------------
    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._stopped_written = False
        self._started_at = time.time()
        self._thread = threading.Thread(target=self._run, name="live-status-writer", daemon=True)
        self._thread.start()
        atexit.register(self.stop)
        return self

    def begin_run(self, configured_total, run_id):
        """Called once tokens.txt is read: from now on every file carries the number of accounts loaded and the run id.
        The first file is written by write_now(), after the accounts are registered, so no file shows a wrong total"""
        self._configured_total = int(configured_total)
        self._run_id = str(run_id)
        self._run_ready = True

    def stop(self, timeout=2.0):
        """Ends the thread and writes the last file, with running=false. Safe to call twice. A run that never began
        writes nothing, so the file of the previous run is kept"""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout)
        if not self._stopped_written:
            self._stopped_written = True
            if self._run_ready:
                self.write_once(running=False)

    def _run(self):
        last_write = 0.0
        while not self._stop.is_set():
            try:
                self._process_web_commands()
            except Exception as err:
                _log.warning(f"Error processing web commands: {err}")
            now = time.monotonic()
            if self._run_ready and now - last_write >= self.interval:
                self.write_once(running=True)
                last_write = now
            self._stop.wait(1.0)

    def _process_web_commands(self):
        if not self.commands_path.exists():
            return
        now = time.time()
        try:
            with open(self.commands_path, "r", encoding="utf-8") as f:
                commands = json.load(f)
            if not isinstance(commands, list):
                return
        except Exception:
            return

        # Ids no longer in the file cannot come back, so the set only holds what the file still lists
        present = {c.get("cmd_id") for c in commands if isinstance(c, dict)}
        self._processed_cmd_ids &= present

        pending = [
            c for c in commands
            if isinstance(c, dict)
            and c.get("cmd_id")
            and c.get("cmd_id") not in self._processed_cmd_ids
            and isinstance(c.get("created_at"), (int, float))
            and now - c["created_at"] <= COMMAND_TTL_S
        ]

        if not pending:
            return

        clients = self._clients()
        records = self._accounts()

        for cmd in pending:
            cmd_id = cmd["cmd_id"]
            self._processed_cmd_ids.add(cmd_id)
            action = cmd.get("action")
            if action not in COMMAND_ACTIONS:
                _log.warning(f"Dashboard command ignored: unknown action {action!r}")
                continue
            target = str(cmd.get("target", "all")).strip()
            value = cmd.get("value")

            if target == "all":
                target_clients = list(clients.values())
            else:
                target_clients = []
                for idx, c in clients.items():
                    rec = records.get(idx) or {}
                    rec_id = str(rec.get("userid")) if rec.get("userid") else None
                    c_user = getattr(c, "user", None)
                    c_id = str(c_user.id) if c_user and getattr(c_user, "id", None) else None
                    if target in (str(idx), str(getattr(c, "account_index", "")), rec_id, c_id):
                        target_clients.append(c)

            for client in target_clients:
                try:
                    self._execute_command_on_client(client, action, value)
                except Exception as err:
                    _log.warning(f"Error executing command {action} on client: {err}")

    def _execute_command_on_client(self, client, action, value):
        ch = getattr(client, "ch", None)
        status = getattr(ch, "command_handler_status", None)
        loop = getattr(client, "loop", None)
        is_loop_running = bool(loop and loop.is_running())

        if action == "sleep":
            if status is not None:
                status.sleep = True
            if is_loop_running and hasattr(client, "log"):
                asyncio.run_coroutine_threadsafe(
                    client.log("Dashboard remote control: sleep", "#87875f"),
                    loop
                )

        elif action == "wake":
            if status is not None:
                status.sleep = False
            if is_loop_running and hasattr(client, "log"):
                asyncio.run_coroutine_threadsafe(
                    client.log("Dashboard remote control: wake", "#87875f"),
                    loop
                )

        elif action == "restart_captcha":
            if status is not None:
                status.captcha = False
                status.captcha_flag = False
            if hasattr(client, "get_cog"):
                cap_cog = client.get_cog("Captcha")
                if cap_cog:
                    cap_cog.captcha_flag = False
                    cap_cog.hcaptcha_solve_in_progress = False
                    cap_cog.solve_running = False
                    cap_cog.captcha_seen_at = None
            if is_loop_running and hasattr(client, "log"):
                asyncio.run_coroutine_threadsafe(
                    client.log("Dashboard remote control: reset captcha flags", "#87875f"),
                    loop
                )

        elif action == "switch_channel":
            if value and is_loop_running and hasattr(client, "fetch_channel") and hasattr(client, "empty_checks_and_switch"):
                async def _do_switch(c, ch_id):
                    try:
                        channel = await c.fetch_channel(int(ch_id))
                        if channel:
                            await c.empty_checks_and_switch(channel)
                            # The channel switcher cog owns the webhook message (core/cogs/channelswitcher.py)
                            switcher = c.get_cog("ChannelSwitcher") if hasattr(c, "get_cog") else None
                            if switcher is not None and hasattr(switcher, "handle_webhook"):
                                await switcher.handle_webhook(channel)
                            await c.log(f"Switched channel to {channel.name} ({ch_id}) via Web Dashboard", "#87875f")
                    except Exception as e:
                        await c.log(f"Failed to switch channel to {ch_id}: {e}", "#c25560")
                asyncio.run_coroutine_threadsafe(_do_switch(client, value), loop)
            elif value and not is_loop_running:
                _log.warning("Dashboard channel switch skipped: the account is not running yet")

    # ---- one sample ------------------------------------------------------
    def write_once(self, running=True):
        with self._write_lock:  # the writer thread and the bot's own calls never write the same temporary file at once
            try:
                data = self.snapshot(running)
                self._write(data)
            except Exception as error:  # the thread must keep going
                self.write_errors += 1
                self._warn(f"Live status could not be written ({type(error).__name__})")

    def _warn(self, text):
        now = time.monotonic()
        if now - self._last_warn >= WARN_EVERY_SECONDS or self._last_warn == 0.0:
            self._last_warn = now
            _log.warning(text)

    def _warn_id_change(self, index, expected, real):
        """Once per account: the id the account logged in with differs from the id its token shows. The file then
        uses the real id, in the same write, so the old key does not stay behind"""
        if index in self._warned_ids:
            return
        self._warned_ids.add(index)
        _log.warning(
            f"Account #{index}: the user id {real} differs from the id its token shows ({expected}); "
            f"the dashboard uses {real} from now on"
        )

    def snapshot(self, running=True):
        wall = time.time()
        mono = time.monotonic()
        clients = self._clients()
        records = self._accounts()
        accounts = []
        for index in sorted(set(records) | set(clients)):
            try:
                accounts.append(self._account(index, records.get(index) or {}, clients.get(index), wall, mono))
            except Exception:  # one broken account must not hide the others
                accounts.append({"i": index, "rs": "unknown"})
        self._seq += 1
        return {
            "schema": SCHEMA,
            "seq": self._seq,
            "written_at": wall,
            "started_at": self._started_at,
            "pid": os.getpid(),
            "running": running,
            "run_id": self._run_id,
            "configured_total": self._configured_total,
            "autostop": self._autostop(wall),
            "quotes": self._quotes(),
            "accounts": accounts,
        }

    def _quotes(self):
        """Level quote pool, cache and last fetch, see utils/quote_source.py. Empty when it cannot be read"""
        try:
            return dict(quote_source.status())
        except Exception:
            return {}

    def _account(self, index, record, client, wall, mono):
        # The key is the user id the token shows, from the first write: an account that has not logged in yet has
        # it too. Once it is logged in, its real id is used (the two are the same unless the token changed)
        real = str(record["userid"]) if record.get("userid") else None
        if real is None and client is not None:
            user = getattr(client, "user", None)
            if getattr(user, "id", None):
                real = str(user.id)
        expected = record.get("expected_key") or slot_key(index)
        if real is not None and real != expected:
            self._warn_id_change(index, expected, real)
        status = record.get("status", "pending")
        out = {
            "i": index,
            "slot": index,
            "id": real or expected,
            "rs": status,
            "phase": _phase(status, client),
        }
        if status in ("failed", "stopped") and record.get("error"):
            out["re"] = _short(record.get("error"))
        if client is None:
            return out
        tracker = self._trackers.setdefault(index, _Tracker())
        out["name"] = getattr(client, "username", None)
        ch = getattr(client, "ch", None)
        status = getattr(ch, "command_handler_status", None)
        out["cap"] = bool(getattr(status, "captcha", False))
        # Set when a captcha episode ran past its deadline, see core/cogs/captcha.py
        out["cap_flag"] = bool(getattr(status, "captcha_flag", False))
        out["sl"] = bool(getattr(status, "sleep", False))
        out["hold"] = bool(getattr(status, "hold_handler", False))
        out["on"] = bool(getattr(status, "state", True))
        out["stp"] = bool(getattr(client, "stopped", False))
        down_at = getattr(client, "gateway_down_at", None)
        out["gwd"] = float(down_at) if down_at else None
        last_sent = getattr(client, "last_sent_at", None)
        out["ls"] = round(wall - (mono - last_sent), 1) if last_sent else None
        stats = getattr(client, "stats", None)
        out["bal"] = getattr(stats, "balance", None) if getattr(stats, "has_updated_balance", False) else None
        out["net"] = getattr(stats, "net_earnings", None)
        out["nogem"] = bool(getattr(stats, "no_gem", False))
        out["hunt_on"] = _hunt_enabled(client)
        # reconnects: each time the gateway comes back, gateway_back_at changes
        back_at = getattr(client, "gateway_back_at", 0.0) or 0.0
        if back_at and back_at != tracker.back_seen:
            tracker.reconnects += 1
            tracker.back_seen = back_at
        out["rc"] = tracker.reconnects
        out["gwb"] = round(float(back_at), 1) if back_at else None
        # command counters
        states = getattr(ch, "cmds_state", None)
        counts, last = {}, {}
        if isinstance(states, dict):
            for cmd in COUNTED:
                state = states.get(cmd)
                ran = state.get("last_ran", 0) if isinstance(state, dict) else 0
                if not ran:
                    continue
                before = tracker.seen.get(cmd)
                if before is None:
                    tracker.count[cmd] = tracker.count.get(cmd, 0) + 1
                elif ran != before:
                    tracker.count[cmd] = tracker.count.get(cmd, 0) + 1
                tracker.seen[cmd] = ran
                last[cmd] = round(float(ran), 1)
        for cmd in COUNTED:
            if cmd in tracker.count:
                counts[cmd] = tracker.count[cmd]
        out["c"] = counts
        out["lr"] = last
        # Inbound per server (ids and numbers, see core/gateway_stats.py) and this account's own server
        out["gi"] = _guild_inbound(client)
        out["fg"] = _farm_guild(client)
        # D7: the autoquest state. null when the quest cog is not loaded for this account (off, or not on the allowlist)
        out["quest"] = _quest_block(client)
        return out

    # ---- file ------------------------------------------------------------
    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        text = json.dumps(data, separators=(",", ":"), default=str)
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
        last_error = None
        for _ in range(REPLACE_TRIES):
            try:
                os.replace(tmp, self.path)  # on Windows this fails while a reader has the file open
                return
            except PermissionError as error:
                last_error = error
                time.sleep(REPLACE_WAIT)
        raise last_error


def _quest_block(client):
    """D7: the autoquest state of one account for the dashboard, read from what the quest cog and its LocalQuestHandler
    already keep. None when the quest cog is not loaded for this account. Reads never raise: a missing value is None"""
    getter = getattr(client, "get_cog", None)
    cog = getter("Quest") if callable(getter) else None
    if cog is None:
        return None
    handler = getattr(client, "quest_handler", None)
    open_quests = []
    for quest in list(getattr(handler, "quests", None) or []):
        if not isinstance(quest, dict) or quest.get("complete"):
            continue
        open_quests.append({"id": quest.get("quest_id"), "current": quest.get("current"), "total": quest.get("total")})
    next_at = getattr(handler, "next_quest_timestamp", 0) or 0
    seals = getattr(handler, "seals", None)
    last_claim = getattr(cog, "_last_claim_at", None)
    return {
        "enabled": True,
        "all_done": bool(getattr(cog, "all_quests_done", False)),
        "next_quest_at": int(next_at) if next_at else None,
        "seals": int(seals) if isinstance(seals, int) else None,
        "open_quests": open_quests,
        "last_claim_at": round(float(last_claim), 1) if last_claim else None,
    }


def _phase(status, client):
    """starting: its turn has not come yet. connecting: the client runs and logs in. online: ready. offline: failed or
    stopped"""
    if status in ("failed", "stopped"):
        return "offline"
    if status == "ready":
        return "online"
    return "connecting" if client is not None else "starting"


def _hunt_enabled(client):
    try:
        return bool(client.settings_dict.commands.hunt.enabled)
    except Exception:
        return True


def _guild_inbound(client):
    """The busiest servers of this account's gateway, see core/gateway_stats.py GuildWindow. [] when unknown"""
    stats = getattr(client, "gateway_stats", None)
    try:
        return stats.top_guilds() if stats is not None else []
    except Exception:
        return []


def _farm_guild(client):
    """The server of this account's spam channel as a string id, or None while the channel is not known"""
    guild_id = getattr(getattr(getattr(client, "cm", None), "guild", None), "id", None)
    return str(guild_id) if guild_id else None


def _real_clients():
    from core import auto_stop

    return auto_stop.clients_snapshot()


def _real_accounts():
    from core.account_registry import registry

    return registry.snapshot()


def _real_autostop(wall):
    try:
        from core import auto_stop

        timer = auto_stop.active_timer()
        if timer is None:
            return {"enabled": False, "stopping": auto_stop.stop_requested()}
        return {
            "enabled": True,
            "stops_at": round(timer.stop_at.timestamp(), 1),
            "minutes": timer.minutes,
            "grace_s": timer.grace_seconds,
            "stopping": auto_stop.stop_requested(),
        }
    except Exception:
        return {"enabled": False}


writer = LiveStatusWriter()


def start():
    """Starts the writer thread for the real bot. Called by uwu.py when the dashboard is on"""
    return writer.start()


def begin_run(configured_total, run_id):
    """Called by uwu.py once tokens.txt is read: the number of accounts and the run id go into every file"""
    writer.begin_run(configured_total, run_id)


def write_now():
    """Writes the file at once (run_bots, after the accounts are registered). Does nothing before begin_run()"""
    if writer._run_ready:
        writer.write_once(running=True)


def stop():
    """Writes the last file (running=false) and ends the thread"""
    writer.stop()
