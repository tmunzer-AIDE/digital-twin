"""Freshness view: when the state was acquired, from where, what failed.

The agent reasons about stale evidence ("valid as of now" — the on-demand
model); partial fetch failures surface here for transparency even when they
did not lower the decision (irrelevant-partial rule).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from digital_twin.providers.base import StateMeta


@dataclass(frozen=True)
class StateMetaView:
    state_acquired_at: str  # ISO 8601
    host: str
    age_seconds: int
    fetched: tuple[str, ...]
    fetch_failures: tuple[tuple[str, str], ...]  # (object, error)


def build_state_meta(meta: StateMeta, *, now: datetime) -> StateMetaView:
    return StateMetaView(
        state_acquired_at=meta.acquired_at.isoformat(),
        host=meta.host,
        age_seconds=int((now - meta.acquired_at).total_seconds()),
        fetched=meta.fetched,
        fetch_failures=tuple((f.object, f.error) for f in meta.failures),
    )
