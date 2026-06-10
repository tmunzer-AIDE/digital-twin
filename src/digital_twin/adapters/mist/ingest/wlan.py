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

from digital_twin.ir import IRCapability, Vlan

from .base import IngestContext
from .wlan_vlans import ap_required_vlans


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
        return frozenset({IRCapability.WLAN_CONFIG})
