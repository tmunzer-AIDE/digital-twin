"""wired.auth.access_change — a switch port's wired-auth admission policy changed.

Enabling/changing 802.1X or MAC-auth (and the RADIUS fallback / dynamic-VLAN
knobs) governs whether clients are admitted and onto which VLAN — outcomes that
depend on the RADIUS server and org NAC rules, which the twin cannot observe or
simulate. So this check does NOT predict pass/fail or the landed VLAN. It floors
REVIEW on any auth-surface change (policy_change), and — when admission TIGHTENS
and currently-connected wired clients are observed in a state the change would
block — escalates detail/confidence (clients_at_risk), capped at REVIEW (RADIUS
could still admit them). Never SAFE, never UNSAFE.
"""

from __future__ import annotations

from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState, Status
from digital_twin.contracts import Finding, FindingCategory, FindingSource, ObjectRef, Severity
from digital_twin.ir import (
    Capability,
    Confidence,
    ConfidenceLevel,
    IRCapability,
    IRDiff,
    min_confidence,
)
from digital_twin.ir.entities import Client, PortAuth, admitted_methods, tightens
from digital_twin.ir.indexes import clients_by_port

_HIGH = Confidence(level=ConfidenceLevel.HIGH)
_INFERRED = Confidence(
    level=ConfidenceLevel.MEDIUM,
    reasons=("admission outcome depends on RADIUS/NAC, which the twin cannot observe",),
)
# observed auth_state values that mean a client is NOT currently authenticated,
# so a newly-required auth would put it at risk
_UNAUTH_STATES = frozenset({"unauthenticated", "unauthorized", "rejected", "failed", "guest"})


def _norm_method(observed: str | None) -> str:
    """Normalize an observed ClientEnrichment.auth_method to {"dot1x","mac",""}."""
    s = (observed or "").lower()
    if "dot1x" in s or "802.1" in s:
        return "dot1x"
    if "mac" in s:
        return "mac"
    return ""


class AuthAccessChangeCheck:
    id = "wired.auth.access_change"
    title = "Wired-auth admission policy changed"
    domain = "wired.auth"
    default_severity = Severity.WARNING

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2})

    def applies_to(self, diff: IRDiff) -> bool:
        return diff.touches("port") or diff.touches("client")

    def run(self, ctx: CheckContext) -> CheckResult:
        base_ir, prop_ir = ctx.baseline.ir, ctx.proposed.ir
        wired = clients_by_port(base_ir)
        findings: list[Finding] = []
        # union of port ids: a base-only port (e.g. a local-only port whose
        # port_auth-bearing entry was deleted) must surface its auth LOSS too —
        # the missing side is None.
        for pid in sorted(base_ir.ports.keys() | prop_ir.ports.keys()):
            base_port = base_ir.ports.get(pid)
            prop_port = prop_ir.ports.get(pid)
            old = base_port.auth if base_port is not None else None
            new = prop_port.auth if prop_port is not None else None
            if old == new:
                continue  # no auth-surface change
            at_risk = self._clients_at_risk(ctx, pid, old, new, wired)
            findings.append(self._finding(ctx, pid, at_risk))
        worst = Status.WARN if findings else Status.PASS
        return CheckResult(
            check_id=self.id,
            status=worst,
            findings=tuple(findings),
            coverage=Coverage(state=CoverageState.COMPLETE),
            confidence=min_confidence(*(f.confidence for f in findings)) if findings else _HIGH,
            reasoning="compared per-port wired-auth surface baseline vs proposed",
        )

    def _clients_at_risk(
        self,
        ctx: CheckContext,
        pid: str,
        old: PortAuth | None,
        new: PortAuth | None,
        wired: dict[str, list[Client]],
    ) -> list[str]:
        """Currently-connected wired clients a tightening would block: observed
        un-authenticated (auth newly required), OR authenticated by a method the
        new config no longer admits (e.g. a dot1x client when the port moves to
        MAC-auth-only). Enrich/cap only — absence of enrichment degrades to []."""
        if not tightens(old, new):
            return []
        admitted = admitted_methods(new)  # None = no auth required (no one blocked by method)
        out: list[str] = []
        for c in wired.get(pid, []):
            ce = ctx.baseline.ir.client_enrichment.get(c.id)
            if ce is None:
                continue  # no observed evidence -> floor only
            unauth = (ce.auth_state or "").lower() in _UNAUTH_STATES
            method = _norm_method(ce.auth_method)
            method_dropped = admitted is not None and method != "" and method not in admitted
            if unauth or method_dropped:
                out.append(c.mac)
        return out

    def _finding(self, ctx: CheckContext, pid: str, at_risk: list[str]) -> Finding:
        cause = tuple(c for c in (ctx.delta_index.cause("port", pid),) if c is not None)
        if at_risk:
            return Finding(
                source=FindingSource.CHECK,
                category=FindingCategory.NETWORK,
                code=f"{self.id}.clients_at_risk",
                severity=Severity.WARNING,  # capped at REVIEW — RADIUS may still admit
                confidence=_HIGH,           # observed un-auth clients = direct evidence of risk
                message=(
                    f"port {pid}: wired-auth now required; {len(at_risk)} connected client(s) "
                    f"observed un-authenticated may be blocked (RADIUS/NAC outcome not modeled)"
                ),
                affected_entities=tuple(at_risk),
                subject=ObjectRef("port", pid),
                evidence={"port": pid, "clients_at_risk": at_risk},
                caused_by=cause,
            )
        return Finding(
            source=FindingSource.CHECK,
            category=FindingCategory.NETWORK,
            code=f"{self.id}.policy_change",
            severity=Severity.WARNING,
            confidence=_INFERRED,
            message=(
                f"port {pid}: wired-auth admission policy changed — access impact "
                "depends on RADIUS/NAC and is not modeled (review)"
            ),
            affected_entities=(pid,),
            subject=ObjectRef("port", pid),
            evidence={"port": pid},
            caused_by=cause,
        )
