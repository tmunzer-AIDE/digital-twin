"""wired.control_plane_reachability — explicit default-route regressions."""

from __future__ import annotations

from digital_twin.checks.base import (
    CheckContext,
    CheckResult,
    Coverage,
    CoverageState,
    status_from_findings,
)
from digital_twin.contracts import Finding, FindingCategory, FindingSource, ObjectRef, Severity
from digital_twin.ir import IR, Capability, Confidence, ConfidenceLevel, IRDiff

_HIGH = Confidence(level=ConfidenceLevel.HIGH)
_DEFAULTS = {"0.0.0.0/0", "::/0"}


def _defaults(ir: IR) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for route in ir.static_routes:
        if route.destination in _DEFAULTS and route.vrf == "default" and not route.discard:
            out.setdefault(route.device_id, set()).add(route.id)
    return out


class ControlPlaneReachabilityCheck:
    id = "wired.control_plane_reachability"
    title = "Device control-plane reachability"
    domain = "wired.control_plane"
    default_severity = Severity.WARNING

    def requires(self) -> frozenset[Capability]:
        return frozenset()

    def applies_to(self, diff: IRDiff) -> bool:
        if diff.touches("static_route"):
            return True
        if any(ref.kind in {"l3intf", "vrf_instance"} for ref in diff.removed):
            return True
        return any(
            modified.ref.kind in {"l3intf", "vrf_instance"}
            for modified in diff.modified
        )

    def run(self, ctx: CheckContext) -> CheckResult:
        base, prop = _defaults(ctx.baseline.ir), _defaults(ctx.proposed.ir)
        findings: list[Finding] = []
        explained_routes: set[str] = set()
        for device, before in sorted(base.items()):
            after = prop.get(device, set())
            if before and not after:
                explained_routes.update(before)
                findings.append(
                    Finding(
                        source=FindingSource.CHECK,
                        category=FindingCategory.NETWORK,
                        code=f"{self.id}.default_route_lost",
                        severity=Severity.WARNING,
                        confidence=_HIGH,
                        message=(
                            f"device {device} loses its last modeled default route; "
                            "control services require review"
                        ),
                        subject=ObjectRef("device", device),
                        affected_entities=(device,),
                        evidence={
                            "device": device,
                            "baseline_routes": sorted(before),
                            "proposed_routes": [],
                            "services": ["Mist cloud", "DNS", "NTP", "RADIUS", "TACACS", "syslog"],
                        },
                        caused_by=ctx.delta_index.causes("static_route", sorted(before)),
                    )
                )
        removed = {(ref.kind, ref.id) for ref in ctx.diff.removed}
        changed = {m.ref.id: set(m.changed_fields) for m in ctx.diff.modified}
        refs = [*ctx.diff.added, *ctx.diff.removed, *(m.ref for m in ctx.diff.modified)]
        for ref in refs:
            if ref.kind not in {"static_route", "l3intf", "vrf_instance"}:
                continue
            if ref.kind == "static_route" and ref.id in explained_routes:
                continue
            if ref.kind in {"l3intf", "vrf_instance"} and (
                ref.kind, ref.id
            ) not in removed and ref.id not in changed:
                continue
            target_device = ref.id.split(":", 1)[0]
            findings.append(
                Finding(
                    source=FindingSource.CHECK,
                    category=FindingCategory.NETWORK,
                    code=f"{self.id}.path_dependency_changed",
                    severity=Severity.WARNING,
                    confidence=_HIGH,
                    message=(
                        f"{ref.kind} {ref.id} changes a routing dependency used by "
                        "device control services; endpoint reachability requires review"
                    ),
                    subject=ObjectRef("device", target_device),
                    affected_entities=(ref.id,),
                    evidence={
                        "changed_dependencies": [f"{ref.kind}:{ref.id}"],
                        "services": [
                            "Mist cloud", "DNS", "NTP", "RADIUS", "TACACS", "syslog"
                        ],
                    },
                    caused_by=ctx.delta_index.causes(ref.kind, [ref.id]),
                )
            )
        return CheckResult(
            check_id=self.id,
            status=status_from_findings(findings),
            findings=tuple(findings),
            coverage=Coverage(
                CoverageState.PARTIAL if findings else CoverageState.COMPLETE,
                (
                    "service endpoints, source interfaces, live RIB, and cloud "
                    "reachability are unavailable",
                )
                if findings
                else (),
            ),
            confidence=_HIGH,
            reasoning="checked loss of explicit configured default routes",
        )
