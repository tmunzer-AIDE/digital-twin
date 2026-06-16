"""Template apply + the baseline-snapshot override rule (guardrail #3)."""

from datetime import UTC, datetime

from digital_twin.contracts import Rejection
from digital_twin.engine.org_template import apply_template, override_template
from digital_twin.providers.base import RawSiteState, SiteScope, StateMeta


def _raw(nt):
    return RawSiteState(
        scope=SiteScope("o1", "s1"), site={"id": "s1", "networktemplate_id": "nt1"},
        setting={"id": "s1"}, networktemplate=nt, devices=(), device_stats=(),
        port_stats=(), wireless_clients=(), wired_clients=(), derived_setting=None,
        meta=StateMeta(acquired_at=datetime.now(UTC), host="h", fetched=("site",), failures=()),
    )


def _raw_gt():
    """RawSiteState with both sitetemplate and gatewaytemplate set."""
    return RawSiteState(
        scope=SiteScope("o1", "s1"), site={"id": "s1"},
        setting={"id": "s1"}, networktemplate=None, devices=(), device_stats=(),
        port_stats=(), wireless_clients=(), wired_clients=(), derived_setting=None,
        meta=StateMeta(acquired_at=datetime.now(UTC), host="h", fetched=("site",), failures=()),
        sitetemplate={"networks": {}},
        gatewaytemplate={"port_config": {}},
    )


def test_apply_template_edits_one_snapshot():
    snap = {"id": "nt1", "networks": {"corp": {"vlan_id": 10}}}
    out = apply_template(snap, {"networks": {"corp": {"vlan_id": 20}}})
    assert out == {"id": "nt1", "networks": {"corp": {"vlan_id": 20}}}  # root replace, id preserved


def test_apply_template_set_and_delete_conflict_rejects():
    r = apply_template({"id": "nt1"}, {"networks": {}, "-networks": ""})
    assert isinstance(r, Rejection) and r.stage == "apply"


def test_override_template_baseline_and_proposed_differ_only_by_edit():
    # the fetched site carries a STALE template copy; override pins both sides to
    # the resolved snapshot / proposed snapshot so the diff is exactly the edit
    fetched = _raw(nt={"id": "nt1", "networks": {"corp": {"vlan_id": 999}}})  # stale
    snapshot = {"id": "nt1", "networks": {"corp": {"vlan_id": 10}}}
    proposed = {"id": "nt1", "networks": {"corp": {"vlan_id": 20}}}
    base_raw, prop_raw = override_template("networktemplate", fetched, snapshot, proposed)
    assert base_raw.networktemplate == snapshot      # NOT the stale 999
    assert prop_raw.networktemplate == proposed
    # everything else identical
    assert base_raw.setting == prop_raw.setting and base_raw.devices == prop_raw.devices


def test_override_sets_typed_field_both_sides():
    fetched = _raw_gt()
    base, prop = override_template("gatewaytemplate", fetched, {"id": "g"}, {"id": "g", "x": 1})
    assert base.gatewaytemplate == {"id": "g"} and prop.gatewaytemplate["x"] == 1
    assert base.sitetemplate == fetched.sitetemplate   # other layers pinned
