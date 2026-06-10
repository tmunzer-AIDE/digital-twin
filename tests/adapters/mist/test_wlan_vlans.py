"""Pure WLAN -> required-VLAN-per-AP resolver (the heart of Fix B).

An enabled, vlan-tagged, locally-bridged WLAN requires its VLAN(s) delivered on
the wired uplink of every AP it applies to. What can't be statically resolved
(wxtag scope, template vlan) is reported per-AP as UNRESOLVED -> coverage note.
"""

from __future__ import annotations

from digital_twin.adapters.mist.ingest.wlan_vlans import ap_required_vlans
from digital_twin.ir import device_id

AP1 = {"id": "uuid-ap-1", "mac": "aa:bb:cc:00:00:01", "type": "ap"}
AP2 = {"id": "uuid-ap-2", "mac": "aabbcc000002", "type": "ap"}
APS = [AP1, AP2]
ID1, ID2 = device_id("aabbcc000001"), device_id("aabbcc000002")


def _wlan(**kw):
    base = {"ssid": "w", "enabled": True, "vlan_enabled": True, "interface": "all"}
    return {**base, **kw}


def test_apply_to_site_tags_all_aps():
    resolved, unresolved = ap_required_vlans([_wlan(apply_to="site", vlan_id=10)], APS)
    assert resolved == {ID1: frozenset({10}), ID2: frozenset({10})}
    assert unresolved == {}


def test_apply_to_aps_only_named_ids():
    resolved, _ = ap_required_vlans(
        [_wlan(apply_to="aps", ap_ids=["uuid-ap-1"], vlan_id=20)], APS
    )
    assert resolved == {ID1: frozenset({20})}


def test_disabled_or_untagged_or_tunnelled_is_ignored():
    wlans = [
        _wlan(apply_to="site", vlan_id=10, enabled=False),
        _wlan(apply_to="site", vlan_id=11, vlan_enabled=False),
        _wlan(apply_to="site", vlan_id=12, interface="mxtunnel"),
    ]
    resolved, unresolved = ap_required_vlans(wlans, APS)
    assert resolved == {}
    assert unresolved == {}


def test_vlan_ids_pool_and_union_across_wlans():
    wlans = [
        _wlan(apply_to="site", vlan_ids=[30, 31]),
        _wlan(apply_to="aps", ap_ids=["uuid-ap-1"], vlan_id=40),
    ]
    resolved, _ = ap_required_vlans(wlans, APS)
    assert resolved[ID1] == frozenset({30, 31, 40})
    assert resolved[ID2] == frozenset({30, 31})


def test_dynamic_vlan_requires_the_static_candidate_pool():
    dv = {"enabled": True, "default_vlan_id": 1, "vlans": {"50": "grpA", "60": "grpB"}}
    resolved, _ = ap_required_vlans([_wlan(apply_to="site", dynamic_vlan=dv)], APS)
    assert resolved[ID1] == frozenset({1, 50, 60})


def test_wxtag_scope_marks_all_aps_unresolved():
    resolved, unresolved = ap_required_vlans(
        [_wlan(apply_to="wxtags", wxtag_ids=["t1"], ssid="guest", vlan_id=70)], APS
    )
    assert resolved == {}
    assert ID1 in unresolved and ID2 in unresolved
    assert any("guest" in r for r in unresolved[ID1])


def test_template_vlan_is_unresolved_for_its_aps():
    resolved, unresolved = ap_required_vlans(
        [_wlan(apply_to="aps", ap_ids=["uuid-ap-1"], ssid="corp", vlan_id="{{corp_vlan}}")], APS
    )
    assert resolved == {}
    assert ID1 in unresolved and ID2 not in unresolved
