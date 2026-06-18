"""Pure delta-attribution helper. DeltaIndex is the cached diff lookup ONLY:
given an entity (kind,id), is it in the delta and with which changed IR fields?
It does NO graph analysis. The Family-2 mapping functions (added in later tasks)
take a CheckContext + the affected component/cycle/vid and consult this index."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from digital_twin.contracts import Cause, ObjectRef
from digital_twin.ir.diff import IRDiff

if TYPE_CHECKING:
    import networkx as nx

    from digital_twin.checks.base import CheckContext
    from digital_twin.representations.graph_data import L2Edge

    from .cycles import Cycle
    from .vlan_reachability import VlanComponent


@dataclass(frozen=True)
class DeltaIndex:
    _fields: dict[tuple[str, str], tuple[str, ...]]   # (kind,id) -> changed fields
    _addremove: frozenset[tuple[str, str]]            # added or removed (no field set)

    def in_delta(self, kind: str, oid: str) -> bool:
        key = (kind, oid)
        return key in self._fields or key in self._addremove

    def cause(self, kind: str, oid: str) -> Cause | None:
        """Cause for an entity IFF it is in the delta; else None (honesty rule)."""
        key = (kind, oid)
        if key in self._fields:
            return Cause(ref=ObjectRef(kind, oid), fields=self._fields[key])
        if key in self._addremove:
            return Cause(ref=ObjectRef(kind, oid), fields=())
        return None

    def causes(self, kind: str, oids: Iterable[object]) -> tuple[Cause, ...]:
        """Map an iterable of ids of one kind to the subset that is in the delta."""
        out = []
        for oid in oids:
            c = self.cause(kind, str(oid))
            if c is not None:
                out.append(c)
        return tuple(out)


def delta_index(diff: IRDiff) -> DeltaIndex:
    fields = {(m.ref.kind, m.ref.id): m.changed_fields for m in diff.modified}
    addremove = frozenset((r.kind, r.id) for r in (*diff.added, *diff.removed))
    return DeltaIndex(_fields=fields, _addremove=addremove)


def _boundary_lost_edges(
    base_g: nx.MultiGraph, prop_g: nx.MultiGraph, nodes: frozenset[str]
) -> list[L2Edge]:
    """L2Edge payloads of baseline edges with EXACTLY ONE endpoint in `nodes`
    that are gone in the proposed graph — the boundary cut of this fragment.
    data['data'] is an L2Edge (graph_data.py)."""
    nodeset = set(nodes)
    out: list[L2Edge] = []
    for u, v, data in base_g.edges(data=True):
        if ((u in nodeset) ^ (v in nodeset)) and not prop_g.has_edge(u, v):
            out.append(data["data"])
    return out


def _edge_causes(di: DeltaIndex, edges: list[L2Edge]) -> tuple[Cause, ...]:
    """Map L2Edge payloads to delta-present port AND link causes."""
    ports: set[str] = set()
    links: set[str] = set()
    for e in edges:
        ports.update(e.member_ports)
        links.update(e.link_ids)
    return tuple(
        dict.fromkeys((*di.causes("port", sorted(ports)), *di.causes("link", sorted(links))))
    )


def causes_for_vlan_cut(ctx: CheckContext, vid: int, component: VlanComponent) -> tuple[Cause, ...]:
    """Blackhole exit_lost: the stranded proposed `component` lost its boundary
    edge(s) to the rest of the vlan domain."""
    edges = _boundary_lost_edges(
        ctx.baseline.vlan_graph(vid), ctx.proposed.vlan_graph(vid), component.nodes
    )
    return _edge_causes(ctx.delta_index, edges)


def causes_for_vlan_split(ctx: CheckContext, vid: int) -> tuple[Cause, ...]:
    """Segmentation split: a baseline vlan component fragmented. Cause = baseline
    edges gone in proposed whose endpoints BOTH survive in proposed but now sit in
    DIFFERENT proposed fragments (the separating edges). Requiring both endpoints
    present avoids blaming a removed leaf edge (a contraction, not a split). A split
    caused purely by removing an articulation NODE yields () here (honest-empty)."""
    base_g, prop_g = ctx.baseline.vlan_graph(vid), ctx.proposed.vlan_graph(vid)
    comp_of: dict[str, int] = {}
    for i, comp in enumerate(ctx.proposed.vlan_components(vid)):
        for n in comp.nodes:
            comp_of[n] = i
    edges = [
        data["data"] for u, v, data in base_g.edges(data=True)
        if not prop_g.has_edge(u, v)
        and u in comp_of and v in comp_of
        and comp_of[u] != comp_of[v]
    ]
    return _edge_causes(ctx.delta_index, edges)


def causes_for_severance(ctx: CheckContext, island: object) -> tuple[Cause, ...]:
    """Cause = delta-changed ports/links whose removal/disabled state dropped a
    physical L2 boundary edge of the island."""
    raw = island.nodes if hasattr(island, "nodes") else island
    nodes = cast("frozenset[str]", raw)
    edges = _boundary_lost_edges(ctx.baseline.l2_graph(), ctx.proposed.l2_graph(), nodes)
    return _edge_causes(ctx.delta_index, edges)


def causes_for_blackhole(
    ctx: CheckContext, vid: int, component: VlanComponent
) -> tuple[Cause, ...]:
    """A component lost its path to an exit. TWO causes combine: (1) lost VLAN
    carriage (causes_for_vlan_cut), and (2) a delta-removed exit-providing l3intf
    for this vid. Shared by blackhole exit_unlocatable (T12) and client_impact (T15)."""
    di = ctx.delta_index
    prop_l3_ids = {p.id for p in ctx.proposed.ir.l3intfs}
    removed_l3 = [
        i.id for i in ctx.baseline.ir.l3intfs if i.vlan_id == vid and i.id not in prop_l3_ids
    ]
    return tuple(dict.fromkeys((
        *causes_for_vlan_cut(ctx, vid, component),
        *di.causes("l3intf", removed_l3),
    )))


# L2LoopCheck ranks cycles ONLY from Port.stp_enabled (l2_loop.py:_rank/_judge) —
# stp_edge/bpdu_filter do NOT change its verdict, so they are NOT loop-arming causes.
_LOOP_PORT_FIELDS = frozenset({"stp_enabled"})


def causes_for_loop(ctx: CheckContext, cycle: Cycle) -> tuple[Cause, ...]:
    """Cause = the delta entity that ARMED this cycle: a cycle port whose
    `stp_enabled` flipped or that was newly added (structural, empty fields), OR an
    added/removed link in the cycle. An unrelated field change on a cycle member
    (mtu, stp_edge, …) is NOT loop-relevant and is filtered out."""
    di = ctx.delta_index
    out: list[Cause] = []
    for p in sorted(cycle.member_ports):
        c = di.cause("port", p)
        if c is not None and (not c.fields or (_LOOP_PORT_FIELDS & set(c.fields))):
            out.append(c)
    out.extend(di.causes("link", cycle.link_ids))  # added/removed link arming the cycle
    return tuple(dict.fromkeys(out))


def _gained_merging_edges(
    base_g: nx.MultiGraph, prop_g: nx.MultiGraph, nodes: frozenset[str]
) -> list[L2Edge]:
    """L2Edge payloads of PROPOSED edges touching `nodes`, absent in baseline, whose
    endpoints were in DIFFERENT baseline components — the edges that MERGED
    formerly-separate components. (A merge's added edge has BOTH endpoints inside
    the proposed component, so the XOR boundary test would miss it.)"""
    import networkx as nx

    comp_of: dict[str, int] = {}
    for i, comp in enumerate(nx.connected_components(base_g)):
        for n in comp:
            comp_of[n] = i
    nodeset = set(nodes)
    out: list[L2Edge] = []
    for u, v, data in prop_g.edges(data=True):
        if (
            (u in nodeset or v in nodeset)
            and not base_g.has_edge(u, v)
            and comp_of.get(u) != comp_of.get(v)
        ):
            out.append(data["data"])
    return out


def causes_for_root_move(
    ctx: CheckContext, component_nodes: frozenset[str], base_root: str, prop_root: str
) -> tuple[Cause, ...]:
    """Dual, restricted to THIS component: (a) a priority change on an
    ELECTION-RELEVANT device — only the old or new root (`base_root`/`prop_root`),
    since a non-root device changing priority cannot move the election; (b) topology
    — a boundary edge LOST (split/removal) or a MERGING edge GAINED (two baseline
    components joined)."""
    di = ctx.delta_index
    base_l2, prop_l2 = ctx.baseline.l2_graph(), ctx.proposed.l2_graph()
    out: list[Cause] = []
    for did in (base_root, prop_root):
        c = di.cause("device", did)
        if c is not None and ("stp_priority" in c.fields or not c.fields):
            out.append(c)
    lost = _boundary_lost_edges(base_l2, prop_l2, component_nodes)
    gained = _gained_merging_edges(base_l2, prop_l2, component_nodes)
    out.extend(_edge_causes(di, [*lost, *gained]))
    return tuple(dict.fromkeys(out))
