"""AnalysisContext: one immutable IR + lazily-memoized representations/analysis.

Checks receive two of these (baseline + proposed) and NEVER rebuild shared work:
the L2 graph is built once, each per-VLAN graph once, each analysis result once.
Pure reads only — the IR is frozen and graphs are built fresh per context.
"""

from __future__ import annotations

from functools import cached_property

import networkx as nx

from digital_twin.ir import IR, Capability
from digital_twin.representations.l2_graph import build_l2_graph
from digital_twin.representations.vlan_graph import build_vlan_graph

from .cycles import Cycle, find_cycles
from .exits import ExitResolution, resolve_exit
from .vlan_reachability import VlanComponent
from .vlan_reachability import vlan_components as compute_vlan_components


class AnalysisContext:
    def __init__(self, ir: IR) -> None:
        self._ir = ir
        self._vlan_graphs: dict[int, nx.MultiGraph] = {}
        self._cycles: dict[int, tuple[Cycle, ...]] = {}
        self._exits: dict[int, ExitResolution] = {}
        self._components: dict[int, tuple[VlanComponent, ...]] = {}

    @property
    def ir(self) -> IR:
        return self._ir

    @property
    def capabilities(self) -> frozenset[Capability]:
        return self._ir.capabilities

    @cached_property
    def _l2(self) -> nx.MultiGraph:
        return build_l2_graph(self._ir)

    def l2_graph(self) -> nx.MultiGraph:
        return self._l2

    def vlan_graph(self, vlan_id: int) -> nx.MultiGraph:
        if vlan_id not in self._vlan_graphs:
            self._vlan_graphs[vlan_id] = build_vlan_graph(self._ir, self._l2, vlan_id)
        return self._vlan_graphs[vlan_id]

    def cycles(self, vlan_id: int) -> tuple[Cycle, ...]:
        if vlan_id not in self._cycles:
            self._cycles[vlan_id] = find_cycles(self.vlan_graph(vlan_id))
        return self._cycles[vlan_id]

    def exit_for(self, vlan_id: int) -> ExitResolution:
        if vlan_id not in self._exits:
            self._exits[vlan_id] = resolve_exit(self._ir, self.vlan_graph(vlan_id))
        return self._exits[vlan_id]

    def vlan_components(self, vlan_id: int) -> tuple[VlanComponent, ...]:
        if vlan_id not in self._components:
            self._components[vlan_id] = compute_vlan_components(
                self.vlan_graph(vlan_id), self.exit_for(vlan_id)
            )
        return self._components[vlan_id]
