"""LLDP-domain ingester: links from port stats + AP lldp_stat, with honesty rules.

- Both ends report each other        -> one Link, LLDP_TWO_SIDED (HIGH).
- Only one MANAGED end reports        -> one Link, LLDP_ONE_SIDED (LOW).
- Neighbor is NOT a Mist device      -> NO Link; the neighbor becomes a wired
  edge-device Client on the local port (user decision: printers/unmanaged
  routers stay in the impact surface — VLAN continuity, DHCP, routing, FW).
- aggregated/lag_name                -> LinkKind.LAG with bundle_id.
- stp_state on a port                -> Port.stp_state + stp_meta (OBSERVED);
  stp.state capability is EARNED only if >=1 such row was applied.
- AP lldp_stat names switch + port   -> AP uplink link; two-sided only when the
  switch's own claims name THAT AP back (not just any neighbor). A shared
  emitted-set prevents the same physical link being added twice.

Ports referenced by stats but absent from config are added as minimal OBSERVED
trunk ports (cannot invent VLANs). Stat shapes pinned by tools/probe_fetch.py.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from digital_twin.ir import (
    AttachKind,
    Client,
    ClientKind,
    IRCapability,
    Link,
    LinkKind,
    Port,
    PortMode,
    Provenance,
    client_id,
    device_id,
    fact_meta,
    link_id,
    port_id,
)

from .base import IngestContext

_Json = Mapping[str, Any]


class LldpIngester:
    name = "lldp"

    def produces(self) -> frozenset[str]:  # potential supply
        return frozenset({IRCapability.STP_STATE})

    def ingest(self, ctx: IngestContext) -> frozenset[str]:
        claims = self._claims(ctx)
        stp_seen = self._apply_stp(ctx)
        emitted: set[str] = set()
        self._emit_links(ctx, claims, emitted)
        self._emit_ap_uplinks(ctx, claims, emitted)
        return frozenset({IRCapability.STP_STATE}) if stp_seen else frozenset()

    # -- claims ---------------------------------------------------------------
    def _claims(self, ctx: IngestContext) -> dict[tuple[str, str], _Json]:
        """(reporter_port_id_global, claimed_neighbor_port_id_global) -> stat row.

        Some orgs' port stats carry NO neighbor_mac — only neighbor_system_name
        (found in real use, 2026-06-10; without this the site model is EDGELESS
        and every strand looks pre-existing). Fallback: resolve the neighbor by
        its system name against the site's managed device names — same rule the
        AP-uplink path already uses. A macless row whose name matches nothing
        is SKIPPED (no stable identity to attach a link or edge-client to).
        """
        by_name = {
            str(d["name"]): device_id(str(d["mac"]))
            for d in ctx.raw.devices
            if d.get("name") and d.get("mac")
        }
        out: dict[tuple[str, str], _Json] = {}
        for row in ctx.raw.port_stats:
            if not row.get("port_id"):
                continue
            if row.get("neighbor_mac"):
                neighbor = device_id(str(row["neighbor_mac"]))
            else:
                named = by_name.get(str(row.get("neighbor_system_name")))
                if named is None:
                    continue
                neighbor = named
            src = port_id(device_id(str(row["mac"])), str(row["port_id"]))
            # Mist port stats name the neighbor's port via `neighbor_port_desc`.
            dst = port_id(neighbor, str(row.get("neighbor_port_desc") or "?"))
            out[(src, dst)] = row
        return out

    # -- STP ------------------------------------------------------------------
    def _apply_stp(self, ctx: IngestContext) -> bool:
        seen = False
        for row in ctx.raw.port_stats:
            if row.get("stp_state") is None or not row.get("port_id"):
                continue
            pid = port_id(device_id(str(row["mac"])), str(row["port_id"]))
            self._ensure_port(ctx, pid)
            ctx.builder.replace_port(
                replace(
                    ctx.builder.get_port(pid),
                    stp_state=str(row["stp_state"]),
                    stp_enabled=True,
                    stp_meta=fact_meta(Provenance.OBSERVED),
                )
            )
            seen = True
        return seen

    # -- links ----------------------------------------------------------------
    def _emit_links(
        self, ctx: IngestContext, claims: dict[tuple[str, str], _Json], emitted: set[str]
    ) -> None:
        for (src, dst), row in claims.items():
            neighbor_dev = dst.partition(":")[0]
            if not ctx.builder.has_device(neighbor_dev):
                self._edge_device_client(ctx, src, neighbor_dev)
                continue
            lid = link_id(src, dst)
            if lid in emitted:
                continue
            emitted.add(lid)
            two_sided = (dst, src) in claims
            prov = Provenance.LLDP_TWO_SIDED if two_sided else Provenance.LLDP_ONE_SIDED
            reasons = () if two_sided else (f"link {lid} seen from {src} only",)
            kind, bundle = self._kind(row, claims.get((dst, src)))
            for pid in (src, dst):
                self._ensure_port(ctx, pid)
            ctx.builder.add_link(
                Link(
                    id=lid,
                    a_port=src,
                    b_port=dst,
                    kind=kind,
                    bundle_id=bundle,
                    meta=fact_meta(prov, reasons),
                )
            )

    def _edge_device_client(self, ctx: IngestContext, local_port: str, mac: str) -> None:
        """An unmanaged LLDP neighbor is an EDGE DEVICE = wired client on this port
        (its VLAN continuity / DHCP / routing / FW exposure must stay visible)."""
        if ctx.builder.has_client(mac):
            return
        ctx.builder.add_client(
            Client(
                mac=client_id(mac),
                kind=ClientKind.WIRED,
                attach_kind=AttachKind.PORT,
                attach_id=local_port,
                meta=fact_meta(Provenance.OBSERVED, ("unmanaged LLDP neighbor (edge device)",)),
            )
        )

    def _kind(self, a: _Json, b: _Json | None) -> tuple[LinkKind, str | None]:
        # A LAG member's port row carries `port_parent` = the bundle name (e.g. "ae0").
        for row in (a, b or {}):
            parent = row.get("port_parent")
            if parent:
                return LinkKind.LAG, str(parent)
        return LinkKind.PHYSICAL, None

    def _emit_ap_uplinks(
        self, ctx: IngestContext, claims: dict[tuple[str, str], _Json], emitted: set[str]
    ) -> None:
        switches = [d for d in ctx.raw.devices if d.get("type") == "switch" and d.get("mac")]
        switch_by_name = {str(d.get("name")): device_id(str(d["mac"])) for d in switches}
        switch_macs = {device_id(str(d["mac"])) for d in switches}
        for stat in ctx.raw.device_stats:
            if stat.get("type") != "ap" or not stat.get("mac"):
                continue
            lldp = stat.get("lldp_stat") or {}
            # Prefer chassis_id (switch base MAC) — robust; fall back to system_name
            # (the switch hostname), which catches Virtual Chassis whose chassis_id
            # is a member FPC MAC rather than the Mist device MAC.
            chassis = device_id(str(lldp["chassis_id"])) if lldp.get("chassis_id") else None
            sw_id = (
                chassis
                if chassis in switch_macs
                else switch_by_name.get(str(lldp.get("system_name")))
            )
            sw_port_name = lldp.get("port_id") or lldp.get("port_desc")
            if not sw_id or not sw_port_name:
                continue
            ap_id = device_id(str(stat["mac"]))
            ap_port = port_id(ap_id, "eth0")
            sw_port = port_id(sw_id, str(sw_port_name))
            lid = link_id(ap_port, sw_port)
            if lid in emitted or any(  # switch-side claim already produced this link
                link_id(src, dst) == lid for (src, dst) in claims if src == sw_port
            ):
                continue
            emitted.add(lid)
            # two-sided only if the switch's claim names THIS AP (not just anyone)
            corroborated = any(
                src == sw_port and dst.partition(":")[0] == ap_id for (src, dst) in claims
            )
            prov = Provenance.LLDP_TWO_SIDED if corroborated else Provenance.LLDP_ONE_SIDED
            for pid, did, name in ((ap_port, ap_id, "eth0"), (sw_port, sw_id, str(sw_port_name))):
                self._ensure_port(ctx, pid, did, name)
            ctx.builder.add_link(
                Link(
                    id=lid,
                    a_port=ap_port,
                    b_port=sw_port,
                    kind=LinkKind.PHYSICAL,
                    meta=fact_meta(prov),
                )
            )

    # -- helpers ----------------------------------------------------------------
    def _ensure_port(
        self, ctx: IngestContext, pid: str, did: str | None = None, name: str | None = None
    ) -> None:
        if ctx.builder.has_port(pid):
            return
        d, _, n = pid.partition(":")
        ctx.builder.add_port(
            Port(
                id=pid,
                device_id=did or d,
                name=name or n,
                mode=PortMode.TRUNK,
                meta=fact_meta(Provenance.OBSERVED),
            )
        )
