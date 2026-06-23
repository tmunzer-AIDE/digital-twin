from digital_twin.drivers.cli import _is_org_nac_plan
from digital_twin.drivers.render import org_nac_verdict_to_dict, render_org_nac_human
from digital_twin.verdict.decision import Decision
from digital_twin.verdict.org_nac_verdict import NacDelta, OrgNacVerdict


def test_is_org_nac_plan_true_for_no_site_nacrule():
    assert _is_org_nac_plan({"scope": {"org_id": "o1"},
                             "ops": [{"object_type": "nacrule", "object_id": "r1"}]}) is True


def test_is_org_nac_plan_false_with_site_or_other_type():
    assert _is_org_nac_plan({"scope": {"org_id": "o", "site_id": "s"},
                             "ops": [{"object_type": "nacrule", "object_id": "r"}]}) is False
    assert _is_org_nac_plan({"scope": {"org_id": "o"},
                             "ops": [{"object_type": "device", "object_id": "d"}]}) is False


def test_render_round_trip():
    v = OrgNacVerdict(Decision.REVIEW, ("r",),
                      (NacDelta("r1", "rule", "modified", ("action",)),), (), (), ())
    d = org_nac_verdict_to_dict(v)
    assert d["decision"] == "review" and d["changes"][0]["rule_id"] == "r1"
    assert "review" in render_org_nac_human(v).lower()
