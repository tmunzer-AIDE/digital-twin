import json
from pathlib import Path

from digital_twin.contracts import Rejection
from digital_twin.scope.allowlist import RAW_ALLOWLIST
from digital_twin.scope.field_gate import changed_paths, screen_op
from digital_twin.scope.paths import allowed

_DEVICE_SWITCH_OAS_PATH = (
    Path(__file__).parents[2]
    / "src/digital_twin/adapters/mist/oas/device_switch.schema.json"
)

CURRENT = {
    "id": "s1",
    "modified_time": 111,
    "networks": {"corp": {"vlan_id": 10}, "voice": {"vlan_id": 30}},
    "port_usages": {"office": {"mode": "access", "port_network": "corp"}},
    "vars": {"x": "1"},
    "dhcpd_config": {"corp": {"ip": "10.0.0.2"}},
}

SWITCH_CUR = {
    "type": "switch",
    "name": "sw-a",
    "notes": "old",
    "port_config": {"ge-0/0/0": {"usage": "office"}},
}


def test_changed_paths_detects_leaf_edit():
    payload = {**CURRENT, "networks": {"corp": {"vlan_id": 10}, "voice": {"vlan_id": 31}}}
    assert changed_paths(CURRENT, payload) == ("networks.voice.vlan_id",)


def test_changed_paths_descends_removed_subtree_to_leaves():
    # full-object replacement: a key present in current but absent from payload
    # IS a change — surfaced at LEAF granularity
    payload = {k: v for k, v in CURRENT.items() if k != "dhcpd_config"}
    assert changed_paths(CURRENT, payload) == ("dhcpd_config.corp.ip",)


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


def test_unmodeled_leaf_inside_allowed_subtree_rejects():
    # the review's P1 case: networks is an in-scope SUBTREE but isolation is an
    # unmodeled LEAF — the IR cannot simulate it, so it must not pass as in-scope
    payload = {
        **CURRENT,
        "networks": {"corp": {"vlan_id": 10, "isolation": True}, "voice": {"vlan_id": 30}},
    }
    r = screen_op("site_setting", CURRENT, payload)
    assert isinstance(r, Rejection)
    assert any("networks.corp.isolation" in reason for reason in r.reasons)


def test_unmodeled_usage_leaf_rejects():
    # SP4 moved mac_limit into scope; Spec 1 moved use_vstp into scope too (a
    # reviewed STP knob) — use a still-fictional leaf to pin the unmodeled case
    payload = {
        **CURRENT,
        "port_usages": {"office": {"mode": "access", "port_network": "corp",
                                    "not_a_real_knob": True}},
    }
    r = screen_op("site_setting", CURRENT, payload)
    assert isinstance(r, Rejection)
    assert any("not_a_real_knob" in reason for reason in r.reasons)


def test_whole_subtree_removal_of_allowed_field_passes():
    payload = {k: v for k, v in CURRENT.items() if k != "vars"}
    assert screen_op("site_setting", CURRENT, payload) is None


def test_device_exact_leaves_name_notes():
    assert screen_op("device", SWITCH_CUR, {**SWITCH_CUR, "name": "sw-b"}) is None
    r = screen_op("device", SWITCH_CUR, {**SWITCH_CUR, "managed": False})
    assert isinstance(r, Rejection)


def test_modeled_local_port_config_leaves_pass():
    # local_port_config is a MODELED input: the resolver reassigns the usage per
    # member (ingest.ports) — proven by the ingest tests. Must be in scope.
    payload = {**SWITCH_CUR, "local_port_config": {"ge-0/0/0": {"usage": "uplink"}}}
    assert screen_op("device", SWITCH_CUR, payload) is None


def test_modeled_port_config_overwrite_leaf_passes():
    # port_config_overwrite.port_network moves the access VLAN (resolver-honored)
    payload = {**SWITCH_CUR, "port_config_overwrite": {"ge-0/0/0": {"port_network": "voice"}}}
    assert screen_op("device", SWITCH_CUR, payload) is None
    # poe_disabled is resolver-honored too (Port.poe + the poe.disconnect check)
    payload = {**SWITCH_CUR, "port_config_overwrite": {"ge-0/0/0": {"poe_disabled": True}}}
    assert screen_op("device", SWITCH_CUR, payload) is None


def test_port_description_is_in_scope_on_every_inline_map():
    # `description` is a cosmetic per-port label with no modeled forwarding/security
    # effect — it must be decidable (no findings), not gated to UNKNOWN.
    for key in ("port_config", "local_port_config", "port_config_overwrite"):
        payload = {**SWITCH_CUR, key: {"ge-0/0/0": {"description": "Disabled by admin"}}}
        assert screen_op("device", SWITCH_CUR, payload) is None, key


def test_port_config_critical_is_in_scope():
    # `critical` is an inert metadata flag (no effect on the resolved port) — a
    # change to it must pass the gate (decidable), not fall through to UNKNOWN.
    cur = {**SWITCH_CUR, "port_config": {"ge-0/0/0": {"usage": "office"}}}
    payload = {**SWITCH_CUR, "port_config": {
        "ge-0/0/0": {"usage": "office", "critical": True}}}
    assert screen_op("device", cur, payload) is None


def test_no_local_overwrite_flip_passes_when_no_local_entry():
    # no_local_overwrite is in scope; a flip with no local_port_config entry (or an
    # entry of only modeled leaves) has no unmodeled ripple -> decidable.
    cur = {
        **SWITCH_CUR,
        "port_config": {"ge-0/0/0": {"usage": "office", "no_local_overwrite": True}},
    }
    payload = {
        **SWITCH_CUR,
        "port_config": {"ge-0/0/0": {"usage": "office", "no_local_overwrite": False}},
    }
    assert screen_op("device", cur, payload) is None


def test_no_local_overwrite_flip_passes_over_modeled_local_leaf():
    cur = {
        "type": "switch",
        "port_config": {"ge-0/0/0": {"usage": "office", "no_local_overwrite": True}},
        "local_port_config": {"ge-0/0/0": {"usage": "uplink"}},  # modeled leaf
    }
    payload = {**cur, "port_config": {"ge-0/0/0": {"usage": "office", "no_local_overwrite": False}}}
    assert screen_op("device", cur, payload) is None


def test_no_local_overwrite_flip_rejects_over_unmodeled_local_leaf():
    # the false-SAFE the guard protected: flipping the flag activates an
    # unmodeled leaf the resolver never projects -> must gate to UNKNOWN.
    # (Spec 1 moved use_vstp into scope as a reviewed STP knob, so this now
    # uses a still-fictional leaf to pin the unmodeled case.)
    cur = {
        "type": "switch",
        "port_config": {"ge-0/0/0": {"usage": "office", "no_local_overwrite": True}},
        "local_port_config": {"ge-0/0/0": {"usage": "office", "not_a_real_knob": True}},
    }
    payload = {**cur, "port_config": {"ge-0/0/0": {"usage": "office", "no_local_overwrite": False}}}
    r = screen_op("device", cur, payload)
    assert isinstance(r, Rejection)
    assert any("no_local_overwrite flip" in reason and "not_a_real_knob" in reason
               for reason in r.reasons)


def test_no_local_overwrite_flip_over_reviewed_stp_leaves_is_decidable():
    # Spec 1: use_vstp/stp_p2p/stp_no_root_port are now REVIEWED local leaves
    # (they reach PortMisc via _MISC_ATTRS/_MODELED_USAGE_ATTRS), so a
    # no_local_overwrite flip over a local entry containing only them must PASS
    # the field gate (the PortMisc diff carries the REVIEW) — no coverage gap.
    cur = {
        "type": "switch",
        "port_config": {"ge-0/0/0": {"usage": "office", "no_local_overwrite": True}},
        "local_port_config": {
            "ge-0/0/0": {"use_vstp": True, "stp_p2p": False, "stp_no_root_port": True},
        },
    }
    payload = {**cur, "port_config": {"ge-0/0/0": {"usage": "office", "no_local_overwrite": False}}}
    assert screen_op("device", cur, payload) is None


def test_no_local_overwrite_flip_over_an_unmodeled_local_leaf_still_gaps():
    # Dormant backstop: the ripple guard survives Spec 1. Derive the
    # still-unmodeled local leaf from the committed device_switch OAS minus
    # the allowlisted local attrs (do NOT invent a fake leaf); pick
    # sorted(...)[0] deterministically. If the set is empty, that emptiness
    # itself is the assertion (the guard-path unit lives in the fabricated-leaf
    # test above it, kept as-is).
    with open(_DEVICE_SWITCH_OAS_PATH) as f:
        schema = json.load(f)
    local_props = set(
        schema["properties"]["local_port_config"]["additionalProperties"]["properties"]
    )
    device_allowlist = RAW_ALLOWLIST["device"]
    unmodeled = sorted(
        leaf for leaf in local_props
        if not allowed(f"local_port_config.ge-0/0/0.{leaf}", device_allowlist)
    )
    if not unmodeled:
        assert unmodeled == []
        return
    leaf = unmodeled[0]
    cur = {
        "type": "switch",
        "port_config": {"ge-0/0/0": {"usage": "office", "no_local_overwrite": True}},
        "local_port_config": {"ge-0/0/0": {"usage": "office", leaf: True}},
    }
    payload = {**cur, "port_config": {"ge-0/0/0": {"usage": "office", "no_local_overwrite": False}}}
    r = screen_op("device", cur, payload)
    assert isinstance(r, Rejection)
    assert any("no_local_overwrite flip" in reason and leaf in reason
               for reason in r.reasons)


def test_unmodeled_overwrite_leaf_still_rejects():
    # the resolver honors port_network/poe_disabled/disabled/speed/duplex/mac_limit
    # from port_config_overwrite, plus the Spec 1 benign poe_keep_state_when_reboot
    # (allowed by the gates but IR-ignored) — mtu stays genuinely unmodeled there
    # (the resolver doesn't read overwrite.mtu; see test_mtu_is_in_scope) and must
    # still reject (leaf-tightened).
    payload = {**SWITCH_CUR, "port_config_overwrite": {
        "ge-0/0/0": {"mtu": 9200}}}
    r = screen_op("device", SWITCH_CUR, payload)
    assert isinstance(r, Rejection)
    assert any("port_config_overwrite.ge-0/0/0.mtu" in reason
               for reason in r.reasons)


def test_device_status_fields_are_ignored():
    # GET-only status fields (adopted/connected/hw_rev/...) ride in the fetched
    # object but never belong in a PUT body — omitting them is not a change
    cur = {
        **SWITCH_CUR,
        "adopted": True,
        "connected": True,
        "hw_rev": "A1",
        "heightSet": False,
        "mist_configured": True,
    }
    payload = dict(SWITCH_CUR)  # clean config-only payload
    assert screen_op("device", cur, payload) is None


def test_deletion_rejections_are_named_as_deletions():
    # screen_op receives the EFFECTIVE proposed object (the engine resolves
    # Mist update semantics first) — a path absent from it was DELETED.
    # device-level dhcpd_config stays out of scope (GS25 moved dhcp_snooping in)
    cur = {**SWITCH_CUR, "dhcpd_config": {"corp": {"type": "local"}}}
    r = screen_op("device", cur, dict(SWITCH_CUR))  # effective lacks dhcpd_config
    assert isinstance(r, Rejection)
    reason = next(x for x in r.reasons if "dhcpd_config" in x)
    assert "deleted" in reason


def test_non_switch_device_rejected_post_fetch():
    # the review's P1 case: M1 models switch config only — an AP update must
    # not pass the gates even if its changed paths look allowable
    ap_cur = {"type": "ap", "name": "ap-1"}
    r = screen_op("device", ap_cur, {**ap_cur, "name": "ap-2"})
    assert isinstance(r, Rejection) and r.stage == "field_gate"
    assert any("'ap'" in reason and "switch" in reason for reason in r.reasons)


def test_dynamic_profile_leaves_are_in_scope():
    # the IR consumes these now (runtime usage resolution): rule edits,
    # reset_default_when, and a port's dynamic_usage pointer are MODELED leaves
    cur = {
        "port_usages": {
            "dyn": {"mode": "dynamic", "rules": [{"src": "lldp_system_name", "equals": "a",
                                                  "usage": "ap"}]}
        },
        "port_config": {"ge-0/0/1": {"usage": "default"}},
    }
    new = {
        "port_usages": {
            "dyn": {
                "mode": "dynamic",
                "reset_default_when": "none",
                "rules": [{"src": "lldp_system_name", "equals": "b", "usage": "uplink"}],
            }
        },
        "port_config": {"ge-0/0/1": {"usage": "default", "dynamic_usage": "dyn"}},
    }
    assert screen_op("device", {**SWITCH_CUR, **cur}, {**SWITCH_CUR, **new}) is None


def test_poe_disabled_is_in_scope():
    cur = {"port_usages": {"ap": {"mode": "trunk", "poe_disabled": False}}}
    new = {"port_usages": {"ap": {"mode": "trunk", "poe_disabled": True}}}
    assert screen_op("device", {**SWITCH_CUR, **cur}, {**SWITCH_CUR, **new}) is None


def test_routed_network_and_irb_leaves_are_in_scope():
    # GS22: networks.*.{subnet,gateway} declare ROUTED intent (Vlan.subnet,
    # the wired.l3.gateway_gap check); other_ip_configs.*.{type,ip,netmask}
    # are the switch IRB facts the IR already ingests
    cur = {"networks": {"corp": {"vlan_id": 10}}}
    new = {"networks": {"corp": {"vlan_id": 10, "subnet": "198.51.100.0/24",
                                 "gateway": "198.51.100.1"}}}
    assert screen_op("device", {**SWITCH_CUR, **cur}, {**SWITCH_CUR, **new}) is None
    irb = {**SWITCH_CUR, "other_ip_configs": {"corp": {"type": "static",
                                                       "ip": "198.51.100.2",
                                                       "netmask": "255.255.255.0"}}}
    assert screen_op("device", SWITCH_CUR, irb) is None
    # unmodeled other_ip_configs leaves stay out of scope (leaf-tightened)
    odd = {**SWITCH_CUR, "other_ip_configs": {"corp": {"evpn_anycast": True}}}
    assert isinstance(screen_op("device", SWITCH_CUR, odd), Rejection)


def test_dhcpd_leaves_are_in_scope_on_site_setting_only():
    # GS24: the IR models the site-level DHCP path (type + relay servers).
    # Device-level switch dhcpd_config is intentionally UNMODELED (the
    # compiler does not carry it) and must stay out of scope.
    cur = {"networks": {"corp": {"vlan_id": 10}},
           "dhcpd_config": {"corp": {"type": "local"}}}
    new = {"networks": {"corp": {"vlan_id": 10}},
           "dhcpd_config": {"corp": {"type": "none"}}}
    assert screen_op("site_setting", cur, new) is None
    relay = {"networks": {"corp": {"vlan_id": 10}},
             "dhcpd_config": {"corp": {"type": "relay", "servers": ["10.9.9.9"]}}}
    assert screen_op("site_setting", cur, relay) is None
    dev = {**SWITCH_CUR, "dhcpd_config": {"corp": {"type": "none"}}}
    assert isinstance(screen_op("device", SWITCH_CUR, dev), Rejection)


def test_stp_leaves_are_in_scope():
    # stp_edge/stp_disable on port_usages + inline stp_edge on
    # local_port_config (schema: NOT on port_config) + stp_config.bridge_priority
    cur = {"port_usages": {"up": {"mode": "trunk"}}}
    new = {"port_usages": {"up": {"mode": "trunk", "stp_disable": True, "stp_edge": False}}}
    assert screen_op("device", {**SWITCH_CUR, **cur}, {**SWITCH_CUR, **new}) is None
    local = {**SWITCH_CUR, "local_port_config": {"ge-0/0/0": {"usage": "up", "stp_edge": True}}}
    assert screen_op("device", SWITCH_CUR, local) is None
    prio = {**SWITCH_CUR, "stp_config": {"bridge_priority": "4096"}}
    assert screen_op("device", SWITCH_CUR, prio) is None
    # inline stp_edge on port_config is NOT a schema field -> stays out of scope
    inline = {**SWITCH_CUR, "port_config": {"ge-0/0/0": {"usage": "up", "stp_edge": True}}}
    assert isinstance(screen_op("device", SWITCH_CUR, inline), Rejection)


def test_gs25_dhcp_lint_leaves_in_scope():
    # site_setting: scope range fields + snooping toggles
    cur = {"dhcpd_config": {"n1": {"type": "local"}}, "dhcp_snooping": {}}
    eff = {
        "dhcpd_config": {"n1": {"type": "local", "ip_start": "10.0.0.1",
                                "ip_end": "10.0.0.9", "gateway": "10.0.0.254"}},
        "dhcp_snooping": {"enabled": True, "all_networks": False, "networks": ["n1"]},
    }
    assert screen_op("site_setting", cur, eff) is None

    # device: snooping override + inline allow_dhcpd
    cur_d = dict(SWITCH_CUR)
    eff_d = {
        **SWITCH_CUR,
        "dhcp_snooping": {"enabled": True, "networks": ["n1"]},
        "local_port_config": {"ge-0/0/1": {"allow_dhcpd": False}},
    }
    assert screen_op("device", cur_d, eff_d) is None


def test_usage_allow_dhcpd_in_scope_on_port_usages_and_local_port_config():
    # allow_dhcpd is modeled on port_usages (site_setting + device) and inline
    # local_port_config — but NOT inline port_config (the refreshed closed
    # device_switch OAS documents allow_dhcpd on local_port_config/port_usages,
    # not on the port_config entry; the narrowing is pinned below).
    cur = {"port_usages": {"u": {"mode": "trunk"}}}
    eff = {"port_usages": {"u": {"mode": "trunk", "allow_dhcpd": False}}}
    assert screen_op("site_setting", cur, eff) is None
    dev_eff = {
        **SWITCH_CUR,
        "port_usages": {"u": {"mode": "trunk", "allow_dhcpd": False}},
        "local_port_config": {"ge-0": {"usage": "u", "allow_dhcpd": True}},
    }
    assert screen_op("device", {**SWITCH_CUR, **cur}, dev_eff) is None


def test_narrowed_inline_port_config_attrs_out_of_scope():
    # OAS-refresh narrowing: the refreshed (closed) device_switch port_config entry
    # does NOT document mode/all_networks/allow_dhcpd (those are on local_port_config
    # / port_usages), and local_port_config does NOT document dynamic_usage. Editing
    # those inline leaves is now out-of-scope -> the field gate rejects (UNKNOWN).
    for leaf, val in (("mode", "access"), ("all_networks", True), ("allow_dhcpd", True)):
        eff = {**SWITCH_CUR, "port_config": {"ge-0/0/0": {"usage": "office", leaf: val}}}
        rej = screen_op("device", SWITCH_CUR, eff)
        assert rej is not None, leaf
        assert f"port_config.ge-0/0/0.{leaf}" in rej.reasons[0], (leaf, rej.reasons)
    lpc_eff = {**SWITCH_CUR,
               "local_port_config": {"ge-0/0/0": {"usage": "office", "dynamic_usage": "x"}}}
    rej = screen_op("device", SWITCH_CUR, lpc_eff)
    assert rej is not None
    assert "local_port_config.ge-0/0/0.dynamic_usage" in rej.reasons[0]


def test_mtu_is_in_scope():
    # mtu lives on port_usages + inline port_config/local_port_config
    # (schema-confirmed; NOT on port_config_overwrite) — the IR models it now
    # (Port.mtu + the mtu.mismatch check)
    cur = {"port_usages": {"up": {"mode": "trunk"}}}
    new = {"port_usages": {"up": {"mode": "trunk", "mtu": 9200}}}
    assert screen_op("device", {**SWITCH_CUR, **cur}, {**SWITCH_CUR, **new}) is None
    inline = {**SWITCH_CUR, "port_config": {"ge-0/0/0": {"usage": "up", "mtu": 9200}}}
    assert screen_op("device", SWITCH_CUR, inline) is None
    # still NOT honored from port_config_overwrite (resolver doesn't read it)
    ow = {**SWITCH_CUR, "port_config_overwrite": {"ge-0/0/0": {"mtu": 9200}}}
    assert isinstance(screen_op("device", SWITCH_CUR, ow), Rejection)


def test_screen_op_networktemplate_allows_modeled_leaf_no_role_check():
    from digital_twin.scope.field_gate import screen_op
    current = {"id": "nt1", "networks": {"corp": {"vlan_id": 10}}}
    payload = {"id": "nt1", "networks": {"corp": {"vlan_id": 20}}}
    assert screen_op("networktemplate", current, payload) is None  # vlan_id is modeled


def test_screen_op_networktemplate_rejects_switch_matching():
    from digital_twin.scope.field_gate import screen_op
    r = screen_op("networktemplate",
                  {"id": "nt1", "switch_matching": {"enable": True}},
                  {"id": "nt1", "switch_matching": {"enable": False}})
    assert r is not None  # switch_matching not allowlisted


def test_ospf_metric_leaf_is_in_scope():
    # GS27-T1: ospf_areas.*.networks.*.metric is now allowlisted on device
    # (and site_setting); a metric-only change must not be rejected.
    cur = {
        **SWITCH_CUR,
        "ospf_config": {"enabled": True},
        "ospf_areas": {"0": {"networks": {"corp": {"passive": False}}}},
    }
    new = {
        **SWITCH_CUR,
        "ospf_config": {"enabled": True},
        "ospf_areas": {"0": {"networks": {"corp": {"passive": False, "metric": 50}}}},
    }
    assert screen_op("device", cur, new) is None


def test_auth_in_port_config_or_overwrite_is_unknown():
    # auth attrs are NOT on port_config/overwrite (OAS) -> a change there is
    # out-of-scope (UNKNOWN), even though local/usage auth is modeled
    for payload in (
        {**SWITCH_CUR, "port_config": {"ge-0/0/0": {"usage": "office", "port_auth": "dot1x"}}},
        {**SWITCH_CUR, "port_config_overwrite": {"ge-0/0/0": {"port_auth": "dot1x"}}},
    ):
        r = screen_op("device", SWITCH_CUR, payload)
        assert isinstance(r, Rejection)
        assert any("port_auth" in reason for reason in r.reasons)


def test_auth_in_local_port_config_passes_for_device():
    payload = {**SWITCH_CUR, "local_port_config": {"ge-0/0/0": {"port_auth": "dot1x"}}}
    assert screen_op("device", SWITCH_CUR, payload) is None


def test_ui_leaf_is_benign_but_unmodeled_operational_leaves_are_gaps():
    payload = {**SWITCH_CUR, "port_usages": {"office": {"mode": "access",
                                                         "ui_evpntopo_id": "abc-123"}}}
    assert screen_op("device", SWITCH_CUR, payload) is None

    payload = {**SWITCH_CUR, "port_usages": {"office": {"mode": "access",
                                                         "enable_qos": True}}}
    assert screen_op("device", SWITCH_CUR, payload) is not None

    payload = {**SWITCH_CUR, "local_port_config": {"ge-0/0/0": {"enable_qos": True}}}
    assert screen_op("device", SWITCH_CUR, payload) is not None

    payload = {**SWITCH_CUR, "port_usages": {"office": {
        "mode": "access", "poe_keep_state_when_reboot": True}}}
    assert screen_op("device", SWITCH_CUR, payload) is not None

    payload = {**SWITCH_CUR, "port_config_overwrite": {
        "ge-0/0/0": {"poe_keep_state_when_reboot": True}}}
    assert screen_op("device", SWITCH_CUR, payload) is not None

    payload = {**SWITCH_CUR, "port_usages": {"office": {
        "mode": "access", "server_fail_retry_interval": 300}}}
    assert screen_op("device", SWITCH_CUR, payload) is not None


def test_fabricated_unknown_leaf_still_coverage_gap():
    # regression: an unclassified leaf must still be an out-of-scope gap, not a
    # silent pass — the cardinal never-false-SAFE rule.
    payload = {**SWITCH_CUR, "port_usages": {"office": {
        "mode": "access", "not_a_real_knob": True}}}
    r = screen_op("device", SWITCH_CUR, payload)
    assert isinstance(r, Rejection)
    assert any("not_a_real_knob" in reason for reason in r.reasons)


# Mist writes `fixed_bindings: {}` (static DHCP reservations) on every scope row.
# An EMPTY map reserves nothing, exactly like an absent one, so it must not open
# a coverage gap by itself — but only on a row whose other settings are gated,
# and never when it adds or empties real reservations.
_SCOPE_ROW = {"type": "local", "ip_start": "10.3.199.10", "ip_end": "10.3.199.99",
              "gateway": "10.3.199.9"}


def test_empty_fixed_bindings_on_a_new_dhcp_scope_is_not_a_gap():
    gw_cur = {"ip_configs": {"lan": {"ip": "10.3.199.9"}}}
    gw_new = {**gw_cur, "dhcpd_config": {"lan": {**_SCOPE_ROW, "fixed_bindings": {}}}}
    assert screen_op("gatewaytemplate", gw_cur, gw_new) is None
    site_cur = {"networks": {"lan": {"vlan_id": 199}}}
    site_new = {**site_cur, "dhcpd_config": {"lan": {**_SCOPE_ROW, "fixed_bindings": {}}}}
    assert screen_op("site_setting", site_cur, site_new) is None


def test_removing_a_dhcp_scope_with_empty_fixed_bindings_is_not_a_gap():
    cur = {"dhcpd_config": {"lan": {**_SCOPE_ROW, "fixed_bindings": {}}}}
    assert screen_op("gatewaytemplate", cur, {"dhcpd_config": {}}) is None


def test_dhcp_fixed_binding_reservations_stay_a_gap():
    empty = {"dhcpd_config": {"lan": {**_SCOPE_ROW, "fixed_bindings": {}}}}
    reserved = {"dhcpd_config": {"lan": {
        **_SCOPE_ROW, "fixed_bindings": {"aabbccddeeff": {"ip": "10.3.199.50"}}}}}
    added = screen_op("gatewaytemplate", empty, reserved)
    assert isinstance(added, Rejection)
    assert any("fixed_bindings.aabbccddeeff.ip" in r for r in added.reasons)
    emptied = screen_op("gatewaytemplate", reserved, empty)  # dropping reservations
    assert isinstance(emptied, Rejection)
    assert any("fixed_bindings.aabbccddeeff.ip" in r for r in emptied.reasons)


def test_dhcp_scope_holding_only_empty_fixed_bindings_stays_a_gap():
    # a row with no other setting can switch a scope on with defaults; nothing
    # else on the row would surface that, so the empty map must stay a gap
    cur = {"ip_configs": {"lan": {"ip": "10.3.199.9"}}}
    new = {**cur, "dhcpd_config": {"lan": {"fixed_bindings": {}}}}
    rej = screen_op("gatewaytemplate", cur, new)
    assert isinstance(rej, Rejection)
    assert any("dhcpd_config.lan.fixed_bindings" in r for r in rej.reasons)


def test_dhcp_options_stay_a_gap_next_to_empty_fixed_bindings():
    new = {"dhcpd_config": {"lan": {
        **_SCOPE_ROW, "fixed_bindings": {},
        "options": {"43": {"type": "hex", "value": "f1"}}}}}
    rej = screen_op("gatewaytemplate", {}, new)
    assert isinstance(rej, Rejection)
    assert any("options.43.value" in r for r in rej.reasons)
    assert not any("fixed_bindings" in r for r in rej.reasons)


_OPTION_15 = {"15": {"type": "string", "value": "example.test"}}


_OPTION_119 = {"119": {"type": "string", "value": "example.test"}}
_NAMING_ROW = {**_SCOPE_ROW, "dns_suffix": ["example.test"],
               "options": {**_OPTION_15, **_OPTION_119}}


def test_dhcp_naming_leaves_are_in_scope_on_gateway_rows():
    assert screen_op("gatewaytemplate", {}, {"dhcpd_config": {"lan": _NAMING_ROW}}) is None


def test_other_dhcp_options_stay_a_gap_next_to_option_15():
    row = {**_SCOPE_ROW, "options": {**_OPTION_15, "43": {"type": "hex", "value": "f1"}}}
    rej = screen_op("gatewaytemplate", {}, {"dhcpd_config": {"lan": row}})
    assert isinstance(rej, Rejection)
    assert any("options.43.value" in r for r in rej.reasons)
    assert not any("options.15" in r for r in rej.reasons)


def test_dhcp_naming_leaves_are_in_scope_on_switch_dhcp_rows():
    for object_type in ("site_setting", "networktemplate", "sitetemplate"):
        new = {"dhcpd_config": {"lan": _NAMING_ROW}}
        assert screen_op(object_type, {}, new) is None, object_type


def test_device_level_switch_dhcp_stays_out_of_scope():
    dev = {**SWITCH_CUR, "dhcpd_config": {"lan": {"type": "local", "dns_suffix": ["example.test"]}}}
    assert isinstance(screen_op("device", SWITCH_CUR, dev), Rejection)


def test_empty_dhcp_options_map_on_a_scope_is_not_a_gap():
    row = {**_SCOPE_ROW, "options": {}, "fixed_bindings": {}}
    assert screen_op("gatewaytemplate", {}, {"dhcpd_config": {"lan": row}}) is None
    assert screen_op("site_setting", {}, {"dhcpd_config": {"lan": row}}) is None


def test_dhcp_scope_holding_only_an_empty_options_map_stays_a_gap():
    rej = screen_op("gatewaytemplate", {}, {"dhcpd_config": {"lan": {"options": {}}}})
    assert isinstance(rej, Rejection)
    assert any("dhcpd_config.lan.options" in r for r in rej.reasons)
