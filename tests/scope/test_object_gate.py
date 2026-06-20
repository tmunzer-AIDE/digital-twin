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
            _op(object_type="wxtag", object_id="x1", order=0),
            _op(object_type="rftemplate", object_id="r1", order=1),
        ]
    )
    r = check_objects(plan)
    assert isinstance(r, Rejection) and len(r.reasons) == 2


def _nt_op(object_id="nt1", action="update"):
    return ChangeOp(action=action, order=0, object_type="networktemplate",
                    object_id=object_id, payload={})


def _org_plan(ops, org_id="o1", site_id=None):
    return ChangePlan(source="mist", scope=ChangeScope(org_id=org_id, site_id=site_id),
                      ops=tuple(ops))


def test_org_mode_template_plan_passes():
    # ORG mode triggers ONLY when ALL ops are networktemplate AND site_id absent
    assert check_objects(_org_plan([_nt_op()])) is None


def test_networktemplate_with_site_id_is_out_of_scope_single_site():
    # site_id present -> NOT org mode -> SITE logic -> the EXISTING
    # "unsupported object_type" rejection (preserves test_template_object_type_*)
    r = check_objects(_org_plan([_nt_op()], site_id="s1"))
    assert isinstance(r, Rejection)
    assert any("networktemplate" in reason for reason in r.reasons)


def _del(object_type, object_id, order=0, payload=None):
    return ChangeOp(action="delete", order=order, object_type=object_type,
                    object_id=object_id, payload=payload if payload is not None else {})


def test_org_mode_delete_allowed():
    # a single networktemplate DELETE (empty payload, no site_id) is now ORG-mode
    # and accepted — the delete-ripple engine handles it (no false-SAFE window).
    assert check_objects(_org_plan([_del("networktemplate", "nt1")])) is None


def test_org_mode_multiple_distinct_ops_allowed():
    # multiple DISTINCT org ops (two different networktemplates) now fan out as a
    # multi-op plan — the single-template-per-plan rule is gone.
    assert check_objects(_org_plan([
        _nt_op(object_id="ntA"),
        ChangeOp(action="update", order=1, object_type="networktemplate",
                 object_id="ntB", payload={}),
    ])) is None


def test_org_mode_delete_with_nonempty_payload_rejected():
    r = check_objects(_org_plan([_del("networktemplate", "nt1", payload={"networks": {}})]))
    assert isinstance(r, Rejection) and r.stage == "object_gate"
    assert any("delete payload must be empty" in reason for reason in r.reasons)


def test_org_mode_mixed_delete_and_update_allowed():
    # delete one template + update another (distinct ids) is a valid multi-op plan.
    assert check_objects(_org_plan([
        _del("networktemplate", "ntA", order=0),
        ChangeOp(action="update", order=1, object_type="gatewaytemplate",
                 object_id="gtB", payload={"port_config": {}}),
    ])) is None


def test_site_delete_still_rejected():
    # a site_setting delete is SITE-mode (site_id present) -> still rejected.
    r = check_objects(_plan([_op(action="delete")]))
    assert isinstance(r, Rejection) and r.stage == "object_gate"
    assert any("delete" in reason for reason in r.reasons)


def test_device_delete_still_rejected():
    # a device delete is SITE-mode -> still rejected (unsupported action).
    r = check_objects(_plan([_op(object_type="device", object_id="d1", action="delete")]))
    assert isinstance(r, Rejection) and r.stage == "object_gate"
    assert any("delete" in reason for reason in r.reasons)


def test_mixing_site_and_org_object_types_rejects():
    r = check_objects(
        _org_plan([_nt_op(), _op(object_type="device", object_id="d1")], site_id=None)
    )
    assert isinstance(r, Rejection)
    # SITE branch surfaces BOTH the unsupported networktemplate op AND site_id-required
    assert any("networktemplate" in reason for reason in r.reasons)
    assert any("site_id" in reason for reason in r.reasons)


def _gt_op(object_type, object_id="t1", action="update"):
    return ChangeOp(action=action, order=0, object_type=object_type,
                    object_id=object_id, payload={})


def test_gatewaytemplate_plan_classified_org():
    # A single-op gatewaytemplate plan with no site_id must classify as ORG-mode
    # (no rejection) — just like networktemplate.
    assert check_objects(_org_plan([_gt_op("gatewaytemplate")])) is None


def test_sitetemplate_plan_classified_org():
    # A single-op sitetemplate plan with no site_id must classify as ORG-mode.
    assert check_objects(_org_plan([_gt_op("sitetemplate")])) is None


def test_org_mode_mixed_types_same_id_now_allowed():
    """The multi-op delete-ripple engine simulates EVERY op (not just ops[0]), so two
    DISTINCT org ops sharing an object_id but with different object_types
    (gatewaytemplate + sitetemplate, both id="same") are now a valid multi-op plan.
    They are distinct objects; object_gate no longer enforces one-template-per-plan,
    and the envelope's duplicate-(type,id) guard does not fire (types differ)."""
    assert check_objects(_org_plan([
        _gt_op("gatewaytemplate", object_id="same"),
        ChangeOp(action="update", order=1, object_type="sitetemplate",
                 object_id="same", payload={}),
    ])) is None


def test_org_mode_multiple_gatewaytemplate_ids_now_allowed():
    # Multiple distinct gatewaytemplate ops fan out as a multi-op plan (no longer
    # the single-template-per-plan rejection).
    assert check_objects(_org_plan([
        _gt_op("gatewaytemplate", object_id="gtA"),
        ChangeOp(action="update", order=1, object_type="gatewaytemplate",
                 object_id="gtB", payload={}),
    ])) is None
