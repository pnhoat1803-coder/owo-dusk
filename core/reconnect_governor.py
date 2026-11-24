"""Process-wide pacing and backoff for gateway reconnect attempts."""

import asyncio
import logging
import math
import random
import threading
import time
from collections import deque
from typing import Deque


class ReconnectGovernor:
    def __init__(
        self,
        min_interval_seconds: float = 3.0,
        *,
        mass_threshold: int = 10,
        mass_window_seconds: float = 5.0,
        backoff_base_seconds: float = 30.0,
        backoff_jitter_seconds: float = 90.0,
        backoff_duration_seconds: float = 120.0,
    ):
        self._lock = threading.Lock()
        self._disconnect_times: Deque[float] = deque()
        self._last_start = 0.0
        self._min_interval = max(
            0.01, self._valid_seconds(min_interval_seconds, 3.0)
        )
        try:
            self._mass_threshold = max(1, int(mass_threshold))
        except (TypeError, ValueError, OverflowError):
            self._mass_threshold = 10
        self._mass_window = self._valid_seconds(mass_window_seconds, 5.0)
        self._backoff_base = self._valid_seconds(backoff_base_seconds, 30.0)
        self._backoff_jitter = self._valid_seconds(backoff_jitter_seconds, 90.0)
        self._backoff_duration = self._valid_seconds(
            backoff_duration_seconds, 120.0
        )
        self._backoff_until = 0.0

    @staticmethod
    def _valid_seconds(value: float, default: float) -> float:
        try:
            seconds = float(value)
        except (TypeError, ValueError, OverflowError):
            return default
        return seconds if math.isfinite(seconds) and seconds >= 0.0 else default

    async def acquire(self) -> None:
        """Wait for a globally paced reconnect slot without sharing loop locks."""
        backoff_ready_at = None
        while True:
            with self._lock:
                now = time.monotonic()
                if self._backoff_until > now and backoff_ready_at is None:
                    backoff_ready_at = now + self._backoff_base + random.uniform(
                        0.0, self._backoff_jitter
                    )

                next_interval_at = self._last_start + self._min_interval
                ready_at = max(
                    next_interval_at,
                    backoff_ready_at if backoff_ready_at is not None else now,
                )
                if ready_at <= now:
                    self._last_start = now
                    return
                delay = ready_at - now

            await asyncio.sleep(delay)

    def set_min_interval(self, seconds: float) -> None:
        seconds = self._valid_seconds(seconds, 3.0)
        with self._lock:
            self._min_interval = max(0.01, seconds)

    def configure_detection(
        self,
        *,
        mass_threshold: int,
        mass_window_seconds: float,
        backoff_base_seconds: float,
        backoff_jitter_seconds: float,
        backoff_duration_seconds: float,
    ) -> None:
        try:
            threshold = max(1, int(mass_threshold))
        except (TypeError, ValueError, OverflowError):
            threshold = 10

        window = self._valid_seconds(mass_window_seconds, 5.0)
        base = self._valid_seconds(backoff_base_seconds, 30.0)
        jitter = self._valid_seconds(backoff_jitter_seconds, 90.0)
        duration = self._valid_seconds(backoff_duration_seconds, 120.0)
        with self._lock:
            self._mass_threshold = threshold
            self._mass_window = window
            self._backoff_base = base
            self._backoff_jitter = jitter
            self._backoff_duration = duration
            self._disconnect_times.clear()
            self._backoff_until = 0.0

    def enter_backoff_mode(
        self,
        base_seconds: float,
        jitter_seconds: float,
        duration_seconds: float,
    ) -> None:
        base = self._valid_seconds(base_seconds, 30.0)
        jitter = self._valid_seconds(jitter_seconds, 90.0)
        duration = self._valid_seconds(duration_seconds, 120.0)
        now = time.monotonic()
        with self._lock:
            self._backoff_base = base
            self._backoff_jitter = jitter
            self._backoff_duration = duration
            self._backoff_until = max(self._backoff_until, now + duration)

    def is_backoff_active(self) -> bool:
        with self._lock:
            return self._backoff_until > time.monotonic()

    def current_delay(self) -> float:
        with self._lock:
            now = time.monotonic()
            interval_wait = max(0.0, self._last_start + self._min_interval - now)
            if self._backoff_until > now:
                backoff_wait = self._backoff_base + self._backoff_jitter / 2.0
                return max(interval_wait, backoff_wait)
            return max(interval_wait, self._min_interval)

    def record_disconnect(self) -> None:
        should_log = False
        active_duration = 0.0
        with self._lock:
            now = time.monotonic()
            cutoff = now - self._mass_window
            while self._disconnect_times and self._disconnect_times[0] < cutoff:
                self._disconnect_times.popleft()
            self._disconnect_times.append(now)

            if (
                len(self._disconnect_times) >= self._mass_threshold
                and self._backoff_until <= now
            ):
                self._backoff_until = now + self._backoff_duration
                active_duration = self._backoff_duration
                should_log = True

        if should_log:
            logging.getLogger(__name__).warning(
                "Mass disconnect threshold reached; reconnect backoff active for %.1f seconds",
                active_duration,
            )


reconnect_governor = ReconnectGovernor(3.0)
