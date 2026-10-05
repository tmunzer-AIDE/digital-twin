from copy import deepcopy

import pytest

from digital_twin.adapters.mist.ingest.dynamic_usage import classify_dynamic_port, evaluate_rules
from digital_twin.scope.derived_gate import check_derived
from digital_twin.scope.field_gate import screen_op


def _config():
    return {
        "type": "switch",
        "networks": {"corp": {"vlan_id": 10}, "guest": {"vlan_id": 20}},
        "port_usages": {
            "office": {"mode": "access", "port_network": "corp"},
            "guest": {"mode": "access", "port_network": "guest"},
        },
        "port_config": {"ge-0/0/0": {"usage": "office"}},
    }


@pytest.mark.parametrize("side", ["baseline", "proposed"])
def test_usage_assignment_screens_opaque_dependencies_on_both_sides(side):
    before = _config()
    before["port_usages"]["office" if side == "baseline" else "guest"]["qos"] = {"future": 1}
    after = deepcopy(before)
    after["port_config"]["ge-0/0/0"]["usage"] = "guest"
    assert screen_op("device", before, after) is None  # only the selector changed
    rejection = check_derived(before, after)
    assert rejection is not None
    assert any("qos.future" in reason for reason in rejection.reasons)


def test_usage_assignment_screens_existing_network_isolation():
    before = _config()
    before["networks"]["guest"]["isolation"] = True
    after = deepcopy(before)
    after["port_config"]["ge-0/0/0"]["usage"] = "guest"
    rejection = check_derived(before, after)
    assert rejection is not None
    assert any("networks.guest.isolation" in r for r in rejection.reasons)


def test_dynamic_profile_follows_targets_not_taken_by_the_observation():
    before = _config()
    before["port_usages"]["guest"]["qos"] = {"future": 1}
    before["port_usages"]["dynamic"] = {
        "mode": "dynamic", "rules": [{"src": "lldp_system_name", "equals": "future-peer",
                                      "usage": "guest"}],
    }
    after = deepcopy(before)
    after["port_config"]["ge-0/0/0"]["dynamic_usage"] = "dynamic"
    rejection = check_derived(before, after)
    assert rejection is not None
    assert any("port_usages.guest.qos.future" in r for r in rejection.reasons)


@pytest.mark.parametrize("root,row,changed", [
    ("dhcp_snooping", {"enabled": False, "enable_ip_source_guard": True}, {"enabled": True}),
    ("ospf_config", {"enabled": False, "hello_interval": 15}, {"enabled": True}),
    ("bgp_config", {"s1": {"local_as": 65000, "type": "external", "auth_key": "SECRET"}},
     {"s1": {"local_as": 65001}}),
    ("dhcpd_config", {"corp": {"type": "none", "dns_servers": ["10.0.0.53"]}},
     {"corp": {"type": "local"}}),
    ("extra_routes", {"0.0.0.0/0": {"via": "10.0.0.1", "metric": 20}},
     {"0.0.0.0/0": {"via": "10.0.0.2"}}),
])
def test_protocol_edits_cannot_certify_opaque_unchanged_row_configuration(root, row, changed):
    before = _config()
    before[root] = row
    after = deepcopy(before)
    for key, value in changed.items():
        if isinstance(value, dict):
            after[root][key].update(value)
        else:
            after[root][key] = value
    rejection = check_derived(before, after)
    assert rejection is not None
    assert "SECRET" not in str(rejection.reasons)


def test_enabling_ospf_inspects_unchanged_area_authentication():
    before = _config()
    before.update(ospf_config={"enabled": False}, ospf_areas={
        "0.0.0.0": {"networks": {"corp": {"passive": False, "auth_type": "md5"}}},
    })
    after = deepcopy(before)
    after["ospf_config"]["enabled"] = True
    rejection = check_derived(before, after)
    assert rejection is not None
    assert any("ospf_areas.0.0.0.0.networks.corp.auth_type" in r for r in rejection.reasons)


def test_auth_activation_inspects_unchanged_radius_timing():
    before = _config()
    before["radius_config"] = {"auth_servers_timeout": 30}
    after = deepcopy(before)
    after["port_usages"]["office"]["port_auth"] = "dot1x"
    rejection = check_derived(before, after)
    assert rejection is not None
    assert any("radius_config.auth_servers_timeout" in r for r in rejection.reasons)


@pytest.mark.parametrize("edit", ["description", "note", "image2_url"])
def test_cosmetic_edits_do_not_require_unrelated_opaque_dependency_models(edit):
    before = _config()
    before["port_usages"]["office"]["qos"] = {"future": True}
    before["port_usages"]["guest"]["future"] = {"mode": True}
    after = deepcopy(before)
    if edit == "description":
        after["port_usages"]["office"][edit] = "Desks"
    elif edit == "note":
        before["local_port_config"] = {"ge-0/0/0": {"usage": "office"}}
        after = deepcopy(before)
        after["local_port_config"]["ge-0/0/0"][edit] = "Desks"
    else:
        # Device images are not present in compiled effective output.
        assert screen_op("device", before, {**before, edit: "https://example.test/a.png"}) is None
    assert check_derived(before, after) is None


def test_unrelated_opaque_port_does_not_taint_an_independent_known_port_edit():
    before = _config()
    before["port_config"]["ge-0/0/1"] = {"usage": "guest", "future": True}
    after = deepcopy(before)
    after["port_config"]["ge-0/0/0"]["poe_disabled"] = True
    assert check_derived(before, after) is None


@pytest.mark.parametrize("object_type,root", [("device", "port_usages"),
                                               ("site_setting", "port_usages"),
                                               ("networktemplate", "port_usages")])
def test_unknown_dynamic_list_children_are_not_authorized_by_the_parent(object_type, root):
    before = {"type": "switch"} if object_type == "device" else {}
    after = {**before, root: {"dyn": {"rules": [
        {"src": "lldp_system_name", "equals": "peer", "usage": "office", "future": True},
    ]}}}
    rejection = screen_op(object_type, before, after)
    assert rejection is not None
    assert any("rules.0.future" in r for r in rejection.reasons)


def test_open_radius_array_cannot_hide_unknown_item_properties():
    after = {"radius_config": {"auth_servers": [
        {"host": "aaa.example.test", "secret": "SECRET", "future": {"enabled": True}},
    ]}}
    rejection = screen_op("site_setting", {}, after)
    assert rejection is not None
    assert any("auth_servers.0.future" in r for r in rejection.reasons)
    assert "SECRET" not in str(rejection.reasons)


@pytest.mark.parametrize("rule", [None, {}, "future", 1,
    {"src": "lldp_system_name", "equals": True, "usage": "office"},
    {"src": "lldp_system_name", "equals_any": "peer", "usage": "office"},
    {"src": "lldp_system_name", "equals": "peer", "equals_any": ["peer"], "usage": "office"},
    {"src": "lldp_system_name", "equals": "peer", "usage": "office", "future": True},
])
def test_dynamic_evaluator_does_not_coerce_or_ignore_unsupported_rule_shapes(rule):
    assert evaluate_rules([rule], {"lldp_system_name": "peer"}).kind == "inconclusive"
    eff = {"port_usages": {"dyn": {"rules": [rule]}}}
    assert classify_dynamic_port(eff, "dyn", {"up": False})[0] == "unresolved"


def test_dynamic_rule_description_is_an_inert_supported_child():
    rule = {"src": "lldp_system_name", "equals": "peer", "usage": "office",
            "description": "Peer annotation"}
    assert evaluate_rules([rule], {"lldp_system_name": "peer"}).kind == "matched"
