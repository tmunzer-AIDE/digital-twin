"""Pure delta-attribution helper. DeltaIndex is the cached diff lookup ONLY:
given an entity (kind,id), is it in the delta and with which changed IR fields?
It does NO graph analysis. The Family-2 mapping functions (added in later tasks)
take a CheckContext + the affected component/cycle/vid and consult this index."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from digital_twin.contracts import Cause, ObjectRef
from digital_twin.ir.diff import IRDiff

if TYPE_CHECKING:
    import networkx as nx

    from digital_twin.checks.base import CheckContext
    from digital_twin.representations.graph_data import L2Edge

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
