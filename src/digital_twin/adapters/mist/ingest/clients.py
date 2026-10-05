"""Clients-domain ingester: observed wired + wireless clients (active now).

- Unattachable observations are recorded as coverage gaps without failing ingest.
  Unambiguous rows remain available; incomplete telemetry cannot earn clients.active.
- Consistent duplicate observations of the same kind are coalesced. Conflicting
  attachment, VLAN or SSID identities invalidate evidence of that kind only.
- Wired search includes wireless and LLDP neighbor addresses learned on transit
  ports. Prefer direct AP/edge observations; transit learning and ambiguous
  search history cannot contradict those attachments or prove a direct client.
- Place wired clients using their current per-port sightings, excluding old
  history, LAG bundles, managed infrastructure and transit learning. The newest
  last_* observation alone is not a proof of the endpoint's edge attachment.
- A direct wired attachment competing with an AP association creates a coverage
  gap. Retain the wireless proof, but do not claim a complete client population.
- clients.active is EARNED only if BOTH client fetches succeeded and every
  observation is consistent and attachable: an empty site
  with successful fetches legitimately knows "no clients"; a failed fetch must
  not masquerade as that knowledge.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from digital_twin.ir import (
    AttachKind,
    Client,
    ClientKind,
    DeviceRole,
    IRBuilder,
    IRCapability,
    client_id,
    device_id,
    port_id,
)

from .base import IngestContext

# The switches' reports of one path land within one MAC-table report cycle of the
# MAC's newest sighting (recorded: edge sightings <=5 min, transit <=15 min behind);
# an older sighting is where the MAC used to be, not where it is.
_CURRENT = timedelta(minutes=15)
# Junos aggregated-Ethernet bundle: LLDP links sit on its member ports, so the
# bundle itself carries no Link even when it is an inter-switch trunk.
_AGGREGATE = re.compile(r"ae\d+")

_Sighting = tuple[Any, Any, Any, datetime | None]  # (device_mac, port_id, vlan, when)


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


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return [] if value is None else [value]


def _when(value: Any) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None
    return when.replace(tzinfo=UTC) if when is not None and when.tzinfo is None else when


def _sightings(row: Mapping[str, Any]) -> list[_Sighting] | None:
    """Every (device_mac, port_id, vlan, when) the row reports; None when its
    devices and ports cannot be paired. Mist's wired-client search returns one row
    per MAC: device_mac_port holds one record per sighting, device_mac / port_id /
    vlan are de-duplicated lists that cannot be zipped, and last_* is merely the
    newest sighting. A row without per-sighting records (older / hand-written
    shape) is one sighting per vlan when it names a single device and port."""
    records = row.get("device_mac_port")
    if isinstance(records, list) and records:
        return [
            (r.get("device_mac"), r.get("port_id"), r.get("vlan"), _when(r.get("when")))
            if isinstance(r, Mapping)
            else (None, None, None, None)
            for r in records
        ]
    if (row.get("last_device_mac") or row.get("last_port_id")) and not (
        row.get("last_device_mac") and row.get("last_port_id")
    ):
        return [(row.get("last_device_mac"), row.get("last_port_id"), row.get("last_vlan"), None)]
    devices, ports = _as_list(row.get("device_mac")), _as_list(row.get("port_id"))
    if len(devices) > 1 or len(ports) > 1:
        return None
    seen: list[_Sighting] = []
    if devices and ports:
        seen = [(devices[0], ports[0], vlan, None) for vlan in _as_list(row.get("vlan")) or [None]]
    if row.get("last_device_mac") and row.get("last_port_id"):
        seen.append((row["last_device_mac"], row["last_port_id"], row.get("last_vlan"), None))
    return seen


@dataclass(frozen=True)
class _Placement:
    port: str | None = None  # the edge port the client attaches to
    vlan: Any = None
    gap: str | None = None  # why the row cannot be placed


def _place(builder: IRBuilder, sightings: list[_Sighting]) -> _Placement:
    """The row's attachment from its current sightings; a placement with neither
    port nor gap means the MAC is not a wired client (reached through an AP)."""
    newest = max((when for *_, when in sightings if when is not None), default=None)
    edges: dict[str, set[Any]] = {}
    via_ap = False
    for device_mac, port, vlan, when in sightings:
        if when is not None and newest is not None and newest - when > _CURRENT:
            continue  # history: where the MAC used to be
        pid = port_id(device_id(str(device_mac)), str(port)) if device_mac and port else None
        if pid is None or not builder.has_port(pid):
            return _Placement(gap="unknown port attachment")
        peers = builder.link_peers(pid)
        if any(
            builder.has_device(d) and builder.get_device(d).role is DeviceRole.AP
            for d in (peer.partition(":")[0] for peer in peers)
        ):
            via_ap = True
        elif not (peers or builder.get_port(pid).is_uplink or _AGGREGATE.fullmatch(str(port))):
            edges.setdefault(pid, set()).update(() if vlan is None else (vlan,))
    if len(edges) > 1 or any(len(vlans) > 1 for vlans in edges.values()):
        return _Placement(gap="conflicting attachments (several edge ports or VLANs)")
    if edges:
        ((pid, vlans),) = edges.items()
        return _Placement(port=pid, vlan=next(iter(vlans), None))
    if via_ap:
        return _Placement()
    return _Placement(gap="learned only on inter-switch links (edge port not observed)")


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
            other_kind = (
                ClientKind.WIRELESS if client.kind is ClientKind.WIRED else ClientKind.WIRED
            )
            other = candidates.get((other_kind, client.mac))
            if other is None and ctx.builder.has_client(client.mac):
                previous = ctx.builder.get_client(client.mac)
                if previous.kind is other_kind:
                    other = previous
            if other is not None:
                wired = client if client.kind is ClientKind.WIRED else other
                if (
                    wired.attach_id not in linked_ports
                    and ctx.builder.get_port(wired.attach_id).is_uplink is not True
                    and not _AGGREGATE.fullmatch(ctx.builder.get_port(wired.attach_id).name)
                ):
                    # A normal access-port sighting is a competing attachment,
                    # not incidental learning behind an AP. Preserve wireless
                    # outage evidence while revoking complete client coverage.
                    gap(
                        f"{domain} client telemetry: conflicting wired and wireless attachment",
                        index,
                    )
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
            raw_mac = w.get("mac")
            if raw_mac and ctx.builder.has_device(device_id(str(raw_mac))):
                continue  # managed infrastructure is not a client
            sightings = _sightings(w)
            if not raw_mac or sightings == []:
                gap("wired client telemetry: missing mac, device_mac or port_id", index)
                if raw_mac:
                    unattachable.add((ClientKind.WIRED, client_id(str(raw_mac))))
                continue
            mac = client_id(str(raw_mac))
            if sightings is None:
                # An unpaired history is incomplete population evidence, not a
                # competing current attachment of an independently located MAC.
                gap("wired client telemetry: unpaired multi-port history", index)
                continue
            placed = _place(ctx.builder, sightings)
            if placed.gap is not None:
                if placed.gap.startswith("learned only"):
                    transit_rows.append((mac, index))
                else:
                    gap(f"wired client telemetry: {placed.gap}", index)
                    unattachable.add((ClientKind.WIRED, mac))
                continue
            if placed.port is None:
                continue  # AP-facing learning: wireless stats are authoritative
            admit(
                Client(
                    mac=mac,
                    kind=ClientKind.WIRED,
                    attach_kind=AttachKind.PORT,
                    attach_id=placed.port,
                    vlan=int(placed.vlan) if placed.vlan is not None else None,
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
                gap("wired client telemetry: transit port without direct attachment "
                    "(learned only on inter-switch links; edge port not observed)", index)
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
