# This file is part of owo-dusk.
#
# Copyright (c) 2024-present EchoQuill
#
# Portions of this file are based on code by EchoQuill, licensed under the
# GNU General Public License v3.0 (GPL-3.0).
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.


def validate_cooldown(cd: list):
    """
    Checks if the cooldown provided is a valid cooldown list or not
    If not valid, the function raises ValueError
    """
    if not cd or not isinstance(cd, list):
        raise ValueError("Expected `list` datatype for cooldown.")

    if len(cd) != 2:
        # List has either less than or more than 2 elements
        raise ValueError("Expected exactly two elements in the cooldown list.")

    if cd[0] > cd[1]:
        raise ValueError("Max cooldown must be greater than min.")

    if cd[0] == cd[1]:
        # Should we consider leniency here?
        raise ValueError("Both min and max cooldown same..")


def validate_frequency(freq):
    """
    Ensures frequency is within 0 to 100
    If not valid, the function raises ValueError
    """
    if not 0 <= freq <= 100:
        raise ValueError("Invalid frequency: must be between 0 and 100")


def validate_stop_scope(scope: str):
    """
    Ensures stopScope is "account" or "process"
    If not valid, the function raises ValueError
    """
    if scope not in ("account", "process"):
        raise ValueError(
            f"Invalid stopScope {scope!r}: must be \"account\" or \"process\""
        )


def validate_ids(li: list):
    """
    Ensures all Ids stored are numbers
    """
    for item in li:
        try:
            # Try Convert to Integer
            int(item)
        except Exception:
            return False
    return True


def validate_seconds(value, key: str, default: int) -> float:
    """Return a positive finite duration, warning once when a setting is invalid."""
    import logging
    import math

    try:
        if isinstance(value, bool):
            raise ValueError("boolean is not a duration")
        parsed = float(value)
        if not math.isfinite(parsed) or parsed <= 0:
            raise ValueError("duration must be positive and finite")
    except (TypeError, ValueError, OverflowError):
        logging.getLogger(__name__).warning(
            "Invalid account.%s; using default %s seconds", key, default
        )
        return float(default)

    return int(parsed) if parsed.is_integer() else parsed


def validate_positive_seconds(value, label: str, default: float) -> float:
    """Return a positive finite duration in seconds, warning once when a setting is invalid."""
    import logging
    import math

    try:
        if isinstance(value, bool):
            raise ValueError("boolean is not a duration")
        parsed = float(value)
        if not math.isfinite(parsed) or parsed <= 0:
            raise ValueError("duration must be positive and finite")
    except (TypeError, ValueError, OverflowError):
        logging.getLogger(__name__).warning(
            "Invalid %s; using default %s seconds", label, default
        )
        return float(default)

    return int(parsed) if parsed.is_integer() else parsed


def validate_positive_count(value, label: str, default: int) -> int:
    """Return a positive whole number, warning once when a setting is invalid."""
    import logging

    try:
        if isinstance(value, bool):
            raise ValueError("boolean is not a count")
        parsed = int(value)
        if parsed <= 0 or parsed != float(value):
            raise ValueError("count must be a positive whole number")
    except (TypeError, ValueError, OverflowError):
        logging.getLogger(__name__).warning(
            "Invalid %s; using default %s", label, default
        )
        return int(default)

    return parsed


def validate_seconds_or_off(value, label: str, default: float) -> float:
    """Return a duration in seconds, where 0 means off. Warns once when a setting is invalid."""
    import logging
    import math

    try:
        if isinstance(value, bool):
            raise ValueError("boolean is not a duration")
        parsed = float(value)
        if not math.isfinite(parsed) or parsed < 0:
            raise ValueError("duration must be zero or positive and finite")
    except (TypeError, ValueError, OverflowError):
        logging.getLogger(__name__).warning(
            "Invalid %s; using default %s seconds", label, default
        )
        return float(default)

    return int(parsed) if parsed.is_integer() else parsed


def validate_count_or_zero(value, label: str, default: int) -> int:
    """Return a whole number of zero or more. Warns once when a setting is invalid."""
    import logging

    try:
        if isinstance(value, bool):
            raise ValueError("boolean is not a count")
        parsed = int(value)
        if parsed < 0 or parsed != float(value):
            raise ValueError("count must be zero or a positive whole number")
    except (TypeError, ValueError, OverflowError):
        logging.getLogger(__name__).warning(
            "Invalid %s; using default %s", label, default
        )
        return int(default)

    return parsed


def validate_megabytes(value, key: str, default: int) -> float:
    """Return a positive finite size in MB, warning once when a setting is invalid."""
    import logging
    import math

    try:
        if isinstance(value, bool):
            raise ValueError("boolean is not a size")
        parsed = float(value)
        if not math.isfinite(parsed) or parsed <= 0:
            raise ValueError("size must be positive and finite")
    except (TypeError, ValueError, OverflowError):
        logging.getLogger(__name__).warning(
            "Invalid debug.%s; using default %s MB", key, default
        )
        return float(default)

    return parsed


def validate_flag(value, key: str, default: bool) -> bool:
    """Return a true or false setting, warning once when the value is not one."""
    import logging

    if isinstance(value, bool):
        return value
    logging.getLogger(__name__).warning("Invalid %s; using default %s", key, default)
    return bool(default)


def validate_whole_number(value, label: str, default: int, minimum: int, maximum: int) -> int:
    """Return a whole number from minimum to maximum, warning once when a setting is invalid."""
    import logging

    try:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("not a number")
        if isinstance(value, float) and not value.is_integer():
            raise ValueError("not a whole number")
        parsed = int(value)
        if not minimum <= parsed <= maximum:
            raise ValueError(f"must be from {minimum} to {maximum}")
    except ValueError:
        logging.getLogger(__name__).warning(
            "Invalid %s %r; using default %s", label, value, default
        )
        return int(default)

    return parsed
