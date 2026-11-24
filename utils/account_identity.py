"""The identity of an account, known from tokens.txt before the account logs in.

A Discord user token starts with the user ID: the ID as ASCII digits, encoded in base64 (urlsafe, the padding often
left out). Only that first part is read here, offline. The token itself is never returned, logged or stored by this
module. An account whose token shows no user ID gets the stable key slot-<index> (index counted from 1 in the order of
tokens.txt), so it still has one key from the first write.
"""

import base64
import binascii

MIN_ID_LENGTH = 15
MAX_ID_LENGTH = 21


def user_id_from_token(token):
    """The numeric user ID a token starts with, as a string, or None when the token does not show one"""
    try:
        head = str(token).split(".", 1)[0]
        head += "=" * (-len(head) % 4)
        value = base64.urlsafe_b64decode(head.encode("ascii")).decode("ascii")
    except (binascii.Error, UnicodeError, ValueError, TypeError, AttributeError):
        return None
    if value.isdigit() and MIN_ID_LENGTH <= len(value) <= MAX_ID_LENGTH:
        return value
    return None


def slot_key(index):
    """The key of an account whose token shows no user ID: slot-<index>, index counted from 1"""
    return "slot-%d" % index


def expected_key(token, index):
    """The key the live status file uses for an account before it logs in: its user ID, else slot-<index>"""
    user_id = user_id_from_token(token)
    return user_id if user_id is not None else slot_key(index)
