"""Dashboard health rules must surface the captcha timeout flag.

`website/__init__.py` starts the Flask app, so the rules module is loaded from
its file instead of through the package.
"""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "health_rules_under_test", ROOT / "website" / "health_rules.py"
)
health_rules = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(health_rules)

CFG = health_rules.DEFAULTS
FLAG_REASON = "Captcha deadline passed: account is flagged"


def test_captcha_state_reports_the_flag():
    health, reasons = health_rules.evaluate(
        {"state": "captcha", "captcha_flag": True}, CFG
    )

    assert health == "bad"
    assert "Captcha is waiting to be solved" in reasons
    assert FLAG_REASON in reasons


def test_stopped_state_reports_the_flag():
    health, reasons = health_rules.evaluate(
        {
            "state": "stopped",
            "stopped_reason": "captcha not solved within 600s",
            "captcha_flag": True,
        },
        CFG,
    )

    assert health == "bad"
    assert any("captcha not solved within 600s" in reason for reason in reasons)
    assert FLAG_REASON in reasons


def test_captcha_state_without_the_flag_has_no_flag_reason():
    _health, reasons = health_rules.evaluate(
        {"state": "captcha", "captcha_flag": False}, CFG
    )

    assert reasons == ["Captcha is waiting to be solved"]
