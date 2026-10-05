"""Clients-domain ingester: observed wired + wireless clients (active now).

- Unattachable observations are recorded as coverage gaps without failing ingest.
  Valid rows remain available, but incomplete telemetry cannot earn clients.active.
- A MAC already present (e.g. added by LldpIngester as an unmanaged edge device)
  is skipped — first writer wins, no duplicate-id crash.
- clients.active is EARNED only if BOTH client fetches succeeded and every
  non-duplicate observation could be attached: an empty site
  with successful fetches legitimately knows "no clients"; a failed fetch must
  not masquerade as that knowledge.
- A wired row is the MAC's MAC-table history (see _sightings). A MAC is learned on
  every switch along its path, so its attachment is its ONE current edge port:
  sightings on inter-device links, Mist-flagged uplinks and LAG bundles are
  transit. Several edge ports or VLANs are a conflicting identity and stay a gap,
  never collapsed onto one; so does a MAC seen only in transit (its edge port is
  unobserved). A MAC seen only through an AP is wireless — the wireless stats are
  its authority — and a managed device's own MAC is infrastructure: neither is a
  wired client nor a gap.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
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
    """One value from a field Mist's wired-client SEARCH returns as the full
    history list: a one-element list is unambiguous, a longer one is not (None)."""
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
            mac = w.get("mac")
            if mac and ctx.builder.has_device(device_id(str(mac))):
                continue  # a managed device's own MAC: infrastructure, not a client
            sightings = _sightings(w)
            if not mac or sightings == []:
                gap("wired client telemetry: missing mac, device_mac or port_id", index)
                continue
            if sightings is None:
                gap("wired client telemetry: unpaired multi-port history", index)
                continue
            placed = _place(ctx.builder, sightings)
            if placed.gap is not None:
                gap(f"wired client telemetry: {placed.gap}", index)
                continue
            if placed.port is None or ctx.builder.has_client(str(mac)):
                continue
            ctx.builder.add_client(
                Client(
                    mac=client_id(str(mac)),
                    kind=ClientKind.WIRED,
                    attach_kind=AttachKind.PORT,
                    attach_id=placed.port,
                    vlan=int(placed.vlan) if placed.vlan is not None else None,
                    ip=_single(w.get("ip")),
                )
            )
        for reason, indexes in gaps.items():
            ctx.builder.mark_client_telemetry_gap(
                f"{reason} ({len(indexes)} row(s); sample indexes: {indexes[:3]})"
            )
        return frozenset({IRCapability.CLIENTS_ACTIVE}) if not gaps else frozenset()
