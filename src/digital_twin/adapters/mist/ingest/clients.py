"""Clients-domain ingester: observed wired + wireless clients (active now).

- Unattachable observations are recorded as coverage gaps without failing ingest.
  Valid rows remain available, but incomplete telemetry cannot earn clients.active.
- Consistent duplicate MAC observations are coalesced. Conflicting attachment,
  VLAN or SSID identities remain visible as coverage gaps.
- clients.active is EARNED only if BOTH client fetches succeeded and every
  non-duplicate observation could be attached: an empty site
  with successful fetches legitimately knows "no clients"; a failed fetch must
  not masquerade as that knowledge.
"""

from __future__ import annotations

from typing import Any

from digital_twin.ir import (
    AttachKind,
    Client,
    ClientKind,
    IRCapability,
    client_id,
    device_id,
    port_id,
)

from .base import IngestContext


def _ssid(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class ClientsIngester:
    name = "clients"

    def produces(self) -> frozenset[str]:  # potential supply
        return frozenset({IRCapability.CLIENTS_ACTIVE})

    def ingest(self, ctx: IngestContext) -> frozenset[str]:
        fetched = ctx.raw.meta.fetched
        if "wireless_clients" not in fetched or "wired_clients" not in fetched:
            return frozenset()  # failed fetch -> no claim (zero clients != unknown)
        gaps: dict[str, list[int]] = {}

        def gap(reason: str, index: int) -> None:
            gaps.setdefault(reason, []).append(index)

        def admit(client: Client, index: int, domain: str) -> None:
            if ctx.builder.has_client(client.mac):
                existing = ctx.builder.get_client(client.mac)
                conflict = (
                    (existing.kind, existing.attach_kind, existing.attach_id)
                    != (client.kind, client.attach_kind, client.attach_id)
                    or existing.vlan is not None and client.vlan is not None
                    and existing.vlan != client.vlan
                    or existing.ssid is not None and client.ssid is not None
                    and existing.ssid != client.ssid
                )
                if conflict:
                    gap(f"{domain} client telemetry: conflicting duplicate identity", index)
                return
            ctx.builder.add_client(client)

        for index, w in enumerate(ctx.raw.wireless_clients):
            if not w.get("mac") or not w.get("ap_mac"):
                gap("wireless client telemetry: missing mac or ap_mac", index)
                continue
            ap = device_id(str(w["ap_mac"]))
            if not ctx.builder.has_device(ap):
                gap("wireless client telemetry: unknown AP attachment", index)
                continue
            vlan = w.get("vlan_id")
            admit(
                Client(
                    mac=client_id(str(w["mac"])),
                    kind=ClientKind.WIRELESS,
                    attach_kind=AttachKind.AP,
                    attach_id=ap,
                    vlan=int(vlan) if vlan is not None else None,
                    ip=w.get("ip"),
                    ssid=_ssid(w.get("ssid")),
                ), index, "wireless",
            )
        for index, w in enumerate(ctx.raw.wired_clients):
            if not w.get("mac") or not w.get("device_mac") or not w.get("port_id"):
                gap("wired client telemetry: missing mac, device_mac or port_id", index)
                continue
            pid = port_id(device_id(str(w["device_mac"])), str(w["port_id"]))
            if not ctx.builder.has_port(pid):
                gap("wired client telemetry: unknown port attachment", index)
                continue
            vlan = w.get("vlan")
            admit(
                Client(
                    mac=client_id(str(w["mac"])),
                    kind=ClientKind.WIRED,
                    attach_kind=AttachKind.PORT,
                    attach_id=pid,
                    vlan=int(vlan) if vlan is not None else None,
                    ip=w.get("ip"),
                ), index, "wired",
            )
        for reason, indexes in gaps.items():
            ctx.builder.mark_client_telemetry_gap(
                f"{reason} ({len(indexes)} row(s); sample indexes: {indexes[:3]})"
            )
        return frozenset({IRCapability.CLIENTS_ACTIVE}) if not gaps else frozenset()
