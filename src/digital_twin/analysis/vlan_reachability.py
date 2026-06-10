"""Connected components of a per-VLAN graph, with membership + exit reachability.

Membership has three bases: member_ports = configuration-based access ports (a
configured-but-empty port still counts); wlan_members = configuration-based AP
WLAN requirements (an AP whose enabled WLANs need the VLAN — a member even with
no client connected); wireless_members = OBSERVATION-based (macs of wireless
clients observed on the VLAN). A component REACHES THE EXIT if any resolved exit
node is inside it. NO severity here.
"""

from __future__ import annotations

from dataclasses import dataclass

import networkx as nx

from .exits import ExitResolution


@dataclass(frozen=True)
class VlanComponent:
    nodes: frozenset[str]
    member_ports: frozenset[str]  # config-based membership (access ports)
    wireless_members: frozenset[str]  # observation-based membership (client macs)
    reaches_exit: bool
    wlan_members: frozenset[str] = frozenset()  # config-based membership (WLAN AP nodes)

    @property
    def has_members(self) -> bool:
        return bool(self.member_ports or self.wireless_members or self.wlan_members)


def vlan_components(
    vlan_graph: nx.MultiGraph, exit_res: ExitResolution
) -> tuple[VlanComponent, ...]:
    exit_nodes = set(exit_res.nodes)
    out: list[VlanComponent] = []
    for nodes in nx.connected_components(vlan_graph):
        member_ports = frozenset(p for n in nodes for p in vlan_graph.nodes[n]["data"].access_ports)
        wireless = frozenset(m for n in nodes for m in vlan_graph.nodes[n]["data"].wireless_clients)
        wlan = frozenset(a for n in nodes for a in vlan_graph.nodes[n]["data"].wlan_aps)
        out.append(
            VlanComponent(
                nodes=frozenset(nodes),
                member_ports=member_ports,
                wireless_members=wireless,
                wlan_members=wlan,
                reaches_exit=bool(nodes & exit_nodes),
            )
        )
    return tuple(sorted(out, key=lambda c: sorted(c.nodes)))
