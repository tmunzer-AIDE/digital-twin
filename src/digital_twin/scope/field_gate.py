"""Post-fetch raw pre-screen: which raw LEAVES does this op actually change?

Two checks, both needing the fetched object:
1. DEVICE ROLE — M1 models switch config only; an op targeting an AP/gateway
   device is rejected here (the spec's post-fetch role check: the role is only
   known once the device is fetched).
2. CHANGED PATHS — diffs payload vs the CURRENT raw object (the rolling pre-op
   state; the engine passes the right one) and matches every changed LEAF
   against the leaf-tightened raw allowlist. Full-object-replacement semantics:
   a field present in current but absent from payload counts as CHANGED
   (removed); added/removed subtrees gate leaf-by-leaf. Server-managed metadata
   (IGNORED_RAW_FIELDS) is excluded — a payload never carries it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from digital_twin.contracts import Rejection
from digital_twin.scope.allowlist import IGNORED_RAW_FIELDS, RAW_ALLOWLIST
from digital_twin.scope.paths import allowed, changed_leaf_paths

_STAGE = "field_gate"


def changed_paths(current: Mapping[str, Any], payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Dot-paths of every leaf that differs (additions, edits, removals)."""
    return changed_leaf_paths(current, payload, ignore_top=IGNORED_RAW_FIELDS)


def screen_op(
    object_type: str, current: Mapping[str, Any], payload: Mapping[str, Any]
) -> Rejection | None:
    if object_type == "device" and current.get("type") != "switch":
        return Rejection(
            stage=_STAGE,
            reasons=(
                f"device type {current.get('type')!r} is not modeled in M1 "
                "(switch config only — AP/gateway devices are out of scope)",
            ),
        )
    allowlist = RAW_ALLOWLIST.get(object_type, ())
    offending = [p for p in changed_paths(current, payload) if not allowed(p, allowlist)]
    if offending:
        return Rejection(
            stage=_STAGE,
            reasons=tuple(
                f"out-of-scope raw path changed: {p} (not in the M1 allowlist)" for p in offending
            ),
        )
    return None
