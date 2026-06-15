import json

from digital_twin.drivers.render import render_human, verdict_to_dict
from digital_twin.ir import IRDiff
from digital_twin.verdict.decision import DecisionInputs
from digital_twin.verdict.verdict import assemble


def _verdict():
    return assemble(
        inputs=DecisionInputs(
            rejections=(), l0_fatal=False, baseline_unavailable=False, check_results=()
        ),
        ir_diff=IRDiff((), (), ()),
        trace_ref="run-1",
    )


def test_verdict_to_dict_is_json_serializable():
    d = verdict_to_dict(_verdict())
    blob = json.dumps(d)  # must not raise
    assert d["decision"] == "safe" and "run-1" in blob


def test_render_human_leads_with_decision():
    text = render_human(_verdict())
    assert text.splitlines()[0].startswith("decision: SAFE")


def test_render_human_names_object_and_path_in_findings():
    # the human output must show WHICH object and WHICH attribute a finding is
    # about — not just code + message
    from digital_twin.contracts import (
        Finding,
        FindingCategory,
        FindingSource,
        ObjectRef,
        Severity,
    )
    from digital_twin.ir import Confidence, ConfidenceLevel

    f = Finding(
        source=FindingSource.ADAPTER,
        category=FindingCategory.OPERATIONAL,
        code="l0.schema.violation",
        severity=Severity.ERROR,
        confidence=Confidence(level=ConfidenceLevel.HIGH),
        message="['1.1.1.1'] is not of type 'string'",
        evidence={"path": "extra_routes.1.2.3.4/32.via"},
        subject=ObjectRef(kind="device", id="dev-x", name="DNT-NTR-SWB-3"),
    )
    v = assemble(
        inputs=DecisionInputs(
            rejections=(), l0_fatal=False, baseline_unavailable=False,
            check_results=(), adapter_findings=(f,),
        ),
        ir_diff=IRDiff((), (), ()),
    )
    text = render_human(v)
    assert "DNT-NTR-SWB-3" in text  # the object name
    assert "extra_routes.1.2.3.4/32.via" in text  # the attribute path


def test_org_verdict_to_dict_shape():
    from digital_twin.drivers.render import org_verdict_to_dict
    from digital_twin.verdict.decision import Decision
    from digital_twin.verdict.org_verdict import OrgVerdict
    ov = OrgVerdict(decision=Decision.UNSAFE, decision_reasons=("site s1: unsafe",),
                    template_id="nt1", per_site={}, driving_sites=("s1",),
                    site_failures={}, template_findings=(), org_rejections=())
    d = org_verdict_to_dict(ov)
    assert d["decision"] == "unsafe" and d["template_id"] == "nt1"
    assert d["driving_sites"] == ["s1"]
