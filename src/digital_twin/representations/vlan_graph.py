"""Per-VLAN representation: a subgraph of the L2 graph for one VLAN.

Pure structural view. Includes a node iff it participates in the VLAN — carries a
VLAN-bearing edge, OR holds a member access port (index), OR holds an exit (index).
Each node carries a ``VlanNode`` payload; each edge carries the L2 edge's ``L2Edge``
(copied). No severity.
"""

from __future__ import annotations

from collections import defaultdict

import networkx as nx

from digital_twin.ir.indexes import access_ports_by_vlan, exits_by_vlan, node_for, vc_root_map
from digital_twin.ir.model import IR

from .graph_data import L2Edge, VlanNode


def build_vlan_graph(ir: IR, l2: nx.MultiGraph, vlan_id: int) -> nx.MultiGraph:
    vc_root = vc_root_map(ir)

    access_by_node: dict[str, list[str]] = defaultdict(list)
    for p in access_ports_by_vlan(ir).get(vlan_id, []):
        access_by_node[node_for(vc_root, p.device_id)].append(p.id)

    exits_by_node: dict[str, list[str]] = defaultdict(list)
    for intf in exits_by_vlan(ir).get(vlan_id, []):
        exits_by_node[node_for(vc_root, intf.device_id)].append(intf.id)

    carrying: list[tuple[str, str, object, L2Edge]] = [
        (u, v, key, data["data"])
        for u, v, key, data in l2.edges(keys=True, data=True)
        if vlan_id in data["data"].vlans
    ]
    carrying_nodes = {n for u, v, _, _ in carrying for n in (u, v)}
    participating = carrying_nodes | set(access_by_node) | set(exits_by_node)

    h: nx.MultiGraph = nx.MultiGraph()
    for node in participating:
        h.add_node(
            node,
            data=VlanNode(
                access_ports=access_by_node.get(node, []),
                exits=exits_by_node.get(node, []),
            ),
        )
    for u, v, key, edge in carrying:
        h.add_edge(u, v, key=key, data=edge.copy())
    return h
