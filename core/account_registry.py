import threading
import time


class AccountRegistry:
    """Thread-safe registry for account startup status."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._wave_cond = threading.Condition(threading.Lock())
        self._all_ready = threading.Event()
        self._accounts: dict[int, dict[str, int | str | None]] = {}
        self._first_sent: set[int] = set()
        self.stagger_step = 2.0
        self.stagger_cap = 120.0

    def register(self, index: int, expected_key: str | None = None) -> None:
        """Register an account as pending. expected_key is the key of the account before it logs in: the user ID its
        token shows, or slot-<index> when the token shows none (see utils/account_identity.py)"""
        with self._lock:
            self._accounts[index] = {
                "status": "pending",
                "userid": None,
                "error": None,
                "expected_key": expected_key,
            }
            self._first_sent.discard(index)

    def has_sent_first(self, index: int) -> bool:
        """Return whether an account has passed its first-send stagger."""
        with self._lock:
            return index in self._first_sent

    def mark_first_sent(self, index: int) -> None:
        """Record that an account has passed its first-send stagger."""
        with self._lock:
            self._first_sent.add(index)

    def signal_ready(self, index: int, userid: int) -> None:
        """Mark an account ready and record its user ID."""
        with self._lock:
            current = self._accounts.get(index, {})
            if current.get("status") in {"failed", "stopped"}:
                return
            self._accounts[index] = {
                "status": "ready",
                "userid": userid,
                "error": None,
                "expected_key": current.get("expected_key"),
            }
        with self._wave_cond:
            self._wave_cond.notify_all()

    def signal_failed(self, index: int, error: str) -> None:
        """Mark an account failed while preserving its user ID, if any."""
        with self._lock:
            current = self._accounts.get(index, {})
            self._accounts[index] = {
                "status": "failed",
                "userid": current.get("userid"),
                "error": str(error),
                "expected_key": current.get("expected_key"),
            }
        with self._wave_cond:
            self._wave_cond.notify_all()

    def wait_for_wave(self, indices: list[int], timeout: float) -> bool:
        """Wait until every account in a wave is ready or failed."""
        deadline = time.monotonic() + timeout
        with self._wave_cond:
            while True:
                with self._lock:
                    complete = all(
                        self._accounts.get(index, {}).get("status")
                        in {"ready", "failed", "stopped"}
                        for index in indices
                    )
                if complete:
                    return True

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._wave_cond.wait(remaining)

    def signal_stopped(self, index: int, reason: str) -> None:
        """Mark a started account as stopped, keeping its user ID."""
        with self._lock:
            current = self._accounts.get(index, {})
            self._accounts[index] = {
                "status": "stopped",
                "userid": current.get("userid"),
                "error": str(reason),
                "expected_key": current.get("expected_key"),
            }
        with self._wave_cond:
            self._wave_cond.notify_all()

    def all_stopped(self) -> bool:
        """Return whether every registered account has been stopped."""
        with self._lock:
            return bool(self._accounts) and all(
                account["status"] in {"stopped", "failed"}
                for account in self._accounts.values()
            )

    def set_all_ready(self) -> None:
        """Release clients waiting for every account to become ready."""
        self._all_ready.set()

    @property
    def all_ready_event(self) -> threading.Event:
        """Return the event shared by all account clients."""
        return self._all_ready

    def get(self, index: int) -> dict:
        """Return a copy of one account record, or its pending default."""
        with self._lock:
            return self._accounts.get(
                index,
                {"status": "pending", "userid": None, "error": None, "expected_key": None},
            ).copy()

    def snapshot(self) -> dict:
        """Return a shallow copy of the account registry."""
        with self._lock:
            return self._accounts.copy()


registry = AccountRegistry()
