"""The dashboard's bot supervisor (website/bot_supervisor.py), with a fake bot: start, refusal of a second start, graceful
stop, terminate and kill, restart, crash handling, stale and foreign pid files, and the rule that only the bot is ever
signalled. The fake bot writes utils/data/bot.pid, obeys the stop request, and writes last_exit.json like the real bot.
It is a temp copy of a script; no network and no Discord. The mode comes from FAKE_BOT_MODE."""

import json
import os
import subprocess
import sys
import textwrap
import time

import pytest

from utils import bot_state
from website import bot_supervisor
from website.bot_supervisor import BotBusy, BotRefused, BotSupervisor, extra_args, read_settings

FAKE_BOT = textwrap.dedent('''
    import json, os, signal, sys, time, uuid
    from pathlib import Path

    ROOT = Path(__file__).resolve().parent
    DATA = ROOT / "utils" / "data"
    PID = DATA / "bot.pid"
    STOP = DATA / "control" / "stop.request"
    LAST = DATA / "control" / "last_exit.json"
    MODE = os.environ.get("FAKE_BOT_MODE", "normal")
    RUN_ID = os.environ.get("DUSK_RUN_ID") or uuid.uuid4().hex
    SUPERVISED = os.environ.get("DUSK_SUPERVISED") == "1"

    def write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)

    def remove(path):
        try:
            path.unlink()
        except OSError:
            pass

    def finish(reason, code):
        write(LAST, {"run_id": RUN_ID, "code": code, "reason": reason, "at": time.time()})
        remove(PID)

    if MODE == "hang":
        for name in ("SIGTERM", "SIGBREAK"):
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), signal.SIG_IGN)

    write(PID, {"pid": os.getpid(), "started_at": "2026-01-01T00:00:00", "run_id": RUN_ID,
                "supervised": SUPERVISED, "cmd": "uwu.py"})
    if MODE == "crash":
        time.sleep(1.0)
        os._exit(1)
    if MODE == "autostop":
        time.sleep(1.0)
        finish("auto_stop", 0)
        sys.exit(0)
    while True:
        time.sleep(0.2)
        if MODE in ("ignore_stop", "hang"):
            continue
        try:
            request = json.loads(STOP.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            request = None
        if request is None:
            continue
        remove(STOP)
        if request.get("run_id") == RUN_ID:
            finish("user_stop", 0)
            sys.exit(0)
''')


def wait_for(predicate, timeout=20.0, step=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    data = root / "utils" / "data"
    data.mkdir(parents=True)
    (root / "config").mkdir()
    (root / "logs").mkdir()
    (root / "uwu.py").write_text(FAKE_BOT, encoding="utf-8")
    (root / "config" / "global_settings.json").write_text(json.dumps({"website": {}}), encoding="utf-8")
    monkeypatch.setattr(bot_state, "PID_PATH", data / "bot.pid")
    monkeypatch.setattr(bot_state, "CONTROL_DIR", data / "control")
    monkeypatch.setattr(bot_state, "STOP_REQUEST_PATH", data / "control" / "stop.request")
    monkeypatch.setattr(bot_state, "LAST_EXIT_PATH", data / "control" / "last_exit.json")
    # The escalation waits are shortened; the logic is the same
    monkeypatch.setattr(bot_supervisor, "TERMINATE_WAIT_SECONDS", 2.0)
    monkeypatch.setattr(bot_supervisor, "KILL_WAIT_SECONDS", 3.0)
    monkeypatch.delenv("FAKE_BOT_MODE", raising=False)
    return root


@pytest.fixture
def make(project):
    created = []

    def factory(restart_delay=1.0):
        sup = BotSupervisor(root=project, python=sys.executable, restart_delay=restart_delay, watch_seconds=0.2)
        created.append(sup)
        return sup

    yield factory
    for sup in created:
        sup._closed = True  # no automatic restart any more
        try:
            sup.stop(timeout=2)
        except (BotBusy, BotRefused, OSError):
            pass


def _write_settings(root, website):
    (root / "config" / "global_settings.json").write_text(json.dumps({"website": website}), encoding="utf-8")


def test_extra_arguments_are_a_whitelist():
    assert extra_args(None) == []
    assert extra_args(5) == ["--max-runtime", "5"]
    assert extra_args(1440) == ["--max-runtime", "1440"]
    for bad in (0, 1441, "5", 2.5, True):
        with pytest.raises(ValueError):
            extra_args(bad)


def test_settings_default_to_off_and_only_a_real_true_turns_them_on(tmp_path):
    (tmp_path / "config").mkdir()
    assert read_settings(tmp_path) == {"restartOnCrash": False, "startBotOnLaunch": False, "stopBotWithDashboard": False}
    _write_settings(tmp_path, {"restartOnCrash": "true", "startBotOnLaunch": True})
    settings = read_settings(tmp_path)
    assert settings["restartOnCrash"] is False and settings["startBotOnLaunch"] is True


def test_start_runs_refuses_a_second_start_and_stops_gracefully(project, make):
    sup = make()
    status = sup.start()
    assert status["state"] in ("starting", "running")
    assert status["supervised"] is True and status["pid"]
    pid = status["pid"]
    assert wait_for(lambda: (bot_state.read_pid() or {}).get("pid") == pid)  # the bot writes its pid file itself
    with pytest.raises(BotRefused) as refused:
        sup.start()
    assert refused.value.code == "already_running"
    began = time.monotonic()
    status = sup.stop(timeout=5)
    assert time.monotonic() - began < 5
    assert status["state"] == "stopped"
    assert status["last_exit"]["reason"] == "user_stop"
    assert bot_state.classify(pid) == "dead"
    assert not bot_state.PID_PATH.exists()


def test_a_busy_supervisor_answers_busy_for_every_action(project, make):
    sup = make()
    assert sup._action.acquire(blocking=False)
    try:
        with pytest.raises(BotBusy):
            sup.start()
        with pytest.raises(BotBusy):
            sup.stop()
        with pytest.raises(BotBusy):
            sup.restart()
    finally:
        sup._action.release()


def test_stop_terminates_a_bot_that_ignores_the_request(project, make, monkeypatch):
    monkeypatch.setenv("FAKE_BOT_MODE", "ignore_stop")
    sup = make()
    pid = sup.start()["pid"]
    status = sup.stop(timeout=1.0)
    assert status["state"] == "stopped"
    assert bot_state.classify(pid) == "dead"
    assert status["last_exit"]["reason"] == "user_stop"


def test_stop_kills_a_bot_that_ignores_terminate_and_leaves_no_orphan(project, make, monkeypatch):
    monkeypatch.setenv("FAKE_BOT_MODE", "hang")
    sup = make()
    pid = sup.start()["pid"]
    status = sup.stop(timeout=1.0)
    assert status["state"] == "stopped"
    assert bot_state.classify(pid) == "dead"
    assert status["last_exit"]["reason"] == "user_stop"


def test_restart_gives_a_new_process_and_a_new_run_id(project, make):
    sup = make()
    first = sup.start()["pid"]
    assert wait_for(lambda: (bot_state.read_pid() or {}).get("pid") == first)
    run_one = bot_state.read_pid()["run_id"]
    status = sup.restart()
    assert status["pid"] and status["pid"] != first
    assert wait_for(lambda: (bot_state.read_pid() or {}).get("pid") == status["pid"])
    assert bot_state.read_pid()["run_id"] != run_one
    sup.stop(timeout=5)


def test_a_crash_is_not_restarted_when_restart_on_crash_is_off(project, make, monkeypatch):
    monkeypatch.setenv("FAKE_BOT_MODE", "crash")
    sup = make()
    sup.start()
    assert wait_for(lambda: sup.status()["state"] == "crashed", timeout=15)
    last = sup.status()["last_exit"]
    assert last["reason"] == "crash" and last["code"] == 1
    time.sleep(2.5)
    assert sup.status()["pid"] is None  # not restarted


def test_a_crash_is_restarted_after_the_delay_and_at_most_three_times_an_hour(project, make, monkeypatch):
    _write_settings(project, {"restartOnCrash": True})
    monkeypatch.setenv("FAKE_BOT_MODE", "crash")
    sup = make(restart_delay=1.0)
    sup.start()
    assert wait_for(lambda: sup.status()["restarts_last_hour"] == 3 and sup.status()["state"] == "crashed", timeout=60)
    time.sleep(2.5)  # the fourth crash is not restarted
    status = sup.status()
    assert status["restarts_last_hour"] == 3
    assert status["state"] == "crashed" and status["pid"] is None


def test_an_auto_stop_is_never_restarted_even_with_restart_on_crash(project, make, monkeypatch):
    _write_settings(project, {"restartOnCrash": True})
    monkeypatch.setenv("FAKE_BOT_MODE", "autostop")
    sup = make()
    sup.start()
    assert wait_for(lambda: sup.status()["last_exit"] is not None, timeout=15)
    time.sleep(2.5)
    status = sup.status()
    assert status["pid"] is None
    assert status["last_exit"]["reason"] == "auto_stop"
    assert status["state"] == "stopped"


def test_a_stale_pid_file_with_a_dead_pid_is_replaced(project, make):
    import subprocess as sp

    gone = sp.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    bot_state.PID_PATH.write_text(json.dumps({"pid": gone.pid, "run_id": "old", "supervised": True}), encoding="utf-8")
    sup = make()
    assert sup.status()["state"] == "stopped"
    pid = sup.start()["pid"]
    assert pid != gone.pid
    sup.stop(timeout=5)


def test_a_pid_file_naming_a_live_non_python_process_is_stale_and_that_process_is_not_killed(project, make):
    if os.name == "nt":
        other_command = ["ping", "-n", "60", "127.0.0.1"]
    else:
        other_command = ["sleep", "60"]
    other = subprocess.Popen(other_command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        bot_state.PID_PATH.write_text(json.dumps({"pid": other.pid, "run_id": "old", "supervised": False}), encoding="utf-8")
        sup = make()
        status = sup.status()
        assert status["state"] == "stopped" and status["pid"] is None
        pid = sup.start()["pid"]
        assert pid != other.pid
        assert other.poll() is None  # the foreign process is never signalled
        sup.stop(timeout=5)
        assert other.poll() is None
    finally:
        other.kill()
        other.wait()


def test_a_bot_started_from_the_command_line_is_found_refused_and_stopped(project, make):
    env = {key: value for key, value in os.environ.items() if key not in ("DUSK_RUN_ID", "DUSK_SUPERVISED")}
    cli = subprocess.Popen(
        [sys.executable, "uwu.py"], cwd=str(project), env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        sup = make()
        assert wait_for(lambda: sup.status()["pid"] == cli.pid, timeout=20)
        assert sup.status()["supervised"] is False
        with pytest.raises(BotRefused) as refused:
            sup.start()
        assert refused.value.code == "already_running"
        status = sup.stop(timeout=8)
        assert status["state"] == "stopped"
        assert cli.wait(timeout=15) == 0
    finally:
        if cli.poll() is None:
            cli.kill()
            cli.wait()


def test_start_on_launch_follows_the_setting_and_never_starts_a_second_bot(project, make):
    sup = make()
    assert sup.start_on_launch() is None  # website.startBotOnLaunch is off by default
    _write_settings(project, {"startBotOnLaunch": True})
    status = sup.start_on_launch()
    assert status is not None and status["pid"]
    assert sup.start_on_launch() is None  # a bot runs already: nothing is started a second time
    sup.stop(timeout=5)


def test_a_dashboard_exit_leaves_the_bot_unless_stop_bot_with_dashboard_is_on(project, make):
    sup = make()
    pid = sup.start()["pid"]
    assert wait_for(lambda: (bot_state.read_pid() or {}).get("pid") == pid)
    sup.shutdown()  # the dashboard ends: by default the bot keeps running
    assert bot_state.classify(pid) == "bot"
    _write_settings(project, {"stopBotWithDashboard": True})
    nxt = make()  # the next dashboard finds the bot through bot.pid
    assert nxt.status()["pid"] == pid
    nxt.shutdown()  # with the setting on, the bot is stopped with the dashboard
    assert wait_for(lambda: bot_state.classify(pid) == "dead", timeout=20)
