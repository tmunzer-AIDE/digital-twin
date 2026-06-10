from digital_twin.checks.base import CheckResult, Coverage, CoverageState, Status
from digital_twin.contracts import Finding, FindingCategory, FindingSource, Severity
from digital_twin.ir import Confidence, ConfidenceLevel, IRDiff
from digital_twin.verdict.decision import Decision, DecisionInputs
from digital_twin.verdict.verdict import assemble


def test_assemble_flattens_findings_and_rolls_up():
    f = Finding(
        source=FindingSource.CHECK,
        category=FindingCategory.NETWORK,
        code="x",
        severity=Severity.WARNING,
        confidence=Confidence(level=ConfidenceLevel.LOW, reasons=("one-sided",)),
        message="m",
    )
    res = CheckResult(
        check_id="wired.l2.loop",
        status=Status.WARN,
        findings=(f,),
        coverage=Coverage(state=CoverageState.COMPLETE),
        confidence=Confidence(level=ConfidenceLevel.LOW),
        reasoning="",
    )
    l0 = Finding(
        source=FindingSource.ADAPTER,
        category=FindingCategory.OPERATIONAL,
        code="l0.schema.violation",
        severity=Severity.ERROR,
        confidence=Confidence(level=ConfidenceLevel.HIGH),
        message="bad type",
    )
    verdict = assemble(
        inputs=DecisionInputs(
            rejections=(),
            l0_fatal=False,
            baseline_unavailable=False,
            check_results=(res,),
            adapter_findings=(l0,),
        ),
        ir_diff=IRDiff((), (), ()),
    )
    assert verdict.decision is Decision.REVIEW
    assert {x.code for x in verdict.findings} == {"x", "l0.schema.violation"}
    assert verdict.overall_severity is Severity.ERROR
    assert verdict.confidence_summary.low == 1 and verdict.confidence_summary.high == 1
    assert verdict.coverage["wired.l2"].complete == 1
    # the adapter finding INFLUENCED the decision (was: merged after deciding)
    assert any("l0.schema.violation" in r for r in verdict.decision_reasons)
