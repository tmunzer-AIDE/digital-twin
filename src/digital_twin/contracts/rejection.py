"""Rejection: the shared errors-as-value for gates and apply.

Every gating outcome carries its stage + human-readable reasons; the engine
(Plan 5) maps any Rejection to decision UNKNOWN with an UNSUPPORTED reason.
Never raised — always returned.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Rejection:
    stage: str  # "envelope" | "object_gate" | "field_gate" | "apply" | "derived_gate"
    reasons: tuple[str, ...]
