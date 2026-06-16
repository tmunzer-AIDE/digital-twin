import pytest

from digital_twin.scope.dhcp_screen import dhcp_row_rejection

S = {"type": "local", "ip_start": "10.0.0.10", "ip_end": "10.0.0.99"}
Rx = {"type": "relay", "servers": ["10.1.1.1"]}      # active relay, target x
Ry = {"type": "relay", "servers": ["10.2.2.2"]}      # active relay, target y
Inactive = {"type": "none"}                           # inactive
Rempty = {"type": "relay", "servers": []}             # inactive relay


@pytest.mark.parametrize("base,prop,stage", [
    (S, dict(Rx), "dhcp_mode_transition"),       # S->R : serving -> active-relay
    (dict(Rx), S, "dhcp_mode_transition"),       # R->S : active-relay -> serving
    (dict(Rx), dict(Ry), "dhcp_relay_target"),   # R->R differing servers
])
def test_participation_unknown_cells(base, prop, stage):
    rej = dhcp_row_rejection(base, prop)
    assert rej is not None and rej.stage == stage


@pytest.mark.parametrize("base,prop", [
    (S, dict(S)),                       # S->S same -> allowed
    (dict(Rx), dict(Rx)),               # R->R same servers -> allowed
    (S, dict(Inactive)),                # S->I  -> dhcp_path loss (REVIEW), not rejected here
    (dict(Inactive), dict(Rx)),         # I->R  -> provider gain, allowed
    ({**S, "ip_start": "10.0.0.10"}, {**S, "ip_start": "10.0.0.10"}),  # no change
])
def test_participation_allowed_cells(base, prop):
    assert dhcp_row_rejection(base, prop) is None


def test_empty_servers_exemption_is_not_preempted():
    # local,["x"] -> relay,[] : S->I, >=1 inactive -> NOT rejected (stays dhcp_path)
    base = {"type": "local", "servers": ["10.1.1.1"], "ip_start": "10.0.0.10"}
    prop = {"type": "relay", "servers": []}
    assert dhcp_row_rejection(base, prop) is None


def test_inert_servers_on_both_serving_is_unknown():
    base = {"type": "local", "servers": ["10.1.1.1"], "ip_start": "10.0.0.10"}
    prop = {"type": "local", "servers": ["10.2.2.2"], "ip_start": "10.0.0.10"}
    rej = dhcp_row_rejection(base, prop)
    assert rej is not None and rej.stage == "dhcp_inert_servers"


def test_inert_scope_field_on_both_non_serving_is_unknown():
    base = {"type": "relay", "servers": [], "gateway": "10.0.0.1"}
    prop = {"type": "relay", "servers": [], "gateway": "10.9.9.9"}
    rej = dhcp_row_rejection(base, prop)
    assert rej is not None and rej.stage == "dhcp_scope_field"


def test_dhcp_row_rejection_handles_non_dict_enabled_flag():
    # dhcpd_config carries a top-level boolean `enabled` alongside scope dicts;
    # the row screen must not crash on a non-dict value (regression: live gateway)
    assert dhcp_row_rejection(True, True) is None
    assert dhcp_row_rejection(True, {"type": "local", "ip_start": "1"}) is None
