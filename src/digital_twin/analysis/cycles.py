"""Cycle detection on the normalized per-VLAN graph (NO severity — that is
l2.loop's job; this only reports structure + confidence).

Two cycle forms in a MultiGraph:
- PARALLEL edges between one node pair (>=2 standalone links; LAG members were
  already collapsed by the representation, so parallel = real redundancy).
- Simple cycles of >=3 nodes (cycle_basis on the simple projection).

Cycle confidence = MIN over its edges' confidences (method itself is exact ->
contributes HIGH, i.e. never lowers).
"""

from __future__ import annotations

from dataclasses import dataclass

import networkx as nx

from digital_twin.ir import Confidence, min_confidence
from digital_twin.representations.graph_data import L2Edge


@dataclass(frozen=True)
class Cycle:
    nodes: tuple[str, ...]
    member_ports: tuple[str, ...]  # every port on the cycle's edges
    link_ids: tuple[str, ...]
    confidence: Confidence


def _edges_between(g: nx.MultiGraph, u: str, v: str) -> list[L2Edge]:
    return [d["data"] for d in g[u][v].values()] if g.has_edge(u, v) else []


def find_cycles(g: nx.MultiGraph) -> tuple[Cycle, ...]:
    out: list[Cycle] = []
    seen_pairs: set[frozenset[str]] = set()

    # form 1: parallel logical edges between one pair
    for u, v in {(u, v) for u, v in g.edges() if u != v}:
        pair = frozenset((u, v))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        edges = _edges_between(g, u, v)
        if len(edges) >= 2:
            out.append(_cycle((u, v), edges))

    # form 2: simple cycles >=3 nodes on the deduplicated projection
    simple = nx.Graph(g)
    for nodes in nx.cycle_basis(simple):
        if len(nodes) < 3:
            continue
        ring = [*nodes, nodes[0]]
        edges = [e for a, b in zip(ring, ring[1:], strict=False) for e in _edges_between(g, a, b)]
        out.append(_cycle(tuple(nodes), edges))

    return tuple(sorted(out, key=lambda c: c.nodes))


def _cycle(nodes: tuple[str, ...], edges: list[L2Edge]) -> Cycle:
    return Cycle(
        nodes=tuple(sorted(nodes)),
        member_ports=tuple(sorted({p for e in edges for p in e.member_ports})),
        link_ids=tuple(sorted({lid for e in edges for lid in e.link_ids})),
        confidence=min_confidence(*(e.confidence for e in edges)),
    )
