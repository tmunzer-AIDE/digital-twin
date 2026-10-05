"""wired.dhcp.capacity — proposed pool capacity versus observed demand."""

from __future__ import annotations

import ipaddress
import math

from digital_twin.checks.base import (
    CheckContext,
    CheckResult,
    Coverage,
    CoverageState,
    status_from_findings,
)
from digital_twin.contracts import Finding, FindingCategory, FindingSource, ObjectRef, Severity
from digital_twin.ir import (
    Capability,
    Confidence,
    ConfidenceLevel,
    DhcpScope,
    IRCapability,
    IRDiff,
)

_HIGH = Confidence(level=ConfidenceLevel.HIGH)


def _capacity(scope: DhcpScope | None) -> int | None:
    if scope is None:
        return None
    if not scope.ip_start or not scope.ip_end:
        return None
    try:
        start, end = ipaddress.ip_address(scope.ip_start), ipaddress.ip_address(scope.ip_end)
    except ValueError:
        return None
    if start.version != end.version or int(end) < int(start):
        return None
    total = int(end) - int(start) + 1
    if scope.gateway:
        try:
            gateway = ipaddress.ip_address(scope.gateway)
            if gateway.version == start.version and int(start) <= int(gateway) <= int(end):
                total -= 1
        except ValueError:
            return None
    return max(total, 0)


class DhcpCapacityCheck:
    id = "wired.dhcp.capacity"
    title = "DHCP scope capacity"
    domain = "wired.dhcp"
    default_severity = Severity.ERROR

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.CLIENTS_ACTIVE})

    def applies_to(self, diff: IRDiff) -> bool:
        return diff.touches("dhcp_scope")

    def run(self, ctx: CheckContext) -> CheckResult:
        base = {s.id: s for s in ctx.baseline.ir.dhcp_scopes}
        prop = {s.id: s for s in ctx.proposed.ir.dhcp_scopes}
        findings: list[Finding] = []
        partial = False
        for sid in sorted(set(base) | set(prop)):
            before, after = base.get(sid), prop.get(sid)
            if after is None or before == after:
                continue
            capacity = _capacity(after)
            previous = _capacity(before)
            demand = sum(1 for c in ctx.baseline.ir.clients if c.vlan == after.vlan)
            if capacity is None:
                partial = True
                findings.append(
                    Finding(
                        source=FindingSource.CHECK,
                        category=FindingCategory.NETWORK,
                        code=f"{self.id}.unresolved",
                        severity=Severity.WARNING,
                        confidence=_HIGH,
                        message=(
                            f"DHCP scope {sid} capacity cannot be computed from its "
                            "proposed range"
                        ),
                        subject=ObjectRef("dhcp_scope", sid),
                        affected_entities=(sid,),
                        evidence={"scope": sid, "capacity": None, "observed_clients": demand},
                        caused_by=ctx.delta_index.causes("dhcp_scope", [sid]),
                    )
                )
                continue
            headroom = capacity - demand
            previous_headroom = None if previous is None else previous - demand
            low_mark = max(5, math.ceil(max(demand, 1) * 0.2))
            worsened = previous_headroom is None or headroom < previous_headroom
            if headroom < 0 and worsened:
                severity, suffix = Severity.ERROR, "below_demand"
            elif headroom < low_mark and worsened:
                severity, suffix = Severity.WARNING, "low_headroom"
            else:
                continue
            findings.append(
                Finding(
                    source=FindingSource.CHECK,
                    category=FindingCategory.NETWORK,
                    code=f"{self.id}.{suffix}",
                    severity=severity,
                    confidence=_HIGH,
                    message=(
                        f"DHCP scope {sid} has capacity {capacity} for {demand} observed "
                        f"clients ({headroom} addresses headroom)"
                    ),
                    subject=ObjectRef("dhcp_scope", sid),
                    affected_entities=(sid,),
                    evidence={
                        "scope": sid,
                        "capacity": capacity,
                        "observed_clients": demand,
                        "headroom": headroom,
                        "previous_capacity": previous,
                    },
                    caused_by=ctx.delta_index.causes("dhcp_scope", [sid]),
                )
            )
        return CheckResult(
            check_id=self.id,
            status=status_from_findings(findings),
            findings=tuple(findings),
            coverage=Coverage(
                CoverageState.PARTIAL if partial else CoverageState.COMPLETE,
                ("reserved addresses beyond the configured gateway are not exposed",),
            ),
            confidence=_HIGH,
            reasoning="compared usable configured pool size with active clients",
        )
