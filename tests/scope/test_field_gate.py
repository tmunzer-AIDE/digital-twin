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
    payload = {
        **CURRENT,
        "port_usages": {"office": {"mode": "access", "port_network": "corp", "allow_dhcpd": True}},
    }
    r = screen_op("site_setting", CURRENT, payload)
    assert isinstance(r, Rejection)
    assert any("allow_dhcpd" in reason for reason in r.reasons)


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


def test_unmodeled_overwrite_leaf_still_rejects():
    # the resolver honors ONLY port_network + poe_disabled from
    # port_config_overwrite — speed et al. stay out of scope (leaf-tightened)
    payload = {**SWITCH_CUR, "port_config_overwrite": {"ge-0/0/0": {"speed": "10g"}}}
    r = screen_op("device", SWITCH_CUR, payload)
    assert isinstance(r, Rejection)
    assert any("port_config_overwrite.ge-0/0/0.speed" in reason for reason in r.reasons)


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
    # Mist update semantics first) — a path absent from it was DELETED
    cur = {**SWITCH_CUR, "dhcp_snooping": {"enabled": True}}
    r = screen_op("device", cur, dict(SWITCH_CUR))  # effective lacks dhcp_snooping
    assert isinstance(r, Rejection)
    reason = next(x for x in r.reasons if "dhcp_snooping" in x)
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
