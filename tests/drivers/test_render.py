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
