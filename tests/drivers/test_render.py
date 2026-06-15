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
