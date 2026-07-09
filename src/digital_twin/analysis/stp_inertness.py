"""STP policy inertness (Spec-6). Pure — no I/O, no findings, no checks import.

Decides whether ONE changed StpPolicy knob on ONE port is PROVABLY INERT in
the current stable state, against the Spec-4 tree prediction validated by
live telemetry. The SAFE claim is STABLE-STATE-ONLY: "no current dataplane
change", never "no future protection posture change" — enabling root-protect
on a designated port deliberately alters future-failure behavior; that is the
operator hardening, not a defect, and it is out of this module's scope.

THE INVARIANT (Spec-4) is discharged by construction: every grant requires
the port's own baseline agreement row `matched` AND its component
`agreement_clean` AND identical HIGH baseline/proposed predictions — a port
the engine does not model (client/AP-facing, unlinked) has no prediction and
no row and can never pass the license. The tree engine reads NONE of the four
StpPolicy knobs, so tree identity is vacuous for a pure policy change: the
license proves trust in the tree POSITION, and the per-knob rule proves the
knob is inert AT that position.
"""
from __future__ import annotations

from dataclasses import dataclass

from digital_twin.analysis.context import AnalysisContext
from digital_twin.analysis.stp_agreement import (
    ComponentAgreement,
    PortAgreement,
    StpAgreementReport,
    compare_to_observed,
)
from digital_twin.analysis.stp_tree import PortPrediction
from digital_twin.ir.confidence import ConfidenceLevel
from digital_twin.ir.entities import DeviceRole, Link
from digital_twin.ir.model import IR

_ELIGIBLE = frozenset({"stp_no_root_port", "stp_required"})


@dataclass(frozen=True)
class InertnessDecision:
    inert: bool
    reasons: tuple[str, ...]  # failing clause names, or the granting facts
    evidence: dict[str, object]  # license + knob facts, finding-ready


def _no(reason: str, evidence: dict[str, object]) -> InertnessDecision:
    return InertnessDecision(inert=False, reasons=(reason,), evidence=evidence)


def _predictions(actx: AnalysisContext) -> dict[str, PortPrediction]:
    """port_id -> PortPrediction for this side's tree (cloned from
    stp_reachability — 5-line idiom, not a shared dependency)."""
    out: dict[str, PortPrediction] = {}
    for comp in actx.stp_tree().components:
        out.update(comp.ports)
    return out


class StpInertness:
    def __init__(
        self,
        baseline: AnalysisContext,
        proposed: AnalysisContext,
        agreement: StpAgreementReport | None = None,
    ) -> None:
        self._baseline = baseline
        self._proposed = proposed
        self._agreement = (
            agreement
            if agreement is not None
            else compare_to_observed(baseline.stp_tree(), baseline.ir)
        )
        self._base_pred = _predictions(baseline)
        self._prop_pred = _predictions(proposed)
        self._rows: dict[str, PortAgreement] = {
            r.port_id: r for r in self._agreement.ports
        }

    # -- license ---------------------------------------------------------------
    def _component_agreement_for(self, pid: str) -> ComponentAgreement | None:
        """The port's baseline ComponentAgreement. Report components are built
        1:1 in prediction-component order (stp_agreement.compare_to_observed),
        so the strict zip is a positional join, not a heuristic."""
        components = self._baseline.stp_tree().components
        for tree_comp, agree_comp in zip(components, self._agreement.components, strict=True):
            if pid in tree_comp.ports:
                return agree_comp
        return None

    def _license(self, pid: str) -> tuple[str | None, dict[str, object]]:
        """None when every clause holds, else the failing reason. Evidence
        accumulates the facts checked so far either way."""
        evidence: dict[str, object] = {"port": pid}
        if pid not in self._baseline.ir.ports:
            return "license (a): port absent from baseline — no earned trust", evidence
        row = self._rows.get(pid)
        if row is None or row.bucket != "matched":
            evidence["agreement_bucket"] = row.bucket if row else None
            return (
                "license (b): the port's own baseline agreement row is not matched",
                evidence,
            )
        evidence["agreement_bucket"] = "matched"
        evidence["observed_role"] = row.observed_role
        evidence["observed_state"] = row.observed_state
        comp = self._component_agreement_for(pid)
        if comp is None or not comp.agreement_clean:
            return "license (c): baseline component agreement is not clean", evidence
        evidence["component_matched_count"] = comp.matched_count
        base_p, prop_p = self._base_pred.get(pid), self._prop_pred.get(pid)
        if (
            base_p is None
            or prop_p is None
            or (base_p.role, base_p.state) != (prop_p.role, prop_p.state)
            or base_p.confidence is not ConfidenceLevel.HIGH
            or prop_p.confidence is not ConfidenceLevel.HIGH
        ):
            evidence["baseline_prediction"] = (
                (base_p.role, base_p.state, base_p.confidence.value) if base_p else None
            )
            evidence["proposed_prediction"] = (
                (prop_p.role, prop_p.state, prop_p.confidence.value) if prop_p else None
            )
            return (
                "license (d): tree position not identical at HIGH confidence "
                "across baseline and proposed",
                evidence,
            )
        evidence["predicted_role"] = base_p.role
        evidence["predicted_state"] = base_p.state
        return None, evidence

    # -- entry point -------------------------------------------------------------
    def decide(
        self, pid: str, knob: str, old_value: bool | str, new_value: bool | str
    ) -> InertnessDecision:
        evidence: dict[str, object] = {"port": pid, "knob": knob}
        if knob not in _ELIGIBLE:
            return _no(f"knob {knob} is not SAFE-eligible in this slice", evidence)
        if not isinstance(old_value, bool) or not isinstance(new_value, bool):
            return _no("unresolved token value — never provable", evidence)
        failure, license_ev = self._license(pid)
        evidence.update(license_ev)
        if failure is not None:
            return _no(failure, evidence)
        if knob == "stp_no_root_port":
            return self._rule_no_root_port(pid, evidence)
        return self._rule_required(pid, new_value, evidence)

    # -- knob rules (Task 3) -------------------------------------------------
    def _rule_no_root_port(
        self, pid: str, evidence: dict[str, object]
    ) -> InertnessDecision:
        """Both directions: inert iff the validated tree position is
        `designated`. Deliberately NOT `role != "root"`: root-protect on an
        ALTERNATE port (which receives superior BPDUs by definition) goes
        root-inconsistent — dataplane unchanged now, failover silently
        removed; that deserves REVIEW. A designated port never receives
        superior BPDUs in the validated stable state, so protect provably
        never triggers; this also covers every root-bridge port."""
        role = str(evidence["predicted_role"])
        if role != "designated":
            return _no(
                f"predicted role {role!r} is not designated — root-protect is "
                f"not provably inert outside the designated role",
                evidence,
            )
        return InertnessDecision(
            inert=True,
            reasons=(
                "validated designated port: superior BPDUs provably absent in "
                "the stable state (stable-state claim only)",
            ),
            evidence=evidence,
        )

    def _rule_required(
        self, pid: str, enabling: bool, evidence: dict[str, object]
    ) -> InertnessDecision:
        if enabling:
            failure = self._validated_switch_peer(pid, evidence)
            if failure is not None:
                return _no(failure, evidence)
            return InertnessDecision(
                inert=True,
                reasons=(
                    "peer positively validated as an STP participant — BPDUs "
                    "demonstrably flow, the requirement is already satisfied",
                ),
                evidence=evidence,
            )
        # disabling: only provable when the requirement is demonstrably not
        # the operative constraint — the port is observed FORWARDING. An
        # observed-blocking port might be held down BY the requirement;
        # removing it could unblock into a loop. Never assumed benign.
        if evidence.get("observed_state") != "forwarding":
            return _no(
                "observed stp_state is not 'forwarding' — removing the BPDU "
                "requirement from a non-forwarding port is not provably inert",
                evidence,
            )
        return InertnessDecision(
            inert=True,
            reasons=(
                "port observed forwarding: the requirement is demonstrably not "
                "the operative constraint (stable-state claim only)",
            ),
            evidence=evidence,
        )

    def _validated_switch_peer(
        self, pid: str, evidence: dict[str, object]
    ) -> str | None:
        """R1-P1 'effectively STP-participating peer': EVERY clause required.
        Returns the failing reason, or None when the peer is fully validated
        (evidence gains peer facts). Cloned peer-scan idiom (analysis/ must
        not import from checks/)."""
        base_ir, prop_ir = self._baseline.ir, self._proposed.ir

        def peer_of(ir: IR) -> tuple[str, Link] | None:
            hits: list[tuple[str, Link]] = []
            for lk in ir.links:
                if lk.a_port == pid:
                    hits.append((lk.b_port, lk))
                elif lk.b_port == pid:
                    hits.append((lk.a_port, lk))
            return hits[0] if len(hits) == 1 else None

        base_hit, prop_hit = peer_of(base_ir), peer_of(prop_ir)
        if base_hit is None or prop_hit is None or base_hit[0] != prop_hit[0]:
            return "no single stable modeled link across baseline and proposed"
        peer_pid = base_hit[0]
        evidence["peer"] = peer_pid
        for _, lk in (base_hit, prop_hit):
            if lk.meta.confidence.level is not ConfidenceLevel.HIGH:
                return "peer tie is not two-sided HIGH in both states"
        for ir in (base_ir, prop_ir):
            peer_port = ir.ports.get(peer_pid)
            if peer_port is None:
                return "peer port not modeled in both states"
            device = ir.devices.get(peer_port.device_id)
            if device is None or device.role is not DeviceRole.SWITCH:
                return "peer device is not a switch — never an STP participant claim"
            if peer_port.bpdu_filter:
                return "peer port has bpdu_filter set in at least one state"
        peer_row = self._rows.get(peer_pid)
        if peer_row is None or peer_row.bucket != "matched":
            return (
                "peer row is not matched — a switch that SHOULD run STP is not "
                "positive evidence that BPDUs flow (R1-P1)"
            )
        base_p, prop_p = self._base_pred.get(peer_pid), self._prop_pred.get(peer_pid)
        if (
            base_p is None
            or prop_p is None
            or (base_p.role, base_p.state) != (prop_p.role, prop_p.state)
            or base_p.confidence is not ConfidenceLevel.HIGH
            or prop_p.confidence is not ConfidenceLevel.HIGH
        ):
            return "peer tree position not identical at HIGH across both states"
        evidence["peer_observed_role"] = peer_row.observed_role
        evidence["peer_predicted_role"] = base_p.role
        return None
