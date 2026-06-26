"""Pure builder: (baseline_ir, proposed_ir, findings) -> VisualMap.

Keyed per rendered view so a VLAN-scoped finding can never paint another VLAN's
chart. Removed-entity OWNERSHIP resolves against baseline_ir; everything rendered
resolves against proposed_ir. decision.py never reads the result.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import networkx as nx

from digital_twin.ir import IR
from digital_twin.ir.entities import L3Intf
from digital_twin.ir.indexes import node_for, vc_root_map
from digital_twin.representations.l2_graph import build_l2_graph
from digital_twin.representations.vlan_graph import build_vlan_graph

_MIST_DEV_HEAD = "00000000-0000-0000-"


def _mac(device_id: str) -> str:
    parts = device_id.split("-")
    if len(parts) == 5 and device_id.startswith(_MIST_DEV_HEAD):
        return parts[-1]
    return device_id


def _node(ir: IR, raw: str) -> str | None:
    """VC-folded device node for `raw`, or None if it is not a device."""
    vc = vc_root_map(ir)
    m = _mac(raw)
    if m in ir.devices or node_for(vc, m) in ir.devices:
        return node_for(vc, m)
    return None


def _port_node(ir: IR, pid: str) -> str | None:
    return _node(ir, pid.split(":", 1)[0]) if ":" in pid else None


def _resolve_affected(ent: str, ir: IR) -> tuple[str, str] | None:
    """(kind, id) for an untyped affected_entities value — ONLY if it resolves in
    the IR. Never promote by string shape (a colon-bearing MAC stays unresolved)."""
    n = _node(ir, ent)
    if n is not None:
        return ("device", n)
    if ent.isdigit() and int(ent) in ir.vlans:
        return ("vlan", ent)
    if ent in ir.ports:
        return ("port", ent)
    return None


def owner_device_nodes(
    kind: str, ent_id: str, baseline_ir: IR, proposed_ir: IR
) -> list[str]:
    """Owner/endpoint device node(s) for a cause. l3intf owner comes from BASELINE
    (it may be removed in proposed). vlan causes own no device -> []."""
    if kind == "device":
        n = _node(proposed_ir, ent_id) or _node(baseline_ir, ent_id)
        return [n] if n else []
    if kind == "port":
        n = _port_node(proposed_ir, ent_id) or _port_node(baseline_ir, ent_id)
        return [n] if n else []
    if kind == "link":
        out: list[str] = []
        for pid in ent_id.split("__"):
            n = _port_node(proposed_ir, pid) or _port_node(baseline_ir, pid)
            if n and n not in out:
                out.append(n)
        return out
    if kind == "l3intf":
        # proposed first (added/kept interface), then baseline (removed interface)
        for src in (proposed_ir, baseline_ir):
            for intf in src.l3intfs:
                if intf.id == ent_id:
                    return [node_for(vc_root_map(src), intf.device_id)]
        return []
    return []


@dataclass
class _ViewIndex:
    vlan_nodes: dict[int, set[str]] = field(default_factory=dict)
    routed_vlans: set[int] = field(default_factory=set)
    intfs_by_vlan: dict[int, list[L3Intf]] = field(default_factory=dict)

    def node_in_vlan(self, node: str, vid: int) -> bool:
        return node in self.vlan_nodes.get(vid, set())

    def intfs_for_vlan(self, vid: int) -> list[L3Intf]:
        return self.intfs_by_vlan.get(vid, [])


def _build_view_index(proposed_ir: IR) -> _ViewIndex:
    idx = _ViewIndex()
    l2 = build_l2_graph(proposed_ir)
    for vid in proposed_ir.vlans:
        g: nx.MultiGraph = build_vlan_graph(proposed_ir, l2, vid)
        idx.vlan_nodes[vid] = set(g.nodes)
    for intf in proposed_ir.l3intfs:
        if intf.vlan_id is not None:
            idx.intfs_by_vlan.setdefault(intf.vlan_id, []).append(intf)
    # routed == has a subnet OR is served by an l3 interface (mirrors _l3_exits_diagram)
    idx.routed_vlans = {
        vid for vid, v in proposed_ir.vlans.items() if v.subnet is not None
    } | set(idx.intfs_by_vlan)
    return idx
