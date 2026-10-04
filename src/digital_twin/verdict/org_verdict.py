"""Org-level rollup over per-site Verdicts (multisite design §7).

decision = worst under UNSAFE > UNKNOWN > REVIEW > SAFE over (every per-site
Verdict's decision) AND org-level findings. A non-operational ERROR/CRITICAL
floors UNSAFE; an operational ERROR/CRITICAL or any WARNING floors REVIEW.
org_rejections (short-circuit causes) are handled by the engine BEFORE fan-out;
when present the engine builds an UNKNOWN OrgVerdict directly.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from digital_twin.contracts import (
    Finding,
    FindingCategory,
    ObjectConfigDiff,
    ObjectRef,
    Rejection,
    Severity,
)
from digital_twin.verdict.decision import Decision
from digital_twin.verdict.verdict import Verdict

_PRECEDENCE = {Decision.SAFE: 0, Decision.REVIEW: 1, Decision.UNKNOWN: 2, Decision.UNSAFE: 3}


@dataclass(frozen=True)
class OrgChange:
    """One org object a plan touches, for the multi-object OrgVerdict."""
    ref: ObjectRef                  # kind=object_type, id, name
    action: str                     # "update" | "delete"


@dataclass(frozen=True)
class OrgVerdict:
    decision: Decision
    decision_reasons: tuple[str, ...]
    changes: tuple[OrgChange, ...]   # the org objects this plan touches (multi-object-native)
    per_site: Mapping[str, Verdict]
    driving_sites: tuple[str, ...]
    site_failures: Mapping[str, str]
    template_findings: tuple[Finding, ...]  # org-level validation and targeted-policy findings
    org_rejections: tuple[Rejection, ...]  # short-circuit causes: gate/conflict/lookup/fatal-L0
    config_diffs: tuple[ObjectConfigDiff, ...] = ()  # raw before→after of the touched org objects


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
    # Compute the org-level finding floor first so it also applies with zero assigned
    # sites. This mirrors decide(): network/security ERROR or CRITICAL is UNSAFE;
    # warnings and operational errors require REVIEW.
    if any(
        f.category is not FindingCategory.OPERATIONAL
        and f.severity in (Severity.ERROR, Severity.CRITICAL)
        for f in template_findings
    ):
        template_floor = Decision.UNSAFE
    elif any(
        f.severity is Severity.WARNING
        or (
            f.category is FindingCategory.OPERATIONAL
            and f.severity in (Severity.ERROR, Severity.CRITICAL)
        )
        for f in template_findings
    ):
        template_floor = Decision.REVIEW
    else:
        template_floor = Decision.SAFE
    if not per_site:
        if template_floor is not Decision.SAFE:
            return template_floor, (
                f"org-level finding floors {template_floor.value.upper()}; no assigned sites",
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
    if decision is template_floor and template_floor is not Decision.SAFE and not driving:
        reasons.append(
            f"org-level finding floors the rollup to {template_floor.value.upper()}"
        )
    for sid in driving:
        reasons.append(f"site {sid}: {per_site[sid].decision.value}")
    if not reasons:
        reasons.append(f"rollup decision {decision.value}")
    return decision, tuple(reasons), driving
