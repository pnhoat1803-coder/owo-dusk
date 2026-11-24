import json
import os
import tempfile
import threading
import time
from datetime import date, datetime, time as day_time, timedelta

from utils.colors import COLORS
from utils.errors import suppress_and_log

path = "utils/data/weekly_runtime.json"
RUNTIME_SCHEMA_VERSION = 2
RUNTIME_DAYS = 7


def _day_key(day):
    return day.isoformat()


def _new_weekly_runtime(now=None):
    now = time.time() if now is None else float(now)
    today = datetime.fromtimestamp(now).date()
    days = {
        _day_key(today - timedelta(days=offset)): 0.0
        for offset in range(RUNTIME_DAYS - 1, -1, -1)
    }
    return {"schema_version": RUNTIME_SCHEMA_VERSION, "days": days, "last_update": now}


def _trim_weekly_runtime(data, now=None):
    now = time.time() if now is None else float(now)
    today = datetime.fromtimestamp(now).date()
    first_day = today - timedelta(days=RUNTIME_DAYS - 1)
    days = data.setdefault("days", {})
    for key in list(days):
        try:
            if date.fromisoformat(key) < first_day or date.fromisoformat(key) > today:
                del days[key]
        except (TypeError, ValueError):
            del days[key]
    for offset in range(RUNTIME_DAYS - 1, -1, -1):
        days.setdefault(_day_key(today - timedelta(days=offset)), 0.0)
    data["schema_version"] = RUNTIME_SCHEMA_VERSION
    return data


def _default_weekly_runtime():
    return _new_weekly_runtime()


def accrue_weekly_runtime(data, now=None):
    """Add elapsed process time to local calendar days, splitting at midnight."""
    now = time.time() if now is None else float(now)
    if not isinstance(data, dict) or data.get("schema_version") != RUNTIME_SCHEMA_VERSION:
        data = _new_weekly_runtime(now)
    data = _trim_weekly_runtime(data, now)
    try:
        cursor = float(data.get("last_update", now))
    except (TypeError, ValueError):
        cursor = now
    if cursor > now:
        cursor = now
    while cursor < now:
        current_day = datetime.fromtimestamp(cursor).date()
        next_midnight = datetime.combine(
            current_day + timedelta(days=1), day_time.min
        ).timestamp()
        segment_end = min(now, next_midnight)
        if segment_end <= cursor:
            segment_end = now
        key = _day_key(current_day)
        data["days"][key] = float(data["days"].get(key, 0)) + (segment_end - cursor)
        cursor = segment_end
    data["last_update"] = now
    return _trim_weekly_runtime(data, now)


def reset_runtime_clock(data, now=None):
    """Anchor a new bot run now so time while stopped is never counted."""
    now = time.time() if now is None else float(now)
    if not isinstance(data, dict) or data.get("schema_version") != RUNTIME_SCHEMA_VERSION:
        data = _new_weekly_runtime(now)
    data = _trim_weekly_runtime(data, now)
    data["last_update"] = now
    return data


def _replace_weekly_runtime_file(tmp_path, path):
    for attempt in range(3):
        try:
            os.replace(tmp_path, path)
            return True
        except PermissionError as error:
            if attempt < 2:
                time.sleep(0.1)
                continue
            print(
                f"{COLORS.BOLD_YELLOW}Weekly runtime data could not be replaced "
                f"({type(error).__name__}), skipping this write.{COLORS.RESET}"
            )
            return False


def _write_weekly_runtime(weekly_runtime_dict, path):
    try:
        target_dir = os.path.dirname(path)
        tmp_file = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target_dir, delete=False
        )
        tmp_path = tmp_file.name
        try:
            json.dump(weekly_runtime_dict, tmp_file, indent=4)
            tmp_file.close()
            _replace_weekly_runtime_file(tmp_path, path)
        finally:
            tmp_file.close()
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
    except OSError:
        pass
    except Exception as error:
        print(f"{COLORS.BOLD_YELLOW}Weekly runtime error: {error} {COLORS.RESET}")


def _read_weekly_runtime(path):
    with open(path, "r", encoding="utf-8") as config_file:
        return json.load(config_file)


def load_weekly_runtime(path="utils/data/weekly_runtime.json") -> dict:
    # Dashboard reads are deliberately non-writing.
    try:
        return _read_weekly_runtime(path)
    except (json.JSONDecodeError, OSError) as error:
        print(
            f"{COLORS.BOLD_YELLOW}Weekly runtime data could not be read "
            f"({type(error).__name__}), using defaults for this read.{COLORS.RESET}"
        )
        return _default_weekly_runtime()


def _load_for_update(path):
    try:
        data = _read_weekly_runtime(path)
    except FileNotFoundError:
        return _default_weekly_runtime()
    except json.JSONDecodeError:
        try:
            os.replace(path, f"{path}.corrupt")
        except OSError:
            return None
        print(
            f"{COLORS.BOLD_YELLOW}Weekly runtime data was corrupted, moved aside, "
            f"and recreated with default values.{COLORS.RESET}"
        )
        return _default_weekly_runtime()
    except OSError as error:
        print(
            f"{COLORS.BOLD_YELLOW}Weekly runtime data could not be read "
            f"({error}), skipping this update.{COLORS.RESET}"
        )
        return None

    if (
        isinstance(data, dict)
        and data.get("schema_version") == RUNTIME_SCHEMA_VERSION
        and isinstance(data.get("days"), dict)
    ):
        return _trim_weekly_runtime(data)

    # Legacy weekday start/end pairs cannot distinguish uptime from downtime. The old file is replaced, not copied.
    print(
        f"{COLORS.BOLD_YELLOW}Weekly runtime migrated; legacy daily hours were "
        f"set to 0 because uptime cannot be separated from downtime.{COLORS.RESET}"
    )
    return _default_weekly_runtime()


@suppress_and_log("Weekly Runtime Updater")
def handle_weekly_runtime(path="utils/data/weekly_runtime.json"):
    while True:
        weekly_runtime_dict = _load_for_update(path)
        if weekly_runtime_dict is not None:
            weekly_runtime_dict = accrue_weekly_runtime(weekly_runtime_dict)
            _write_weekly_runtime(weekly_runtime_dict, path)
        time.sleep(15)


@suppress_and_log("Weekly Runtime Update Starter")
def start_runtime_loop(path="utils/data/weekly_runtime.json"):
    weekly_runtime_dict = _load_for_update(path)
    if weekly_runtime_dict is not None:
        weekly_runtime_dict = reset_runtime_clock(weekly_runtime_dict)
        _write_weekly_runtime(weekly_runtime_dict, path)
    loop_thread = threading.Thread(target=handle_weekly_runtime, args=(path,), daemon=True)
    loop_thread.start()
