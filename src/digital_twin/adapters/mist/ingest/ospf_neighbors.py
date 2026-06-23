"""OSPF neighbor telemetry ingester (GS27). OBSERVATIONAL, SELF-ISOLATING: it
never lets an exception reach IngesterRegistry.run. Earns OSPF_TELEMETRY iff the
site_ospf fetch succeeded (shape reachable, incl. genuinely-zero). A row with no
usable peer_ip is COUNTED as unparsed (not silently dropped) so a partially
unrecognized fetch reads telemetry-blind, never 'no neighbors'."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from digital_twin.ir import IRCapability, OspfNeighbor, device_id

from .base import IngestContext

_Json = Mapping[str, Any]


def _clean(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _row_to_neighbor(row: _Json) -> OspfNeighbor | None:
    # Field names confirmed against the Mist OAS (ospf_peers) at build; fail-soft.
    mac = _clean(row.get("mac"))
    peer_ip = _clean(row.get("peer_ip") or row.get("neighbor_ip"))
    if not mac or not peer_ip:
        return None                       # unusable -> caller counts it unparsed
    return OspfNeighbor(
        device_id=device_id(mac),
        peer_ip=peer_ip,
        area=_clean(row.get("area")),
        state=_clean(row.get("state") or row.get("status")) or "",
        vrf=_clean(row.get("vrf_name") or row.get("vrf")),
        neighbor_router_id=_clean(row.get("neighbor_router_id") or row.get("router_id")),
    )


def build_ospf_neighbors(rows: tuple[_Json, ...]) -> tuple[list[OspfNeighbor], int]:
    neighbors: list[OspfNeighbor] = []
    unparsed = 0
    for row in rows:
        try:
            n = _row_to_neighbor(row)
        except Exception:  # noqa: BLE001 — one bad row never drops the batch
            n = None
        if n is None:
            unparsed += 1
        else:
            neighbors.append(n)
    return neighbors, unparsed


class OspfNeighborIngester:
    """Earns OSPF_TELEMETRY on fetch-success; publishes neighbors + unparsed count."""

    name = "ospf_neighbors"

    def produces(self) -> frozenset[str]:
        return frozenset({IRCapability.OSPF_TELEMETRY})

    def ingest(self, ctx: IngestContext) -> frozenset[str]:
        if "ospf_neighbors" not in ctx.raw.meta.fetched:
            return frozenset()            # not fetched -> telemetry-blind, no claim
        try:
            neighbors, unparsed = build_ospf_neighbors(tuple(ctx.raw.ospf_neighbors))
            ctx.builder.set_ospf_neighbors(neighbors, unparsed)
        except Exception:  # noqa: BLE001 — best-effort: degrade to blind, never fatal
            return frozenset()
        return frozenset({IRCapability.OSPF_TELEMETRY})
