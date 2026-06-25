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


def _result(status, findings=(), coverage_state=CoverageState.COMPLETE, confidence="default"):
    if confidence == "default":
        # evaluated results carry HIGH by default in tests; None stays None for
        # non-evaluated statuses (N_A / INSUFFICIENT_DATA / CHECK_ERROR)
        evaluated = status in (Status.PASS, Status.WARN, Status.FAIL)
        confidence = Confidence(level=ConfidenceLevel.HIGH) if evaluated else None
    return CheckResult(
        check_id="c",
        status=status,
        findings=tuple(findings),
        coverage=Coverage(state=coverage_state),
        confidence=confidence,
        reasoning="",
    )


def _inputs(**kw):
    defaults = dict(rejections=(), l0_fatal=False, baseline_unavailable=False, check_results=())
    return DecisionInputs(**{**defaults, **kw})


def test_nonfatal_adapter_error_floors_review_not_safe():
    # the review's P1 repro: an L0 schema violation (operational ERROR) with no
    # check findings must not yield SAFE — Mist would reject this payload
    l0 = _finding(Severity.ERROR, FindingCategory.OPERATIONAL)
    l0 = Finding(**{**l0.__dict__, "source": FindingSource.ADAPTER})
    d, reasons = decide(_inputs(adapter_findings=(l0,)))
    assert d is Decision.REVIEW
    assert any("t" in r for r in reasons)


def test_adapter_operational_error_never_unsafe():
    l0 = _finding(Severity.CRITICAL, FindingCategory.OPERATIONAL)
    d, _ = decide(_inputs(adapter_findings=(l0,)))
    assert d is Decision.REVIEW  # operational never drives UNSAFE


def test_pass_result_with_low_confidence_floors_review():
    res = CheckResult(
        check_id="c",
        status=Status.PASS,
        findings=(),
        coverage=Coverage(state=CoverageState.COMPLETE),
        confidence=Confidence(level=ConfidenceLevel.LOW),
        reasoning="",
    )
    d, reasons = decide(_inputs(check_results=(res,)))
    assert d is Decision.REVIEW
    assert any("confidence" in r for r in reasons)


def test_pass_result_with_missing_confidence_floors_review():
    res = CheckResult(
        check_id="c",
        status=Status.PASS,
        findings=(),
        coverage=Coverage(state=CoverageState.COMPLETE),
        confidence=None,
        reasoning="",
    )
    assert decide(_inputs(check_results=(res,)))[0] is Decision.REVIEW


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
    # a WARNING at LOW is a real (uncertain) conclusion -> REVIEW
    res = _result(Status.WARN, [_finding(Severity.WARNING, level=ConfidenceLevel.LOW)])
    assert decide(_inputs(check_results=(res,)))[0] is Decision.REVIEW


def test_low_confidence_info_context_does_not_floor():
    # INFO findings are pre-existing CONTEXT (delta-untouched by the check
    # layer's contract) — their uncertainty is about the baseline, not the
    # delta, so it must not gate the verdict. The check RESULT confidence
    # still floors REVIEW whenever the delta's conclusion relied on
    # non-HIGH facts (separate rule, unchanged).
    res = _result(Status.PASS, [_finding(Severity.INFO, level=ConfidenceLevel.MEDIUM)])
    d, _ = decide(_inputs(check_results=(res,)))
    assert d is Decision.SAFE


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


def test_unknown_attribute_finding_alone_floors_to_review():
    # a lone WARNING adapter finding (l0.schema.unknown_attribute) floors to REVIEW —
    # never silently SAFE, never UNSAFE (operational, not NETWORK)
    f = Finding(
        source=FindingSource.ADAPTER,
        category=FindingCategory.OPERATIONAL,
        code="l0.schema.unknown_attribute",
        severity=Severity.WARNING,
        confidence=Confidence(level=ConfidenceLevel.HIGH),
        message="attribute 'port_config.ge-0/0/1.disabled' is not documented",
    )
    d, _ = decide(DecisionInputs(
        rejections=(), l0_fatal=False, baseline_unavailable=False,
        check_results=(), adapter_findings=(f,)))
    assert d is Decision.REVIEW
