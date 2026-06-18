"""wired.l2.loop — a cycle is NOT a loop by itself (spec table):

all cycle ports STP-running -> protected redundancy (PASS); any port STP
DISABLED -> FAIL (ERROR, network, HIGH); STP UNKNOWN on any port -> WARN with
LOW confidence (floors the decision to REVIEW).

ATTRIBUTION is per the spec's CONDITION, not the cycle's mere existence: the
attributable condition is "cycle + STP disabled/unknown". A delta that disables
STP on an ALREADY-EXISTING cycle introduces that condition and must FAIL — only
when the condition is no worse than in the baseline is it pre-existing context
(INFO). Ranks: protected(0) < unknown(1) < disabled(2); attributable when the
proposed rank exceeds the baseline rank (or the cycle itself is new).
requires() is wired.l2 only — STP_STATE absence degrades to the UNKNOWN row,
which is exactly the honest answer (not INSUFFICIENT_DATA).
"""

from __future__ import annotations

from digital_twin.analysis.context import AnalysisContext
from digital_twin.analysis.cycles import Cycle
from digital_twin.analysis.delta_cause import causes_for_loop
from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState, Status
from digital_twin.contracts import (
    Cause,
    Finding,
    FindingCategory,
    FindingSource,
    ObjectRef,
    Severity,
)
from digital_twin.ir import (
    Capability,
    Confidence,
    ConfidenceLevel,
    IRCapability,
    IRDiff,
    min_confidence,
)


class L2LoopCheck:
    id = "wired.l2.loop"
    title = "L2 loop risk (cycle without STP protection)"
    domain = "wired.l2"
    default_severity = Severity.ERROR

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2})

    def applies_to(self, diff: IRDiff) -> bool:
        return any(diff.touches(k) for k in ("link", "port", "vlan", "device"))

    def run(self, ctx: CheckContext) -> CheckResult:
        findings: list[Finding] = []
        worst = Status.PASS
        confidences: list[Confidence] = []
        vlan_ids = sorted(set(ctx.baseline.ir.vlans) | set(ctx.proposed.ir.vlans))
        for vid in vlan_ids:
            baseline_rank = {c.nodes: self._rank(ctx.baseline, c) for c in ctx.baseline.cycles(vid)}
            for cycle in ctx.proposed.cycles(vid):
                previous = baseline_rank.get(cycle.nodes)
                attributable = previous is None or self._rank(ctx.proposed, cycle) > previous
                finding, status = self._judge(ctx, vid, cycle, attributable)
                if finding:
                    findings.append(finding)
                    confidences.append(finding.confidence)
                worst = _worse(worst, status)
        confidence = (
            min_confidence(*confidences) if confidences else Confidence(level=ConfidenceLevel.HIGH)
        )
        return CheckResult(
            check_id=self.id,
            status=worst,
            findings=tuple(findings),
            coverage=Coverage(state=CoverageState.COMPLETE),
            confidence=confidence,
            reasoning=f"examined {len(vlan_ids)} vlan graphs for cycles",
        )

    @staticmethod
    def _rank(side: AnalysisContext, cycle: Cycle) -> int:
        """Condition severity of a cycle in ONE IR: protected(0)<unknown(1)<disabled(2)."""
        states = [side.ir.port(p).stp_enabled for p in cycle.member_ports]
        if any(s is False for s in states):
            return 2
        if any(s is None for s in states):
            return 1
        return 0

    def _judge(
        self, ctx: CheckContext, vid: int, cycle: Cycle, attributable: bool
    ) -> tuple[Finding | None, Status]:
        ports = [ctx.proposed.ir.port(p) for p in cycle.member_ports]
        disabled = [p.id for p in ports if p.stp_enabled is False]
        unknown = [p.id for p in ports if p.stp_enabled is None]
        if not attributable:  # condition no worse than baseline: context, not caused
            return (
                self._finding(
                    code="wired.l2.loop.preexisting",
                    severity=Severity.INFO,
                    confidence=cycle.confidence,
                    message=(
                        f"pre-existing cycle on vlan {vid} (condition unchanged by delta — context)"
                    ),
                    cycle=cycle,
                    vid=vid,
                    caused_by=(),  # pre-existing: not attributed to the delta
                ),
                Status.PASS,
            )
        caused_by = causes_for_loop(ctx, cycle)
        if disabled:
            return (
                self._finding(
                    code="wired.l2.loop.unprotected",
                    severity=Severity.ERROR,
                    confidence=min_confidence(
                        cycle.confidence, Confidence(level=ConfidenceLevel.HIGH)
                    ),
                    message=(
                        f"new cycle on vlan {vid} with STP DISABLED on "
                        f"{len(disabled)} port(s) — unprotected redundant path"
                    ),
                    cycle=cycle,
                    vid=vid,
                    extra={"stp_disabled_ports": disabled},
                    caused_by=caused_by,
                ),
                Status.FAIL,
            )
        if unknown:
            return (
                self._finding(
                    code="wired.l2.loop.unverified",
                    severity=Severity.WARNING,
                    confidence=Confidence(
                        level=ConfidenceLevel.LOW,
                        reasons=tuple(f"STP state unknown on {p}" for p in unknown[:5]),
                    ),
                    message=f"new cycle on vlan {vid}; STP state unverified — potential loop",
                    cycle=cycle,
                    vid=vid,
                    extra={"stp_unknown_ports": unknown},
                    caused_by=caused_by,
                ),
                Status.WARN,
            )
        return (
            self._finding(
                code="wired.l2.loop.protected",
                severity=Severity.INFO,
                confidence=min_confidence(cycle.confidence, Confidence(level=ConfidenceLevel.HIGH)),
                message=f"new cycle on vlan {vid} fully STP-protected (redundancy, not a loop)",
                cycle=cycle,
                vid=vid,
                caused_by=caused_by,
            ),
            Status.PASS,
        )

    def _finding(
        self,
        *,
        code: str,
        severity: Severity,
        confidence: Confidence,
        message: str,
        cycle: Cycle,
        vid: int,
        extra: dict[str, object] | None = None,
        caused_by: tuple[Cause, ...] = (),
    ) -> Finding:
        return Finding(
            source=FindingSource.CHECK,
            category=FindingCategory.NETWORK,
            code=code,
            severity=severity,
            confidence=confidence,
            message=message,
            affected_entities=cycle.member_ports,
            subject=ObjectRef("vlan", str(vid)),
            evidence={
                "vlan": vid,
                "cycle_nodes": list(cycle.nodes),
                "link_ids": list(cycle.link_ids),
                **(extra or {}),
            },
            caused_by=caused_by,
        )


_ORDER = [Status.PASS, Status.WARN, Status.FAIL]


def _worse(a: Status, b: Status) -> Status:
    return a if _ORDER.index(a) >= _ORDER.index(b) else b
