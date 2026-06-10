from digital_twin.contracts import Rejection
from digital_twin.scope.derived_gate import changed_effective_fields, check_derived

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


def test_out_of_scope_field_appearing_rejects():
    prop = {**BASE, "radius_config": {"servers": []}}
    assert isinstance(check_derived(BASE, prop), Rejection)


def test_changed_effective_fields_lists_top_level_only():
    prop = {**BASE, "networks": {"corp": {"vlan_id": 11}}, "extra": 1}
    assert changed_effective_fields(BASE, prop) == ("extra", "networks")
