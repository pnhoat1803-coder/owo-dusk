"""Thread-safe global token bucket shared by account event loops."""

import asyncio
import math
import threading
import time


class GlobalRateLimiter:
    """Limit callers across threads and event loops with a one-token bucket."""

    def __init__(self, rate_per_second: float) -> None:
        self._lock = threading.Lock()
        self._rate = self._validate_rate(rate_per_second)
        self._capacity = 1.0
        self._tokens = self._capacity
        self._updated_at = time.monotonic()

    @staticmethod
    def _validate_rate(rate_per_second: float) -> float:
        rate = float(rate_per_second)
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError("rate_per_second must be a finite positive number")
        return rate

    def _refill_locked(self, now: float) -> None:
        elapsed = max(0.0, now - self._updated_at)
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._updated_at = now

    def set_rate(self, rate_per_second: float) -> None:
        """Set the shared refill rate while preserving the current bucket state."""
        rate = self._validate_rate(rate_per_second)
        with self._lock:
            self._refill_locked(time.monotonic())
            self._rate = rate

    @property
    def rate_per_second(self) -> float:
        """Return the current shared token refill rate."""
        with self._lock:
            return self._rate

    def try_acquire(self) -> bool:
        """Take a token immediately, returning False when the bucket is empty."""
        with self._lock:
            self._refill_locked(time.monotonic())
            if self._tokens < 1.0:
                return False
            self._tokens -= 1.0
            return True

    async def acquire(self) -> None:
        """Wait asynchronously until a shared token is available."""
        while True:
            with self._lock:
                now = time.monotonic()
                self._refill_locked(now)
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                delay = (1.0 - self._tokens) / self._rate
            await asyncio.sleep(delay)


rate_limiter = GlobalRateLimiter(3.0)
