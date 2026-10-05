"""gateway.wan.redundancy — configured WAN path loss on gateways."""

from __future__ import annotations

from collections import defaultdict

from digital_twin.checks.base import (
    CheckContext,
    CheckResult,
    Coverage,
    CoverageState,
    status_from_findings,
)
from digital_twin.contracts import Finding, FindingCategory, FindingSource, ObjectRef, Severity
from digital_twin.ir import IR, Capability, Confidence, ConfidenceLevel, DeviceRole, IRDiff

_HIGH = Confidence(level=ConfidenceLevel.HIGH)


def _active_wans(ir: IR) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    gateways = {d.id for d in ir.devices.values() if d.role is DeviceRole.GATEWAY}
    for port in ir.ports.values():
        if port.device_id in gateways and port.profile == "wan" and not port.disabled:
            out[port.device_id].add(port.id)
    return out


class GatewayWanRedundancyCheck:
    id = "gateway.wan.redundancy"
    title = "Gateway WAN path redundancy"
    domain = "gateway.wan"
    default_severity = Severity.ERROR

    def requires(self) -> frozenset[Capability]:
        return frozenset()

    def applies_to(self, diff: IRDiff) -> bool:
        return diff.touches("port")

    def run(self, ctx: CheckContext) -> CheckResult:
        base = _active_wans(ctx.baseline.ir)
        prop = _active_wans(ctx.proposed.ir)
        touched_ports = {
            ref.id
            for ref in (*ctx.diff.added, *ctx.diff.removed, *(m.ref for m in ctx.diff.modified))
            if ref.kind == "port"
        }
        findings: list[Finding] = []
        for did in sorted(set(base) | set(prop)):
            before, after = base.get(did, set()), prop.get(did, set())
            changed = (before ^ after) & touched_ports
            if not changed:
                continue
            removed = before - after
            added = after - before
            if removed:
                last = not after
                severity = Severity.ERROR if last else Severity.WARNING
                suffix = "last_path_removed" if last else "redundancy_reduced"
                findings.append(
                    Finding(
                        source=FindingSource.CHECK,
                        category=FindingCategory.NETWORK,
                        code=f"{self.id}.{suffix}",
                        severity=severity,
                        confidence=_HIGH,
                        message=(
                            f"gateway {did} loses {'its last' if last else 'a redundant'} "
                            f"configured WAN path ({len(before)} -> {len(after)})"
                        ),
                        subject=ObjectRef("device", did),
                        affected_entities=tuple(sorted(removed)),
                        evidence={
                            "device": did,
                            "baseline_viable_paths": sorted(before),
                            "proposed_viable_paths": sorted(after),
                            "removed_paths": sorted(removed),
                            "health_evidence": "unavailable",
                        },
                        caused_by=ctx.delta_index.causes("port", sorted(removed)),
                    )
                )
            elif added:
                findings.append(
                    Finding(
                        source=FindingSource.CHECK,
                        category=FindingCategory.NETWORK,
                        code=f"{self.id}.path_added_unverified",
                        severity=Severity.WARNING,
                        confidence=_HIGH,
                        message=(
                            f"gateway {did} adds a configured WAN path, but path health "
                            "has not been observed"
                        ),
                        subject=ObjectRef("device", did),
                        affected_entities=tuple(sorted(added)),
                        evidence={
                            "device": did,
                            "added_paths": sorted(added),
                            "health_evidence": "unavailable",
                        },
                        caused_by=ctx.delta_index.causes("port", sorted(added)),
                    )
                )
        return CheckResult(
            check_id=self.id,
            status=status_from_findings(findings),
            findings=tuple(findings),
            coverage=Coverage(
                CoverageState.PARTIAL if findings else CoverageState.COMPLETE,
                ("WAN health, VPN use, preference, and bandwidth telemetry are unavailable",)
                if findings
                else (),
            ),
            confidence=_HIGH,
            reasoning="compared active configured WAN ports per gateway",
        )
