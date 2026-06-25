"""OrgVerdict rollup: worst-of per-site by precedence + template_findings floor."""

from digital_twin.contracts import Finding, FindingCategory, FindingSource, Rejection, Severity
from digital_twin.ir import Confidence, ConfidenceLevel
from digital_twin.verdict.decision import Decision
from digital_twin.verdict.org_verdict import decide_org


def _verdict(decision):
    from digital_twin.ir import IRDiff
    from digital_twin.verdict.confidence_summary import summarize
    from digital_twin.verdict.verdict import Verdict
    return Verdict(
        decision=decision, decision_reasons=(), overall_severity=None, findings=(),
        check_results=(), coverage={}, confidence_summary=summarize(()),
        ir_diff=IRDiff((), (), ()),
    )


def _op_finding():
    return Finding(
        source=FindingSource.ADAPTER, category=FindingCategory.OPERATIONAL,
        code="l0.schema.x", severity=Severity.ERROR,
        confidence=Confidence(level=ConfidenceLevel.HIGH), message="schema",
    )


def test_rollup_is_worst_of_sites():
    per = {
        "s1": _verdict(Decision.SAFE),
        "s2": _verdict(Decision.UNSAFE),
        "s3": _verdict(Decision.REVIEW),
    }
    decision, reasons, driving = decide_org(per, template_findings=(), org_rejections=())
    assert decision is Decision.UNSAFE
    assert driving == ("s2",)


def test_unknown_site_wins():
    per = {"s1": _verdict(Decision.UNSAFE), "s2": _verdict(Decision.UNKNOWN)}
    decision, _r, driving = decide_org(per, template_findings=(), org_rejections=())
    assert decision is Decision.UNKNOWN and driving == ("s2",)


def test_template_findings_floor_review():
    per = {"s1": _verdict(Decision.SAFE)}
    decision, _r, driving = decide_org(per, template_findings=(_op_finding(),), org_rejections=())
    assert decision is Decision.REVIEW and driving == ()  # driven by the template, not a site


def test_zero_sites_is_safe():
    decision, reasons, driving = decide_org({}, template_findings=(), org_rejections=())
    assert decision is Decision.SAFE
    assert any("no sites" in r for r in reasons)


def test_zero_sites_with_template_finding_is_review():
    # a non-fatal template L0 floors REVIEW even with zero assigned sites
    decision, _r, _d = decide_org({}, template_findings=(_op_finding(),), org_rejections=())
    assert decision is Decision.REVIEW


def test_org_rejections_short_circuit_unknown():
    # a short-circuit cause (e.g. fatal L0 / field-gate / lookup) -> UNKNOWN,
    # even with otherwise-SAFE sites; reasons carry the rejection stage
    r = Rejection(stage="l0", reasons=("structurally-fatal L0 on the proposed template",))
    decision, reasons, driving = decide_org(
        {"s1": _verdict(Decision.SAFE)}, template_findings=(), org_rejections=(r,)
    )
    assert decision is Decision.UNKNOWN and driving == ()
    assert any("l0" in reason for reason in reasons)


def test_decide_org_floors_warning_template_finding():
    # a WARNING template finding (e.g. l0.schema.unknown_attribute that did not also
    # trip the field gate) floors the rollup to REVIEW — not SAFE (matches decide())
    wf = Finding(
        source=FindingSource.ADAPTER,
        category=FindingCategory.OPERATIONAL,
        code="l0.schema.unknown_attribute",
        severity=Severity.WARNING,
        confidence=Confidence(level=ConfidenceLevel.HIGH),
        message="attribute 'x.y' is not documented",
    )
    decision, _, _ = decide_org({}, template_findings=(wf,), org_rejections=())
    assert decision is Decision.REVIEW
