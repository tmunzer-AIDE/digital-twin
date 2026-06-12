"""wired.dhcp.scope_lint — DHCP scope hygiene over the minted DhcpScope rows (GS25).

WARNING/REVIEW tier (MVP): two scopes whose lease ranges overlap hand out
duplicate addresses, and a range/gateway outside the owning network's subnet
mints unroutable leases — both misconfigurations, neither a proven outage,
so the ceiling is WARNING.

Violation-specific parity (native-mismatch precedent): a violation demotes to
INFO only when the SAME violation existed in baseline with the SAME values —
overlap requires both ranges string-identical AND already overlapping;
out-of-subnet requires the identical violation tuple AND identical subnet.
Touching the hazard forfeits the demotion.

Dimension-specific abstention (gateway_gap lesson — INFO/irrelevance never
drags PARTIAL):
- a scope with an unparseable/absent range blinds the OVERLAP dimension; the
  note attaches only when the check concluded something (non-INFO finding) or
  the delta touches dhcp_scope rows;
- subnet_unresolved=True blinds the OUT-OF-SUBNET dimension for THAT scope
  only; the note attaches only when that very scope is in the delta (the
  blind subnet can only hide a violation of itself). Plain subnet=None is no
  intent — nothing to verify, no note.
"""

from __future__ import annotations

import ipaddress
import itertools

from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState, Status
from digital_twin.contracts import Finding, FindingCategory, FindingSource, Severity
from digital_twin.ir import (
    Capability,
    Confidence,
    ConfidenceLevel,
    DhcpScope,
    IRCapability,
    IRDiff,
    min_confidence,
)

_HIGH = Confidence(level=ConfidenceLevel.HIGH)


def _ip(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(ipaddress.ip_address(value))
    except ValueError:
        return None


def _range(scope: DhcpScope) -> tuple[int, int] | None:
    lo, hi = _ip(scope.ip_start), _ip(scope.ip_end)
    if lo is None or hi is None:
        return None
    return (lo, hi) if lo <= hi else (hi, lo)


def _net(subnet: str | None) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    if subnet is None:
        return None
    try:
        return ipaddress.ip_network(subnet, strict=False)
    except ValueError:
        return None


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


def _subnet_violations(scope: DhcpScope) -> tuple[str, ...]:
    """Each parseable ip_start/ip_end/gateway outside the scope's parseable
    subnet, as stable "field=value" strings (the parity comparison key)."""
    net = _net(scope.subnet)
    if net is None:
        return ()
    out: list[str] = []
    for field in ("ip_start", "ip_end", "gateway"):
        value: str | None = getattr(scope, field)
        if value is None:
            continue
        try:
            addr = ipaddress.ip_address(value)
        except ValueError:
            continue
        if addr not in net:
            out.append(f"{field}={value}")
    return tuple(out)


class DhcpScopeLintCheck:
    id = "wired.dhcp.scope_lint"
    title = "DHCP scope ranges overlap or escape their subnet"
    domain = "wired.dhcp"
    default_severity = Severity.WARNING

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2})

    def applies_to(self, diff: IRDiff) -> bool:
        return any(diff.touches(k) for k in ("dhcp_scope", "vlan"))

    def run(self, ctx: CheckContext) -> CheckResult:
        base = {s.id: s for s in ctx.baseline.ir.dhcp_scopes}
        prop = {s.id: s for s in ctx.proposed.ir.dhcp_scopes}
        prop_sorted = sorted(prop.values(), key=lambda s: s.id)
        findings: list[Finding] = []

        # .overlap — pairwise over scopes with parseable ranges
        parseable = [s for s in prop_sorted if _range(s) is not None]
        for a, b in itertools.combinations(parseable, 2):
            ra, rb = _range(a), _range(b)
            assert ra is not None and rb is not None  # filtered above
            if not _overlaps(ra, rb):
                continue
            severity = Severity.WARNING
            ba, bb = base.get(a.id), base.get(b.id)
            if ba is not None and bb is not None:
                bra, brb = _range(ba), _range(bb)
                untouched = (a.ip_start, a.ip_end) == (ba.ip_start, ba.ip_end) and (
                    b.ip_start,
                    b.ip_end,
                ) == (bb.ip_start, bb.ip_end)
                if bra is not None and brb is not None and _overlaps(bra, brb) and untouched:
                    severity = Severity.INFO  # same hazard, same values: pre-existing
            findings.append(
                Finding(
                    source=FindingSource.CHECK,
                    category=FindingCategory.NETWORK,
                    code=f"{self.id}.overlap",
                    severity=severity,
                    confidence=_HIGH,
                    message=(
                        f"DHCP scopes {a.id} ({a.ip_start}-{a.ip_end}) and "
                        f"{b.id} ({b.ip_start}-{b.ip_end}) hand out overlapping "
                        "address ranges — duplicate leases"
                    ),
                    affected_entities=(a.id, b.id),
                    evidence={"scopes": [a.id, b.id]},
                )
            )

        # .out_of_subnet — per scope with a parseable subnet
        for s in prop_sorted:
            violations = _subnet_violations(s)
            if not violations:
                continue
            severity = Severity.WARNING
            bs = base.get(s.id)
            if bs is not None and bs.subnet == s.subnet and _subnet_violations(bs) == violations:
                severity = Severity.INFO  # identical violation, identical subnet
            findings.append(
                Finding(
                    source=FindingSource.CHECK,
                    category=FindingCategory.NETWORK,
                    code=f"{self.id}.out_of_subnet",
                    severity=severity,
                    confidence=_HIGH,
                    message=(
                        f"DHCP scope {s.id}: {', '.join(violations)} fall(s) outside "
                        f"the network's subnet {s.subnet} — leases would be unroutable"
                    ),
                    affected_entities=(s.id,),
                    evidence={"scope": s.id, "subnet": s.subnet, "violations": list(violations)},
                )
            )

        non_info = [f for f in findings if f.severity is not Severity.INFO]
        notes: list[str] = []
        # range blindness: relevant when something was concluded or scopes changed
        if non_info or ctx.diff.touches("dhcp_scope"):
            notes.extend(
                f"scope {s.id}: lease range unparseable — overlap lint cannot "
                "clear changes against it"
                for s in prop_sorted
                if _range(s) is None
            )
        # subnet blindness: per-scope — only when THAT scope is in the delta
        changed_ids = {
            r.id
            for r in (*ctx.diff.added, *ctx.diff.removed, *(m.ref for m in ctx.diff.modified))
            if r.kind == "dhcp_scope"
        }
        notes.extend(
            f"scope {s.id}: subnet intent exists but is unresolved — "
            "out-of-subnet lint skipped for it"
            for s in prop_sorted
            if s.subnet_unresolved and s.id in changed_ids
        )

        return CheckResult(
            check_id=self.id,
            status=Status.WARN if non_info else Status.PASS,
            findings=tuple(findings),
            coverage=Coverage(
                state=CoverageState.PARTIAL if notes else CoverageState.COMPLETE,
                notes=tuple(notes),
            ),
            confidence=(
                min_confidence(*(f.confidence for f in non_info)) if non_info else _HIGH
            ),
            reasoning="linted proposed DHCP scopes pairwise for range overlap and "
            "per-scope against the declared subnet, demoting baseline-identical violations",
        )
