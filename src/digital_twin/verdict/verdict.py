"""Verdict assembly: one document, three independent axes (findings, coverage,
confidence) + the single agent-facing decision. Two finding sources (adapter L0
+ checks) flatten into ONE list; check_results stay as the per-check audit."""

from __future__ import annotations

from dataclasses import dataclass

from digital_twin.checks.base import CheckResult
from digital_twin.contracts import Diagram, Finding, ObjectConfigDiff, Severity
from digital_twin.ir import IRDiff

from .confidence_summary import ConfidenceSummary, summarize
from .coverage import DomainCoverage, rollup
from .decision import Decision, DecisionInputs, decide
from .state_meta import StateMetaView

_SEVERITY_ORDER = [Severity.INFO, Severity.WARNING, Severity.ERROR, Severity.CRITICAL]


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    decision_reasons: tuple[str, ...]
    overall_severity: Severity | None  # None when there are no findings
    findings: tuple[Finding, ...]
    check_results: tuple[CheckResult, ...]
    coverage: dict[str, DomainCoverage]
    confidence_summary: ConfidenceSummary
    ir_diff: IRDiff
    state_meta: StateMetaView | None = None  # freshness (None pre-fetch)
    trace_ref: str | None = None  # run id of the trace record
    diagrams: tuple[Diagram, ...] = ()  # topology charts (mermaid); () when no proposed IR
    config_diffs: tuple[ObjectConfigDiff, ...] = ()  # raw before→after (non-load-bearing)


def assemble(
    *,
    inputs: DecisionInputs,
    ir_diff: IRDiff,
    domains: dict[str, str] | None = None,
    state_meta: StateMetaView | None = None,
    trace_ref: str | None = None,
) -> Verdict:
    # adapter findings live INSIDE DecisionInputs so they reach decide() —
    # the flat verdict list and the decision can never disagree on inputs
    decision, reasons = decide(inputs)
    findings = (*inputs.adapter_findings, *(f for r in inputs.check_results for f in r.findings))
    overall = max((f.severity for f in findings), key=_SEVERITY_ORDER.index) if findings else None
    check_domains = domains or {
        r.check_id: r.check_id.rsplit(".", 1)[0] for r in inputs.check_results
    }
    return Verdict(
        decision=decision,
        decision_reasons=reasons,
        overall_severity=overall,
        findings=findings,
        check_results=inputs.check_results,
        coverage=rollup(inputs.check_results, check_domains),
        confidence_summary=summarize(findings),
        ir_diff=ir_diff,
        state_meta=state_meta,
        trace_ref=trace_ref,
    )
