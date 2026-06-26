"""wired.port.mac_limit_exceeded — a lowered/new MAC limit that drops currently-
connected wired clients, or that we cannot confirm is safe.

Per port whose mac_limit the delta made MORE RESTRICTIVE (None=unlimited >
concrete > lower-concrete; an unresolved/templated value is uncertain): if the
limit is unresolved -> REVIEW (.unresolved); else if active wired-client data is
present on BOTH sides (CLIENTS_ACTIVE) -> compare the baseline client count
(currently connected) against the new limit: over -> REVIEW (.exceeded), within
-> silent; if client data is absent -> REVIEW (.unverified, cannot confirm). The
*count* over-limit is certain but *which* MACs the switch evicts (and aging) are
not, so this is capped at REVIEW — never ERROR/UNSAFE; requires WIRED_L2 only so
the .unverified path is not registry-short-circuited.
"""

from __future__ import annotations

from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState, Status
from digital_twin.contracts import (
    Cause,
    Finding,
    FindingCategory,
    FindingSource,
    ObjectRef,
    Severity,
)
from digital_twin.ir import Capability, Confidence, ConfidenceLevel, IRCapability, IRDiff
from digital_twin.ir.entities import Client
from digital_twin.ir.indexes import clients_by_port

_HIGH = Confidence(level=ConfidenceLevel.HIGH)
_MEDIUM = Confidence(
    level=ConfidenceLevel.MEDIUM,
    reasons=("current per-port client count is unobservable (no active-client data)",),
)


def _more_restrictive(old: int | str | None, new: int | str | None) -> bool:
    """`new` caps more than `old`. None=unlimited (least), int=that cap, str=
    unresolved (treated as uncertain -> restrictive when it changed)."""
    if new is None:
        return False
    if isinstance(new, str):
        return old != new
    # new is concrete int
    if old is None or isinstance(old, str):
        return True
    return new < old


class MacLimitExceededCheck:
    id = "wired.port.mac_limit_exceeded"
    title = "MAC limit lowered below connected clients"
    domain = "wired.port"
    default_severity = Severity.WARNING

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2})

    def applies_to(self, diff: IRDiff) -> bool:
        return diff.touches("port") or diff.touches("client")

    def run(self, ctx: CheckContext) -> CheckResult:
        base_ir, prop_ir = ctx.baseline.ir, ctx.proposed.ir
        clients_known = (
            IRCapability.CLIENTS_ACTIVE in base_ir.capabilities
            and IRCapability.CLIENTS_ACTIVE in prop_ir.capabilities
        )
        wired = clients_by_port(base_ir)
        findings: list[Finding] = []
        for pid in sorted(base_ir.ports.keys() | prop_ir.ports.keys()):
            old = base_ir.ports[pid].mac_limit if pid in base_ir.ports else None
            new = prop_ir.ports[pid].mac_limit if pid in prop_ir.ports else None
            if old == new or not _more_restrictive(old, new):
                continue
            f = self._finding(ctx, pid, new, wired, clients_known)
            if f is not None:
                findings.append(f)
        worst = Status.WARN if findings else Status.PASS
        return CheckResult(
            check_id=self.id, status=worst, findings=tuple(findings),
            coverage=Coverage(state=CoverageState.COMPLETE),
            confidence=_HIGH,
            reasoning="compared per-port mac_limit vs connected clients baseline vs proposed",
        )

    def _finding(
        self, ctx: CheckContext, pid: str, new: int | str | None,
        wired: dict[str, list[Client]], clients_known: bool,
    ) -> Finding | None:
        cause = ctx.delta_index.causes("port", [pid])
        if isinstance(new, str):  # unresolved/templated
            return self._mk(pid, "unresolved", _MEDIUM,
                            f"mac_limit changed to a non-evaluable value ({new})", cause)
        # new is a concrete int (restrictive, per the caller's _more_restrictive gate)
        if not isinstance(new, int):
            return None  # unreachable: _more_restrictive gates out None; satisfies mypy
        if not clients_known:
            return self._mk(pid, "unverified", _MEDIUM,
                            f"mac_limit set to {new}; current client count is unobservable", cause)
        observed = len(wired.get(pid, []))
        if observed > new:
            return self._mk(pid, "exceeded", _HIGH,
                            f"{observed} connected client(s) exceed the new mac_limit {new}", cause)
        return None  # proven within the cap

    def _mk(
        self, pid: str, code: str, conf: Confidence, msg: str, cause: tuple[Cause, ...],
    ) -> Finding:
        return Finding(
            source=FindingSource.CHECK, category=FindingCategory.NETWORK,
            code=f"{self.id}.{code}", severity=Severity.WARNING, confidence=conf,
            message=f"port {pid}: {msg}", affected_entities=(pid,),
            subject=ObjectRef("port", pid), evidence={"port": pid}, caused_by=cause,
        )
