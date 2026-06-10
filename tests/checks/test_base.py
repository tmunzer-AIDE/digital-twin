from digital_twin.checks.base import CheckResult, Coverage, CoverageState, Status


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
