"""Run log redaction: a secret is masked by value and by the shape it takes."""

from utils.run_log import REDACTED, _redact


def test_bearer_header_is_masked_without_knowing_the_value():
    line = "Authorization: Bearer CP_abcdef123456"

    assert _redact(line, []) == f"Authorization: Bearer {REDACTED}"


def test_query_parameter_is_masked_without_knowing_the_value():
    line = "GET https://v1.captchaly.com/hcaptcha?sitekey=x&apikey=FAKEtestkey123"

    assert _redact(line, []) == (
        f"GET https://v1.captchaly.com/hcaptcha?sitekey=x&apikey={REDACTED}"
    )


def test_json_api_key_is_masked_without_knowing_the_value():
    line = '{"api_key": "CP_abcdef123456", "enabled": true}'

    assert _redact(line, []) == f'{{"api_key": "{REDACTED}", "enabled": true}}'


def test_known_secret_value_is_masked_everywhere():
    line = "provider=captchaly key CP_abcdef123456 was rejected"

    assert _redact(line, ["CP_abcdef123456"]) == (
        f"provider=captchaly key {REDACTED} was rejected"
    )


def test_discord_token_and_webhook_are_still_masked():
    line = "token FAKE0000000000000000000000.FAKEXX.FAKE_TOKEN_FOR_TESTS_ONLY_xxxxxxxx"

    assert REDACTED in _redact(line, [])


def test_plain_text_is_left_alone():
    line = "Captcha detected! - 12:00:01"

    assert _redact(line, []) == line
