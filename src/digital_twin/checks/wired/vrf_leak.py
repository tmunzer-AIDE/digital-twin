"""routing.vrf_leak — explicit VRF membership boundary changes."""

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
from digital_twin.ir import IR, Capability, Confidence, ConfidenceLevel, IRDiff

_HIGH = Confidence(level=ConfidenceLevel.HIGH)


def _owners(ir: IR) -> dict[tuple[str, str], set[str]]:
    out: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
    for vrf in ir.vrf_instances:
        for network in vrf.networks:
            out[(vrf.device_id, network)].add(vrf.name)
    return out


class VrfLeakCheck:
    id = "routing.vrf_leak"
    title = "VRF reachability boundary"
    domain = "routing.vrf"
    default_severity = Severity.ERROR

    def requires(self) -> frozenset[Capability]:
        return frozenset()

    def applies_to(self, diff: IRDiff) -> bool:
        return diff.touches("vrf_instance")

    def run(self, ctx: CheckContext) -> CheckResult:
        base, prop = _owners(ctx.baseline.ir), _owners(ctx.proposed.ir)
        findings: list[Finding] = []
        touched = {m.ref.id for m in ctx.diff.modified if m.ref.kind == "vrf_instance"}
        touched |= {r.id for r in (*ctx.diff.added, *ctx.diff.removed) if r.kind == "vrf_instance"}
        devices = {rid.split(":", 1)[0] for rid in touched}
        for (device, network), after in sorted(prop.items()):
            if device not in devices:
                continue
            before = base.get((device, network), set())
            if len(after) > 1 and len(after) > len(before):
                findings.append(
                    Finding(
                        source=FindingSource.CHECK,
                        category=FindingCategory.NETWORK,
                        code=f"{self.id}.multiple_membership",
                        severity=Severity.ERROR,
                        confidence=_HIGH,
                        message=(
                            f"network {network} is assigned to multiple VRFs: "
                            f"{', '.join(sorted(after))}"
                        ),
                        subject=ObjectRef("device", device),
                        affected_entities=(network,),
                        evidence={
                            "device": device,
                            "network": network,
                            "baseline_vrfs": sorted(before),
                            "proposed_vrfs": sorted(after),
                        },
                        caused_by=ctx.delta_index.causes("vrf_instance", sorted(touched)),
                    )
                )
            elif before and before != after:
                findings.append(
                    Finding(
                        source=FindingSource.CHECK,
                        category=FindingCategory.NETWORK,
                        code=f"{self.id}.membership_changed",
                        severity=Severity.WARNING,
                        confidence=_HIGH,
                        message=f"network {network} moves across a VRF boundary",
                        subject=ObjectRef("device", device),
                        affected_entities=(network,),
                        evidence={
                            "device": device,
                            "network": network,
                            "baseline_vrfs": sorted(before),
                            "proposed_vrfs": sorted(after),
                        },
                        caused_by=ctx.delta_index.causes("vrf_instance", sorted(touched)),
                    )
                )
        for key, before in sorted(base.items()):
            if key[0] in devices and key not in prop:
                findings.append(
                    Finding(
                        source=FindingSource.CHECK,
                        category=FindingCategory.NETWORK,
                        code=f"{self.id}.membership_removed",
                        severity=Severity.WARNING,
                        confidence=_HIGH,
                        message=f"network {key[1]} loses VRF membership",
                        subject=ObjectRef("device", key[0]),
                        affected_entities=(key[1],),
                        evidence={
                            "device": key[0],
                            "network": key[1],
                            "baseline_vrfs": sorted(before),
                            "proposed_vrfs": [],
                        },
                        caused_by=ctx.delta_index.causes("vrf_instance", sorted(touched)),
                    )
                )
        return CheckResult(
            check_id=self.id,
            status=status_from_findings(findings),
            findings=tuple(findings),
            coverage=Coverage(
                CoverageState.PARTIAL if findings else CoverageState.COMPLETE,
                ("route targets and live inter-VRF reachability are not exposed",)
                if findings
                else (),
            ),
            confidence=_HIGH,
            reasoning="compared explicit per-device VRF network membership",
        )
