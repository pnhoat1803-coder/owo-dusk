"""The shipped defaults are safe: no captcha provider and no webhook is switched on without its key or URL."""

import json
from pathlib import Path

import tomllib

CONFIG = Path(__file__).resolve().parent.parent / "config"


def _global():
    return json.loads((CONFIG / "global_settings.json").read_text(encoding="utf-8"))


def _captcha():
    with open(CONFIG / "captcha.toml", "rb") as handle:
        return tomllib.load(handle)


def test_captcha_providers_are_off_by_default():
    captcha = _captcha()
    assert captcha["hcaptcha_solver"]["captchaly"]["enabled"] is False
    assert captcha["hcaptcha_solver"]["yescaptcha"]["enabled"] is False


def test_the_image_captcha_solver_is_off_by_default():
    assert _captcha()["image_solver"]["enabled"] is False


def test_the_discord_webhook_is_off_by_default():
    assert _global()["webhook"]["enabled"] is False


def test_the_other_switches_keep_their_values():
    global_settings = _global()
    assert global_settings["levelCheck"]["enabled"] is True
    assert global_settings["website"]["enabled"] is True


def test_no_secret_value_is_set_in_the_shipped_config():
    global_settings = _global()
    assert global_settings["website"]["password"] == ""
    assert global_settings["ocrApi"] == ""
    assert global_settings["webhook"]["webhookUrl"] == ""
    assert _captcha()["hcaptcha_solver"]["captchaly"]["api_key"] == ""
    assert _captcha()["hcaptcha_solver"]["yescaptcha"]["api_key"] == ""


def _settings():
    return json.loads((CONFIG / "settings.json").read_text(encoding="utf-8"))


def _misc():
    return json.loads((CONFIG / "misc.json").read_text(encoding="utf-8"))


def _danger():
    with open(CONFIG / "danger.toml", "rb") as handle:
        return tomllib.load(handle)


def test_autoquest_is_off_and_no_account_is_allowed():
    autoquest = _settings()["autoQuest"]
    assert autoquest["enabled"] is False
    assert autoquest["helpOthers"] is False
    assert autoquest["enableCommandsToCompleteQuest"] is False
    danger = _danger()
    assert danger["allow_auto_quest"] is False
    assert danger["auto_quest_accounts"] == []


def test_the_level_first_check_delays_are_15_and_75_minutes():
    level = _global()["levelCheck"]
    assert level["firstCheckMinDelayMinutes"] == 15
    assert level["firstCheckMaxDelayMinutes"] == 75


def test_the_captcha_trace_is_off_by_default():
    assert _misc()["debug"]["captchaTrace"]["enabled"] is False


def test_the_discord_attachment_links_are_not_shipped():
    text = (CONFIG / "webhookContent.json").read_text(encoding="utf-8")
    assert "cdn.discordapp.com" "/attachments/" not in text
    assert "media.discordapp.net" "/attachments/" not in text


def test_the_auto_stop_timer_stays_on():
    assert _global()["autoStop"]["enabled"] is True


def test_the_dashboard_ships_with_no_password_and_the_lan_host():
    website = _global()["website"]
    assert website["password"] == ""  # an empty password always means 127.0.0.1 only, see utils/lan.py
    assert website["host"] == "0.0.0.0"


def test_the_quote_switches_are_as_shipped():
    settings = json.loads((CONFIG / "settings.json").read_text(encoding="utf-8"))
    grind = settings["commands"]["lvlGrind"]
    assert grind["enabled"] is True and grind["useQuoteInstead"] is True
    assert grind.get("quoteSource", "api") == "api"  # the bot calls the quotes API only when all three say so
    with open(CONFIG / "danger.toml", "rb") as handle:
        assert tomllib.load(handle)["allow_quotes"] is True
