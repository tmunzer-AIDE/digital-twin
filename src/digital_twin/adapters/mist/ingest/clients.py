"""Clients-domain ingester: observed wired + wireless clients (active now).

- A client referencing an unknown AP/port is SKIPPED (stale stats are not fatal)
  — the IRBuilder would rightly reject the dangling reference otherwise.
- A MAC already present (e.g. added by LldpIngester as an unmanaged edge device)
  is skipped — first writer wins, no duplicate-id crash.
- clients.active is EARNED only if BOTH client fetches succeeded: an empty site
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
        for w in ctx.raw.wireless_clients:
            if not w.get("mac") or not w.get("ap_mac"):
                continue
            ap = device_id(str(w["ap_mac"]))
            if not ctx.builder.has_device(ap) or ctx.builder.has_client(str(w["mac"])):
                continue  # stale stat / already known edge device
            vlan = w.get("vlan_id")
            ctx.builder.add_client(
                Client(
                    mac=client_id(str(w["mac"])),
                    kind=ClientKind.WIRELESS,
                    attach_kind=AttachKind.AP,
                    attach_id=ap,
                    vlan=int(vlan) if vlan is not None else None,
                    ip=w.get("ip"),
                    ssid=_ssid(w.get("ssid")),
                )
            )
        for w in ctx.raw.wired_clients:
            if not w.get("mac") or not w.get("device_mac") or not w.get("port_id"):
                continue
            pid = port_id(device_id(str(w["device_mac"])), str(w["port_id"]))
            if not ctx.builder.has_port(pid) or ctx.builder.has_client(str(w["mac"])):
                continue
            vlan = w.get("vlan")
            ctx.builder.add_client(
                Client(
                    mac=client_id(str(w["mac"])),
                    kind=ClientKind.WIRED,
                    attach_kind=AttachKind.PORT,
                    attach_id=pid,
                    vlan=int(vlan) if vlan is not None else None,
                    ip=w.get("ip"),
                )
            )
        return frozenset({IRCapability.CLIENTS_ACTIVE})
