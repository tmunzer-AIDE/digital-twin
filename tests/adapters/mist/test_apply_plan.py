from digital_twin.adapters.mist.apply import apply_plan
from digital_twin.contracts import ChangeOp, Rejection
from digital_twin.providers.base import RawSiteState
from tests.adapters.mist.fixtures import raw_site

RAW = raw_site()


def _op(object_type, object_id, payload, order=0):
    return ChangeOp(
        action="update", order=order, object_type=object_type, object_id=object_id, payload=payload
    )


def test_single_op_applies():
    out = apply_plan(RAW, (_op("device", "dev-a", {"name": "new-name"}),))
    assert isinstance(out, RawSiteState)
    dev = next(d for d in out.devices if d.get("id") == "dev-a")
    assert dev["name"] == "new-name"


def test_ops_apply_in_order_value_not_list_position():
    ops = (
        _op("device", "dev-a", {"name": "second"}, order=5),
        _op("site_setting", RAW.scope.site_id, {"networks": {}}, order=1),
    )
    out = apply_plan(RAW, ops)
    assert isinstance(out, RawSiteState)
    assert next(d for d in out.devices if d.get("id") == "dev-a")["name"] == "second"
    assert out.setting.get("networks") == {}


def test_unknown_object_id_rejects_with_stage_apply():
    r = apply_plan(RAW, (_op("device", "ghost", {"name": "x"}),))
    assert isinstance(r, Rejection) and r.stage == "apply"
    assert any("ghost" in reason for reason in r.reasons)


def test_duplicate_order_rejects_defense_in_depth():
    ops = (
        _op("device", "dev-a", {}, order=1),
        _op("site_setting", RAW.scope.site_id, {}, order=1),
    )
    assert isinstance(apply_plan(RAW, ops), Rejection)


def test_same_target_twice_rejects():
    ops = (
        _op("device", "dev-a", {"name": "a"}, order=0),
        _op("device", "dev-a", {"name": "b"}, order=1),
    )
    r = apply_plan(RAW, ops)
    assert isinstance(r, Rejection)


def test_original_raw_never_mutated():
    before = next(d for d in RAW.devices if d.get("id") == "dev-a")["name"]
    apply_plan(RAW, (_op("device", "dev-a", {"name": "mutant"}),))
    assert next(d for d in RAW.devices if d.get("id") == "dev-a")["name"] == before
