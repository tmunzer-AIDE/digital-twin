from digital_twin.contracts import Rejection
from digital_twin.scope.allowlist import GATEWAY_EFFECTIVE_ALLOWLIST
from digital_twin.scope.derived_gate import changed_effective_paths, check_derived

BASE = {
    "networks": {"corp": {"vlan_id": 10}},
    "port_usages": {"office": {"mode": "access"}},
    "vars": {"dhcp_ip": "10.0.0.2"},
    "dhcpd_config": {"corp": {"ip": "10.0.0.2"}},
}


def test_no_change_passes():
    assert check_derived(BASE, dict(BASE)) is None


def test_in_scope_effective_change_passes():
    prop = {**BASE, "networks": {"corp": {"vlan_id": 11}}}
    assert check_derived(BASE, prop) is None


def test_vars_ripple_into_out_of_scope_field_rejects():
    # the spec's headline case: a vars edit compiles into a dhcpd_config change
    prop = {**BASE, "vars": {"dhcp_ip": "10.9.9.9"}, "dhcpd_config": {"corp": {"ip": "10.9.9.9"}}}
    r = check_derived(BASE, prop)
    assert isinstance(r, Rejection) and r.stage == "derived_gate"
    assert any("dhcpd_config" in reason for reason in r.reasons)
    # vars itself changing is fine — it's the allowed input
    assert not any(reason.startswith("vars") for reason in r.reasons)


def test_unmodeled_leaf_inside_in_scope_subtree_rejects():
    # the review's P1 case at the EFFECTIVE level: networks is in scope, but
    # isolation is an unmodeled leaf the IR cannot see
    prop = {**BASE, "networks": {"corp": {"vlan_id": 10, "isolation": True}}}
    r = check_derived(BASE, prop)
    assert isinstance(r, Rejection)
    assert any("networks.corp.isolation" in reason for reason in r.reasons)


def test_out_of_scope_field_appearing_rejects():
    prop = {**BASE, "radius_config": {"servers": []}}
    assert isinstance(check_derived(BASE, prop), Rejection)


def test_modeled_local_and_overwrite_effective_changes_pass():
    # both maps are resolver-modeled inputs; their modeled leaves changing in
    # the effective config must not trip the derived gate
    prop = {
        **BASE,
        "local_port_config": {"ge-0/0/0": {"usage": "uplink"}},
        "port_config_overwrite": {"ge-0/0/0": {"port_network": "voice"}},
    }
    assert check_derived(BASE, prop) is None


def test_changed_effective_paths_are_leaf_level():
    prop = {**BASE, "networks": {"corp": {"vlan_id": 11}}, "extra": 1}
    assert changed_effective_paths(BASE, prop) == ("extra", "networks.corp.vlan_id")


def test_role_keyed_allowlist_param():
    # gateway disabled flip is in GATEWAY_EFFECTIVE_ALLOWLIST -> NOT rejected by path
    base = {"port_config": {"a": {"disabled": False}}}
    prop = {"port_config": {"a": {"disabled": True}}}
    assert check_derived(base, prop, allowlist=GATEWAY_EFFECTIVE_ALLOWLIST) is None


def test_dhcp_row_screen_runs_inside_check_derived():
    # an effective dhcpd row transition the row helper rejects -> UNKNOWN, even
    # though dhcpd_config.*.* paths are allowlisted
    base = {"dhcpd_config": {"n": {"type": "local", "servers": ["a"], "ip_start": "1"}}}
    prop = {"dhcpd_config": {"n": {"type": "relay", "servers": ["a"]}}}
    rej = check_derived(base, prop, allowlist=GATEWAY_EFFECTIVE_ALLOWLIST)
    assert rej is not None and rej.stage == "dhcp_mode_transition"
