from digital_twin.contracts import ChangePlan, Rejection
from digital_twin.scope.envelope import parse_change_plan

VALID = {
    "source": "mist",
    "scope": {"org_id": "o1", "site_id": "s1"},
    "intent": "why",
    "ops": [
        {
            "action": "update",
            "order": 0,
            "object_type": "site_setting",
            "object_id": "s1",
            "payload": {"networks": {}},
        },
        {
            "action": "update",
            "order": 1,
            "object_type": "device",
            "object_id": "d1",
            "payload": {"name": "sw"},
        },
    ],
}


def test_valid_plan_parses():
    plan = parse_change_plan(VALID)
    assert isinstance(plan, ChangePlan)
    assert [op.order for op in plan.ops] == [0, 1]
    assert plan.intent == "why"


def test_intent_is_optional():
    data = {k: v for k, v in VALID.items() if k != "intent"}
    plan = parse_change_plan(data)
    assert isinstance(plan, ChangePlan) and plan.intent is None


def test_missing_source_rejects():
    r = parse_change_plan({**VALID, "source": ""})
    assert isinstance(r, Rejection) and r.stage == "envelope"


def test_empty_ops_rejects():
    assert isinstance(parse_change_plan({**VALID, "ops": []}), Rejection)


def test_duplicate_order_rejects():
    ops = [dict(VALID["ops"][0]), {**dict(VALID["ops"][1]), "order": 0}]
    r = parse_change_plan({**VALID, "ops": ops})
    assert isinstance(r, Rejection)
    assert any("order" in reason for reason in r.reasons)


def test_two_ops_on_same_object_rejects():
    # full-object replacement: the later op silently kills the earlier one
    op0 = dict(VALID["ops"][0])
    r = parse_change_plan({**VALID, "ops": [op0, {**op0, "order": 5}]})
    assert isinstance(r, Rejection)
    assert any("same object" in reason for reason in r.reasons)


def test_non_dict_payload_rejects():
    bad = {**dict(VALID["ops"][0]), "payload": "oops"}
    assert isinstance(parse_change_plan({**VALID, "ops": [bad]}), Rejection)


def test_all_reasons_collected_not_just_first():
    bad = {"source": "", "scope": {}, "ops": []}
    r = parse_change_plan(bad)
    assert isinstance(r, Rejection) and len(r.reasons) >= 3
