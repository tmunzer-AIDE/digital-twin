"""Structural representations (views) over the IR — pure, no severity."""

from .graph_data import L2Edge, VlanNode
from .l2_graph import build_l2_graph, link_carried_vlans
from .vlan_graph import build_vlan_graph

__all__ = ["build_l2_graph", "build_vlan_graph", "link_carried_vlans", "L2Edge", "VlanNode"]
