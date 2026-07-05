"""Stable-state STP tree prediction (Spec-4). Pure — no I/O, no findings.

THE INVARIANT: prediction alone never earns SAFE; every future verdict-facing
consumer must call analysis/stp_agreement.compare_to_observed and cap
confidence on component-level disagreement.
"""
from __future__ import annotations

import dataclasses
import heapq
import itertools
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


# --- election + directed root-path-cost (RPC), with taint tracking ----------


@dataclass(frozen=True)
class _Rpc:
    """One settled node's root-path-cost, folded from the root down."""

    cost: int
    defaulted: bool  # OR of every defaulted port cost along the path
    link_conf: ConfidenceLevel  # MIN link confidence along the path


@dataclass(frozen=True)
class _ComponentElection:
    """Per-component election + RPC result — the input Task 4's role
    assignment walks (candidates = every edge-end entering B with
    rpc(neighbor).cost + end.cost, etc.)."""

    root: str | None
    root_assumed_default: bool
    abstained: bool
    rpc: Mapping[str, _Rpc]
    note: str | None = None


def _end_for_node(edge: _ActiveEdge, node: str) -> tuple[_End, _End]:
    """(entering end, neighbor's end) for traversal INTO `node` across `edge`."""
    if edge.a.node == node:
        return edge.a, edge.b
    return edge.b, edge.a


def component_rpc(
    ir: IR, topology: _ActiveTopology, component: frozenset[str]
) -> _ComponentElection:
    """Elect the component's root and compute every other node's directed,
    tainted root-path-cost via a hand-rolled Dijkstra. Same-bridge pseudo-edges
    NEVER enter this graph — they exist only for role classification later."""
    switches = [d for d in component if ir.devices[d].role is DeviceRole.SWITCH]
    pseudo_nodes = {pe.node for pe in topology.pseudo_edges if pe.node in component}

    # Trivial-root rule FIRST: exactly one switch node AND >=1 pseudo-edge on
    # it -> root = that switch; root_of's <2-switches->None semantics stay
    # untouched (its consumers rely on that exact contract).
    if len(switches) == 1 and switches[0] in pseudo_nodes:
        root = switches[0]
        return _ComponentElection(
            root=root,
            root_assumed_default=False,
            abstained=False,
            rpc={root: _Rpc(cost=0, defaulted=False, link_conf=ConfidenceLevel.HIGH)},
        )

    elected = root_of(ir, component)
    if elected is None:
        # <2 switches, no pseudo-edge: no predictions, no note.
        return _ComponentElection(root=None, root_assumed_default=False, abstained=False, rpc={})
    if not isinstance(elected, tuple):
        return _ComponentElection(
            root=None,
            root_assumed_default=False,
            abstained=True,
            rpc={},
            note=(
                f"component of {len(component)} devices: uninterpretable bridge "
                "priority — root election abstained"
            ),
        )
    root, root_assumed_default = elected

    # Build adjacency restricted to this component's edges only (pseudo-edges
    # never enter the Dijkstra graph).
    adjacency: dict[str, list[_ActiveEdge]] = {}
    for edge in topology.edges:
        if edge.a.node not in component or edge.b.node not in component:
            continue
        adjacency.setdefault(edge.a.node, []).append(edge)
        adjacency.setdefault(edge.b.node, []).append(edge)

    rpc: dict[str, _Rpc] = {root: _Rpc(cost=0, defaulted=False, link_conf=ConfidenceLevel.HIGH)}
    counter = itertools.count()
    # Heap entries ordered by PRIMITIVES ONLY, with a monotonic counter before
    # the payload — two equal-cost parallel paths must never fall through to
    # comparing _ActiveEdge (not orderable -> TypeError).
    heap: list[tuple[int, str, int, int, str, int, _ActiveEdge]] = []
    for edge in adjacency.get(root, []):
        target = edge.b.node if edge.a.node == root else edge.a.node
        entering_end, _ = _end_for_node(edge, target)
        heapq.heappush(
            heap,
            (
                entering_end.cost,
                target,
                int(entering_end.cost_defaulted),
                int(edge.link_confidence),
                edge.key,
                next(counter),
                edge,
            ),
        )

    while heap:
        cost, node, _defaulted_flag, _conf_flag, _key, _seq, edge = heapq.heappop(heap)
        entering_end, _neighbor_end = _end_for_node(edge, node)
        pred_node = edge.b.node if edge.a.node == node else edge.a.node
        pred_rpc = rpc[pred_node]
        candidate_defaulted = pred_rpc.defaulted or entering_end.cost_defaulted
        candidate_link_conf = min(pred_rpc.link_conf, edge.link_confidence)
        settled = rpc.get(node)
        if settled is not None:
            # Already settled: a strictly worse cost is discarded (not the
            # shortest path). An EQUAL cost is another shortest path a real
            # switch could equally have taken — pessimistically MERGE its
            # taint into the settled record rather than silently dropping it.
            # (Costs can never be BETTER than the settled one: heapq pops in
            # non-decreasing cost order, so the first pop for any node is
            # already its true shortest cost.)
            if cost == settled.cost:
                rpc[node] = dataclasses.replace(
                    settled,
                    defaulted=settled.defaulted or candidate_defaulted,
                    link_conf=min(settled.link_conf, candidate_link_conf),
                )
            continue
        rpc[node] = _Rpc(
            cost=cost,
            defaulted=candidate_defaulted,
            link_conf=candidate_link_conf,
        )
        for next_edge in adjacency.get(node, []):
            neighbor = next_edge.b.node if next_edge.a.node == node else next_edge.a.node
            if neighbor in rpc:
                continue
            neighbor_entering_end, _ = _end_for_node(next_edge, neighbor)
            heapq.heappush(
                heap,
                (
                    cost + neighbor_entering_end.cost,
                    neighbor,
                    int(neighbor_entering_end.cost_defaulted),
                    int(next_edge.link_confidence),
                    next_edge.key,
                    next(counter),
                    next_edge,
                ),
            )

    return _ComponentElection(
        root=root, root_assumed_default=root_assumed_default, abstained=False, rpc=rpc
    )


# --- component construction (Task 4, deferred from Task 3) ------------------


def _components(topology: _ActiveTopology) -> tuple[frozenset[str], ...]:
    """Connected components over the union of active-edge node pairs, plus
    pseudo-edge-only nodes as their own single-node components. Deterministic:
    sorted node adjacency walk, sorted component ordering."""
    adjacency: dict[str, set[str]] = {}
    all_nodes: set[str] = set()
    for edge in topology.edges:
        adjacency.setdefault(edge.a.node, set()).add(edge.b.node)
        adjacency.setdefault(edge.b.node, set()).add(edge.a.node)
        all_nodes.add(edge.a.node)
        all_nodes.add(edge.b.node)
    for pe in topology.pseudo_edges:
        adjacency.setdefault(pe.node, set())
        all_nodes.add(pe.node)

    visited: set[str] = set()
    components: list[frozenset[str]] = []
    for start in sorted(all_nodes):
        if start in visited:
            continue
        stack = [start]
        visited.add(start)
        comp_nodes: set[str] = set()
        while stack:
            node = stack.pop()
            comp_nodes.add(node)
            for neighbor in sorted(adjacency.get(node, ())):
                if neighbor not in visited:
                    visited.add(neighbor)
                    stack.append(neighbor)
        components.append(frozenset(comp_nodes))
    components.sort(key=lambda c: sorted(c))
    return tuple(components)


# --- role assignment + confidence (Task 4) -----------------------------------

_NOTE_LAG = "LAG bundle: member ports share the bundle's elected role"
_NOTE_STP_EDGE = "edge-configured; elected normally"


def _cap(level: ConfidenceLevel, cap: ConfidenceLevel) -> ConfidenceLevel:
    return min(level, cap)


def _min_port(ports: tuple[str, ...]) -> str:
    return min(ports)


@dataclass(frozen=True)
class _Decision:
    role: str
    deciding_factor: str
    confidence: ConfidenceLevel
    lag: bool


def _make_port_predictions(
    end: _End,
    decision: _Decision,
    *,
    extra_notes: tuple[str, ...] = (),
) -> dict[str, PortPrediction]:
    state = "forwarding" if decision.role in ("root", "designated") else "blocking"
    notes = list(extra_notes)
    if end.lag:
        notes.append(_NOTE_LAG)
    out: dict[str, PortPrediction] = {}
    for pid in end.ports:
        out[pid] = PortPrediction(
            port_id=pid,
            role=decision.role,
            state=state,
            confidence=decision.confidence,
            deciding_factor=decision.deciding_factor,
            notes=tuple(notes),
        )
    return out


def _edge_end_notes(ir: IR, end: _End) -> tuple[str, ...]:
    notes = []
    if any(ir.port(pid).stp_edge for pid in end.ports):
        notes.append(_NOTE_STP_EDGE)
    return tuple(notes)


def _assign_component_roles(
    ir: IR,
    topology: _ActiveTopology,
    component: frozenset[str],
    election: _ComponentElection,
) -> ComponentTree:
    root = election.root
    ports: dict[str, PortPrediction] = {}
    decided_ends: set[tuple[str, tuple[str, ...]]] = set()  # (node, ports) already assigned

    def mark(node: str, end: _End, decision: _Decision) -> None:
        key = (node, end.ports)
        if key in decided_ends:
            return
        decided_ends.add(key)
        extra = _edge_end_notes(ir, end)
        ports.update(_make_port_predictions(end, decision, extra_notes=extra))

    # component-wide confidence cap
    comp_cap = ConfidenceLevel.MEDIUM if election.root_assumed_default else ConfidenceLevel.HIGH

    def final_conf(raw: ConfidenceLevel, lag: bool) -> ConfidenceLevel:
        level = _cap(raw, comp_cap)
        if lag:
            level = _cap(level, ConfidenceLevel.MEDIUM)
        return level

    # --- component edges/pseudo-edges restricted to this component ----------
    comp_edges = [e for e in topology.edges if e.a.node in component and e.b.node in component]
    comp_pseudo = [pe for pe in topology.pseudo_edges if pe.node in component]

    # 1. Pseudo-edges FIRST — the exception, wins over everything.
    for pe in comp_pseudo:
        port_a, port_b = pe.port_a, pe.port_b
        end_a = _End(node=pe.node, ports=(port_a,), cost=0, cost_defaulted=False, lag=False)
        end_b = _End(node=pe.node, ports=(port_b,), cost=0, cost_defaulted=False, lag=False)
        conf = final_conf(ConfidenceLevel.LOW, lag=False)
        mark(
            pe.node,
            end_a,
            _Decision("designated", "port_id_tie", conf, lag=False),
        )
        mark(
            pe.node,
            end_b,
            _Decision("backup", "port_id_tie", conf, lag=False),
        )

    if root is None:
        return ComponentTree(
            nodes=component,
            root=None,
            root_assumed_default=election.root_assumed_default,
            ports=dict(sorted(ports.items())),
        )

    # 2. Root bridge: every remaining active end on the root -> designated.
    for edge in comp_edges:
        for end in (edge.a, edge.b):
            if end.node != root:
                continue
            key = (end.node, end.ports)
            if key in decided_ends:
                continue
            other_end = edge.b if end is edge.a else edge.a
            raw_conf = min(ConfidenceLevel.HIGH, edge.link_confidence)
            if end.cost_defaulted or other_end.cost_defaulted:
                raw_conf = ConfidenceLevel.LOW
            conf = final_conf(raw_conf, lag=end.lag)
            mark(end.node, end, _Decision("designated", "root_bridge", conf, lag=end.lag))

    # 3. Root port per non-root bridge B: candidates = every edge-end entering
    #    B, keyed (rpc(neighbor).cost + B's end cost, neighbor_id,
    #    min neighbor port name, min own port name).
    incoming: dict[str, list[tuple[_ActiveEdge, _End, _End]]] = {}
    for edge in comp_edges:
        for end, neighbor_end in ((edge.a, edge.b), (edge.b, edge.a)):
            if end.node == root:
                continue
            incoming.setdefault(end.node, []).append((edge, end, neighbor_end))

    _RootPortKey = tuple[int, str, str, str]

    def _factor_vs_runner_up(best_key: _RootPortKey, other_key: _RootPortKey) -> str:
        if best_key[0] != other_key[0]:
            return "cost"
        if best_key[1] != other_key[1]:
            return "bridge_id"
        return "port_id_tie"

    # node -> sorted [(key, edge, end, neighbor_end, neighbor_rpc), ...],
    # kept around so step 5 can classify losing root-port candidates against
    # the SAME ranking (not a node-level SPT re-derivation).
    root_port_candidates: dict[
        str, list[tuple[_RootPortKey, _ActiveEdge, _End, _End, _Rpc]]
    ] = {}

    for node, candidates in incoming.items():
        keyed: list[tuple[_RootPortKey, _ActiveEdge, _End, _End, _Rpc]] = []
        for edge, end, neighbor_end in candidates:
            neighbor_rpc = election.rpc.get(neighbor_end.node)
            if neighbor_rpc is None:
                continue  # unreachable neighbor (shouldn't happen within a component)
            total_cost = neighbor_rpc.cost + end.cost
            rp_key: _RootPortKey = (
                total_cost,
                neighbor_end.node,
                _min_port(neighbor_end.ports),
                _min_port(end.ports),
            )
            keyed.append((rp_key, edge, end, neighbor_end, neighbor_rpc))
        if not keyed:
            continue
        keyed.sort(key=lambda t: t[0])
        root_port_candidates[node] = keyed
        best_key, best_edge, best_end, best_neighbor_end, best_neighbor_rpc = keyed[0]

        # Determine the deciding factor: what distinguishes the winner from
        # the runner-up (if any).
        deciding = "sole_path" if len(keyed) == 1 else _factor_vs_runner_up(best_key, keyed[1][0])

        raw_conf = ConfidenceLevel.LOW if deciding == "port_id_tie" else ConfidenceLevel.HIGH
        raw_conf = min(raw_conf, best_neighbor_rpc.link_conf, best_edge.link_confidence)
        if best_neighbor_rpc.defaulted or best_end.cost_defaulted:
            raw_conf = ConfidenceLevel.LOW
        conf = final_conf(raw_conf, lag=best_end.lag)
        mark(node, best_end, _Decision("root", deciding, conf, lag=best_end.lag))

    # 4. Designated end per edge: lower (rpc.cost, node_id) side -> designated.
    for edge in comp_edges:
        a_rpc = election.rpc.get(edge.a.node)
        b_rpc = election.rpc.get(edge.b.node)
        if a_rpc is None or b_rpc is None:
            continue
        a_key = (a_rpc.cost, edge.a.node)
        b_key = (b_rpc.cost, edge.b.node)
        if a_key < b_key:
            desig_end, desig_rpc, other_end = edge.a, a_rpc, edge.b
        else:
            desig_end, desig_rpc, other_end = edge.b, b_rpc, edge.a
        key = (desig_end.node, desig_end.ports)
        if key in decided_ends:
            continue
        raw_conf = ConfidenceLevel.HIGH
        raw_conf = min(raw_conf, desig_rpc.link_conf, edge.link_confidence)
        if desig_rpc.defaulted or desig_end.cost_defaulted or other_end.cost_defaulted:
            raw_conf = ConfidenceLevel.LOW
        conf = final_conf(raw_conf, lag=desig_end.lag)
        decision = _Decision("designated", "bridge_id", conf, lag=desig_end.lag)
        mark(desig_end.node, desig_end, decision)

    # 5. Every remaining participating end -> alternate/blocking. Each such
    #    end lost EITHER the root-port race at its own node (find its rank in
    #    that node's candidate list and classify against the winner) or the
    #    designated-end race on its edge (the other side had lower
    #    (rpc.cost, node_id) — always "bridge_id", inter-bridge ids never tie).
    for edge in comp_edges:
        for end in (edge.a, edge.b):
            key = (end.node, end.ports)
            if key in decided_ends:
                continue
            neighbor_end = edge.b if end is edge.a else edge.a
            neighbor_rpc = election.rpc.get(neighbor_end.node)

            node_candidates = root_port_candidates.get(end.node, [])
            own_end_key = None
            for cand_key, cand_edge, cand_end, _cand_neighbor, _cand_rpc in node_candidates:
                if cand_edge is edge and cand_end is end:
                    own_end_key = cand_key
                    break

            if own_end_key is not None:
                winner_key = node_candidates[0][0]
                deciding = _factor_vs_runner_up(winner_key, own_end_key)
            else:
                # Not a root-port candidate at its own node at all (its node
                # IS the root, handled in step 2) — unreachable in practice,
                # but fall back to the edge-level comparison defensively.
                deciding = "bridge_id"

            raw_conf = ConfidenceLevel.LOW
            if neighbor_rpc is not None and deciding != "port_id_tie":
                candidate_conf = min(neighbor_rpc.link_conf, edge.link_confidence)
                candidate_defaulted = neighbor_rpc.defaulted or end.cost_defaulted
                if not candidate_defaulted:
                    raw_conf = min(ConfidenceLevel.HIGH, candidate_conf)
            conf = final_conf(raw_conf, lag=end.lag)
            mark(end.node, end, _Decision("alternate", deciding, conf, lag=end.lag))

    return ComponentTree(
        nodes=component,
        root=root,
        root_assumed_default=election.root_assumed_default,
        ports=dict(sorted(ports.items())),
    )


def predict_stp_tree(ir: IR) -> StpTreePrediction:
    """Assign a role to every participating port end across every STP
    component. Pure — see module docstring for the SAFE-gating invariant."""
    topology = active_topology(ir)
    components = _components(topology)
    trees: list[ComponentTree] = []
    notes: list[str] = list(topology.notes)
    for component in components:
        election = component_rpc(ir, topology, component)
        if election.note is not None:
            notes.append(election.note)
        trees.append(_assign_component_roles(ir, topology, component, election))
    notes.sort()
    return StpTreePrediction(components=tuple(trees), notes=tuple(notes))
