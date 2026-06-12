"""wired.dhcp.scope_lint — DHCP scope hygiene over the minted DhcpScope rows (GS25).

WARNING/REVIEW tier (MVP): two scopes whose lease ranges overlap hand out
duplicate addresses, a range/gateway outside the owning network's subnet
mints unroutable leases, and a handed-out gateway incoherent with the owning
network's declared gateway (GS22-GW) points clients at the wrong next hop —
all misconfigurations (config coherence, not proven outage), so the ceiling
is WARNING.

Violation-specific parity (native-mismatch precedent): a violation demotes to
INFO only when the SAME violation existed in baseline with the SAME values —
overlap requires both ranges string-identical AND already overlapping;
out-of-subnet requires the identical violation tuple AND identical subnet;
gateway_mismatch requires BOTH the handed and declared values byte-identical
in baseline. Touching the hazard forfeits the demotion.

Dimension-specific abstention (gateway_gap lesson — INFO/irrelevance never
drags PARTIAL):
- a scope with an unparseable/absent/mixed-family/inverted range blinds the
  OVERLAP dimension (anomalous = unevaluable, never normalized); the
  note attaches only when the check concluded something (non-INFO finding) or
  the delta touches dhcp_scope rows;
- subnet_unresolved=True blinds the OUT-OF-SUBNET dimension for THAT scope
  only; the note attaches only when that very scope is in the delta (the
  blind subnet can only hide a violation of itself). Plain subnet=None is no
  intent — nothing to verify, no note;
- network_gateway_unresolved=True (or an unparseable handed/declared pair)
  blinds the GATEWAY-COHERENCE dimension for THAT scope only — same per-scope
  delta relevance as the subnet notes. Either side plainly absent is no
  intent — silent.
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
    same_ip,
)

_HIGH = Confidence(level=ConfidenceLevel.HIGH)


def _range(scope: DhcpScope) -> tuple[int, int, int] | None:
    """(ip_version, lo, hi) — None when absent, unparseable, mixed-family or
    inverted (anomalous = unevaluable, never normalized)."""
    if scope.ip_start is None or scope.ip_end is None:
        return None
    try:
        start = ipaddress.ip_address(scope.ip_start)
        end = ipaddress.ip_address(scope.ip_end)
    except ValueError:
        return None
    if start.version != end.version or int(end) < int(start):
        return None
    return (start.version, int(start), int(end))


def _net(subnet: str | None) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    if subnet is None:
        return None
    try:
        return ipaddress.ip_network(subnet, strict=False)
    except ValueError:
        return None


def _overlaps(a: tuple[int, int, int], b: tuple[int, int, int]) -> bool:
    """(version, lo, hi) ranges overlap — never across IP families."""
    return a[0] == b[0] and a[1] <= b[2] and b[1] <= a[2]


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
        # cross-family containment is version-dependent in ipaddress; by this
        # check's definition a different-family value lies outside the subnet
        if addr.version != net.version or addr not in net:
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
        ranged: dict[str, tuple[int, int, int]] = {}
        for s in prop_sorted:
            r = _range(s)
            if r is not None:
                ranged[s.id] = r
        for (a_id, ra), (b_id, rb) in itertools.combinations(ranged.items(), 2):
            if not _overlaps(ra, rb):
                continue
            a, b = prop[a_id], prop[b_id]
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

        # --- .gateway_mismatch (DHCP hands out a gateway incoherent with
        # its owning network — config coherence, not proven outage)
        for s in prop_sorted:
            verdict = same_ip(s.gateway, s.network_gateway)
            if verdict is not False:
                continue  # equal, or either side absent/unreadable
            bs = base.get(s.id)
            preexisting = (
                bs is not None
                and bs.gateway == s.gateway
                and bs.network_gateway == s.network_gateway
            )
            findings.append(
                Finding(
                    source=FindingSource.CHECK,
                    category=FindingCategory.NETWORK,
                    code=f"{self.id}.gateway_mismatch",
                    severity=Severity.INFO if preexisting else Severity.WARNING,
                    confidence=_HIGH,
                    message=(
                        f"DHCP scope {s.id} hands out gateway {s.gateway} but its "
                        f"network declares {s.network_gateway}"
                        + (" (pre-existing, unchanged)" if preexisting else "")
                    ),
                    affected_entities=(s.id,),
                    evidence={
                        "scope": s.id,
                        "handed": s.gateway,
                        "declared": s.network_gateway,
                    },
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
        # subnet blindness: per-scope — only when THAT scope is in the delta.
        # removed refs are dead weight: a removed scope is absent from proposed,
        # so its per-scope note can never fire.
        changed_ids = {
            r.id
            for r in (*ctx.diff.added, *(m.ref for m in ctx.diff.modified))
            if r.kind == "dhcp_scope"
        }
        notes.extend(
            f"scope {s.id}: subnet intent exists but is unresolved — "
            "out-of-subnet lint skipped for it"
            for s in prop_sorted
            if s.subnet_unresolved and s.id in changed_ids
        )
        # gateway-coherence blindness: per-scope, same delta relevance
        notes.extend(
            f"scope {s.id}: owning network gateway is unreadable or unknowable "
            "— gateway coherence is unevaluated for it"
            for s in prop_sorted
            if s.network_gateway_unresolved and s.id in changed_ids
        )
        notes.extend(
            f"scope {s.id}: handed/declared gateway is unparseable — gateway "
            "coherence cannot be evaluated"
            for s in prop_sorted
            if s.gateway is not None
            and s.network_gateway is not None
            and same_ip(s.gateway, s.network_gateway) is None
            and s.id in changed_ids
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
