"""The decision table (spec): UNKNOWN > UNSAFE > REVIEW > SAFE, first match wins.
A blind spot can NEVER resolve to SAFE."""

from digital_twin.checks.base import CheckResult, Coverage, CoverageState, Status
from digital_twin.contracts import (
    Finding,
    FindingCategory,
    FindingSource,
    Rejection,
    Severity,
)
from digital_twin.ir import Confidence, ConfidenceLevel
from digital_twin.verdict.decision import Decision, DecisionInputs, decide


def _finding(severity, category=FindingCategory.NETWORK, level=ConfidenceLevel.HIGH):
    return Finding(
        source=FindingSource.CHECK,
        category=category,
        code="t",
        severity=severity,
        confidence=Confidence(level=level),
        message="m",
    )


def _result(status, findings=(), coverage_state=CoverageState.COMPLETE):
    return CheckResult(
        check_id="c",
        status=status,
        findings=tuple(findings),
        coverage=Coverage(state=coverage_state),
        confidence=None,
        reasoning="",
    )


def _inputs(**kw):
    defaults = dict(rejections=(), l0_fatal=False, baseline_unavailable=False, check_results=())
    return DecisionInputs(**{**defaults, **kw})


def test_rejection_is_unknown():
    d, reasons = decide(_inputs(rejections=(Rejection(stage="object_gate", reasons=("x",)),)))
    assert d is Decision.UNKNOWN and "object_gate" in reasons[0]


def test_no_baseline_is_unknown():
    d, _ = decide(_inputs(baseline_unavailable=True))
    assert d is Decision.UNKNOWN


def test_network_error_finding_is_unsafe():
    res = _result(Status.FAIL, [_finding(Severity.ERROR)])
    d, _ = decide(_inputs(check_results=(res,)))
    assert d is Decision.UNSAFE


def test_operational_error_finding_is_not_unsafe():
    res = _result(Status.CHECK_ERROR, [_finding(Severity.ERROR, FindingCategory.OPERATIONAL)])
    d, _ = decide(_inputs(check_results=(res,)))
    assert d is Decision.REVIEW  # crash floors at REVIEW, never UNSAFE


def test_warning_finding_is_review():
    res = _result(Status.WARN, [_finding(Severity.WARNING)])
    assert decide(_inputs(check_results=(res,)))[0] is Decision.REVIEW


def test_insufficient_data_is_review():
    res = _result(Status.INSUFFICIENT_DATA, coverage_state=CoverageState.INSUFFICIENT)
    assert decide(_inputs(check_results=(res,)))[0] is Decision.REVIEW


def test_low_confidence_finding_floors_review():
    res = _result(Status.PASS, [_finding(Severity.INFO, level=ConfidenceLevel.LOW)])
    assert decide(_inputs(check_results=(res,)))[0] is Decision.REVIEW


def test_partial_coverage_floors_review():
    res = _result(Status.PASS, coverage_state=CoverageState.PARTIAL)
    assert decide(_inputs(check_results=(res,)))[0] is Decision.REVIEW


def test_all_clean_is_safe():
    res = _result(Status.PASS, [_finding(Severity.INFO)])
    na = _result(Status.NOT_APPLICABLE, coverage_state=CoverageState.NOT_APPLICABLE)
    d, reasons = decide(_inputs(check_results=(res, na)))
    assert d is Decision.SAFE and reasons


def test_precedence_unknown_beats_unsafe():
    res = _result(Status.FAIL, [_finding(Severity.ERROR)])
    d, _ = decide(
        _inputs(rejections=(Rejection(stage="envelope", reasons=("bad",)),), check_results=(res,))
    )
    assert d is Decision.UNKNOWN
