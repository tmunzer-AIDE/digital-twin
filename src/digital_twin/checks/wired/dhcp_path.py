"""wired.dhcp.path — the delta removes a vlan's only modeled DHCP path (GS24).

Vlan.dhcp_sources holds the MODELED providers: the site-level switch-hosted
server/relay ("site") and gateways' own dhcpd_config (device ids). Clients on
a vlan whose last modeled path disappears lose addressing at lease renewal —
the outage is delayed and therefore easy to misattribute, which is exactly why
it deserves a pre-apply verdict.

Honesty rails (the review-series lessons, applied from birth):
- a vlan that NEVER had a modeled path is silent — external DHCP servers are
  invisible and no intent marker exists for DHCP (unlike subnet for L3);
- removal with OBSERVED clients on the vlan -> ERROR at config confidence
  (UNSAFE at HIGH); without -> WARNING (future joiners still break);
- an l3_unmodeled gateway may hold the invisible replacement server: the
  claim caps at MEDIUM (-> WARNING/REVIEW) and coverage notes it — the cap
  must live on the FINDING (verdict precedence reads findings before
  coverage);
- clients unfetched (no CLIENTS_ACTIVE) -> the blast radius is UNKNOWN:
  severity stays WARNING and coverage degrades — never a silent downgrade.
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

_HIGH = Confidence(level=ConfidenceLevel.HIGH)
_BLIND_GATEWAY = Confidence(
    level=ConfidenceLevel.MEDIUM,
    reasons=("an unmodeled gateway may hold the replacement DHCP server",),
)


class DhcpPathCheck:
    id = "wired.dhcp.path"
    title = "vlan loses its only modeled DHCP path"
    domain = "wired.dhcp"
    default_severity = Severity.ERROR

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2})

    def applies_to(self, diff: IRDiff) -> bool:
        # dhcp_sources lives on vlans; device diffs can change gateway sources
        return any(diff.touches(k) for k in ("vlan", "device"))

    def run(self, ctx: CheckContext) -> CheckResult:
        base_ir, prop_ir = ctx.baseline.ir, ctx.proposed.ir
        clients_known = IRCapability.CLIENTS_ACTIVE in prop_ir.capabilities
        blind_notes = tuple(
            f"gateway {d.id}: network namespace unmodeled — its DHCP servers "
            "are invisible to this check"
            for d in sorted(prop_ir.devices.values(), key=lambda d: d.id)
            if d.l3_unmodeled
        )
        findings: list[Finding] = []
        notes: list[str] = []
        for vid, vlan in sorted(prop_ir.vlans.items()):
            base_vlan = base_ir.vlans.get(vid)
            if vlan.dhcp_sources or base_vlan is None or not base_vlan.dhcp_sources:
                continue  # still served, or never had a modeled path
            n_clients = sum(1 for c in base_ir.clients if c.vlan == vid)
            confidence = _HIGH
            if blind_notes:
                confidence = min_confidence(confidence, _BLIND_GATEWAY)
            high = confidence.level is ConfidenceLevel.HIGH
            severity = (
                Severity.ERROR if (clients_known and n_clients and high) else Severity.WARNING
            )
            if not clients_known:
                notes.append(
                    f"vlan {vid}: client data unavailable — the blast radius of the "
                    "removed DHCP path is unknown"
                )
            who = (
                f"{n_clients} observed client(s)"
                if clients_known
                else "an unknown number of clients"
            )
            findings.append(
                Finding(
                    source=FindingSource.CHECK,
                    category=FindingCategory.NETWORK,
                    code=f"{self.id}.removed",
                    severity=severity,
                    confidence=confidence,
                    message=(
                        f"vlan {vid} loses its only modeled DHCP path "
                        f"({', '.join(base_vlan.dhcp_sources)}) — {who} on it lose "
                        "addressing at lease renewal"
                    ),
                    affected_entities=(str(vid),),
                    evidence={
                        "vlan": vid,
                        "removed_sources": list(base_vlan.dhcp_sources),
                        "observed_clients": n_clients if clients_known else None,
                    },
                )
            )
        if findings and blind_notes:
            notes.extend(blind_notes)
        worst = Status.PASS
        for f in findings:
            this = Status.FAIL if f.severity is Severity.ERROR else Status.WARN
            if this is Status.FAIL or worst is Status.PASS:
                worst = this
        return CheckResult(
            check_id=self.id,
            status=worst,
            findings=tuple(findings),
            coverage=Coverage(
                state=CoverageState.PARTIAL if notes else CoverageState.COMPLETE,
                notes=tuple(notes),
            ),
            confidence=(
                min_confidence(*(f.confidence for f in findings)) if findings else _HIGH
            ),
            reasoning="compared each vlan's modeled DHCP providers, baseline vs proposed",
        )
