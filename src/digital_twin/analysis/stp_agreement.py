"""Agreement comparator (Spec-4 Task 6): joins stp_tree predictions against
OBSERVED telemetry (Port.stp_role / .stp_state). Pure — no I/O, no findings.

Deliberately SEPARATE from stp_tree.py: the prediction engine never imports
this module. See stp_tree.py's module docstring for the safety-rail
invariant this comparator exists to serve — no verdict-facing check may
treat a raw prediction as SAFE without running it through here first.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from digital_twin.analysis.stp_tree import ComponentTree, PortPrediction, StpTreePrediction
from digital_twin.ir.confidence import ConfidenceLevel
from digital_twin.ir.model import IR

_KNOWN_ROLES = frozenset({"root", "designated", "alternate", "backup"})
_PROTECTION = frozenset({"disabled-bpdu-inconsistent"})

_MISMATCH_BUCKET = {
    ConfidenceLevel.HIGH: "mismatched_high",
    ConfidenceLevel.MEDIUM: "mismatched_medium",
    ConfidenceLevel.LOW: "mismatched_low",
}


@dataclass(frozen=True)
class PortAgreement:
    port_id: str
    predicted: PortPrediction
    observed_role: str | None
    observed_state: str | None
    bucket: str  # "matched"|"mismatched_high"|"mismatched_medium"|
    #              "mismatched_low"|"unvalidatable"|"bpdu_inconsistent"


@dataclass(frozen=True)
class ComponentAgreement:
    nodes: frozenset[str]
    disagreement: bool  # any mismatched_* among this component's OWN ports


@dataclass(frozen=True)
class StpAgreementReport:
    matched: int
    mismatched_high: int
    mismatched_medium: int
    mismatched_low: int
    unvalidatable: int
    bpdu_inconsistent: int
    ports: tuple[PortAgreement, ...]
    components: tuple[ComponentAgreement, ...]


def _bucket(pred: PortPrediction, role: str | None, state: str | None) -> str:
    if role in _PROTECTION:
        return "bpdu_inconsistent"
    if role is None or role not in _KNOWN_ROLES:
        return "unvalidatable"  # absent, "", or unknown token — NEVER a mismatch
    # Empty string state ("") is a present-but-empty non-observation (PR #43
    # convention). Skip state comparison when state is None or "" (not observed).
    if role == pred.role and (not state or state == pred.state):
        return "matched"  # state compared independently when present
    return _MISMATCH_BUCKET[pred.confidence]  # keyed EXACTLY on prediction tier (P2)


def _component_rows(
    comp: ComponentTree, ir: IR
) -> list[PortAgreement]:
    rows: list[PortAgreement] = []
    for pid in sorted(comp.ports):
        pred = comp.ports[pid]
        port = ir.ports.get(pid)
        role = port.stp_role if port else None  # "" already None-normalized upstream
        state = port.stp_state if port else None
        rows.append(PortAgreement(pid, pred, role, state, _bucket(pred, role, state)))
    return rows


def compare_to_observed(prediction: StpTreePrediction, ir: IR) -> StpAgreementReport:
    """Join every predicted port against its observed STP telemetry, bucket
    the result, and roll up a per-component disagreement flag. Deterministic:
    rows are port_id-sorted within each component, components stay in
    prediction order."""
    rows: list[PortAgreement] = []
    for comp in prediction.components:
        rows.extend(_component_rows(comp, ir))

    components = tuple(
        ComponentAgreement(
            nodes=comp.nodes,
            disagreement=any(
                r.bucket.startswith("mismatched") for r in rows if r.port_id in comp.ports
            ),
        )
        for comp in prediction.components
    )

    counts: Counter[str] = Counter(r.bucket for r in rows)
    return StpAgreementReport(
        matched=counts["matched"],
        mismatched_high=counts["mismatched_high"],
        mismatched_medium=counts["mismatched_medium"],
        mismatched_low=counts["mismatched_low"],
        unvalidatable=counts["unvalidatable"],
        bpdu_inconsistent=counts["bpdu_inconsistent"],
        ports=tuple(rows),
        components=components,
    )
