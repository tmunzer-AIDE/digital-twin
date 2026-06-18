"""Org-plan overlays (delete-ripple design §core model). Each org op becomes an
OrgOverlay; `proposed is None` means the layer is ABSENT (a delete), distinct from
{} (an empty-but-present template). The per-site filter uses `assigned_site_ids`
(the canonical resolver output), never the raw site.<type>_id field."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import replace as dc_replace
from typing import Any

from digital_twin.providers.base import RawSiteState


@dataclass(frozen=True)
class OrgOverlay:
    object_type: str                          # networktemplate | gatewaytemplate | sitetemplate
    object_id: str
    name: str | None
    action: str                               # "update" | "delete"
    assigned_site_ids: frozenset[str]
    baseline: Mapping[str, Any]
    proposed: Mapping[str, Any] | None         # None == REMOVED (layer absent)


def affected_sites(overlays: tuple[OrgOverlay, ...]) -> tuple[str, ...]:
    """Deterministic union of each overlay's baseline assigned_site_ids. Structured
    as a helper so a future site-reassignment op can feed a baseline∪proposed union;
    MVP = baseline assignment (a template op cannot change assignment)."""
    out: set[str] = set()
    for o in overlays:
        out |= o.assigned_site_ids
    return tuple(sorted(out))


def _pin(raw: RawSiteState, object_type: str, value: Mapping[str, Any] | None) -> RawSiteState:
    """Replace exactly the named template field. `None` => layer absent (delete).
    Explicit branch keeps mypy happy without casting."""
    v: dict[str, Any] | None = dict(value) if value is not None else None
    if object_type == "gatewaytemplate":
        return dc_replace(raw, gatewaytemplate=v)
    if object_type == "sitetemplate":
        return dc_replace(raw, sitetemplate=v)
    return dc_replace(raw, networktemplate=v)  # networktemplate default


def apply_overlays(
    fetched: RawSiteState, site_id: str, overlays: tuple[OrgOverlay, ...]
) -> tuple[RawSiteState, RawSiteState]:
    """(baseline_raw, proposed_raw) for one site: pin every overlay the site is
    assigned to (site_id in overlay.assigned_site_ids) onto its layer slot —
    baseline=overlay.baseline, proposed=overlay.proposed (None == layer absent).
    A site not assigned to a given overlay is NOT pinned for it. Untouched layers
    keep the fetched copy (fetch-race guard). Order-independent: a site has ≤1
    overlay per layer slot."""
    base_raw, prop_raw = fetched, fetched
    for o in overlays:
        if site_id not in o.assigned_site_ids:
            continue
        base_raw = _pin(base_raw, o.object_type, o.baseline)
        prop_raw = _pin(prop_raw, o.object_type, o.proposed)
    return base_raw, prop_raw
