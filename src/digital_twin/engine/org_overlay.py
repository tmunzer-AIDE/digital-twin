"""Org-plan overlays (delete-ripple design §core model). Each org op becomes an
OrgOverlay; `proposed is None` means the layer is ABSENT (a delete), distinct from
{} (an empty-but-present template). The per-site filter uses `assigned_site_ids`
(the canonical resolver output), never the raw site.<type>_id field."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from dataclasses import replace as dc_replace
from typing import Any

from digital_twin.providers.base import RawSiteState


@dataclass(frozen=True)
class OrgOverlay:
    # networktemplate | gatewaytemplate | sitetemplate | wlan | wlantemplate
    object_type: str
    object_id: str
    name: str | None
    action: str                               # "update" | "delete"
    assigned_site_ids: frozenset[str]
    baseline: Mapping[str, Any]
    proposed: Mapping[str, Any] | None         # None == REMOVED (layer absent)
    wlan_baseline_by_site: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    wlan_proposed_by_site: Mapping[str, Mapping[str, Any] | None] = field(default_factory=dict)
    wlan_template_rows_by_site: Mapping[str, tuple[Mapping[str, Any], ...]] = (
        field(default_factory=dict)
    )

    def __post_init__(self) -> None:
        if self.object_type == "wlantemplate":
            row_sites = frozenset(self.wlan_template_rows_by_site)
            if row_sites != self.assigned_site_ids:
                raise ValueError("wlantemplate overlay row sites must equal assigned_site_ids")
            return
        if self.object_type != "wlan":
            return
        baseline_sites = frozenset(self.wlan_baseline_by_site)
        proposed_sites = frozenset(self.wlan_proposed_by_site)
        if baseline_sites != self.assigned_site_ids:
            raise ValueError("wlan overlay baseline sites must equal assigned_site_ids")
        if proposed_sites != baseline_sites:
            raise ValueError("wlan overlay proposed sites must equal baseline sites")


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


def _pin_wlan(
    raw: RawSiteState, object_id: str, value: Mapping[str, Any] | None
) -> RawSiteState:
    if value is None:
        filtered_rows = tuple(row for row in raw.wlans if str(row.get("id") or "") != object_id)
        return dc_replace(raw, wlans=filtered_rows)
    replacement = dict(value)
    out_rows: list[Mapping[str, Any]] = []
    replaced = False
    for row in raw.wlans:
        if str(row.get("id") or "") == object_id:
            out_rows.append(replacement)
            replaced = True
        else:
            out_rows.append(row)
    if not replaced:
        out_rows.append(replacement)
    return dc_replace(raw, wlans=tuple(out_rows))


def _pin_wlan_template(
    raw: RawSiteState,
    template_id: str,
    captured_rows: tuple[Mapping[str, Any], ...],
    replacement_rows: tuple[Mapping[str, Any], ...] | None,
) -> RawSiteState:
    captured_ids = {str(row.get("id")) for row in captured_rows if row.get("id") is not None}
    filtered_rows = tuple(
        row for row in raw.wlans
        if str(row.get("template_id") or "") != template_id
        and str(row.get("id") or "") not in captured_ids
    )
    if replacement_rows is None:
        return dc_replace(raw, wlans=filtered_rows)
    rows = tuple(
        sorted((dict(row) for row in replacement_rows), key=lambda row: str(row.get("id") or ""))
    )
    return dc_replace(raw, wlans=(*filtered_rows, *rows))


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
        if o.object_type == "wlan":
            base_raw = _pin_wlan(base_raw, o.object_id, o.wlan_baseline_by_site[site_id])
            prop_raw = _pin_wlan(prop_raw, o.object_id, o.wlan_proposed_by_site[site_id])
        elif o.object_type == "wlantemplate":
            captured_rows = tuple(o.wlan_template_rows_by_site[site_id])
            base_raw = _pin_wlan_template(base_raw, o.object_id, captured_rows, captured_rows)
            prop_raw = _pin_wlan_template(prop_raw, o.object_id, captured_rows, None)
        else:
            base_raw = _pin(base_raw, o.object_type, o.baseline)
            prop_raw = _pin(prop_raw, o.object_type, o.proposed)
    return base_raw, prop_raw
