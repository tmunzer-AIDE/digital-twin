"""STP-aware per-VLAN reachability (Spec-5). Pure — no I/O, no findings.

Blocked-link taint of blackhole reachability. Blocking is read SIDE-LOCALLY
(baseline edges vs baseline tree, proposed vs proposed); only the hard/soft
LICENCE is baseline-derived (compare_to_observed on the baseline) and shared.
A hard-eligible blocked edge is removed from that side's reachability graph; a
soft-only edge is kept (its effect is a REVIEW floor, handled by the check).
"""
from __future__ import annotations

from digital_twin.analysis.context import AnalysisContext
from digital_twin.analysis.stp_agreement import ComponentAgreement, compare_to_observed
from digital_twin.analysis.stp_tree import PortPrediction
from digital_twin.analysis.vlan_reachability import VlanComponent, vlan_components
from digital_twin.ir.confidence import ConfidenceLevel


def _predictions(actx: AnalysisContext) -> dict[str, PortPrediction]:
    """port_id -> its PortPrediction for this side's tree (empty if unpredicted)."""
    out: dict[str, PortPrediction] = {}
    for comp in actx.stp_tree().components:
        out.update(comp.ports)
    return out


def _edge_key(member_ports: list[str]) -> frozenset[str]:
    return frozenset(member_ports)


def _edge_keys(actx: AnalysisContext, vid: int) -> set[frozenset[str]]:
    g = actx.vlan_graph(vid)
    return {_edge_key(data["data"].member_ports) for _, _, data in g.edges(data=True)}


def _blocks(pred: dict[str, PortPrediction], pid: str) -> bool:
    p = pred.get(pid)
    return p is not None and p.state == "blocking"


class StpReachability:
    def __init__(self, baseline: AnalysisContext, proposed: AnalysisContext) -> None:
        self._baseline = baseline
        self._proposed = proposed
        # baseline licence, computed once
        self._base_pred = _predictions(baseline)
        self._prop_pred = _predictions(proposed)
        report = compare_to_observed(baseline.stp_tree(), baseline.ir)
        self._base_agreements: tuple[ComponentAgreement, ...] = report.components
        self._base_comp_keys: dict[int, set[frozenset[str]]] = {}
        self._hard_cache: dict[tuple[str, int], tuple[VlanComponent, ...]] = {}

    # -- licence -------------------------------------------------------------
    def _clean_component_for(self, u: str, v: str) -> bool:
        """Both endpoints in ONE baseline STP component whose agreement is clean."""
        for a in self._base_agreements:
            if u in a.nodes and v in a.nodes:
                return a.agreement_clean
        return False

    def _baseline_edge_keys(self, vid: int) -> set[frozenset[str]]:
        if vid not in self._base_comp_keys:
            self._base_comp_keys[vid] = _edge_keys(self._baseline, vid)
        return self._base_comp_keys[vid]

    # -- side-local classification ------------------------------------------
    def _classify(
        self, actx: AnalysisContext, vid: int
    ) -> tuple[set[frozenset[str]], set[frozenset[str]]]:
        """(hard_keys, soft_keys) of blocked edges on THIS side of `vid`."""
        pred = self._base_pred if actx is self._baseline else self._prop_pred
        hard: set[frozenset[str]] = set()
        soft: set[frozenset[str]] = set()
        g = actx.vlan_graph(vid)
        base_keys = self._baseline_edge_keys(vid)
        for u, v, data in g.edges(data=True):
            edge = data["data"]
            blocking_ports = [p for p in edge.member_ports if _blocks(pred, p)]
            if not blocking_ports:
                continue
            key = _edge_key(edge.member_ports)
            block_conf = min(pred[p].confidence for p in blocking_ports)
            licensed = (
                key in base_keys  # (a) existed in baseline
                and self._clean_component_for(u, v)  # (b)+(c) same clean baseline component
                and block_conf is ConfidenceLevel.HIGH  # (d) side-local HIGH
            )
            (hard if licensed else soft).add(key)
        return hard, soft

    # -- removed-graph reachability -----------------------------------------
    def _components(
        self, actx: AnalysisContext, vid: int, remove: set[frozenset[str]]
    ) -> tuple[VlanComponent, ...]:
        g = actx.vlan_graph(vid)
        if not remove:
            return vlan_components(g, actx.exit_for(vid))
        h = g.copy()
        for u, v, key, data in list(h.edges(keys=True, data=True)):
            if _edge_key(data["data"].member_ports) in remove:
                h.remove_edge(u, v, key=key)
        return vlan_components(h, actx.exit_for(vid))

    def baseline_components(self, vid: int) -> tuple[VlanComponent, ...]:
        ck = ("b", vid)
        if ck not in self._hard_cache:
            hard, _ = self._classify(self._baseline, vid)
            self._hard_cache[ck] = self._components(self._baseline, vid, hard)
        return self._hard_cache[ck]

    def proposed_components(self, vid: int) -> tuple[VlanComponent, ...]:
        ck = ("p", vid)
        if ck not in self._hard_cache:
            hard, _ = self._classify(self._proposed, vid)
            self._hard_cache[ck] = self._components(self._proposed, vid, hard)
        return self._hard_cache[ck]
