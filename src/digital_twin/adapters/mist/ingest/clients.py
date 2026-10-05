"""Clients-domain ingester: observed wired + wireless clients (active now).

- Unattachable observations are recorded as coverage gaps without failing ingest.
  Valid rows remain available, but incomplete telemetry cannot earn clients.active.
- A MAC already present (e.g. added by LldpIngester as an unmanaged edge device)
  is skipped — first writer wins, no duplicate-id crash.
- clients.active is EARNED only if BOTH client fetches succeeded and every
  non-duplicate observation could be attached: an empty site
  with successful fetches legitimately knows "no clients"; a failed fetch must
  not masquerade as that knowledge.
"""

from __future__ import annotations

from collections.abc import Mapping
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


def _single(value: Any) -> Any:
    """One value from a field Mist's wired-client SEARCH returns as the full
    history list: a one-element list is unambiguous, a longer one is not (None)."""
    if isinstance(value, list):
        return value[0] if len(value) == 1 else None
    return value


def _wired_attachment(row: Mapping[str, Any]) -> tuple[Any, Any, Any]:
    """(device_mac, port_id, vlan) of the client's CURRENT attachment. The wired-
    client search returns device_mac / port_id / vlan as every switch and port the
    MAC was learned on (uplinks included) and the current one as last_device_mac /
    last_port_id / last_vlan. Rows without last_* fall back to the plain fields
    when those are single values; an ambiguous history yields no attachment."""
    if row.get("last_device_mac") and row.get("last_port_id"):
        vlan = row["last_vlan"] if "last_vlan" in row else _single(row.get("vlan"))
        return row["last_device_mac"], row["last_port_id"], vlan
    return _single(row.get("device_mac")), _single(row.get("port_id")), _single(row.get("vlan"))


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

        for index, w in enumerate(ctx.raw.wireless_clients):
            if not w.get("mac") or not w.get("ap_mac"):
                gap("wireless client telemetry: missing mac or ap_mac", index)
                continue
            ap = device_id(str(w["ap_mac"]))
            if not ctx.builder.has_device(ap):
                gap("wireless client telemetry: unknown AP attachment", index)
                continue
            if ctx.builder.has_client(str(w["mac"])):
                continue  # already represented (e.g. by LLDP)
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
        for index, w in enumerate(ctx.raw.wired_clients):
            attached_mac, attached_port, vlan = _wired_attachment(w)
            if not w.get("mac") or not attached_mac or not attached_port:
                gap("wired client telemetry: missing mac, device_mac or port_id", index)
                continue
            pid = port_id(device_id(str(attached_mac)), str(attached_port))
            if not ctx.builder.has_port(pid):
                gap("wired client telemetry: unknown port attachment", index)
                continue
            if ctx.builder.has_client(str(w["mac"])):
                continue
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
        for reason, indexes in gaps.items():
            ctx.builder.mark_client_telemetry_gap(
                f"{reason} ({len(indexes)} row(s); sample indexes: {indexes[:3]})"
            )
        return frozenset({IRCapability.CLIENTS_ACTIVE}) if not gaps else frozenset()
