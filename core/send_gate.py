"""Shared readiness, first-send staggering, and fleet rate permission."""

import asyncio
import math
import threading
from contextlib import contextmanager
from contextvars import ContextVar

from core.account_registry import registry


_first_send_locks: dict[tuple[int, int], asyncio.Lock] = {}
_first_send_locks_lock = threading.Lock()
_active_permission: ContextVar[int | None] = ContextVar(
    "active_send_permission", default=None
)


def _get_first_send_lock(account_index: int) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    key = (id(loop), account_index)
    with _first_send_locks_lock:
        lock = _first_send_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _first_send_locks[key] = lock
        return lock


async def await_all_ready() -> None:
    """Wait until every registered account has completed startup."""
    while not registry.all_ready_event.is_set():
        await asyncio.sleep(0.5)


async def await_send_gate(account_index: int) -> None:
    """Wait for fleet readiness and this account's one-time stagger."""
    await await_all_ready()

    async with _get_first_send_lock(account_index):
        if registry.has_sent_first(account_index):
            return

        step = float(registry.stagger_step)
        cap = float(registry.stagger_cap)
        if not math.isfinite(step) or step < 0.0:
            step = 5.0
        if not math.isfinite(cap) or cap < 0.0:
            cap = 600.0
        delay = max(0, int(account_index) - 1) * step
        delay = min(delay, cap)
        if delay:
            await asyncio.sleep(delay)
        registry.mark_first_sent(account_index)


async def await_send_permission(
    account_index: int, *, stagger_first_send: bool = True
) -> None:
    """Wait for readiness and an optional first-command stagger."""
    if _active_permission.get() == account_index:
        _active_permission.set(None)
        return
    if stagger_first_send:
        await await_send_gate(account_index)
    else:
        await await_all_ready()


@contextmanager
def send_permission_context(account_index: int):
    """Skip a duplicate send gate for a nested send from this caller."""
    token = _active_permission.set(account_index)
    try:
        yield
    finally:
        _active_permission.reset(token)
