"""wired.storm_control_policy — risky storm-control changes on critical ports."""

from __future__ import annotations

from digital_twin.checks.base import (
    CheckContext,
    CheckResult,
    Coverage,
    CoverageState,
    status_from_findings,
)
from digital_twin.contracts import Finding, FindingCategory, FindingSource, ObjectRef, Severity
from digital_twin.ir import Capability, Confidence, ConfidenceLevel, IRCapability, IRDiff, Port

_HIGH = Confidence(level=ConfidenceLevel.HIGH)


def _storm(port: Port) -> dict[str, str]:
    digest = port.misc.storm_control if port.misc else None
    if not digest:
        return {}
    return dict(part.split("=", 1) for part in digest.split(";") if "=" in part)


def _percentage(values: dict[str, str]) -> int:
    try:
        return int(values.get("percentage", "80"))
    except ValueError:
        return 80


class StormControlPolicyCheck:
    id = "wired.storm_control_policy"
    title = "Storm-control policy"
    domain = "wired.storm_control"
    default_severity = Severity.WARNING

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2})

    def applies_to(self, diff: IRDiff) -> bool:
        return diff.touches("port")

    def run(self, ctx: CheckContext) -> CheckResult:
        findings: list[Finding] = []
        prop_links = {p for link in ctx.proposed.ir.links for p in (link.a_port, link.b_port)}
        for pid in sorted(ctx.baseline.ir.ports.keys() & ctx.proposed.ir.ports.keys()):
            before, after = ctx.baseline.ir.ports[pid], ctx.proposed.ir.ports[pid]
            old, new = _storm(before), _storm(after)
            if old == new:
                continue
            risky_shutdown = new.get("disable_port") == "True" and (
                after.is_uplink is True or pid in prop_links or after.profile in {"ap", "uplink"}
            )
            lowered = _percentage(new) < _percentage(old)
            if not (risky_shutdown or lowered):
                continue
            findings.append(
                Finding(
                    source=FindingSource.CHECK,
                    category=FindingCategory.NETWORK,
                    code=(
                        f"{self.id}."
                        f"{'shutdown_on_critical_port' if risky_shutdown else 'threshold_lowered'}"
                    ),
                    severity=Severity.WARNING,
                    confidence=_HIGH,
                    message=(
                        f"storm control on {pid} "
                        + (
                            "can disable an AP/uplink or linked port"
                            if risky_shutdown
                            else f"lowers its threshold to {_percentage(new)}%"
                        )
                    ),
                    subject=ObjectRef("port", pid),
                    affected_entities=(pid,),
                    evidence={
                        "port": pid,
                        "baseline_storm_control": old,
                        "proposed_storm_control": new,
                        "traffic_telemetry": "unavailable",
                    },
                    caused_by=ctx.delta_index.causes("port", [pid]),
                )
            )
        return CheckResult(
            check_id=self.id,
            status=status_from_findings(findings),
            findings=tuple(findings),
            coverage=Coverage(
                CoverageState.PARTIAL if findings else CoverageState.COMPLETE,
                ("broadcast, multicast, and unknown-unicast rate telemetry is unavailable",)
                if findings
                else (),
            ),
            confidence=_HIGH,
            reasoning="checked shutdown behavior and configured threshold reductions",
        )
