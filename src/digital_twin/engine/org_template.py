"""Org-template apply + the baseline-snapshot override (multisite design §3).

A networktemplate is one org object shared by every assigned site. We apply the
edit to ONE resolved snapshot and override each fetched site's networktemplate
with the snapshot (baseline) / proposed snapshot, so the per-site diff is EXACTLY
the edit — never a fetch-time race between resolve_org_template and fetch_sites.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace as dc_replace
from typing import Any

from digital_twin.adapters.mist.apply.objects import effective_update, update_conflicts
from digital_twin.contracts import Rejection
from digital_twin.providers.base import RawSiteState

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


def _pin(raw: RawSiteState, object_type: str, value: dict[str, Any]) -> RawSiteState:
    """Return a new RawSiteState with exactly the named template field replaced.

    The three ORG object types each map to a same-named field on RawSiteState.
    Using an explicit branch keeps mypy happy without casting."""
    if object_type == "gatewaytemplate":
        return dc_replace(raw, gatewaytemplate=value)
    if object_type == "sitetemplate":
        return dc_replace(raw, sitetemplate=value)
    # default: networktemplate (object_gate already validated object_type)
    return dc_replace(raw, networktemplate=value)


def override_template(
    object_type: str, fetched_raw: RawSiteState, snapshot: _Json, proposed: _Json
) -> tuple[RawSiteState, RawSiteState]:
    """(baseline_raw, proposed_raw) for one site, both pinned to the ONE snapshot
    of the edited layer (object_type), discarding the per-site-fetched copy of
    that layer (fetch-race guard). The other fetched layers stay pinned."""
    # shallow dict() is sufficient: compile/merge.py deepcopies the template
    # before touching nested values, so no caller mutates these in place
    baseline_raw = _pin(fetched_raw, object_type, dict(snapshot))
    proposed_raw = _pin(fetched_raw, object_type, dict(proposed))
    return baseline_raw, proposed_raw
