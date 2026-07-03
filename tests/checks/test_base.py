from digital_twin.checks.base import (
    CheckResult,
    Coverage,
    CoverageState,
    Status,
    status_from_findings,
)
from digital_twin.contracts import Finding, FindingCategory, FindingSource, Severity
from digital_twin.ir import Confidence, ConfidenceLevel


def _finding(severity: Severity) -> Finding:
    return Finding(
        source=FindingSource.CHECK,
        category=FindingCategory.NETWORK,
        code="test.rollup",
        severity=severity,
        confidence=Confidence(level=ConfidenceLevel.HIGH),
        message="test finding",
    )


def test_status_vocabulary_matches_spec():
    assert {s.value for s in Status} == {
        "pass",
        "warn",
        "fail",
        "not_applicable",
        "insufficient_data",
        "check_error",
    }


def test_coverage_states():
    assert {s.value for s in CoverageState} == {
        "complete",
        "partial",
        "insufficient",
        "not_applicable",
    }


def test_check_result_constructs():
    r = CheckResult(
        check_id="wired.l2.loop",
        status=Status.PASS,
        findings=(),
        coverage=Coverage(state=CoverageState.COMPLETE),
        confidence=None,
        reasoning="no cycles found",
    )
    assert r.status is Status.PASS and r.coverage.notes == ()


def test_rollup_empty_is_pass():
    assert status_from_findings([]) is Status.PASS


def test_rollup_info_only_is_pass():
    # INFO is context, never a conclusion: it must not floor the status.
    assert status_from_findings([_finding(Severity.INFO)]) is Status.PASS


def test_rollup_warning_is_warn():
    assert status_from_findings([_finding(Severity.WARNING)]) is Status.WARN


def test_rollup_error_is_fail():
    assert status_from_findings([_finding(Severity.ERROR)]) is Status.FAIL


def test_rollup_critical_is_fail():
    # CRITICAL is at least as severe as ERROR — mapping it lower would
    # risk a false SAFE.
    assert status_from_findings([_finding(Severity.CRITICAL)]) is Status.FAIL


def test_rollup_mixed_is_fail():
    findings = [_finding(Severity.WARNING), _finding(Severity.ERROR)]
    assert status_from_findings(findings) is Status.FAIL
    assert status_from_findings(list(reversed(findings))) is Status.FAIL


def test_rollup_info_excluded_even_alongside_conclusions():
    assert (
        status_from_findings([_finding(Severity.INFO), _finding(Severity.WARNING)])
        is Status.WARN
    )
    assert (
        status_from_findings([_finding(Severity.ERROR), _finding(Severity.INFO)])
        is Status.FAIL
    )


def test_rollup_custom_exclude():
    # An emptied exclusion set makes INFO count as a WARN-level conclusion.
    assert (
        status_from_findings([_finding(Severity.INFO)], exclude=frozenset())
        is Status.WARN
    )
