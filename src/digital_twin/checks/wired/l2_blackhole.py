"""wired.l2.blackhole — a member component that loses its path to the VLAN exit.

Per VLAN (spec contract):
- exit resolved by analysis/exits (IRB HIGH > boundary uplink edge-confidence >
  NONE). NONE while members exist -> INSUFFICIENT_DATA for that vlan.
- FAIL only when the component reached the exit in IR, loses it in IR', AND the
  exit is HIGH confidence; a MEDIUM/LOW exit downgrades to WARN ("FAIL only at
  HIGH confidence").
- Components stranded in BOTH IRs are pre-existing -> INFO context.
- Switched membership is configuration-based (access ports — empty ports count).
  AP/wireless membership is observation-based; when client data is absent the
  coverage is PARTIAL (noted), never silently complete.
"""

from __future__ import annotations

from digital_twin.analysis.exits import ExitKind
from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState, Status
from digital_twin.contracts import Finding, FindingCategory, FindingSource, Severity
from digital_twin.ir import (
    Capability,
    Confidence,
    ConfidenceLevel,
    IRCapability,
    IRDiff,
    min_confidence,
)


class L2BlackholeCheck:
    id = "wired.l2.blackhole"
    title = "VLAN segment loses its exit"
    domain = "wired.l2"
    default_severity = Severity.ERROR

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2, IRCapability.L3_EXITS})

    def applies_to(self, diff: IRDiff) -> bool:
        return any(diff.touches(k) for k in ("link", "port", "vlan", "l3intf", "device"))

    def run(self, ctx: CheckContext) -> CheckResult:
        findings: list[Finding] = []
        statuses: list[Status] = []
        confidences: list[Confidence] = []
        notes: list[str] = []
        if IRCapability.CLIENTS_ACTIVE not in ctx.proposed.capabilities:
            notes.append(
                "AP/wireless VLAN membership is observation-based and client data "
                "is absent — wireless membership not evaluated"
            )
        for vid in sorted(set(ctx.baseline.ir.vlans) | set(ctx.proposed.ir.vlans)):
            statuses.append(self._check_vlan(ctx, vid, findings, confidences))
        status = _aggregate(statuses)
        coverage_state = CoverageState.PARTIAL if notes else CoverageState.COMPLETE
        if status is Status.INSUFFICIENT_DATA:
            coverage_state = CoverageState.INSUFFICIENT
        if confidences:
            confidence: Confidence | None = min_confidence(*confidences)
        elif status is Status.PASS:
            # vacuous pass (no exits consulted, nothing stranded) is still a
            # deterministic structural conclusion — HIGH, not "absent"
            confidence = Confidence(level=ConfidenceLevel.HIGH)
        else:
            confidence = None
        return CheckResult(
            check_id=self.id,
            status=status,
            findings=tuple(findings),
            coverage=Coverage(state=coverage_state, notes=tuple(notes)),
            confidence=confidence,
            reasoning="compared member-component exit reachability per vlan",
        )

    def _check_vlan(
        self,
        ctx: CheckContext,
        vid: int,
        findings: list[Finding],
        confidences: list[Confidence],
    ) -> Status:
        proposed_exit = ctx.proposed.exit_for(vid)
        if proposed_exit.confidence is not None:
            # the exit consulted for this vlan bounds the conclusion's confidence
            # even when nothing is stranded (a LOW exit = a LOW "still reachable")
            confidences.append(proposed_exit.confidence)
        stranded = [
            c for c in ctx.proposed.vlan_components(vid) if c.has_members and not c.reaches_exit
        ]
        if not stranded:
            return Status.PASS
        if proposed_exit.kind is ExitKind.NONE:
            findings.append(
                self._finding(
                    code="wired.l2.blackhole.exit_unlocatable",
                    severity=Severity.WARNING,
                    category=FindingCategory.OPERATIONAL,
                    confidence=Confidence(
                        level=ConfidenceLevel.LOW,
                        reasons=(f"no IRB and no boundary uplink found for vlan {vid}",),
                    ),
                    message=f"vlan {vid} has members but its exit cannot be located",
                    vid=vid,
                    nodes=sorted(n for c in stranded for n in c.nodes),
                )
            )
            return Status.INSUFFICIENT_DATA
        baseline_reaching = {
            frozenset(c.nodes)
            for c in ctx.baseline.vlan_components(vid)
            if c.has_members and c.reaches_exit
        }
        exit_conf = proposed_exit.confidence
        assert exit_conf is not None  # kind != NONE guarantees it (appended above)
        worst = Status.PASS
        for comp in stranded:
            newly = any(comp.nodes & prev for prev in baseline_reaching)
            if not newly:
                findings.append(
                    self._finding(
                        code="wired.l2.blackhole.preexisting",
                        severity=Severity.INFO,
                        category=FindingCategory.NETWORK,
                        confidence=exit_conf,
                        message=(
                            f"vlan {vid}: component already had no exit path before the "
                            "delta (context)"
                        ),
                        vid=vid,
                        nodes=sorted(comp.nodes),
                    )
                )
                continue
            high = exit_conf.level is ConfidenceLevel.HIGH
            findings.append(
                self._finding(
                    code="wired.l2.blackhole.exit_lost",
                    severity=Severity.ERROR if high else Severity.WARNING,
                    category=FindingCategory.NETWORK,
                    confidence=exit_conf,
                    message=(
                        f"vlan {vid}: member segment loses its path to the "
                        f"{proposed_exit.kind} exit"
                    ),
                    vid=vid,
                    nodes=sorted(comp.nodes),
                )
            )
            worst = _aggregate([worst, Status.FAIL if high else Status.WARN])
        return worst

    def _finding(
        self,
        *,
        code: str,
        severity: Severity,
        category: FindingCategory,
        confidence: Confidence,
        message: str,
        vid: int,
        nodes: list[str],
    ) -> Finding:
        return Finding(
            source=FindingSource.CHECK,
            category=category,
            code=code,
            severity=severity,
            confidence=confidence,
            message=message,
            affected_entities=tuple(nodes),
            evidence={"vlan": vid, "component_nodes": nodes},
        )


_ORDER = [Status.PASS, Status.INSUFFICIENT_DATA, Status.WARN, Status.FAIL]


def _aggregate(statuses: list[Status]) -> Status:
    return max(statuses, key=_ORDER.index) if statuses else Status.PASS
