"""The account identity known from tokens.txt before login (utils/account_identity.py). Fake tokens only, no network."""

import base64
import inspect

import utils.account_identity as account_identity

FAKE_TAIL = ".AAAAAA.FAKE_TOKEN_MARKER_TEST"


def _token(user_id, padded=True):
    head = base64.urlsafe_b64encode(str(user_id).encode("ascii")).decode("ascii")
    if not padded:
        head = head.rstrip("=")
    return head + FAKE_TAIL


def test_user_id_is_read_with_and_without_base64_padding():
    for user_id in ("123456789012345678", "12345678901234567", "100000000000000002", "100000000000000001"):
        assert account_identity.user_id_from_token(_token(user_id, padded=True)) == user_id
        assert account_identity.user_id_from_token(_token(user_id, padded=False)) == user_id


def test_garbage_empty_and_non_digit_payloads_give_none():
    assert account_identity.user_id_from_token("") is None
    assert account_identity.user_id_from_token("not a token") is None
    assert account_identity.user_id_from_token(None) is None
    # Decodes, but is not digits
    assert account_identity.user_id_from_token(_token("abcdefghijklmnopq")) is None
    # Digits, but too short (14) or too long (22) to be a Discord user ID
    assert account_identity.user_id_from_token(_token("12345678901234")) is None
    assert account_identity.user_id_from_token(_token("1234567890123456789012")) is None


def test_expected_key_is_the_id_or_the_stable_slot():
    assert account_identity.expected_key(_token("123456789012345678"), 7) == "123456789012345678"
    assert account_identity.expected_key("garbage", 7) == "slot-7"
    assert account_identity.slot_key(3) == "slot-3"


def test_the_module_never_logs_or_prints():
    source = inspect.getsource(account_identity)
    assert "import logging" not in source
    assert "print(" not in source
    assert "log." not in source
