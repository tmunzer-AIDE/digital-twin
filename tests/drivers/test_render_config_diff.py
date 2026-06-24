from digital_twin.contracts import FieldChange, ObjectConfigDiff
from digital_twin.drivers.render import (
    org_nac_verdict_to_dict,
    render_org_nac_human,
)
from digital_twin.verdict.decision import Decision
from digital_twin.verdict.org_nac_verdict import OrgNacVerdict

_CD = ObjectConfigDiff(
    object_type="nacrule", object_id="b", name="b", action="update",
    changes=(
        FieldChange("order", "changed", 2, 0),
        FieldChange("apply_tags", "added", None, ["t"]),
        FieldChange("note", "removed", "old", None),
    ),
)


def _nac_verdict():
    return OrgNacVerdict(Decision.REVIEW, ("r",), (), (), (), (), (_CD,))


def test_org_nac_dict_serializes_config_diffs():
    out = org_nac_verdict_to_dict(_nac_verdict())
    assert out["config_diffs"] == [{
        "object_type": "nacrule", "object_id": "b", "name": "b", "action": "update",
        "changes": [
            {"path": "order", "kind": "changed", "before": 2, "after": 0},
            {"path": "apply_tags", "kind": "added", "before": None, "after": ["t"]},
            {"path": "note", "kind": "removed", "before": "old", "after": None},
        ],
    }]


def test_org_nac_human_renders_config_block():
    human = render_org_nac_human(_nac_verdict())
    assert "config changes:" in human
    assert '  nacrule "b" (update):' in human
    assert "~ order: 2 → 0" in human
    assert "+ apply_tags: ['t']" in human
    assert "- note: 'old'" in human


def test_empty_config_diffs_no_block():
    v = OrgNacVerdict(Decision.SAFE, (), (), (), (), (), ())
    assert "config changes:" not in render_org_nac_human(v)


def test_config_diffs_default_is_empty_tuple():
    # additive/back-compat: omitting config_diffs is valid (6-arg construction)
    v = OrgNacVerdict(Decision.SAFE, (), (), (), (), ())
    assert v.config_diffs == ()


def test_secret_never_appears_in_serialized_output():
    # P3b / spec security bar: pin the SERIALIZER path — a secret assembled into a
    # config diff must not surface in either the JSON dict or the human render.
    import json

    from digital_twin.config_diff import object_config_diff

    cd = object_config_diff(object_type="nacrule", object_id="b", name="b",
                            action="update", before={"psk": "OLDSECRET"},
                            after={"psk": "NEWSECRET"})
    v = OrgNacVerdict(Decision.REVIEW, ("r",), (), (), (), (), (cd,))
    blob = json.dumps(org_nac_verdict_to_dict(v)) + render_org_nac_human(v)
    assert "OLDSECRET" not in blob and "NEWSECRET" not in blob
    assert "‹redacted›" in blob


def test_org_verdict_dict_and_human_render_config_diffs():
    # P3: pin the manually-wired ORG renderers (a missed key/block would slip past
    # the NAC-only tests above).
    from digital_twin.drivers.render import org_verdict_to_dict, render_org_human
    from digital_twin.verdict.org_verdict import OrgVerdict

    cd = ObjectConfigDiff(
        object_type="sitetemplate", object_id="st1", name="st1", action="update",
        changes=(FieldChange("port_usages.trunkB.networks", "changed", ["corp"], []),),
    )
    ov = OrgVerdict(
        decision=Decision.REVIEW, decision_reasons=("r",), changes=(),
        per_site={}, driving_sites=(), site_failures={},
        template_findings=(), org_rejections=(), config_diffs=(cd,),
    )
    out = org_verdict_to_dict(ov)
    assert out["config_diffs"] == [{
        "object_type": "sitetemplate", "object_id": "st1", "name": "st1", "action": "update",
        "changes": [{"path": "port_usages.trunkB.networks", "kind": "changed",
                     "before": ["corp"], "after": []}],
    }]
    human = render_org_human(ov)
    assert "config changes:" in human
    assert '  sitetemplate "st1" (update):' in human
