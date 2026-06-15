from digital_twin.contracts import ChangeOp, ChangePlan, ChangeScope, Rejection
from digital_twin.scope.object_gate import check_objects


def _plan(ops, site_id="s1", source="mist"):
    return ChangePlan(
        source=source, scope=ChangeScope(org_id="o1", site_id=site_id), ops=tuple(ops)
    )


def _op(object_type="site_setting", object_id="s1", action="update", order=0):
    return ChangeOp(
        action=action, order=order, object_type=object_type, object_id=object_id, payload={}
    )


def test_m1_valid_plan_passes():
    plan = _plan([_op(), _op(object_type="device", object_id="d1", order=1)])
    assert check_objects(plan) is None


def test_unknown_source_rejects():
    r = check_objects(_plan([_op()], source="aruba"))
    assert isinstance(r, Rejection) and r.stage == "object_gate"


def test_template_object_type_rejects_as_fanout():
    r = check_objects(_plan([_op(object_type="networktemplate", object_id="nt1")]))
    assert isinstance(r, Rejection)
    assert any("networktemplate" in reason for reason in r.reasons)


def test_template_modification_rejects_switch_and_gateway():
    # MODIFYING an org template assigned to sites is an org->site fan-out (the
    # inherited layer changes across every assigned site) and is NOT simulated:
    # the update-action passes, but the template object_type is rejected ->
    # UNKNOWN, never silently passed. Covers switch (networktemplate) AND gateway
    # (gatewaytemplate) templates, plus sitetemplate.
    for object_type in ("networktemplate", "gatewaytemplate", "sitetemplate"):
        r = check_objects(
            _plan([_op(object_type=object_type, object_id="t1", action="update")])
        )
        assert isinstance(r, Rejection) and r.stage == "object_gate"
        assert any(object_type in reason for reason in r.reasons)


def test_missing_site_id_rejects_single_site_rule():
    r = check_objects(_plan([_op()], site_id=None))
    assert isinstance(r, Rejection)


def test_site_setting_object_id_must_match_scope_site():
    r = check_objects(_plan([_op(object_id="OTHER-site")]))
    assert isinstance(r, Rejection)


def test_non_update_action_rejects():
    r = check_objects(_plan([_op(action="create")]))
    assert isinstance(r, Rejection)
    assert any("create" in reason for reason in r.reasons)


def test_delete_action_rejects():
    # object-level deletion (e.g. removing a template/device/site) fans out
    # beyond a modeled update and is NOT simulated — it must be rejected
    # pre-fetch (UNKNOWN), never silently passed. Distinct from Mist's
    # attribute-delete ({"-attr": ""}) inside an update, handled downstream.
    r = check_objects(_plan([_op(action="delete")]))
    assert isinstance(r, Rejection) and r.stage == "object_gate"
    assert any("delete" in reason for reason in r.reasons)


def test_delete_action_rejects_even_for_supported_object_type():
    # a delete on a device (a SUPPORTED object_type) is still rejected — the
    # action gate runs before the object_type check
    r = check_objects(_plan([_op(object_type="device", object_id="d1", action="delete")]))
    assert isinstance(r, Rejection)
    assert any("delete" in reason for reason in r.reasons)


def test_all_offending_ops_reported():
    plan = _plan(
        [
            _op(object_type="wlan", object_id="w1", order=0),
            _op(object_type="rftemplate", object_id="r1", order=1),
        ]
    )
    r = check_objects(plan)
    assert isinstance(r, Rejection) and len(r.reasons) == 2
