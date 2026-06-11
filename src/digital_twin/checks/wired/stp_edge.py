"""wired.stp.edge_on_uplink — STP protection removed on a switch-to-switch link.

`stp_disable` (Port.bpdu_filter) DROPS BPDUs: the port stops participating in
loop protection entirely — on an inter-switch link that is exactly where a
loop would melt the network (MVP: STP-BPDU). `stp_edge` merely declares "no
BPDUs expected"; Junos self-heals when one arrives, so it is a soft hazard.
AP uplinks are SKIPPED (BoundaryView default): configuring stp_edge toward an
AP is correct, recommended practice.

Attribution (link_boundary parity rules):
- bpdu_filter introduced/activated on an evaluable switch-switch boundary ->
  ERROR at min(port fact, link existence) confidence (UNSAFE at HIGH);
  the same flag already live on the same end -> INFO context;
- stp_edge introduced -> WARNING (REVIEW); pre-existing -> silent (soft
  hazard, too weak for context noise).
"""

from __future__ import annotations

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
from digital_twin.ir.entities import Port

from .link_boundary import BoundaryView

_HIGH = Confidence(level=ConfidenceLevel.HIGH)


class StpEdgeOnUplinkCheck:
    id = "wired.stp.edge_on_uplink"
    title = "STP protection removed on a switch-to-switch link"
    domain = "wired.stp"
    default_severity = Severity.ERROR

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2})

    def applies_to(self, diff: IRDiff) -> bool:
        # every entity kind run() reads (links, port flags, device roles)
        return any(diff.touches(k) for k in ("link", "port", "device"))

    def run(self, ctx: CheckContext) -> CheckResult:
        prop_view = BoundaryView(ctx.proposed.ir)
        base_view = BoundaryView(ctx.baseline.ir)
        findings: list[Finding] = []
        for lnk in ctx.proposed.ir.links:
            pair = prop_view.pair(lnk)
            if pair is None:
                continue
            base_pair = base_view.pair(lnk)
            base_by_id = {p.id: p for p in base_pair} if base_pair else {}
            for end, peer in (pair, pair[::-1]):
                flag = "bpdu_filter" if end.bpdu_filter else "edge_port" if end.stp_edge else None
                if flag is None:
                    continue
                base_end: Port | None = base_by_id.get(end.id)
                preexisting = base_end is not None and (
                    base_end.bpdu_filter if flag == "bpdu_filter" else base_end.stp_edge
                )
                if preexisting and flag == "edge_port":
                    continue  # soft hazard, unchanged: not worth context noise
                confidence = min_confidence(end.meta.confidence, lnk.meta.confidence)
                high = confidence.level is ConfidenceLevel.HIGH
                if preexisting:
                    severity = Severity.INFO
                    message = (
                        f"port {end.id}: pre-existing BPDU filter on the link to "
                        f"{peer.id}, unchanged by the delta (context)"
                    )
                elif flag == "bpdu_filter":
                    severity = Severity.ERROR if high else Severity.WARNING
                    message = (
                        f"port {end.id} drops BPDUs (stp_disable) on the link to "
                        f"{peer.id} — loop protection is OFF on a switch-to-switch path"
                    )
                else:
                    severity = Severity.WARNING
                    message = (
                        f"port {end.id} is configured stp_edge on the link to {peer.id} "
                        f"— an inter-switch port should expect BPDUs"
                    )
                findings.append(
                    Finding(
                        source=FindingSource.CHECK,
                        category=FindingCategory.NETWORK,
                        code=f"{self.id}.{flag}",
                        severity=severity,
                        confidence=confidence,
                        message=message,
                        affected_entities=(end.id, peer.id),
                        evidence={
                            "port": end.id,
                            "peer": peer.id,
                            "link": lnk.id,
                            "flag": flag,
                        },
                    )
                )
        worst = Status.PASS
        conclusions = [f for f in findings if f.severity is not Severity.INFO]
        for f in conclusions:
            this = Status.FAIL if f.severity is Severity.ERROR else Status.WARN
            if this is Status.FAIL or worst is Status.PASS:
                worst = this
        return CheckResult(
            check_id=self.id,
            status=worst,
            findings=tuple(findings),
            coverage=Coverage(state=CoverageState.COMPLETE),
            confidence=(
                min_confidence(*(f.confidence for f in conclusions)) if conclusions else _HIGH
            ),
            reasoning="checked stp_edge/BPDU-filter flags on every switch-to-switch link end",
        )
