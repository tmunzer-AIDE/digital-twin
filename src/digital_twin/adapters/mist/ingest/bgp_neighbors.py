"""BGP neighbor telemetry ingester (GS28). OBSERVATIONAL, SELF-ISOLATING: it
never lets an exception reach IngesterRegistry.run. Earns BGP_TELEMETRY iff the
bgp_neighbors fetch succeeded (shape reachable, incl. genuinely-zero). A row with
no usable (mac, peer_ip) is COUNTED as unparsed (not silently dropped) so a
partially unrecognized fetch reads telemetry-blind, never 'no neighbors'."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from digital_twin.ir import BgpNeighbor, IRCapability, device_id

from .base import IngestContext

_Json = Mapping[str, Any]


def _clean(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _as_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _row_to_neighbor(row: _Json) -> BgpNeighbor | None:
    # Field names confirmed against the Mist OAS (bgp_peers) at build; fail-soft.
    mac = _clean(row.get("mac"))
    peer_ip = _clean(row.get("peer_ip") or row.get("neighbor"))
    if not mac or not peer_ip:
        return None                       # unusable -> caller counts it unparsed
    up_raw = row.get("up")
    return BgpNeighbor(
        device_id=device_id(mac),
        peer_ip=peer_ip,
        state=_clean(row.get("state") or row.get("status")) or "",
        up=(bool(up_raw) if isinstance(up_raw, bool) else None),
        neighbor_as=_as_int(row.get("neighbor_as")),
        vrf=_clean(row.get("vrf_name") or row.get("vrf")),
    )


def build_bgp_neighbors(rows: tuple[_Json, ...]) -> tuple[list[BgpNeighbor], int]:
    neighbors: list[BgpNeighbor] = []
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


class BgpNeighborIngester:
    """Earns BGP_TELEMETRY on fetch-success; publishes neighbors + unparsed count."""

    name = "bgp_neighbors"

    def produces(self) -> frozenset[str]:
        return frozenset({IRCapability.BGP_TELEMETRY})

    def ingest(self, ctx: IngestContext) -> frozenset[str]:
        if "bgp_neighbors" not in ctx.raw.meta.fetched:
            return frozenset()            # not fetched -> telemetry-blind, no claim
        try:
            neighbors, unparsed = build_bgp_neighbors(tuple(ctx.raw.bgp_neighbors))
            ctx.builder.set_bgp_neighbors(neighbors, unparsed)
        except Exception:  # noqa: BLE001 — best-effort: degrade to blind, never fatal
            return frozenset()
        return frozenset({IRCapability.BGP_TELEMETRY})
