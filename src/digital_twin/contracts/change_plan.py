"""ChangePlan: the envelope an AI agent submits — ordered full-object-replacement ops.

A ChangeOp payload is the COMPLETE new object (Mist PUT semantics), never a
merge-patch. `order` is a total order; semantics are enforced by scope/envelope
(shape) and adapters apply (state), not here — these are plain value types.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ChangeScope:
    org_id: str
    site_id: str | None = None


@dataclass(frozen=True)
class ChangeOp:
    action: str  # M1: "update" only (gated in scope/object_gate)
    order: int
    object_type: str
    object_id: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class ChangePlan:
    source: str  # owning adapter, e.g. "mist"
    scope: ChangeScope
    ops: tuple[ChangeOp, ...]
    intent: str | None = None
