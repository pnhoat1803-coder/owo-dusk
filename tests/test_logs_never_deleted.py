"""Log cleanup never deletes the wrong files (utils/log_paths.py, and every function that deletes or rotates logs).

Each test builds its own folder under a temp directory, runs one cleanup or rotation, and checks which files are left.
The bot's log folder is a temp folder for every test (tests/conftest.py), so nothing here touches the real logs/ folder.
"""

import logging
import os
import types

import pytest
from flask import Flask

from core import level_check
from utils import log_paths, run_log
from website import api_v2, bot_supervisor


def make_runs(folder, count):
    """count run files, oldest first (the time stamp in the name is what orders them)"""
    names = []
    for index in range(count):
        name = "run_2026-01-01_00-00-%02d.txt" % index
        (folder / name).write_text("run %d\n" % index, encoding="utf-8")
        names.append(name)
    return names


def names_in(folder):
    return {path.name for path in folder.iterdir()}


def test_thirty_mixed_files_only_the_oldest_run_files_go(tmp_path):
    folder = tmp_path / "logs"
    folder.mkdir()
    runs = make_runs(folder, 24)
    others = [
        "bot_supervised.log",
        "dashboard_actions.log",
        "gateway_stats_2026-01-01_00-00-00.txt",
        "reply_trace_20260101_000000.txt",
        "notes_keep_me.txt",
        "README.md",
    ]
    for name in others:
        (folder / name).write_text("keep\n", encoding="utf-8")
    assert len(names_in(folder)) == 30

    # The prune of a real RunLog, run once. Its threads are not started: only the prune is called
    run_log_object = object.__new__(run_log.RunLog)
    run_log_object.folder = folder
    run_log_object.keep = 20
    run_log_object._prune()

    assert names_in(folder) == set(runs[4:]) | set(others)  # the 4 oldest run files went, nothing else

    # Every non-run name is refused by the deletion helper when asked for it
    for name in others:
        assert log_paths.safe_unlink(folder / name, folder, "run_*.txt") is False
    assert names_in(folder) == set(runs[4:]) | set(others)


def test_keep_zero_still_keeps_the_newest_file(tmp_path):
    folder = tmp_path / "logs"
    folder.mkdir()
    runs = make_runs(folder, 3)
    removed = log_paths.remove_old(folder, "run_*.txt", keep=0)
    assert [path.name for path in removed] == runs[:2]
    assert names_in(folder) == {runs[2]}


def test_without_a_keep_value_the_newest_twenty_stay(tmp_path):
    folder = tmp_path / "logs"
    folder.mkdir()
    runs = make_runs(folder, 22)
    removed = log_paths.remove_old(folder, "run_*.txt")
    assert [path.name for path in removed] == runs[:2]
    assert names_in(folder) == set(runs[2:])


def test_a_folder_with_a_run_name_is_not_deleted(tmp_path):
    folder = tmp_path / "logs"
    folder.mkdir()
    (folder / "run_2025-12-31_23-59-59.txt").mkdir()  # a folder with a log name, older than every run file
    runs = make_runs(folder, 21)
    removed = log_paths.remove_old(folder, "run_*.txt", keep=20)
    assert [path.name for path in removed] == [runs[0]]  # only the oldest plain file goes
    assert (folder / "run_2025-12-31_23-59-59.txt").is_dir()
    assert names_in(folder) == set(runs[1:]) | {"run_2025-12-31_23-59-59.txt"}


def test_captcha_trace_rotation_removes_only_its_oldest_part(tmp_path):
    folder = tmp_path / "logs" / "captcha"
    folder.mkdir(parents=True)
    active = folder / "acct_20260101_000000_trace.txt"
    active.write_text("x" * 200, encoding="utf-8")
    (folder / "acct_20260101_000000_trace.1.txt").write_text("part one\n", encoding="utf-8")
    (folder / "acct_20260101_000000_trace.2.txt").write_text("part two, the oldest\n", encoding="utf-8")
    (folder / "run_20260101_000000_RESOLVED.txt").write_text("resolved\n", encoding="utf-8")
    (folder / "notes.txt").write_text("notes\n", encoding="utf-8")

    trace = run_log.CaptchaTrace(folder, {"enabled": True, "max_files": 3, "max_bytes": 100}, [])
    state = {"handle": open(active, "ab"), "path": active, "size": 200}
    trace._rotate(state)
    state["handle"].close()

    assert (folder / "acct_20260101_000000_trace.2.txt").read_text(encoding="utf-8") == "part one\n"
    assert (folder / "acct_20260101_000000_trace.1.txt").read_text(encoding="utf-8") == "x" * 200
    assert active.read_text(encoding="utf-8") == ""  # the active trace starts again, empty
    assert (folder / "run_20260101_000000_RESOLVED.txt").read_text(encoding="utf-8") == "resolved\n"
    assert (folder / "notes.txt").read_text(encoding="utf-8") == "notes\n"
    assert names_in(folder) == {
        "acct_20260101_000000_trace.txt",
        "acct_20260101_000000_trace.1.txt",
        "acct_20260101_000000_trace.2.txt",
        "run_20260101_000000_RESOLVED.txt",
        "notes.txt",
    }


def test_supervisor_log_rotation_replaces_only_its_one_old_part(tmp_path, monkeypatch):
    folder = tmp_path / "logs"
    folder.mkdir()
    log = folder / "bot_supervised.log"
    log.write_text("new run output\n" * 4, encoding="utf-8")
    (folder / "bot_supervised.log.1").write_text("previous part\n", encoding="utf-8")
    (folder / "bot_supervised.log.2").write_text("older part, untouched\n", encoding="utf-8")
    monkeypatch.setattr(bot_supervisor, "LOG_ROTATE_BYTES", 10)

    bot_supervisor._rotate_log(log)

    assert not log.exists()
    assert (folder / "bot_supervised.log.1").read_text(encoding="utf-8") == "new run output\n" * 4
    assert (folder / "bot_supervised.log.2").read_text(encoding="utf-8") == "older part, untouched\n"


def test_dashboard_action_log_rotation_keeps_one_part(tmp_path, monkeypatch):
    folder = log_paths.log_dir()  # this test's temp folder (DUSK_LOG_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    action = folder / "dashboard_actions.log"
    action.write_text("old line\n" * 20, encoding="utf-8")
    (folder / "dashboard_actions.log.1").write_text("older part\n", encoding="utf-8")
    (folder / "other_keep.txt").write_text("keep\n", encoding="utf-8")
    monkeypatch.setattr(api_v2, "ACTIONS_LOG_ROTATE_BYTES", 10)
    monkeypatch.setattr(api_v2, "store", types.SimpleNamespace(root=str(tmp_path)))

    with Flask("log-rotation-test").test_request_context("/"):
        api_v2._log_action("start", "ok")

    assert (folder / "dashboard_actions.log.1").read_text(encoding="utf-8") == "old line\n" * 20
    assert (folder / "other_keep.txt").read_text(encoding="utf-8") == "keep\n"
    assert (folder / "dashboard_actions.log").read_text(encoding="utf-8").endswith(" | bot start | ok\n")


def test_level_debug_log_rotation_keeps_one_old_file(tmp_path):
    folder = tmp_path / "utils" / "data"
    folder.mkdir(parents=True)
    path = folder / "level_debug.jsonl"
    path.write_text('{"old":"' + "x" * 50 + '"}\n', encoding="utf-8")
    (folder / "level_debug.jsonl.1").write_text("older\n", encoding="utf-8")
    (folder / "keep.json").write_text("keep\n", encoding="utf-8")

    level_check.DebugLog(path=path, max_bytes=20).write({"n": 1})

    assert (folder / "level_debug.jsonl.1").read_text(encoding="utf-8").startswith('{"old":')
    assert (folder / "keep.json").read_text(encoding="utf-8") == "keep\n"
    assert path.read_text(encoding="utf-8").strip() == '{"n":1}'


def test_files_outside_the_folder_or_with_other_names_are_refused(tmp_path, caplog):
    folder = tmp_path / "logs"
    folder.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "run_2026-01-01_00-00-00.txt"
    victim.write_text("keep me\n", encoding="utf-8")
    inside = folder / "run_2026-01-01_00-00-01.txt"
    inside.write_text("keep me too\n", encoding="utf-8")
    other = folder / "bot_supervised.log"
    other.write_text("keep me as well\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="utils.log_paths"):
        assert log_paths.safe_unlink(victim, folder, "run_*.txt") is False
        assert log_paths.safe_unlink(other, folder, "run_*.txt") is False
        assert log_paths.safe_rename(inside, outside / "moved.txt", folder, "moved.txt") is False
        assert log_paths.rotate_to_part(outside / "missing.log", folder) is False

    assert victim.exists() and inside.exists() and other.exists()
    assert "refused" in caplog.text


def test_links_are_never_deleted(tmp_path):
    folder = tmp_path / "logs"
    folder.mkdir()
    target = tmp_path / "outside.txt"
    target.write_text("keep\n", encoding="utf-8")
    link = folder / "run_2026-01-01_00-00-00.txt"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("creating a link needs a permission this machine does not give")

    assert log_paths.safe_unlink(link, folder, "run_*.txt") is False
    assert log_paths.remove_old(folder, "run_*.txt", keep=0) == []
    assert target.read_text(encoding="utf-8") == "keep\n"


def test_during_tests_the_bot_log_folder_is_a_temp_folder(tmp_path):
    folder = log_paths.log_dir()
    assert folder == (tmp_path / "dusk_logs").resolve()  # set by tests/conftest.py for this test
    assert tmp_path.resolve() in folder.parents
    assert folder != (log_paths.PROJECT_ROOT / "logs").resolve()
    assert os.environ[log_paths.ENV] == str(tmp_path / "dusk_logs")


def test_without_the_variable_the_logs_stay_next_to_the_project(tmp_path, monkeypatch):
    monkeypatch.delenv(log_paths.ENV, raising=False)
    assert log_paths.log_dir() == log_paths.PROJECT_ROOT / "logs"
    assert log_paths.log_file("bot_supervised.log", root=tmp_path) == tmp_path / "logs" / "bot_supervised.log"
