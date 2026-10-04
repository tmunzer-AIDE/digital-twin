"""switch.lag_redundancy — aggregate member loss."""

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
from digital_twin.ir import (
    IR,
    Capability,
    Confidence,
    ConfidenceLevel,
    IRCapability,
    IRDiff,
    LinkKind,
)

_HIGH = Confidence(level=ConfidenceLevel.HIGH)


def _bundles(ir: IR) -> dict[tuple[tuple[str, str], str], set[str]]:
    out: defaultdict[tuple[tuple[str, str], str], set[str]] = defaultdict(set)
    for link in ir.links:
        if link.kind not in {LinkKind.LAG, LinkKind.MCLAG} or not link.bundle_id:
            continue
        first, second = sorted(
            (link.a_port.partition(":")[0], link.b_port.partition(":")[0])
        )
        devices = (first, second)
        out[(devices, link.bundle_id)].add(link.id)
    return out


def _configured_bundles(ir: IR) -> dict[tuple[str, str], set[str]]:
    out: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
    for port in ir.ports.values():
        if port.lag_bundle:
            out[(port.device_id, port.lag_bundle)].add(port.id)
    return out


def _observed_lag_ports(ir: IR) -> set[str]:
    return {
        port
        for link in ir.links
        if link.kind in {LinkKind.LAG, LinkKind.MCLAG}
        for port in (link.a_port, link.b_port)
    }


def _lacp_hazards(ir: IR) -> dict[str, tuple[str, ...]]:
    hazards: dict[str, tuple[str, ...]] = {}
    for link in ir.links:
        if link.kind not in {LinkKind.LAG, LinkKind.MCLAG}:
            continue
        left, right = ir.ports[link.a_port], ir.ports[link.b_port]
        modes = tuple(mode for mode in (left.lacp_mode, right.lacp_mode) if mode)
        base_modes = {mode.removesuffix("-slow") for mode in modes}
        if left.lag_bundle is None or right.lag_bundle is None:
            hazards[link.id] = modes
        elif "static" in base_modes and len(base_modes) > 1:
            hazards[link.id] = modes
        elif modes and all(mode.removesuffix("-slow") == "passive" for mode in modes):
            hazards[link.id] = modes
    return hazards


class LagRedundancyCheck:
    id = "switch.lag_redundancy"
    title = "Link aggregate redundancy"
    domain = "switch.lag"
    default_severity = Severity.ERROR

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.L2_TOPOLOGY})

    def applies_to(self, diff: IRDiff) -> bool:
        return diff.touches("link") or any(
            modified.ref.kind == "port"
            and bool(
                {"lag_bundle", "lacp_mode", "lag_unresolved"}
                & set(modified.changed_fields)
            )
            for modified in diff.modified
        )

    def run(self, ctx: CheckContext) -> CheckResult:
        base, prop = _bundles(ctx.baseline.ir), _bundles(ctx.proposed.ir)
        findings: list[Finding] = []
        for key, before in sorted(base.items()):
            after = prop.get(key, set())
            removed = before - after
            if not removed:
                continue
            last = not after
            device = key[0][0]
            findings.append(
                Finding(
                    source=FindingSource.CHECK,
                    category=FindingCategory.NETWORK,
                    code=f"{self.id}.{'last_member_removed' if last else 'redundancy_reduced'}",
                    severity=Severity.ERROR if last else Severity.WARNING,
                    confidence=_HIGH,
                    message=(
                        f"LAG {key[1]} loses "
                        f"{'its final observed forwarding member' if last else 'redundancy'} "
                        f"({len(before)} -> {len(after)} members)"
                    ),
                    subject=ObjectRef("device", device),
                    affected_entities=tuple(sorted(removed)),
                    evidence={
                        "device": device,
                        "bundle": key[1],
                        "baseline_members": sorted(before),
                        "proposed_members": sorted(after),
                    },
                    caused_by=ctx.delta_index.causes("link", sorted(removed)),
                )
            )
        configured_base = _configured_bundles(ctx.baseline.ir)
        configured_prop = _configured_bundles(ctx.proposed.ir)
        observed = _observed_lag_ports(ctx.baseline.ir)
        for modified in ctx.diff.modified:
            if modified.ref.kind != "port" or not (
                {"lag_bundle", "lacp_mode", "lag_unresolved"}
                & set(modified.changed_fields)
            ):
                continue
            proposed_port = ctx.proposed.ir.ports[modified.ref.id]
            if not proposed_port.lag_unresolved:
                continue
            findings.append(Finding(
                source=FindingSource.CHECK, category=FindingCategory.NETWORK,
                code=f"{self.id}.membership_unresolved", severity=Severity.WARNING,
                confidence=_HIGH,
                message=(f"port {proposed_port.id} enables aggregation without a stable "
                         "bundle identifier"),
                subject=ObjectRef("port", proposed_port.id),
                affected_entities=(proposed_port.id,),
                evidence={"port": proposed_port.id, "lag_unresolved": True},
                caused_by=ctx.delta_index.causes("port", [proposed_port.id]),
            ))
        for configured_key, before in sorted(configured_base.items()):
            after = configured_prop.get(configured_key, set())
            removed = before - after
            if not removed:
                continue
            last = not after
            forwarding = bool(removed & observed)
            unsafe = last and forwarding
            findings.append(Finding(
                source=FindingSource.CHECK, category=FindingCategory.NETWORK,
                code=(
                    f"{self.id}.configured_"
                    f"{'last_member_removed' if last else 'redundancy_reduced'}"
                ),
                severity=Severity.ERROR if unsafe else Severity.WARNING,
                confidence=_HIGH,
                message=(
                    f"configured LAG {configured_key[1]} on {configured_key[0]} loses "
                    f"{'its final member' if last else 'redundancy'} "
                    f"({len(before)} -> {len(after)} members)"
                ),
                subject=ObjectRef("device", configured_key[0]),
                affected_entities=tuple(sorted(removed)),
                evidence={"device": configured_key[0], "bundle": configured_key[1],
                          "baseline_members": sorted(before),
                          "proposed_members": sorted(after),
                          "observed_forwarding_member": forwarding},
                caused_by=ctx.delta_index.causes("port", sorted(removed)),
            ))
        base_hazards = _lacp_hazards(ctx.baseline.ir)
        for link_id, modes in sorted(_lacp_hazards(ctx.proposed.ir).items()):
            if base_hazards.get(link_id) == modes:
                continue
            link = next(link for link in ctx.proposed.ir.links if link.id == link_id)
            findings.append(Finding(
                source=FindingSource.CHECK, category=FindingCategory.NETWORK,
                code=f"{self.id}.lacp_inconsistent", severity=Severity.ERROR,
                confidence=_HIGH,
                message=f"LAG link {link_id} has incompatible or incomplete LACP intent",
                subject=ObjectRef("link", link_id), affected_entities=(link.a_port, link.b_port),
                evidence={"link": link_id, "lacp_modes": list(modes)},
                caused_by=ctx.delta_index.causes("port", [link.a_port, link.b_port]),
            ))
        return CheckResult(
            check_id=self.id,
            status=status_from_findings(findings),
            findings=tuple(findings),
            coverage=Coverage(
                CoverageState.PARTIAL if findings else CoverageState.COMPLETE,
                ("per-member forwarding state is inferred from observed aggregate links",)
                if findings else (),
            ),
            confidence=_HIGH,
            reasoning="compared configured and observed LAG membership plus peer LACP intent",
        )
