"""Org-template apply (multisite design §3).

A template is one org object shared by every assigned site. We apply the edit to
ONE resolved snapshot; the per-site overlay (engine/org_overlay.apply_overlays)
then pins each fetched site's layer to the snapshot (baseline) / proposed
snapshot, so the per-site diff is EXACTLY the edit — never a fetch-time race
between resolve_org_template and fetch_sites.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from digital_twin.adapters.mist.apply.objects import effective_update, update_conflicts
from digital_twin.contracts import Rejection

_Json = Mapping[str, Any]


def apply_template(snapshot: _Json, payload: _Json) -> dict[str, Any] | Rejection:
    """The proposed template = snapshot + edit (Mist root-level update semantics).
    A set-AND-delete on the same attribute is an authoring error -> Rejection."""
    conflicts = update_conflicts(payload)
    if conflicts:
        return Rejection(
            stage="apply",
            reasons=tuple(
                f"conflicting set AND '-{c}' delete marker for the same attribute"
                for c in conflicts
            ),
        )
    return effective_update(snapshot, payload)
