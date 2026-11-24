"""Importing core.client must not raise and must not start the database worker.

An import-time failure happens before the first log line, so it is the worst
place for a regression. The database worker is lazy for the same reason: its
thread keeps the interpreter alive at shutdown.
"""

import importlib

import core.database.database as database_module


def test_core_client_imports():
    module = importlib.import_module("core.client")

    assert module.MyClient is not None
    assert module.captcha_orchestrator is None
    assert module.hcaptcha_solver is None


def test_core_client_import_starts_no_database_worker():
    importlib.import_module("core.client")

    assert database_module.database_handler.started is False
