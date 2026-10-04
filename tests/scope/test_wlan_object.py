from datetime import UTC, datetime

from digital_twin.adapters.mist.apply import apply_plan
from digital_twin.adapters.mist.apply.objects import (
    delete_object,
    effective_update,
    get_object,
    replace_object,
)
from digital_twin.contracts import ChangeOp, ChangePlan, ChangeScope, Rejection
from digital_twin.providers.base import RawSiteState, SiteScope, StateMeta
from digital_twin.scope.field_gate import screen_op
from digital_twin.scope.object_gate import check_objects
from digital_twin.scope.wlan_policy import classify_wlan_delta, usage_gated_paths_for_update

_SITE = {"id": "w1", "ssid": "corp", "enabled": True, "for_site": True, "isolation": False}
_INHERITED = {"id": "w2", "ssid": "guest", "enabled": True, "for_site": False, "template_id": "t1"}


def _raw() -> RawSiteState:
    return RawSiteState(
        scope=SiteScope(org_id="o1", site_id="s1"), site={"id": "s1"}, setting={},
        networktemplate=None, devices=(), device_stats=(), port_stats=(),
        wireless_clients=(), wired_clients=(), derived_setting=None, wlans=(_SITE, _INHERITED),
        meta=StateMeta(acquired_at=datetime.now(UTC), host="t", fetched=(), failures=()),
    )


def _op(object_id, payload):
    return ChangeOp(action="update", order=0, object_type="wlan",
                    object_id=object_id, payload=payload)


def _delete_op(object_id):
    return ChangeOp(action="delete", order=0, object_type="wlan",
                    object_id=object_id, payload={})


def test_object_gate_accepts_wlan():
    plan = ChangePlan(source="mist", scope=ChangeScope(org_id="o1", site_id="s1"),
                      ops=(_op("w1", {"isolation": True}),))
    assert check_objects(plan) is None


def test_get_and_replace_target_raw_wlans_by_id():
    raw = _raw()
    assert get_object(raw, "wlan", "w1")["ssid"] == "corp"
    out = replace_object(raw, "wlan", "w1", {"isolation": True})
    assert next(w for w in out.wlans if w["id"] == "w1")["isolation"] is True
    assert out.devices == ()  # device branch not taken (no fall-through)


def test_delete_object_removes_raw_wlan_by_id_only():
    out = delete_object(_raw(), "wlan", "w1")
    assert tuple(w["id"] for w in out.wlans) == ("w2",)
    assert out.devices == ()  # device branch not taken (no fall-through)


def test_apply_plan_dispatches_wlan_delete():
    out = apply_plan(_raw(), (_delete_op("w1"),))
    assert not isinstance(out, Rejection)
    assert tuple(w["id"] for w in out.wlans) == ("w2",)


def test_apply_plan_rejects_unsupported_delete_without_crashing():
    out = apply_plan(
        _raw(),
        (ChangeOp(action="delete", order=0, object_type="site_setting",
                  object_id="s1", payload={}),),
    )
    assert isinstance(out, Rejection)
    assert "delete is not supported" in out.reasons[0]


def test_field_gate_admits_usage_gated_wlan_leaf_but_still_denies_output_only_leaf():
    # the engine passes the EFFECTIVE object (effective_update) to screen_op, not the
    # partial payload — a partial dict would read every other root as a deletion.
    assert screen_op("wlan", _SITE, effective_update(_SITE, {"isolation": True})) is None
    hidden = effective_update(_SITE, {"hide_ssid": True})
    assert screen_op("wlan", _SITE, hidden) is None
    assert classify_wlan_delta(_SITE, hidden).usage_gated == ("hide_ssid",)
    generated = effective_update(_SITE, {"portal_api_secret": "server-owned"})
    assert isinstance(screen_op("wlan", _SITE, generated), Rejection)


def test_inherited_wlan_op_rejected_post_fetch():
    r = screen_op("wlan", _INHERITED, effective_update(_INHERITED, {"isolation": True}))
    assert isinstance(r, Rejection) and any("inherited" in x for x in r.reasons)


def test_org_wlan_screening_bypasses_site_ownership_check():
    r = screen_op(
        "wlan",
        _INHERITED,
        effective_update(_INHERITED, {"isolation": True}),
        enforce_wlan_site_ownership=False,
    )
    assert r is None


def test_org_wlan_assignment_edit_remains_out_of_scope():
    r = screen_op(
        "wlan",
        _INHERITED,
        effective_update(_INHERITED, {"site_ids": ["s2"]}),
        enforce_wlan_site_ownership=False,
    )
    assert isinstance(r, Rejection)
    assert any("site_ids" in reason for reason in r.reasons)


def test_secure_to_open_auth_root_replace_ignores_removed_secret_companion():
    # The modeled secure -> open transition necessarily replaces the auth root and
    # drops its secret companion. The transition rule owns that consequence.
    psk = {"id": "w1", "ssid": "corp", "enabled": True, "for_site": True,
           "isolation": False, "auth": {"type": "psk", "psk": "secret"}}
    r = screen_op("wlan", psk, effective_update(psk, {"auth": {"type": "open"}}))
    assert r is None


def test_auth_secret_change_is_admitted_but_owned_by_usage_gate():
    psk = {"id": "w1", "ssid": "corp", "enabled": True, "for_site": True,
           "isolation": False, "auth": {"type": "psk", "psk": "old"}}
    proposed = effective_update(psk, {"auth": {"type": "psk", "psk": "new"}})
    assert screen_op("wlan", psk, proposed) is None
    assert usage_gated_paths_for_update(psk, proposed) == ("auth.psk",)
