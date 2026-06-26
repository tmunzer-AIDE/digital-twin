"""VLAN-exit resolution — the spec's core blackhole contract (precedence 1-4).

1. IRB/SVI on a compiled device (VlanNode.exits non-empty)      -> IRB, HIGH.
2. No IRB, but the VLAN is carried on an edge to a GATEWAY-role
   node (out-of-scope upstream in M1)                            -> BOUNDARY_UPLINK,
   confidence = that edge's confidence (two-sided HIGH / one-sided LOW; the
   spec's MEDIUM row — config-inferred role — has no M1 source: device roles
   come from Mist inventory, which is authoritative).
3. No IRB or modeled gateway, but the VLAN is carried on a port
   with is_uplink=True (Mist flag, scans ir.ports directly since
   the upstream gateway is unmodeled and has no Link edge)       -> INFERRED_UPLINK,
   LOW confidence so it can never certify SAFE (decision.py
   floors REVIEW on any sub-HIGH result confidence).
4. None of the above                                             -> NONE, no
   confidence (the check maps this to INSUFFICIENT_DATA, never PASS).

NO severity here; the check interprets.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import networkx as nx

from digital_twin.ir import IR, Confidence, ConfidenceLevel, min_confidence
from digital_twin.ir.entities import DeviceRole, L3Role
from digital_twin.ir.indexes import node_for, vc_root_map


class ExitKind(StrEnum):
    IRB = "irb"
    BOUNDARY_UPLINK = "boundary_uplink"
    INFERRED_UPLINK = "inferred_uplink"
    NONE = "none"


@dataclass(frozen=True)
class ExitResolution:
    kind: ExitKind
    nodes: tuple[str, ...]  # graph nodes that ARE the exit (empty for NONE)
    confidence: Confidence | None  # None only for NONE


def resolve_exit(ir: IR, vlan_id: int, vlan_graph: nx.MultiGraph) -> ExitResolution:
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

    # rule 3: VLAN carried on a qualifying is_uplink port — an inferred exit "up
    # the uplink" when the upstream gateway is unmodeled. LOW confidence so it can
    # never certify SAFE (decision.py floors REVIEW on a sub-HIGH result conf).
    # vc_root is already computed above for rule 2.
    graph_nodes = set(vlan_graph.nodes)
    uplink_nodes = sorted(
        {
            node_for(vc_root, p.device_id)
            for p in ir.ports.values()
            if p.is_uplink is True
            and not p.disabled
            and (vlan_id in p.tagged_vlans or p.native_vlan == vlan_id)
        }
        & graph_nodes
    )
    if uplink_nodes:
        return ExitResolution(
            kind=ExitKind.INFERRED_UPLINK,
            nodes=tuple(uplink_nodes),
            confidence=Confidence(
                level=ConfidenceLevel.LOW,
                reasons=("exit inferred from Mist uplink flag; upstream gateway unmodeled",),
            ),
        )

    return ExitResolution(kind=ExitKind.NONE, nodes=(), confidence=None)


def exit_anchor_nodes(ir: IR) -> set[str]:
    """VC-folded graph nodes that ARE a network exit on the PHYSICAL graph:
    gateway-role devices, or devices owning a routed IRB/SVI that is tied to a
    concrete VLAN. A fragment that contains one of these still reaches an L3 exit
    and is therefore NOT L2-isolated. (WAN/LOOPBACK L3 interfaces are not exits; a
    gateway's own L3 interface already belongs to a DeviceRole.GATEWAY device
    counted here. An IRB/SVI with vlan_id=None is unresolved/malformed and is NOT
    an exit, matching resolve_exit, which only treats concrete-VLAN IRBs as exits.)

    This lifts resolve_exit's two MODELED exit kinds (rule 1: IRB; rule 2:
    gateway node) from the per-VLAN graph to the vlan-agnostic physical graph,
    for callers that ask 'does this physical fragment retain any exit'. It does
    NOT lift rule 3's INFERRED_UPLINK — an is_uplink inference is not an L3
    anchor (deliberate; see the inferred-uplink spec)."""
    vc = vc_root_map(ir)
    anchors = {
        node_for(vc, d.id) for d in ir.devices.values() if d.role is DeviceRole.GATEWAY
    }
    anchors |= {
        node_for(vc, i.device_id)
        for i in ir.l3intfs
        if i.role in (L3Role.IRB, L3Role.SVI) and i.vlan_id is not None
    }
    return anchors
