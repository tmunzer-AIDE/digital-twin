from digital_twin.contracts import ChangeOp, ChangePlan, ChangeScope, Rejection


def test_change_plan_constructs():
    plan = ChangePlan(
        source="mist",
        scope=ChangeScope(org_id="o1", site_id="s1"),
        intent="move voice vlan",
        ops=(
            ChangeOp(
                action="update",
                order=0,
                object_type="site_setting",
                object_id="s1",
                payload={"networks": {}},
            ),
        ),
    )
    assert plan.ops[0].object_type == "site_setting"
    assert plan.scope.site_id == "s1"


def test_scope_site_id_is_optional():
    assert ChangeScope(org_id="o1").site_id is None


def test_rejection_carries_stage_and_reasons():
    r = Rejection(stage="object_gate", reasons=("unsupported object_type 'wlan'",))
    assert r.stage == "object_gate"
    assert "wlan" in r.reasons[0]
