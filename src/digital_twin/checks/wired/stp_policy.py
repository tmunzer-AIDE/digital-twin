"""wired.stp.policy — precise STP policy attribution under a REVIEW floor.

The four StpPolicy knobs are modeled but the bridge domain is not provable
(unmanaged switches, invisible BPDU sources, off-fabric roots, convergence),
so a policy change NEVER resolves SAFE in this slice: concrete predicted harm
escalates (.blocking_risk / .root_protect_risk, ERROR only at HIGH evidence);
everything else floors REVIEW via .policy_change. SAFE is deferred to a
future STP tree engine validated against live stp_state (see the 2026-07-03
spec)."""

from __future__ import annotations

import dataclasses

from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState, Status
from digital_twin.contracts import Finding, FindingCategory, FindingSource, ObjectRef, Severity
from digital_twin.ir import Capability, Confidence, ConfidenceLevel, IRCapability, IRDiff
from digital_twin.ir.entities import StpPolicy

_MEDIUM = Confidence(
    level=ConfidenceLevel.MEDIUM,
    reasons=("the bridge domain (unmanaged switches, off-fabric roots, convergence) "
             "is not provable",),
)
_HIGH = Confidence(level=ConfidenceLevel.HIGH)


def _changed_knobs(old: StpPolicy | None, new: StpPolicy | None) -> list[str]:
    o, n = old or StpPolicy(), new or StpPolicy()
    return [
        f.name for f in dataclasses.fields(StpPolicy)
        if getattr(o, f.name) != getattr(n, f.name)
    ]


def _is_unresolved(value: object) -> bool:
    return isinstance(value, str) and value.startswith("unresolved:")


class StpPolicyCheck:
    id = "wired.stp.policy"
    title = "STP policy change — blocking/root-protect/mismatch attribution"
    domain = "wired.stp"
    default_severity = Severity.WARNING

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2})

    def applies_to(self, diff: IRDiff) -> bool:
        # precise: a port entry added/removed, or stp_policy among its
        # changed fields — an unrelated port edit must not wake this check
        added_or_removed = any(r.kind == "port" for r in (*diff.added, *diff.removed))
        changed = any(
            m.ref.kind == "port" and "stp_policy" in m.changed_fields
            for m in diff.modified
        )
        return added_or_removed or changed

    def run(self, ctx: CheckContext) -> CheckResult:
        base_ir, prop_ir = ctx.baseline.ir, ctx.proposed.ir
        findings: list[Finding] = []
        notes: list[str] = []
        for pid in sorted(base_ir.ports.keys() | prop_ir.ports.keys()):
            old = base_ir.ports[pid].stp_policy if pid in base_ir.ports else None
            new = prop_ir.ports[pid].stp_policy if pid in prop_ir.ports else None
            if old == new:
                continue
            knobs = _changed_knobs(old, new)
            if not knobs:
                continue
            new_policy = new or StpPolicy()
            unresolved_knobs = [
                k for k in knobs if _is_unresolved(getattr(new_policy, k))
            ]
            for k in unresolved_knobs:
                notes.append(
                    f"port {pid}: {k} is an unresolved: token — no precise "
                    f"prediction is possible, floored to REVIEW"
                )
            findings.append(
                Finding(
                    source=FindingSource.CHECK, category=FindingCategory.NETWORK,
                    code=f"{self.id}.policy_change", severity=Severity.WARNING,
                    confidence=_MEDIUM,
                    message=f"port {pid}: STP policy changed ({', '.join(knobs)}) — "
                            f"impact not provable in this slice (review)",
                    affected_entities=(pid,), subject=ObjectRef("port", pid),
                    evidence={"port": pid, "knobs": knobs},
                    caused_by=ctx.delta_index.causes("port", [pid]),
                )
            )
        coverage = (
            Coverage(state=CoverageState.PARTIAL, notes=tuple(notes))
            if notes
            else Coverage(state=CoverageState.COMPLETE)
        )
        return CheckResult(
            check_id=self.id,
            status=Status.WARN if findings else Status.PASS,
            findings=tuple(findings),
            coverage=coverage,
            confidence=_MEDIUM if findings else _HIGH,
            reasoning="compared per-port StpPolicy baseline vs proposed; every "
                      "change floors REVIEW (bridge domain not provable)",
        )
