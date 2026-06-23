from digital_twin.scope.envelope import parse_change_plan
from digital_twin.scope.object_gate import check_objects


def _plan(action="update", payload=None, site_id=None):
    return parse_change_plan({
        "source": "mist",
        "scope": {"org_id": "o1", **({"site_id": site_id} if site_id else {})},
        "ops": [{"action": action, "order": 0, "object_type": "nacrule",
                 "object_id": "r1",
                 "payload": payload if payload is not None else {"name": "x"}}],
    })


def test_nac_update_create_delete_pass():
    for action, payload in (("update", {"name": "x"}), ("create", {"name": "x", "action": "allow"}),
                            ("delete", {})):
        assert check_objects(_plan(action, payload)) is None


def test_nac_delete_with_payload_rejected():
    rej = check_objects(_plan("delete", {"name": "x"}))
    assert rej is not None and any("delete" in r for r in rej.reasons)


def test_nac_bad_action_rejected():
    rej = check_objects(_plan("frobnicate", {"name": "x"}))
    assert rej is not None
