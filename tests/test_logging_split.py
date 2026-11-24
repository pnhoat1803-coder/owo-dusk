"""Two logging sinks: compact console, verbose file, per-episode captcha trace."""

import asyncio
import re
import tokenize
from types import SimpleNamespace

import pytest

import utils.run_log as run_log
from core.client import MyClient

PROVIDERS = [
    "utils/captcha_solver/captchaly.py",
    "utils/captcha_solver/yescaptcha.py",
    "utils/captcha_solver/orchestrator.py",
    "utils/captcha_solver/owo_verify.py",
]

TRACE = {
    "enabled": True,
    "level": "DEBUG",
    "include_dm_raw": False,
    "max_bytes": 1024 * 1024,
    "max_files": 5,
}
NO_TRACE = dict(TRACE, enabled=False)


class ConsoleRecorder:
    def __init__(self):
        self.lines = []

    def __call__(self, *args, **kwargs):
        self.lines.append(str(args[0]) if args else "")

    @property
    def text(self):
        return "\n".join(self.lines)


@pytest.fixture
def make_runlog(tmp_path, monkeypatch):
    made = []

    def build(trace=None, console_level="INFO", file_level="DEBUG"):
        rl = run_log.RunLog(
            tmp_path / "logs",
            10 * 1024 * 1024,
            5,
            [],
            console_level=console_level,
            file_level=file_level,
            trace_settings=trace if trace is not None else dict(NO_TRACE),
        )
        monkeypatch.setattr(run_log, "_runlog", rl)
        monkeypatch.setattr(run_log, "_accounts", {})
        made.append(rl)
        return rl

    yield build

    for rl in made:
        rl.trace.finish_all()
        rl.captcha.close()
        rl.close()


@pytest.fixture
def console(monkeypatch):
    import utils.system as syst

    recorder = ConsoleRecorder()
    monkeypatch.setattr(syst.system.console, "print", recorder)
    return recorder


def fake_client():
    return SimpleNamespace(username="tester", misc={"debug": {"enabled": True}})


def emit(client, text, **kwargs):
    kwargs.setdefault("web_log", False)
    kwargs.setdefault("webhook_useless_log", False)
    asyncio.run(MyClient.log(client, text, **kwargs))


def test_console_filters_debug(make_runlog, console):
    rl = make_runlog()
    emit(fake_client(), "debug-only-line", level="DEBUG")
    emit(fake_client(), "info-only-line", level="INFO")
    rl.drain()
    rl.flush_file()
    assert "debug-only-line" not in console.text
    assert "info-only-line" in console.text
    filed = rl.path.read_text(encoding="utf-8")
    assert "debug-only-line" in filed
    assert "info-only-line" in filed


def test_file_includes_debug_with_full_format(make_runlog, console):
    rl = make_runlog()
    emit(fake_client(), "debug-format-line", level="DEBUG")
    rl.drain()
    rl.flush_file()
    lines = [
        line
        for line in rl.path.read_text(encoding="utf-8").splitlines()
        if "debug-format-line" in line
    ]
    assert len(lines) == 1
    assert re.match(
        r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \| tester \| .*debug-format-line", lines[0]
    )


def test_trace_category_gating(make_runlog, console):
    rl = make_runlog(trace=dict(TRACE))
    run_log.bind_account("acct")
    run_log.captcha_start()
    run_log.write("general-debug", level="DEBUG", category=None)
    run_log.write("[captcha] captcha-debug", level="DEBUG", category="captcha")
    run_log.captcha_finish("RESOLVED", "test")

    rl.drain()
    rl.flush_file()
    traces = list((rl.folder / "captcha").glob("*_trace.txt"))
    assert len(traces) == 1
    trace_text = traces[0].read_text(encoding="utf-8")
    main = rl.path.read_text(encoding="utf-8")
    assert "general-debug" in main
    assert "general-debug" not in trace_text
    assert "captcha-debug" in main
    assert "captcha-debug" in trace_text


def test_console_redaction(make_runlog, console):
    rl = make_runlog()
    emit(fake_client(), "key CP_TESTABCDEFGHIJ1234 end", level="INFO")
    rl.drain()
    rl.flush_file()
    assert "CP_TESTABCDEFGHIJ1234" not in console.text
    assert "***" in console.text
    filed = rl.path.read_text(encoding="utf-8")
    assert "CP_TESTABCDEFGHIJ1234" not in filed
    assert "***" in filed


def test_trace_rotation(make_runlog):
    rl = make_runlog(trace=dict(TRACE, max_bytes=100, max_files=3))
    run_log.bind_account("acct")
    run_log.captcha_start()
    for index in range(30):
        run_log.write(
            f"[captcha] poll iteration {index}", level="DEBUG", category="captcha"
        )
    run_log.captcha_finish("RESOLVED", "rotate")

    rl.drain()
    rl.flush_file()
    traces = list((rl.folder / "captcha").glob("*_trace*.txt"))
    assert any(path.name.endswith("_trace.1.txt") for path in traces)
    assert len(traces) <= 3


def test_trace_disabled(make_runlog, console):
    rl = make_runlog(trace=dict(NO_TRACE))
    run_log.bind_account("acct")
    run_log.captcha_start()
    run_log.write("[captcha] no-trace-line", level="DEBUG", category="captcha")
    run_log.captcha_finish("RESOLVED", "off")

    rl.drain()
    rl.flush_file()
    assert not list((rl.folder / "captcha").glob("*_trace*"))
    assert "no-trace-line" in rl.path.read_text(encoding="utf-8")


def test_invalid_level_fallback(capfd):
    settings = run_log.read_settings(
        {"debug": {"logInTextFile": True, "consoleLevel": "WAT"}}
    )
    assert settings["console_level"] == "INFO"
    assert settings["file_level"] == "DEBUG"
    err = capfd.readouterr().err
    assert "consoleLevel" in err
    assert err.count("is not a log level") == 1


def test_resolved_file_format_unchanged(tmp_path):
    log = run_log.CaptchaLog(tmp_path / "captcha", "20260101_000000", [])
    log.start(1, "acct")
    log.collect(1, ["line one\n", "line two\n"])
    log.finish(1, "RESOLVED", "ok")
    log.close()
    path = tmp_path / "captcha" / "run_20260101_000000_RESOLVED.txt"
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0].startswith("===== acct | ")
    assert "| RESOLVED | ok =====" in lines[0]
    assert "line one" in text
    assert "line two" in text
    assert not lines[0].startswith("{")
    assert " | INFO | " not in text


def test_bot_log_signature_backward_compatible(make_runlog, console):
    rl = make_runlog()
    asyncio.run(
        MyClient.log(
            fake_client(), "pos-text", "#ffffff", False, False, False, None, None, False
        )
    )
    rl.drain()
    rl.flush_file()
    assert "pos-text" in console.text
    assert "pos-text" in rl.path.read_text(encoding="utf-8")


def test_provider_print_replaced():
    for path in PROVIDERS:
        with open(path, "rb") as handle:
            tokens = list(tokenize.tokenize(handle.readline))
        for index, token in enumerate(tokens):
            if token.type == tokenize.NAME and token.string == "print":
                following = tokens[index + 1] if index + 1 < len(tokens) else None
                assert not (
                    following is not None
                    and following.type == tokenize.OP
                    and following.string == "("
                ), f"print() left in {path}"
