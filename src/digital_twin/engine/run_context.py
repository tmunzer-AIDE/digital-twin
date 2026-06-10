"""Per-run identity: run_id + trace handle (state_meta accumulates on the run)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from digital_twin.observability.trace import Trace


def _new_run_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class RunContext:
    run_id: str = field(default_factory=_new_run_id)
    trace: Trace | None = None

    def __post_init__(self) -> None:
        if self.trace is None:
            self.trace = Trace(run_id=self.run_id)
