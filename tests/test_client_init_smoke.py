"""MyClient must construct even with the captcha orchestrator aliased.

discord.py-self stores its own `captcha_handler` option on Client, so a
property with that name on MyClient breaks construction and therefore login.
"""

import core.client as client_module
from utils.loader import global_settings_dict


class StubOrchestrator:
    balance = 100

    def describe(self):
        return "stub"

    def balance_summary(self):
        return None


def _build(monkeypatch, **kwargs):
    stub = StubOrchestrator()
    monkeypatch.setattr(client_module, "captcha_orchestrator", stub)
    monkeypatch.setattr(client_module, "hcaptcha_solver", stub)
    bot = client_module.MyClient(
        "FAKE.TOKEN.VALUE",
        123456789,
        global_settings_dict,
        1,
        account_index=1,
        **kwargs,
    )
    return bot, stub


def test_myclient_init_sets_the_orchestrator(monkeypatch):
    bot, stub = _build(monkeypatch)

    # The __init__ tail ran: config, command handler and listeners are in place
    assert bot.misc
    assert bot.ch is not None
    assert bot.captcha_orchestrator is stub
    assert bot.hcaptcha_solver is stub
    # discord.py-self owns this attribute; it must stay the library's, not ours
    assert bot.captcha_handler is None


def test_myclient_accepts_discords_captcha_handler_option(monkeypatch):
    async def handler(captcha, client):
        return "token"

    bot, _stub = _build(monkeypatch, captcha_handler=handler)

    assert bot.captcha_handler is handler
