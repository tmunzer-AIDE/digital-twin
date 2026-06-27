"""The agent-facing decision: SAFE | REVIEW | UNSAFE | UNKNOWN.

Deterministic precedence (first match wins): hard-UNKNOWN > UNSAFE >
coverage-gap UNKNOWN > REVIEW > SAFE.
Key invariant (spec): a blind spot — INSUFFICIENT_DATA, partial coverage,
non-HIGH confidence, or a crashed check — can NEVER resolve to SAFE; it floors
at REVIEW. Operational findings never drive UNSAFE.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from digital_twin.checks.base import CheckResult, CoverageState, Status
from digital_twin.contracts import Finding, FindingCategory, Rejection, Severity
from digital_twin.ir import ConfidenceLevel


class Decision(StrEnum):
    SAFE = "safe"
    REVIEW = "review"
    UNSAFE = "unsafe"
    UNKNOWN = "unknown"


# Statuses where the check actually evaluated and concluded something — these
# must carry HIGH result-confidence for SAFE (INSUFFICIENT_DATA/CHECK_ERROR are
# already REVIEW via the status rule; NOT_APPLICABLE evaluated nothing).
_EVALUATED = (Status.PASS, Status.WARN, Status.FAIL)


@dataclass(frozen=True)
class DecisionInputs:
    rejections: tuple[Rejection, ...]  # gates/apply (any -> UNKNOWN)
    l0_fatal: bool  # structurally-fatal L0 short-circuit
    baseline_unavailable: bool  # FetchError / ingest not ok
    check_results: tuple[CheckResult, ...]
    adapter_findings: tuple[Finding, ...] = ()  # L0 (non-fatal) — must reach the decision
    coverage_gaps: tuple[Rejection, ...] = ()


def decide(inputs: DecisionInputs) -> tuple[Decision, tuple[str, ...]]:
    # 1) hard-UNKNOWN — could not simulate
    unknown: list[str] = []
    for r in inputs.rejections:
        unknown.extend(f"UNSUPPORTED [{r.stage}]: {reason}" for reason in r.reasons)
    if inputs.l0_fatal:
        unknown.append("structurally-fatal L0 violation short-circuited the run")
    if inputs.baseline_unavailable:
        unknown.append("no usable baseline state (fetch/ingest failed)")
    if unknown:
        return Decision.UNKNOWN, tuple(unknown)

    # BOTH sources reach the decision (adapter L0 + checks) — same Finding model
    findings = [
        *inputs.adapter_findings,
        *(f for res in inputs.check_results for f in res.findings),
    ]

    # 2) UNSAFE — confident network breakage only
    unsafe = [
        f"{f.code}: {f.message}"
        for f in findings
        if f.category is FindingCategory.NETWORK
        and f.severity in (Severity.ERROR, Severity.CRITICAL)
    ]
    if unsafe:
        return Decision.UNSAFE, tuple(unsafe)

    # 3) coverage-gap UNKNOWN — valid simulation with partial coverage
    gap_reasons = [
        f"COVERAGE GAP [{r.stage}]: {reason}"
        for r in inputs.coverage_gaps
        for reason in r.reasons
    ]
    if gap_reasons:
        return Decision.UNKNOWN, tuple(gap_reasons)

    # 4) REVIEW — any warning or blind spot
    review: list[str] = []
    review.extend(f"{f.code}: {f.message}" for f in findings if f.severity is Severity.WARNING)
    review.extend(
        # operational ERROR/CRITICAL (e.g. an L0 schema violation Mist would
        # reject): never UNSAFE, but never silently SAFE either
        f"{f.code}: {f.message}"
        for f in findings
        if f.category is FindingCategory.OPERATIONAL
        and f.severity in (Severity.ERROR, Severity.CRITICAL)
    )
    review.extend(
        f"{res.check_id}: {res.status}"
        for res in inputs.check_results
        if res.status in (Status.INSUFFICIENT_DATA, Status.CHECK_ERROR)
    )
    review.extend(
        # INFO findings are pre-existing CONTEXT (the check layer emits them
        # only for delta-untouched conditions): their uncertainty is about the
        # baseline, not the delta — the result-confidence rule below still
        # floors any conclusion that actually relied on non-HIGH facts
        f"{f.code}: confidence {f.confidence.level.name}"
        for f in findings
        if f.confidence.level is not ConfidenceLevel.HIGH and f.severity is not Severity.INFO
    )
    review.extend(
        # an evaluated result below HIGH (or missing) confidence is a blind spot
        f"{res.check_id}: result confidence "
        f"{res.confidence.level.name if res.confidence else 'absent'}"
        for res in inputs.check_results
        if res.status in _EVALUATED
        and (res.confidence is None or res.confidence.level is not ConfidenceLevel.HIGH)
    )
    review.extend(
        f"{res.check_id}: coverage {res.coverage.state}"
        for res in inputs.check_results
        if res.coverage.state in (CoverageState.PARTIAL, CoverageState.INSUFFICIENT)
    )
    if review:
        return Decision.REVIEW, tuple(review)

    # 5) SAFE — evaluated, covered, high confidence, clean
    evaluated = [r.check_id for r in inputs.check_results if r.status is not Status.NOT_APPLICABLE]
    return Decision.SAFE, (
        f"all applicable checks passed ({', '.join(evaluated) or 'none applicable'}); "
        "coverage complete; confidence HIGH",
    )
