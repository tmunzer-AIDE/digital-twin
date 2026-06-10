"""Connected components of a per-VLAN graph, with membership + exit reachability.

A component HAS MEMBERS if any node holds an access port for the VLAN
(config-based switched membership — a configured-but-empty port still counts;
the AP/wireless observation-based side is layered on by the checks, which have
client data). It REACHES THE EXIT if any of the resolved exit nodes is inside
the component. NO severity here.
"""

from __future__ import annotations

from dataclasses import dataclass

import networkx as nx

from .exits import ExitResolution


@dataclass(frozen=True)
class VlanComponent:
    nodes: frozenset[str]
    has_members: bool
    reaches_exit: bool


def vlan_components(
    vlan_graph: nx.MultiGraph, exit_res: ExitResolution
) -> tuple[VlanComponent, ...]:
    exit_nodes = set(exit_res.nodes)
    out: list[VlanComponent] = []
    for nodes in nx.connected_components(vlan_graph):
        members = any(vlan_graph.nodes[n]["data"].is_member for n in nodes)
        out.append(
            VlanComponent(
                nodes=frozenset(nodes),
                has_members=members,
                reaches_exit=bool(nodes & exit_nodes),
            )
        )
    return tuple(sorted(out, key=lambda c: sorted(c.nodes)))
