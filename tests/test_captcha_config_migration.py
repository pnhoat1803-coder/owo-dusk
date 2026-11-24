"""Config parsing: provider list, legacy migration, clamping, disabled providers."""

import pytest

from utils.captcha_solver.base import BaseCaptchaProvider
from utils.captcha_solver.captchaly import CaptchalyProvider
from utils.captcha_solver.orchestrator import (
    CaptchaConfigError,
    build_orchestrator,
    normalise_providers,
)
from utils.captcha_solver.yescaptcha import YesCaptchaProvider

from captcha_fakes import FakeYesCaptchaClient

LEGACY_CONFIG = {
    "hcaptcha_solver": {"enabled": True, "api_key": "LEGACY_KEY", "retries": 20}
}


def _modern_config(*entries, **solver_keys):
    solver = {"enabled": True, "providers": list(entries)}
    solver.update(solver_keys)
    return {"hcaptcha_solver": solver}


def test_legacy_config_migrates_to_one_yescaptcha():
    entries, notes = normalise_providers(LEGACY_CONFIG)

    assert len(entries) == 1
    entry = entries[0]
    assert entry["name"] == "yescaptcha"
    assert entry["priority"] == 1
    assert entry["api_key"] == "LEGACY_KEY"
    # retries = 20 is clamped into 3..5
    assert entry["min_retries"] == 5
    assert entry["max_retries"] == 5
    assert any("deprecated" in note for note in notes)


def test_legacy_config_builds_a_yescaptcha_provider():
    orchestrator = build_orchestrator(
        LEGACY_CONFIG, client_factory=lambda key: FakeYesCaptchaClient()
    )

    assert [provider.name for provider in orchestrator.providers] == ["yescaptcha"]
    provider = orchestrator.providers[0]
    assert isinstance(provider, YesCaptchaProvider)
    assert provider.priority == 1
    assert provider.min_retries == 5 and provider.max_retries == 5
    assert provider.balance == 100


def test_legacy_retries_below_minimum_is_clamped_to_three():
    config = {"hcaptcha_solver": {"enabled": True, "api_key": "KEY", "retries": 1}}

    entries, _notes = normalise_providers(config)

    assert entries[0]["min_retries"] == 3
    assert entries[0]["max_retries"] == 3


def test_provider_list_is_ordered_by_priority():
    config = _modern_config(
        {
            "name": "yescaptcha",
            "enabled": True,
            "priority": 2,
            "api_key": "YES_KEY",
            "min_retries": 3,
            "max_retries": 5,
        },
        {
            "name": "captchaly",
            "enabled": True,
            "priority": 1,
            "api_key": "CP_KEY",
            "min_retries": 4,
            "max_retries": 5,
        },
    )

    orchestrator = build_orchestrator(
        config, client_factory=lambda key: FakeYesCaptchaClient()
    )

    assert [provider.name for provider in orchestrator.providers] == [
        "captchaly",
        "yescaptcha",
    ]
    assert [provider.priority for provider in orchestrator.providers] == [1, 2]
    assert orchestrator.describe() == "1:captchaly -> 2:yescaptcha"
    assert isinstance(orchestrator.providers[0], CaptchalyProvider)


def test_duplicate_priorities_are_separated():
    config = _modern_config(
        {"name": "captchaly", "enabled": True, "priority": 1, "api_key": "CP"},
        {"name": "yescaptcha", "enabled": True, "priority": 1, "api_key": "YES"},
    )

    entries, notes = normalise_providers(config)

    assert [entry["priority"] for entry in entries] == [1, 2]
    assert any("priority" in note for note in notes)


def test_disabled_provider_is_skipped():
    config = _modern_config(
        {"name": "captchaly", "enabled": False, "priority": 1, "api_key": "CP"},
        {"name": "yescaptcha", "enabled": True, "priority": 2, "api_key": "YES"},
    )

    entries, notes = normalise_providers(config)

    assert [entry["name"] for entry in entries] == ["yescaptcha"]
    assert any("disabled" in note for note in notes)


def test_unknown_provider_is_ignored():
    config = _modern_config(
        {"name": "someother", "enabled": True, "priority": 1, "api_key": "X"},
        {"name": "yescaptcha", "enabled": True, "priority": 2, "api_key": "YES"},
    )

    entries, notes = normalise_providers(config)

    assert [entry["name"] for entry in entries] == ["yescaptcha"]
    assert any("unknown provider" in note for note in notes)


def test_provider_table_form_is_accepted():
    """A [providers.captchaly] sub-table is equivalent to an array-of-tables entry."""
    config = {
        "hcaptcha_solver": {
            "enabled": True,
            "providers": {
                "captchaly": {
                    "enabled": True,
                    "priority": 1,
                    "api_key": "CP",
                    "min_retries": 3,
                    "max_retries": 4,
                }
            },
        }
    }

    entries, _notes = normalise_providers(config)

    assert entries[0]["name"] == "captchaly"
    assert entries[0]["max_retries"] == 4


def test_no_usable_provider_is_a_config_error():
    config = _modern_config(
        {"name": "captchaly", "enabled": False, "priority": 1, "api_key": ""}
    )

    with pytest.raises(CaptchaConfigError):
        normalise_providers(config)


def test_missing_hcaptcha_solver_table_is_a_config_error():
    with pytest.raises(CaptchaConfigError):
        normalise_providers({"image_solver": {"enabled": True}})


def test_zero_balance_provider_is_dropped():
    config = _modern_config(
        {"name": "captchaly", "enabled": True, "priority": 1, "api_key": "CP"},
        {"name": "yescaptcha", "enabled": True, "priority": 2, "api_key": "YES"},
    )

    orchestrator = build_orchestrator(
        config, client_factory=lambda key: FakeYesCaptchaClient(balance=0)
    )

    assert [provider.name for provider in orchestrator.providers] == ["captchaly"]
    assert any("no balance" in note for note in orchestrator.notes)


def test_zero_balance_on_the_only_provider_is_a_config_error():
    config = _modern_config(
        {"name": "yescaptcha", "enabled": True, "priority": 1, "api_key": "YES"}
    )

    with pytest.raises(CaptchaConfigError):
        build_orchestrator(
            config, client_factory=lambda key: FakeYesCaptchaClient(balance=0)
        )


def test_auto_detect_enterprise_flag_reaches_the_provider():
    config = _modern_config(
        {"name": "captchaly", "enabled": True, "priority": 1, "api_key": "CP"},
        auto_detect_enterprise=False,
        captcha_type="enterprise",
    )

    orchestrator = build_orchestrator(config)

    provider = orchestrator.providers[0]
    assert provider.auto_detect_enterprise is False
    assert provider.captcha_type == "enterprise"


def test_invalid_captcha_type_falls_back_to_standard():
    config = _modern_config(
        {"name": "captchaly", "enabled": True, "priority": 1, "api_key": "CP"},
        auto_detect_enterprise=False,
        captcha_type="nonsense",
    )

    orchestrator = build_orchestrator(config)

    assert orchestrator.providers[0].captcha_type == "standard"
    assert any("captcha_type" in note for note in orchestrator.notes)


def test_providers_are_base_captcha_providers():
    orchestrator = build_orchestrator(
        LEGACY_CONFIG, client_factory=lambda key: FakeYesCaptchaClient()
    )

    assert all(
        isinstance(provider, BaseCaptchaProvider)
        for provider in orchestrator.providers
    )


def _new_format_config():
    return {
        "hcaptcha_solver": {
            "auto_detect_enterprise": True,
            "captchaly": {
                "enabled": True,
                "priority": 1,
                "api_key": "CP_KEY",
                "min_retries": 3,
                "max_retries": 5,
            },
            "yescaptcha": {
                "enabled": True,
                "priority": 2,
                "api_key": "YES_KEY",
                "min_retries": 3,
                "max_retries": 5,
            },
        }
    }


def test_config_new_format_both_providers():
    entries, notes = normalise_providers(_new_format_config())

    assert [entry["name"] for entry in entries] == ["captchaly", "yescaptcha"]
    assert [entry["priority"] for entry in entries] == [1, 2]
    assert entries[0]["min_retries"] == 3
    assert entries[0]["max_retries"] == 5
    assert notes == []


def test_config_previous_array_format_still_works():
    entries, _notes = normalise_providers(
        _modern_config(
            {"name": "yescaptcha", "enabled": True, "priority": 2, "api_key": "YES"},
            {"name": "captchaly", "enabled": True, "priority": 1, "api_key": "CP"},
        )
    )

    assert [entry["name"] for entry in entries] == ["captchaly", "yescaptcha"]
    assert [entry["priority"] for entry in entries] == [1, 2]


def test_config_unknown_provider_name_is_rejected():
    entries, notes = normalise_providers(
        _modern_config(
            {"name": "thirdparty", "enabled": True, "priority": 1, "api_key": "X"},
            {"name": "yescaptcha", "enabled": True, "priority": 2, "api_key": "YES"},
        )
    )

    assert [entry["name"] for entry in entries] == ["yescaptcha"]
    assert any("thirdparty" in note and "rejected" in note for note in notes)


def test_config_unknown_new_table_is_rejected():
    config = _new_format_config()
    config["hcaptcha_solver"]["other"] = {
        "enabled": True,
        "priority": 3,
        "api_key": "X",
    }

    entries, notes = normalise_providers(config)

    assert [entry["name"] for entry in entries] == ["captchaly", "yescaptcha"]
    assert any("hcaptcha_solver.other" in note for note in notes)


def test_config_legacy_format_still_works():
    entries, notes = normalise_providers(LEGACY_CONFIG)

    assert [entry["name"] for entry in entries] == ["yescaptcha"]
    assert entries[0]["priority"] == 1
    assert entries[0]["min_retries"] == 5
    assert entries[0]["max_retries"] == 5
    assert any("deprecated" in note for note in notes)
