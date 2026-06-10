"""Per-run structured trace: each pipeline stage with timing + note + error.

The verdict's trace_ref names a run; this object IS that run's record (the
replay store serializes it next to the fixture). Monotonic clock for duration;
no wall-time inside (replayable).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Trace:
    run_id: str
    stages: list[dict[str, Any]] = field(default_factory=list)

    @contextmanager
    def stage(self, name: str, note: str | None = None) -> Iterator[None]:
        started = time.monotonic()
        record: dict[str, Any] = {"stage": name}
        if note is not None:
            record["note"] = note
        try:
            yield
        except BaseException as e:
            record["error"] = str(e)
            raise
        finally:
            record["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
            self.stages.append(record)

    def note(self, stage: str, note: str) -> None:
        self.stages.append({"stage": stage, "note": note, "duration_ms": 0.0})

    def to_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "stages": list(self.stages)}
