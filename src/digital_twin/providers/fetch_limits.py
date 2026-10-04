"""Fetch budgets shared across a simulation's lookups, requests and pages.

This module has no SDK imports, so offline simulation/replay stays independent
of the live transport. Budgets are local to an execution context, not a global
mutable session or a timer that leaves background requests running.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps


@dataclass(frozen=True)
class FetchLimits:
    connect_timeout: float = 5.0
    read_timeout: float = 20.0
    total_timeout: float = 120.0
    max_attempts: int = 3
    max_pages: int = 1000
    max_rows: int = 1_000_000
    backoff_cap: float = 5.0

    def __post_init__(self) -> None:
        timeouts = (self.connect_timeout, self.read_timeout, self.total_timeout, self.backoff_cap)
        for value in timeouts:
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError("fetch timeouts and backoff cap must be finite and positive")
        for value in (self.max_attempts, self.max_pages, self.max_rows):
            if type(value) is not int or value <= 0:
                raise ValueError("fetch attempt, page and row limits must be positive integers")


DEFAULT_LIMITS = FetchLimits()
_deadline: ContextVar[float | None] = ContextVar("mist_fetch_deadline", default=None)


class FetchDeadlineExceeded(RuntimeError):
    pass


def remaining(limits: FetchLimits) -> float:
    deadline = _deadline.get()
    seconds = limits.total_timeout if deadline is None else deadline - time.monotonic()
    if seconds <= 0:
        raise FetchDeadlineExceeded("Mist fetch deadline exceeded")
    return seconds


@contextmanager
def fetch_budget(limits: FetchLimits) -> Iterator[None]:
    # Nested provider calls and SDK reads must never reset the simulation budget.
    if _deadline.get() is not None:
        yield
        return
    token = _deadline.set(time.monotonic() + limits.total_timeout)
    try:
        yield
    finally:
        _deadline.reset(token)


def with_fetch_budget[**P, T](operation: Callable[P, T]) -> Callable[P, T]:
    @wraps(operation)
    def run(*args: P.args, **kwargs: P.kwargs) -> T:
        owner = kwargs.get("provider") if "provider" in kwargs else args[0]
        limits = getattr(owner, "_fetch_limits", DEFAULT_LIMITS)
        with fetch_budget(limits):
            return operation(*args, **kwargs)

    return run
