"""Org-level rollup over per-site Verdicts (multisite design §7).

decision = worst under UNKNOWN > UNSAFE > REVIEW > SAFE over (every per-site
Verdict's decision) AND (template_findings: an operational ERROR/CRITICAL floors
REVIEW). org_rejections (short-circuit causes) are handled by the engine BEFORE
fan-out; when present the engine builds an UNKNOWN OrgVerdict directly.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from digital_twin.contracts import Finding, FindingCategory, ObjectRef, Rejection, Severity
from digital_twin.verdict.decision import Decision
from digital_twin.verdict.verdict import Verdict

_PRECEDENCE = {Decision.SAFE: 0, Decision.REVIEW: 1, Decision.UNSAFE: 2, Decision.UNKNOWN: 3}


@dataclass(frozen=True)
class OrgChange:
    """One org object a plan touches, for the multi-object OrgVerdict."""
    ref: ObjectRef                  # kind=object_type, id, name
    action: str                     # "update" | "delete"


@dataclass(frozen=True)
class OrgVerdict:
    decision: Decision
    decision_reasons: tuple[str, ...]
    template_id: str
    per_site: Mapping[str, Verdict]
    driving_sites: tuple[str, ...]
    site_failures: Mapping[str, str]
    template_findings: tuple[Finding, ...]  # NON-fatal template L0 Findings only (REVIEW floor)
    org_rejections: tuple[Rejection, ...]  # short-circuit causes: gate/conflict/lookup/fatal-L0


def decide_org(
    per_site: Mapping[str, Verdict],
    *,
    template_findings: tuple[Finding, ...],
    org_rejections: tuple[Rejection, ...],
) -> tuple[Decision, tuple[str, ...], tuple[str, ...]]:
    if org_rejections:  # short-circuit cause -> UNKNOWN (engine usually handles pre-fan-out)
        rejection_reasons = tuple(
            f"[{r.stage}] {reason}" for r in org_rejections for reason in r.reasons
        )
        return Decision.UNKNOWN, rejection_reasons, ()
    # template-level operational ERROR/CRITICAL floors REVIEW (computed FIRST, so
    # it still applies when there are zero assigned sites)
    template_floor = Decision.REVIEW if any(
        f.category is FindingCategory.OPERATIONAL
        and f.severity in (Severity.ERROR, Severity.CRITICAL)
        for f in template_findings
    ) else Decision.SAFE
    if not per_site:
        if template_floor is Decision.REVIEW:
            return Decision.REVIEW, (
                "template-level L0 finding floors REVIEW; template assigned to no sites",
            ), ()
        return Decision.SAFE, ("template valid; assigned to no sites; no impact simulated",), ()
    worst = max(
        (v.decision for v in per_site.values()),
        key=lambda d: _PRECEDENCE[d],
    )
    decision = max((worst, template_floor), key=lambda d: _PRECEDENCE[d])
    driving = tuple(sorted(sid for sid, v in per_site.items() if v.decision is decision)) \
        if decision is worst and _PRECEDENCE[worst] >= _PRECEDENCE[template_floor] else ()
    reasons: list[str] = []
    if decision is template_floor and template_floor is Decision.REVIEW and not driving:
        reasons.append("template-level L0 finding floors the rollup to REVIEW")
    for sid in driving:
        reasons.append(f"site {sid}: {per_site[sid].decision.value}")
    if not reasons:
        reasons.append(f"rollup decision {decision.value}")
    return decision, tuple(reasons), driving
