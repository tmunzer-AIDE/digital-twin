from digital_twin.verdict.decision import Decision
from digital_twin.verdict.org_nac_verdict import NacDelta, OrgNacVerdict


def test_org_nac_verdict_shape():
    v = OrgNacVerdict(decision=Decision.REVIEW, decision_reasons=("x",),
                      changes=(NacDelta("r1", "r", "modified", ("action",)),),
                      check_results=(), adapter_findings=(), rejections=())
    assert v.decision is Decision.REVIEW and v.changes[0].changed_fields == ("action",)
