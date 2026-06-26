"""wired.port.unmodeled_change — inter_switch_link / storm_control / enable_qos
changed. These have no reachability/connectivity model the twin reasons about
(inter_switch_link enables the unmodeled networks.isolation feature; storm_control
is a runtime traffic-protection knob; enable_qos is pure scheduling), so the twin
recognizes the change and floors REVIEW — never SAFE, never ERROR/UNSAFE.
"""

from __future__ import annotations

from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState, Status
from digital_twin.contracts import Finding, FindingCategory, FindingSource, ObjectRef, Severity
from digital_twin.ir import Capability, Confidence, ConfidenceLevel, IRCapability, IRDiff
from digital_twin.ir.entities import PortMisc

_MEDIUM = Confidence(
    level=ConfidenceLevel.MEDIUM,
    reasons=("the changed knob has no modeled connectivity impact",),
)


def _changed(old: PortMisc | None, new: PortMisc | None) -> list[str]:
    o, n = old or PortMisc(), new or PortMisc()
    out: list[str] = []
    if o.inter_switch_link != n.inter_switch_link:
        out.append("inter_switch_link")
    if o.enable_qos != n.enable_qos:
        out.append("enable_qos")
    if o.storm_control != n.storm_control:
        out.append("storm_control")
    return out


class PortUnmodeledChangeCheck:
    id = "wired.port.unmodeled_change"
    title = "Recognized-but-unmodeled port knob changed"
    domain = "wired.port"
    default_severity = Severity.WARNING

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2})

    def applies_to(self, diff: IRDiff) -> bool:
        return diff.touches("port")

    def run(self, ctx: CheckContext) -> CheckResult:
        base_ir, prop_ir = ctx.baseline.ir, ctx.proposed.ir
        findings: list[Finding] = []
        for pid in sorted(base_ir.ports.keys() | prop_ir.ports.keys()):
            old = base_ir.ports[pid].misc if pid in base_ir.ports else None
            new = prop_ir.ports[pid].misc if pid in prop_ir.ports else None
            if old == new:
                continue
            knobs = _changed(old, new)
            if not knobs:
                continue
            findings.append(
                Finding(
                    source=FindingSource.CHECK, category=FindingCategory.NETWORK,
                    code=f"{self.id}.recognized", severity=Severity.WARNING, confidence=_MEDIUM,
                    message=f"port {pid}: {', '.join(knobs)} changed — impact not modeled (review)",
                    affected_entities=(pid,), subject=ObjectRef("port", pid),
                    evidence={"port": pid, "knobs": knobs},
                    caused_by=ctx.delta_index.causes("port", [pid]),
                )
            )
        return CheckResult(
            check_id=self.id, status=Status.WARN if findings else Status.PASS,
            findings=tuple(findings), coverage=Coverage(state=CoverageState.COMPLETE),
            confidence=_MEDIUM if findings else Confidence(level=ConfidenceLevel.HIGH),
            reasoning="compared per-port recognized-but-unmodeled knobs baseline vs proposed",
        )
