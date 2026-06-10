"""L2 representation: link VLAN math + the device-level L2 multigraph.

Pure structural views — no algorithms with verdicts, no severity. Edges are derived
from specific ports, so a port-level config change changes the edge (and is detected).

AP-uplink edges are VLAN-TRANSPARENT: an AP bridges whatever its switch port
delivers and its own eth port carries no vlan facts (the lldp ingester cannot
invent them), so when exactly one link end is an AP-role device the edge carries
the SWITCH side's offered set (tagged + native).

The same honesty rule covers a VLAN-BLIND switch peer: a stat-ensured port
(OBSERVED, no vlan facts — e.g. absent from the peer's port_config; found in
real use 2026-06-10) is "carriage unknown", NOT "carries nothing" — computing
the intersection would silently hide every severance of the configured side.
Such an edge carries the CONFIGURED side's offered set with confidence capped
at MEDIUM (assumed, not verified). Two config-known ends keep exact
intersection semantics — a configured empty trunk is a real statement.
"""

from __future__ import annotations

import networkx as nx

from digital_twin.ir.confidence import Confidence, ConfidenceLevel, min_confidence
from digital_twin.ir.entities import DeviceRole, Link, LinkKind, Port, PortMode
from digital_twin.ir.indexes import node_for, vc_root_map
from digital_twin.ir.model import IR
from digital_twin.ir.provenance import Provenance

from .graph_data import L2Edge

_ASSUMED = Confidence(
    level=ConfidenceLevel.MEDIUM,
    reasons=("peer port has no vlan facts — carriage assumed from the configured side",),
)


def _vlan_blind(port: Port) -> bool:
    """A port with no vlan facts whose facts are NOT a config statement:
    stat-ensured (OBSERVED) or unresolved-usage (INFERRED). Carriage UNKNOWN —
    a CONFIG empty trunk, by contrast, genuinely carries nothing."""
    return (
        port.meta.provenance in (Provenance.OBSERVED, Provenance.INFERRED)
        and port.native_vlan is None
        and not port.tagged_vlans
    )


def _tagged(port: Port) -> set[int]:
    return set(port.tagged_vlans) if port.mode is PortMode.TRUNK else set()


def _offered(port: Port) -> set[int]:
    out = _tagged(port)
    if port.native_vlan is not None:
        out = out | {port.native_vlan}
    return out


def link_carried_vlans(port_a: Port, port_b: Port) -> set[int]:
    """Tagged intersection (trunks) ∪ the native VLAN when both natives match.

    An access port presents its VLAN untagged, so it joins a trunk only via the
    trunk's native — never a tagged VLAN.
    """
    carried = _tagged(port_a) & _tagged(port_b)
    if port_a.native_vlan is not None and port_a.native_vlan == port_b.native_vlan:
        carried.add(port_a.native_vlan)
    return carried


def _bundle_key(link: Link, na: str, nb: str) -> tuple[frozenset[str], str] | None:
    """A stable key for a LAG/MCLAG bundle on a node pair, or None for standalone links."""
    if link.kind in (LinkKind.LAG, LinkKind.MCLAG) and link.bundle_id is not None:
        return (frozenset((na, nb)), link.bundle_id)
    return None


def _edge_data(link: Link, pa: Port, pb: Port, vlans: set[int], *, assumed: bool) -> L2Edge:
    confidence = link.meta.confidence
    if assumed:  # blind-peer carriage is an assumption, never better than MEDIUM
        confidence = min_confidence(confidence, _ASSUMED)
    return L2Edge(
        vlans=set(vlans),
        kind=link.kind.value,
        bundle_id=link.bundle_id,
        link_ids=[link.id],
        member_ports=[pa.id, pb.id],
        confidence=confidence,
    )


def build_l2_graph(ir: IR) -> nx.MultiGraph:
    """Device-level L2 multigraph with port-derived edges (payload: ``data`` -> L2Edge).

    LAG/MCLAG links sharing (node-pair, bundle_id) collapse to ONE logical edge (vlans
    unioned, confidence = min over members, member_ports accumulated); standalone links
    each get their own edge (parallel = a cycle); VC-internal links are dropped.
    """
    g: nx.MultiGraph = nx.MultiGraph()
    vc_root = vc_root_map(ir)
    for dev in ir.devices.values():
        if dev.id not in vc_root:  # members fold into their VC root
            g.add_node(dev.id)

    bundle_keys: dict[tuple[frozenset[str], str], object] = {}
    for link in ir.links:
        pa, pb = ir.port(link.a_port), ir.port(link.b_port)
        na, nb = node_for(vc_root, pa.device_id), node_for(vc_root, pb.device_id)
        if na == nb:
            continue  # VC-internal / self
        if pa.disabled or pb.disabled:
            continue  # an admin-disabled end kills the link physically
        a_is_ap = ir.devices[pa.device_id].role is DeviceRole.AP
        b_is_ap = ir.devices[pb.device_id].role is DeviceRole.AP
        a_blind, b_blind = _vlan_blind(pa), _vlan_blind(pb)
        assumed = False
        if a_is_ap != b_is_ap:  # exactly one end is an AP: vlan-transparent bridge,
            vlans = _offered(pb if a_is_ap else pa)  # the switch side defines delivery
        elif a_blind != b_blind:  # one end's carriage is UNKNOWN: deliver the
            vlans = _offered(pb if a_blind else pa)  # configured side, capped MEDIUM
            assumed = True
        else:
            vlans = link_carried_vlans(pa, pb)
        bkey = _bundle_key(link, na, nb)
        if bkey is not None and bkey in bundle_keys:
            edge: L2Edge = g[na][nb][bundle_keys[bkey]]["data"]
            edge.vlans |= vlans
            edge.link_ids.append(link.id)
            edge.member_ports.extend((pa.id, pb.id))
            edge.confidence = min_confidence(edge.confidence, link.meta.confidence)
            if assumed:  # an assumed member's carriage caps the whole bundle
                edge.confidence = min_confidence(edge.confidence, _ASSUMED)
            continue
        key = g.add_edge(na, nb, data=_edge_data(link, pa, pb, vlans, assumed=assumed))
        if bkey is not None:
            bundle_keys[bkey] = key
    return g
