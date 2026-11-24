"""Every account uses config/settings.json. A leftover <discord-id>.settings.json is never read, because per-account
settings were removed. Runs the real MyClient.update_config on a fake account in a temp folder: no Discord, no network.
"""

import asyncio
import json
import types

import utils.config_models.configs as configs
from core.client import MyClient

FAKE_ID = 424242424242
DEFAULT = {"autoDaily": True, "sleep": {"frequencyPercentage": 5}, "commands": {"hunt": {"enabled": True}}}
OVERRIDE = {"autoDaily": False, "sleep": {"frequencyPercentage": 99}, "commands": {"hunt": {"enabled": False}}}


class FakeAccount:
    """Only what update_config touches: the lock, the user id, the settings slot and start_cogs"""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.user = types.SimpleNamespace(id=FAKE_ID)
        self.settings_dict = None
        self.cogs_started = False

    async def start_cogs(self):
        self.cogs_started = True


def test_account_reads_the_default_settings_even_with_an_override_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "config"
    config.mkdir()
    (config / "settings.json").write_text(json.dumps(DEFAULT), encoding="utf-8")
    (config / f"{FAKE_ID}.settings.json").write_text(json.dumps(OVERRIDE), encoding="utf-8")

    seen = []

    def fake_fetch(raw):
        seen.append(raw)
        return types.SimpleNamespace(raw=raw)

    monkeypatch.setattr(configs, "FetchSettings", fake_fetch)

    account = FakeAccount()
    asyncio.run(MyClient.update_config(account))

    assert seen == [DEFAULT]
    assert account.settings_dict.raw == DEFAULT
    assert account.cogs_started is True


def test_account_without_a_default_file_fails_instead_of_using_an_override(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "config"
    config.mkdir()
    (config / f"{FAKE_ID}.settings.json").write_text(json.dumps(OVERRIDE), encoding="utf-8")

    account = FakeAccount()
    try:
        asyncio.run(MyClient.update_config(account))
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("update_config must not fall back to the per-account file")
    assert account.settings_dict is None
    assert account.cogs_started is False
