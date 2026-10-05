from digital_twin.adapters.mist.compile.merge import merge_site_effective
from digital_twin.contracts import Rejection
from digital_twin.scope.allowlist import GATEWAY_EFFECTIVE_ALLOWLIST
from digital_twin.scope.derived_gate import (
    changed_effective_paths,
    check_derived,
    check_derived_gap,
    check_derived_gaps,
)

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


def test_check_derived_gap_returns_structured_leaf_paths():
    prop = {
        **BASE,
        "networks": {"corp": {"vlan_id": 10, "isolation": True}},
        "radius_config": {"servers": []},
    }
    gap = check_derived_gap(BASE, prop, artifact="device sw1")
    assert gap is not None
    assert gap.rejection.stage == "derived_gate"
    assert gap.paths == ("networks.corp.isolation", "radius_config.servers")
    assert gap.dhcp_row is None
    assert any("device sw1" in reason for reason in gap.rejection.reasons)


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


def test_check_derived_gap_returns_structured_dhcp_row():
    base = {"dhcpd_config": {"n": {"type": "local", "servers": ["a"], "ip_start": "1"}}}
    prop = {"dhcpd_config": {"n": {"type": "relay", "servers": ["a"]}}}
    gap = check_derived_gap(
        base,
        prop,
        artifact="gateway gw1",
        allowlist=GATEWAY_EFFECTIVE_ALLOWLIST,
    )
    assert gap is not None
    assert gap.rejection.stage == "dhcp_mode_transition"
    assert gap.paths == ()
    assert gap.dhcp_row == "n"
    assert any("dhcpd_config.n in gateway gw1" in reason for reason in gap.rejection.reasons)


def test_check_derived_gaps_accumulates_leaf_and_dhcp_rows():
    base = {
        "extra": 0,
        "dhcpd_config": {
            "a": {"type": "local", "servers": ["a"], "ip_start": "1"},
            "b": {"type": "local", "servers": ["b"], "ip_start": "1"},
        },
    }
    prop = {
        "extra": 1,
        "dhcpd_config": {
            "a": {"type": "relay", "servers": ["a"]},
            "b": {"type": "relay", "servers": ["b"]},
        },
    }

    gaps = check_derived_gaps(
        base,
        prop,
        artifact="gateway gw1",
        allowlist=GATEWAY_EFFECTIVE_ALLOWLIST,
    )

    assert [g.rejection.stage for g in gaps] == [
        "derived_gate",
        "dhcp_mode_transition",
        "dhcp_mode_transition",
    ]
    assert gaps[0].paths == ("extra",)
    assert [g.dhcp_row for g in gaps[1:]] == ["a", "b"]
    assert any("dhcpd_config.a in gateway gw1" in r for r in gaps[1].rejection.reasons)
    assert any("dhcpd_config.b in gateway gw1" in r for r in gaps[2].rejection.reasons)


_SCOPE_ROW = {"type": "local", "ip_start": "10.3.199.10", "ip_end": "10.3.199.99",
              "gateway": "10.3.199.9"}


def test_empty_fixed_bindings_on_a_new_effective_dhcp_scope_passes():
    prop = {"dhcpd_config": {"lan": {**_SCOPE_ROW, "fixed_bindings": {}}}}
    assert check_derived({}, prop, allowlist=GATEWAY_EFFECTIVE_ALLOWLIST) is None
    assert check_derived({}, prop) is None  # site effective allowlist


def test_effective_fixed_binding_reservations_still_reject():
    # a higher layer's `{}` that wipes inherited reservations shows up as the
    # removed reservation leaves, which stay out of scope
    base = {"dhcpd_config": {"lan": {
        **_SCOPE_ROW, "fixed_bindings": {"aabbccddeeff": {"ip": "10.3.199.50"}}}}}
    prop = {"dhcpd_config": {"lan": {**_SCOPE_ROW, "fixed_bindings": {}}}}
    rej = check_derived(base, prop, allowlist=GATEWAY_EFFECTIVE_ALLOWLIST)
    assert rej is not None
    assert any("fixed_bindings.aabbccddeeff.ip" in r for r in rej.reasons)


def test_effective_scope_holding_only_empty_fixed_bindings_still_rejects():
    prop = {"dhcpd_config": {"lan": {"fixed_bindings": {}}}}
    rej = check_derived({}, prop, allowlist=GATEWAY_EFFECTIVE_ALLOWLIST)
    assert rej is not None
    assert any("dhcpd_config.lan.fixed_bindings" in r for r in rej.reasons)


def test_site_row_with_empty_fixed_bindings_over_template_reservations_still_rejects():
    # dhcpd_config merges per row (the site row replaces the template's), so a
    # site row carrying `fixed_bindings: {}` drops the template's reservations:
    # the raw gate lets the empty map through, so the derived gate must not
    template = {"dhcpd_config": {"lan": {
        **_SCOPE_ROW, "fixed_bindings": {"aabbccddeeff": {"ip": "10.3.199.50"}}}}}
    site = {"dhcpd_config": {"lan": {**_SCOPE_ROW, "fixed_bindings": {}}}}
    base = merge_site_effective(template, {})
    prop = merge_site_effective(template, site)
    rej = check_derived(base, prop)
    assert rej is not None
    assert any("fixed_bindings.aabbccddeeff.ip" in r for r in rej.reasons)


def test_effective_dhcp_naming_leaves_pass_on_gateway_and_site():
    prop = {"dhcpd_config": {"lan": {
        **_SCOPE_ROW, "dns_suffix": ["example.test"],
        "options": {"15": {"type": "string", "value": "example.test"},
                    "119": {"type": "string", "value": "example.test"}}}}}
    assert check_derived({}, prop, allowlist=GATEWAY_EFFECTIVE_ALLOWLIST) is None
    assert check_derived({}, prop) is None


def test_empty_effective_dhcp_options_map_passes():
    prop = {"dhcpd_config": {"lan": {**_SCOPE_ROW, "options": {}}}}
    assert check_derived({}, prop, allowlist=GATEWAY_EFFECTIVE_ALLOWLIST) is None
    assert check_derived({}, prop) is None


def test_site_row_with_empty_options_over_template_options_still_rejects():
    # per-row merge: the site row's `options: {}` drops the template's option 43
    template = {"dhcpd_config": {"lan": {
        **_SCOPE_ROW, "options": {"43": {"type": "hex", "value": "f1"}}}}}
    site = {"dhcpd_config": {"lan": {**_SCOPE_ROW, "options": {}}}}
    rej = check_derived(merge_site_effective(template, {}), merge_site_effective(template, site))
    assert rej is not None
    assert any("options.43.value" in r for r in rej.reasons)
