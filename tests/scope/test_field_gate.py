from digital_twin.contracts import Rejection
from digital_twin.scope.field_gate import changed_paths, screen_op

CURRENT = {
    "id": "s1",
    "modified_time": 111,
    "networks": {"corp": {"vlan_id": 10}, "voice": {"vlan_id": 30}},
    "port_usages": {"office": {"mode": "access", "port_network": "corp"}},
    "vars": {"x": "1"},
    "dhcpd_config": {"corp": {"ip": "10.0.0.2"}},
}


def test_changed_paths_detects_leaf_edit():
    payload = {**CURRENT, "networks": {"corp": {"vlan_id": 10}, "voice": {"vlan_id": 31}}}
    assert changed_paths(CURRENT, payload) == ("networks.voice.vlan_id",)


def test_changed_paths_counts_removal_as_change():
    # full-object replacement: a key present in current but absent from payload IS a change
    payload = {k: v for k, v in CURRENT.items() if k != "dhcpd_config"}
    assert changed_paths(CURRENT, payload) == ("dhcpd_config",)


def test_changed_paths_ignores_server_metadata():
    payload = {k: v for k, v in CURRENT.items() if k not in ("id", "modified_time")}
    assert changed_paths(CURRENT, payload) == ()


def test_in_scope_change_passes():
    payload = {**CURRENT, "vars": {"x": "2"}}
    assert screen_op("site_setting", CURRENT, payload) is None


def test_out_of_scope_change_rejects_with_paths():
    payload = {**CURRENT, "dhcpd_config": {"corp": {"ip": "10.0.0.99"}}}
    r = screen_op("site_setting", CURRENT, payload)
    assert isinstance(r, Rejection) and r.stage == "field_gate"
    assert any("dhcpd_config" in reason for reason in r.reasons)


def test_whole_subtree_removal_of_allowed_field_passes():
    payload = {k: v for k, v in CURRENT.items() if k != "vars"}
    assert screen_op("site_setting", CURRENT, payload) is None


def test_device_exact_leaves_name_notes():
    cur = {"name": "sw-a", "notes": "old", "port_config": {"ge-0/0/0": {"usage": "office"}}}
    assert screen_op("device", cur, {**cur, "name": "sw-b"}) is None
    r = screen_op("device", cur, {**cur, "managed": False})
    assert isinstance(r, Rejection)
