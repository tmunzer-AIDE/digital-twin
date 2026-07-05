"""Stable-state STP tree prediction (Spec-4). Pure — no I/O, no findings.

THE INVARIANT: prediction alone never earns SAFE; every future verdict-facing
consumer must call analysis/stp_agreement.compare_to_observed and cap
confidence on component-level disagreement.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from digital_twin.ir.confidence import ConfidenceLevel
from digital_twin.ir.entities import DeviceRole, Port
from digital_twin.ir.indexes import node_for, vc_root_map
from digital_twin.ir.model import IR
from digital_twin.representations.graph_data import L2Edge
from digital_twin.representations.l2_graph import build_l2_graph

DEFAULT_PRIORITY = 32768
ABSTAIN = "abstain"

_IEEE_COST = {
    "10m": 2_000_000,
    "100m": 200_000,
    "1g": 20_000,
    "2.5g": 8_000,
    "5g": 4_000,
    "10g": 2_000,
    "25g": 800,
    "40g": 500,
    "100g": 200,
}
_DEFAULT_COST_KEY = "1g"


def root_of(ir: IR, component: frozenset[str]) -> tuple[str, bool] | str | None:
    """(root device id, any-default-assumed) for the component's switches —
    None when fewer than two switches (no election to disturb), ABSTAIN when
    an uninterpretable priority makes the election unpredictable (the caller
    must surface that as PARTIAL coverage, never a clean pass)."""
    switches = [d for d in component if ir.devices[d].role is DeviceRole.SWITCH]
    if len(switches) < 2:
        return None
    if any(ir.devices[d].stp_priority_invalid for d in switches):
        return ABSTAIN
    assumed = any(ir.devices[d].stp_priority is None for d in switches)

    def election_key(d: str) -> tuple[int, str]:
        prio = ir.devices[d].stp_priority
        # explicit `is None`: 0 is a VALID priority — the strongest one
        return (DEFAULT_PRIORITY if prio is None else prio, d)

    return min(switches, key=election_key), assumed


# --- public result contracts -------------------------------------------------


@dataclass(frozen=True)
class PortPrediction:
    port_id: str
    role: str  # "root" | "designated" | "alternate" | "backup"
    state: str  # "forwarding" | "blocking"
    confidence: ConfidenceLevel
    deciding_factor: str  # "cost"|"bridge_id"|"port_id_tie"|"sole_path"|"root_bridge"
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ComponentTree:
    nodes: frozenset[str]
    root: str | None
    root_assumed_default: bool
    ports: Mapping[str, PortPrediction]


@dataclass(frozen=True)
class StpTreePrediction:
    components: tuple[ComponentTree, ...]
    notes: tuple[str, ...]


# --- internal topology view --------------------------------------------------


@dataclass(frozen=True)
class _End:
    """One side of a logical edge."""

    node: str  # VC-folded owner
    ports: tuple[str, ...]  # >1 iff LAG bundle members
    cost: int  # IEEE cost of the BEST member end (min)
    cost_defaulted: bool  # any contributing speed unknown -> True
    lag: bool


@dataclass(frozen=True)
class _ActiveEdge:
    key: str  # stable id (sorted link_ids joined)
    a: _End
    b: _End
    link_confidence: ConfidenceLevel


@dataclass(frozen=True)
class _PseudoEdge:
    """Same-bridge self-loop pair."""

    node: str
    port_a: str  # deterministic: min(port name) first
    port_b: str


@dataclass(frozen=True)
class _ActiveTopology:
    edges: tuple[_ActiveEdge, ...]
    pseudo_edges: tuple[_PseudoEdge, ...]
    notes: tuple[str, ...] = field(default_factory=tuple)


def _port_cost(port: Port) -> tuple[int, bool]:
    """(IEEE cost, defaulted) for one member end: observed_speed wins over
    configured speed; an unknown speed defaults to the 1G value and flags it."""
    speed = port.observed_speed or port.speed
    if speed is None or speed not in _IEEE_COST:
        return _IEEE_COST[_DEFAULT_COST_KEY], True
    return _IEEE_COST[speed], False


def _port_excluded(port: Port) -> bool:
    return port.disabled or port.bpdu_filter


def _end_for(ir: IR, vc_root: dict[str, str], node: str, member_ports: list[str]) -> _End | None:
    """Build the node's _End from its member ports on this logical edge,
    dropping individually-excluded members. None iff EVERY member is excluded."""
    own_members = sorted(
        {pid for pid in member_ports if node_for(vc_root, ir.port(pid).device_id) == node}
    )
    surviving = [pid for pid in own_members if not _port_excluded(ir.port(pid))]
    if not surviving:
        return None
    costs = [_port_cost(ir.port(pid)) for pid in surviving]
    cost = min(c for c, _ in costs)
    defaulted = any(d for _, d in costs)
    return _End(
        node=node,
        ports=tuple(surviving),
        cost=cost,
        cost_defaulted=defaulted,
        lag=len(surviving) > 1,
    )


def _active_edge(
    ir: IR, vc_root: dict[str, str], na: str, nb: str, edge: L2Edge
) -> _ActiveEdge | None:
    a_end = _end_for(ir, vc_root, na, edge.member_ports)
    b_end = _end_for(ir, vc_root, nb, edge.member_ports)
    if a_end is None or b_end is None:
        return None
    # Resolve each end's device role via one of its own surviving member ports.
    a_role_dev = ir.devices[ir.port(a_end.ports[0]).device_id]
    b_role_dev = ir.devices[ir.port(b_end.ports[0]).device_id]
    if a_role_dev.role is not DeviceRole.SWITCH or b_role_dev.role is not DeviceRole.SWITCH:
        return None
    key = "+".join(sorted(edge.link_ids))
    return _ActiveEdge(key=key, a=a_end, b=b_end, link_confidence=edge.confidence.level)


def _pseudo_edges(
    ir: IR, vc_root: dict[str, str]
) -> tuple[tuple[_PseudoEdge, ...], tuple[str, ...]]:
    """Synthesize same-bridge self-loop pseudo-edges from Port.self_loop_peer
    claims (ingest mints no same-device Link; build_l2_graph drops self edges).
    Reciprocal + both-exclusion-passing claims -> one deduped pseudo-edge per
    frozenset(port pair); one-sided claims synthesize nothing, only a note."""
    edges: list[_PseudoEdge] = []
    notes: list[str] = []
    seen: set[frozenset[str]] = set()
    # Iterate ports in sorted order for deterministic output
    for port in sorted(ir.ports.values(), key=lambda p: p.id):
        peer_id = port.self_loop_peer
        if peer_id is None:
            continue
        peer = ir.ports.get(peer_id)
        if peer is None or peer.self_loop_peer != port.id:
            notes.append(
                f"one-sided self-loop claim on {port.id} (peer {peer_id} does not "
                "reciprocate) — unconfirmed physical loop, no pseudo-edge synthesized"
            )
            continue
        if _port_excluded(port) or _port_excluded(peer):
            continue
        pair = frozenset((port.id, peer.id))
        if pair in seen:
            continue
        seen.add(pair)
        node = node_for(vc_root, port.device_id)
        port_a, port_b = sorted((port.id, peer.id))
        edges.append(_PseudoEdge(node=node, port_a=port_a, port_b=port_b))
    # Sort edges by (node, port_a, port_b) and notes by content for determinism
    edges.sort(key=lambda e: (e.node, e.port_a, e.port_b))
    notes.sort()
    return tuple(edges), tuple(notes)


def active_topology(ir: IR) -> _ActiveTopology:
    """Build the STP-active subgraph (edges + synthesized pseudo-edges) ahead
    of election/roles: excludes disabled/bpdu_filter ports (a single excluded
    LAG member only drops that member), links with any excluded end or any
    non-SWITCH-role end, and folds VC members to their root."""
    vc_root = vc_root_map(ir)
    graph = build_l2_graph(ir)
    edges: list[_ActiveEdge] = []
    for na, nb, edge_data in graph.edges(data="data"):
        active = _active_edge(ir, vc_root, na, nb, edge_data)
        if active is not None:
            edges.append(active)
    edges.sort(key=lambda e: e.key)
    pseudo_edges, notes = _pseudo_edges(ir, vc_root)
    return _ActiveTopology(edges=tuple(edges), pseudo_edges=pseudo_edges, notes=notes)
