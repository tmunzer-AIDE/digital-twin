"""Clients-domain ingester: observed wired + wireless clients (active now).

- Unattachable observations are recorded as coverage gaps without failing ingest.
  Unambiguous rows remain available; incomplete telemetry cannot earn clients.active.
- Consistent duplicate observations of the same kind are coalesced. Conflicting
  attachment, VLAN or SSID identities invalidate evidence of that kind only.
- Wired search includes wireless and LLDP neighbor addresses learned on transit
  ports. Prefer direct AP/edge observations; transit learning and ambiguous
  search history cannot contradict those attachments or prove a direct client.
- clients.active is EARNED only if BOTH client fetches succeeded and every
  observation is consistent and attachable: an empty site
  with successful fetches legitimately knows "no clients"; a failed fetch must
  not masquerade as that knowledge.
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
    """Accept scalar or singleton history; never guess from multiple values."""
    if isinstance(value, list):
        return value[0] if len(value) == 1 else None
    return value


def _wired_attachment(row: Mapping[str, Any]) -> tuple[Any, Any, Any]:
    """Search history is list-shaped; last_* gives the latest learned port.

    It still may be a transit port, rather than a direct client attachment.
    A partially supplied last_* pair must not fall back to historical fields.
    """
    if row.get("last_device_mac") or row.get("last_port_id"):
        vlan = row["last_vlan"] if "last_vlan" in row else row.get("vlan")
        return (_single(row.get("last_device_mac")), _single(row.get("last_port_id")),
                _single(vlan))
    return (_single(row.get("device_mac")), _single(row.get("port_id")),
            _single(row.get("vlan")))


def _ambiguous_wired_history(row: Mapping[str, Any]) -> bool:
    return not (row.get("last_device_mac") or row.get("last_port_id")) and any(
        isinstance(value := row.get(key), list) and len(value) > 1
        for key in ("device_mac", "port_id")
    )


class ClientsIngester:
    name = "clients"

    def produces(self) -> frozenset[str]:  # potential supply
        return frozenset({IRCapability.CLIENTS_ACTIVE})

    def ingest(self, ctx: IngestContext) -> frozenset[str]:
        fetched = ctx.raw.meta.fetched
        if "wireless_clients" not in fetched or "wired_clients" not in fetched:
            return frozenset()  # failed fetch -> no claim (zero clients != unknown)
        gaps: dict[str, list[int]] = {}
        candidates: dict[tuple[ClientKind, str], Client] = {}
        disputed: set[tuple[ClientKind, str]] = set()
        unattachable: set[tuple[ClientKind, str]] = set()
        linked_ports = ctx.builder.linked_port_ids()
        transit_rows: list[tuple[str, int]] = []

        def gap(reason: str, index: int) -> None:
            gaps.setdefault(reason, []).append(index)

        def admit(client: Client, index: int, domain: str) -> None:
            key = (client.kind, client.mac)
            if key in disputed:
                gap(f"{domain} client telemetry: conflicting duplicate identity", index)
                return
            existing = candidates.get(key)
            if existing is None and ctx.builder.has_client(client.mac):
                previous = ctx.builder.get_client(client.mac)
                if previous.kind is client.kind:
                    existing = previous
            if existing is not None:
                conflict = (
                    (existing.kind, existing.attach_kind, existing.attach_id)
                    != (client.kind, client.attach_kind, client.attach_id)
                    or existing.vlan is not None and client.vlan is not None
                    and existing.vlan != client.vlan
                    or existing.ssid is not None and client.ssid is not None
                    and existing.ssid != client.ssid
                )
                if conflict:
                    candidates.pop(key, None)
                    disputed.add(key)
                    gap(f"{domain} client telemetry: conflicting duplicate identity", index)
                else:
                    # Retain known identity fields across compatible rows so a
                    # later contradictory value cannot hide behind an initial
                    # missing SSID/VLAN (including an LLDP-created client).
                    candidates[key] = replace(
                        existing, vlan=existing.vlan if existing.vlan is not None else client.vlan,
                        ssid=existing.ssid or client.ssid, ip=existing.ip or client.ip,
                    )
                return
            candidates[key] = client

        for index, w in enumerate(ctx.raw.wireless_clients):
            if not w.get("mac") or not w.get("ap_mac"):
                gap("wireless client telemetry: missing mac or ap_mac", index)
                if w.get("mac"):
                    unattachable.add((ClientKind.WIRELESS, client_id(str(w["mac"]))))
                continue
            ap = device_id(str(w["ap_mac"]))
            if not ctx.builder.has_device(ap):
                gap("wireless client telemetry: unknown AP attachment", index)
                unattachable.add((ClientKind.WIRELESS, client_id(str(w["mac"]))))
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
            attached_mac, attached_port, vlan = _wired_attachment(w)
            if not w.get("mac") or not attached_mac or not attached_port:
                gap("wired client telemetry: missing mac, device_mac or port_id", index)
                # A history set without a current port is a population gap,
                # not a contradictory current sighting of an LLDP neighbor.
                if w.get("mac") and not _ambiguous_wired_history(w):
                    unattachable.add((ClientKind.WIRED, client_id(str(w["mac"]))))
                continue
            mac = client_id(str(w["mac"]))
            pid = port_id(device_id(str(attached_mac)), str(attached_port))
            if not ctx.builder.has_port(pid):
                gap("wired client telemetry: unknown port attachment", index)
                unattachable.add((ClientKind.WIRED, mac))
                continue
            if pid in linked_ports or ctx.builder.get_port(pid).is_uplink is True:
                # MAC learning on a managed link (including an AP uplink) does
                # not locate the endpoint. Keep independent direct evidence;
                # otherwise report incomplete population coverage.
                transit_rows.append((mac, index))
                continue
            admit(
                Client(
                    mac=mac,
                    kind=ClientKind.WIRED,
                    attach_kind=AttachKind.PORT,
                    attach_id=pid,
                    vlan=int(vlan) if vlan is not None else None,
                    ip=_single(w.get("last_ip", w.get("ip"))),
                ), index, "wired",
            )
        excluded = disputed | unattachable
        selected: dict[str, Client] = {}
        for key, client in candidates.items():
            if key not in excluded:
                # An AP association locates a wireless endpoint more directly
                # than switch MAC learning. The IR has one entity per MAC.
                if client.mac not in selected or client.kind is ClientKind.WIRELESS:
                    selected[client.mac] = client
        # Resolve transit coverage after all direct rows, so input order cannot
        # decide whether a learned MAC is already located by another source.
        for mac, index in transit_rows:
            previous = ctx.builder.get_client(mac) if ctx.builder.has_client(mac) else None
            if mac not in selected and (
                previous is None or (previous.kind, mac) in excluded
            ):
                gap("wired client telemetry: transit port without direct attachment", index)
        # Withdraw only disputed evidence of the same kind. A bad wired search
        # row cannot erase a valid wireless association (or vice versa).
        ctx.builder.discard_clients(
            mac for kind, mac in excluded
            if mac not in selected and ctx.builder.has_client(mac)
            and ctx.builder.get_client(mac).kind is kind
        )
        for mac, client in selected.items():
            if ctx.builder.has_client(mac):
                ctx.builder.replace_client(client)
            else:
                ctx.builder.add_client(client)
        for reason, indexes in gaps.items():
            ctx.builder.mark_client_telemetry_gap(
                f"{reason} ({len(indexes)} row(s); sample indexes: {indexes[:3]})"
            )
        return frozenset({IRCapability.CLIENTS_ACTIVE}) if not gaps else frozenset()
