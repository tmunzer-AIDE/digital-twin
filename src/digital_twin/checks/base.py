"""The check-plugin contract (spec): Status vs Severity are distinct vocabularies.

A check receives ONLY the two AnalysisContexts + the neutral IRDiff — never raw
vendor payload. Severity is assigned in checks and nowhere else. A check emits
FAIL only at HIGH confidence (otherwise it degrades to WARN/INSUFFICIENT_DATA);
that invariant is what keeps FAIL -> UNSAFE an always-confident assertion.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from digital_twin.analysis.context import AnalysisContext
from digital_twin.contracts import Finding, Severity
from digital_twin.ir import Capability, Confidence, IRDiff


class Status(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"
    INSUFFICIENT_DATA = "insufficient_data"
    CHECK_ERROR = "check_error"


class CoverageState(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class Coverage:
    state: CoverageState
    notes: tuple[str, ...] = ()  # e.g. ("AP vlan membership is observation-based",)


@dataclass(frozen=True)
class CheckContext:
    baseline: AnalysisContext
    proposed: AnalysisContext
    diff: IRDiff


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    status: Status
    findings: tuple[Finding, ...]
    coverage: Coverage
    confidence: Confidence | None  # None when nothing was evaluated (N_A / error)
    reasoning: str


class Check(Protocol):
    id: str
    title: str
    domain: str  # groups in the verdict, e.g. "wired.l2"
    default_severity: Severity

    def requires(self) -> frozenset[Capability]: ...

    def applies_to(self, diff: IRDiff) -> bool: ...

    def run(self, ctx: CheckContext) -> CheckResult: ...
