"""VLAN-exit resolution — the spec's core blackhole contract (precedence 1-3).

1. IRB/SVI on a compiled device (VlanNode.exits non-empty)      -> IRB, HIGH.
2. No IRB, but the VLAN is carried on an edge to a GATEWAY-role
   node (out-of-scope upstream in M1)                            -> BOUNDARY_UPLINK,
   confidence = that edge's confidence (two-sided HIGH / one-sided LOW; the
   spec's MEDIUM row — config-inferred role — has no M1 source: device roles
   come from Mist inventory, which is authoritative).
3. Neither                                                       -> NONE, no
   confidence (the check maps this to INSUFFICIENT_DATA, never PASS).

NO severity here; the check interprets.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import networkx as nx

from digital_twin.ir import IR, Confidence, ConfidenceLevel, min_confidence
from digital_twin.ir.entities import DeviceRole
from digital_twin.ir.indexes import node_for, vc_root_map


class ExitKind(StrEnum):
    IRB = "irb"
    BOUNDARY_UPLINK = "boundary_uplink"
    NONE = "none"


@dataclass(frozen=True)
class ExitResolution:
    kind: ExitKind
    nodes: tuple[str, ...]  # graph nodes that ARE the exit (empty for NONE)
    confidence: Confidence | None  # None only for NONE


def resolve_exit(ir: IR, vlan_graph: nx.MultiGraph) -> ExitResolution:
    # rule 1: in-scope IRB/SVI (the representation already indexed them)
    irb_nodes = tuple(sorted(n for n, d in vlan_graph.nodes(data=True) if d["data"].is_exit))
    if irb_nodes:
        return ExitResolution(
            kind=ExitKind.IRB,
            nodes=irb_nodes,
            confidence=Confidence(level=ConfidenceLevel.HIGH),
        )

    # rule 2: an edge carrying the VLAN to a gateway-role node
    vc_root = vc_root_map(ir)
    gateway_nodes = {
        node_for(vc_root, d.id) for d in ir.devices.values() if d.role is DeviceRole.GATEWAY
    }
    hits: dict[str, list[Confidence]] = {}
    for u, v, data in vlan_graph.edges(data=True):
        for node in (u, v):
            if node in gateway_nodes:
                hits.setdefault(node, []).append(data["data"].confidence)
    if hits:
        return ExitResolution(
            kind=ExitKind.BOUNDARY_UPLINK,
            nodes=tuple(sorted(hits)),
            confidence=min_confidence(*(c for confs in hits.values() for c in confs)),
        )

    return ExitResolution(kind=ExitKind.NONE, nodes=(), confidence=None)
