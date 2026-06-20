"""WLAN-domain ingester: site WLAN config -> per-AP required VLANs in the IR.

Runs after the switch ingester (which creates AP Device + VLAN entities). For
each enabled, vlan-tagged, locally-bridged WLAN it records — per AP it applies
to — the VLAN(s) the AP must receive on its wired uplink, and ENSURES a Vlan
entity exists for each (so the per-VLAN graph enumerates it even when no switch
network names it). Unresolvable WLANs (wxtag scope, template vlan) are recorded
as coverage gaps. WLAN_CONFIG is EARNED only when the wlan fetch succeeded — a
failed/absent fetch leaves AP VLAN needs to the observation-based fallback.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from digital_twin.ir import IRCapability, Vlan, Wlan

from .base import IngestContext
from .wlan_vlans import ap_required_vlans


def _mint_wlan(row: Mapping[str, Any]) -> Wlan:
    auth = row.get("auth") or {}
    auth_type = auth.get("type")
    return Wlan(
        id=str(row.get("id", "")),
        ssid=str(row.get("ssid", "")),
        enabled=bool(row.get("enabled")),
        auth_type=str(auth_type) if auth_type is not None else None,
        isolation=bool(row.get("isolation")) or bool(row.get("l2_isolation")),
        apply_to=str(av) if (av := row.get("apply_to")) is not None else None,
        ap_ids=tuple(sorted({str(x) for x in (row.get("ap_ids") or [])})),
        wxtag_ids=tuple(sorted({str(x) for x in (row.get("wxtag_ids") or [])})),
        # fail-closed: site-writable ONLY when positively site-owned
        inherited=not (row.get("for_site") is True and not row.get("template_id")),
    )


class WlanIngester:
    name = "wlan"

    def produces(self) -> frozenset[str]:  # potential supply
        return frozenset({IRCapability.WLAN_CONFIG})

    def ingest(self, ctx: IngestContext) -> frozenset[str]:
        if "wlans" not in ctx.raw.meta.fetched:
            return frozenset()  # not fetched -> no claim (observation fallback stands)
        ap_devices = [d for d in ctx.raw.devices if d.get("type") == "ap" and d.get("mac")]
        resolved, unresolved = ap_required_vlans(ctx.raw.wlans, ap_devices)
        for ap_id, vlans in resolved.items():
            if not ctx.builder.has_device(ap_id):
                continue  # AP not in the IR (e.g. devices fetch failed) -> skip
            for vid in vlans:
                if not ctx.builder.has_vlan(vid):
                    ctx.builder.add_vlan(Vlan(vlan_id=vid, scope=ctx.raw.scope.site_id))
            ctx.builder.require_ap_vlans(ap_id, vlans)
        for ap_id, reasons in unresolved.items():
            if ctx.builder.has_device(ap_id):
                ctx.builder.mark_ap_wlan_unresolved(ap_id, tuple(reasons))
        for row in ctx.raw.wlans:
            if row.get("id"):
                ctx.builder.add_wlan(_mint_wlan(row))
        return frozenset({IRCapability.WLAN_CONFIG})
