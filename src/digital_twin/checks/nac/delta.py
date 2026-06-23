"""GS34 delta-reporting: honest add/remove/change of NAC rules (no impact modeling)."""

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
from digital_twin.ir import Confidence, ConfidenceLevel, IRDiff, NacRule

_HIGH = Confidence(level=ConfidenceLevel.HIGH)


def _finding(rule: NacRule | None, rid: str, kind: str, fields: tuple[str, ...]) -> Finding:
    name = rule.name if rule else None
    ref = ObjectRef("nacrule", rid, name)
    return Finding(
        source=FindingSource.CHECK,
        category=FindingCategory.NETWORK,
        code="nac.rule.change",
        severity=Severity.WARNING,
        confidence=_HIGH,
        message=(f"NAC rule {name or rid!r} {kind}"
                 + (f" ({', '.join(fields)})" if fields else "")
                 + " — access impact is not modeled (review)"),
        affected_entities=(rid,),
        subject=ref,
        evidence={"kind": kind, "changed_fields": list(fields)},
        caused_by=(Cause(ref=ref, fields=fields),),
    )


class NacDeltaCheck:
    id = "nac.rule.change"
    title = "NAC rule added / removed / changed"
    domain = "nac"
    default_severity = Severity.WARNING

    def requires(self) -> frozenset[object]:
        return frozenset()

    def applies_to(self, diff: IRDiff) -> bool:
        return diff.touches("nacrule")

    def run(self, ctx: CheckContext) -> CheckResult:
        base = {r.id: r for r in ctx.baseline.ir.nacrules}
        prop = {r.id: r for r in ctx.proposed.ir.nacrules}
        findings: list[Finding] = []
        for e in ctx.diff.added:
            if e.kind == "nacrule":
                findings.append(_finding(prop.get(e.id), e.id, "added", ()))
        for e in ctx.diff.removed:
            if e.kind == "nacrule":
                findings.append(_finding(base.get(e.id), e.id, "removed", ()))
        for m in ctx.diff.modified:
            if m.ref.kind == "nacrule":
                findings.append(_finding(prop.get(m.ref.id), m.ref.id, "modified",
                                         m.changed_fields))
        return CheckResult(
            check_id=self.id,
            status=Status.WARN if findings else Status.PASS,
            findings=tuple(findings),
            coverage=Coverage(state=CoverageState.COMPLETE),
            confidence=_HIGH,
            reasoning=f"{len(findings)} NAC rule change(s)",
        )
