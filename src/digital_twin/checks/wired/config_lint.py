"""Shared delta-conditioning core for the config-lint tier (GS30–GS33).

Each lint check computes a list of `Violation`s on baseline and on proposed; this
core emits INTRODUCED violations (key not in baseline) as WARNING and PRE-EXISTING
ones (key in baseline) as INFO context. The violation KEY carries the violation
facts, so a *changed* violation reads as introduced, not pre-existing.

Every lint fact is config-derived and deterministic, so the tier is HIGH-confidence
by design (no sub-HIGH path)."""

from __future__ import annotations

from collections.abc import Hashable
from dataclasses import dataclass, field
from typing import Any

from digital_twin.checks.base import CheckResult, Coverage, Status
from digital_twin.contracts import (
    Cause,
    Finding,
    FindingCategory,
    FindingSource,
    ObjectRef,
    Severity,
)
from digital_twin.ir import Confidence, ConfidenceLevel, IRDiff

_HIGH = Confidence(level=ConfidenceLevel.HIGH)


def touched_ids(diff: IRDiff, kind: str) -> set[str]:
    """Entity ids of `kind` the delta added/removed/modified. Used to RELEVANCE-SCOPE
    coverage notes: a lint check emits a PARTIAL note only when the unverifiable item is
    itself delta-touched (PARTIAL floors to REVIEW, so an unrelated old wxtag/unparseable
    item must never taint an unrelated change)."""
    refs = (*diff.added, *diff.removed, *(m.ref for m in diff.modified))
    return {r.id for r in refs if r.kind == kind}


@dataclass(frozen=True)
class Violation:
    key: Hashable               # identity incl. facts (changed violation => introduced)
    subject: ObjectRef
    affected: tuple[str, ...]
    summary: str                # human phrase
    evidence: dict[str, Any] = field(default_factory=dict)
    caused_by: tuple[Cause, ...] = ()


def run_delta_lint(
    *, check_id: str, base: list[Violation], proposed: list[Violation], coverage: Coverage
) -> CheckResult:
    base_keys = {v.key for v in base}
    findings: list[Finding] = []
    for v in proposed:
        introduced = v.key not in base_keys
        sev = Severity.WARNING if introduced else Severity.INFO
        code = "introduced" if introduced else "preexisting"
        suffix = "" if introduced else " (pre-existing, unchanged by the delta — context)"
        findings.append(
            Finding(
                source=FindingSource.CHECK,
                category=FindingCategory.NETWORK,
                code=f"{check_id}.{code}",
                severity=sev,
                confidence=_HIGH,
                message=f"{v.summary}{suffix}",
                affected_entities=v.affected,
                subject=v.subject,
                evidence=dict(v.evidence),
                caused_by=v.caused_by if introduced else (),
            )
        )
    conclusions = [f for f in findings if f.severity is not Severity.INFO]
    return CheckResult(
        check_id=check_id,
        status=Status.WARN if conclusions else Status.PASS,
        findings=tuple(findings),
        coverage=coverage,
        confidence=_HIGH,
        reasoning=f"{len(proposed)} violation(s) on proposed; {len(conclusions)} introduced",
    )
