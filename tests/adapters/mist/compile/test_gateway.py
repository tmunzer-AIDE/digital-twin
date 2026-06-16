from digital_twin.adapters.mist.compile.gateway import compile_gateway_device

# ---------------------------------------------------------------------------
# Namespace-strip tests: site layers must NOT contribute dhcpd_config/networks
# ---------------------------------------------------------------------------


def test_site_setting_dhcpd_config_not_inherited_by_gateway():
    """site_setting.dhcpd_config (switch/site-namespace) must NOT appear
    in compile_gateway_device output; only gatewaytemplate+device contribute."""
    gt = {"dhcpd_config": {"gw_scope": {"type": "local", "ip_start": "10.1.0.10"}}}
    ss = {"dhcpd_config": {"site_scope": {"type": "local", "ip_start": "192.168.1.10"}}}
    eff = compile_gateway_device(gt, None, ss, {})
    dhcp = eff.get("dhcpd_config", {})
    assert "gw_scope" in dhcp, "gatewaytemplate dhcpd_config must survive"
    assert "site_scope" not in dhcp, (
        "site_setting.dhcpd_config must NOT reach the gateway effective"
    )


def test_gatewaytemplate_and_device_dhcpd_config_survive_and_merge():
    """gatewaytemplate.dhcpd_config AND device.dhcpd_config both appear
    in the result (DICT_MERGE: per-key, not root-replace)."""
    gt = {"dhcpd_config": {"gt_scope": {"type": "local", "ip_start": "10.2.0.10"}}}
    device = {"dhcpd_config": {"dev_scope": {"type": "local", "ip_start": "10.3.0.10"}}}
    ss = {"dhcpd_config": {"site_scope": {"type": "local", "ip_start": "192.168.2.10"}}}
    eff = compile_gateway_device(gt, None, ss, device)
    dhcp = eff.get("dhcpd_config", {})
    assert "gt_scope" in dhcp, "gatewaytemplate dhcpd_config scope must survive"
    assert "dev_scope" in dhcp, "device dhcpd_config scope must survive"
    assert "site_scope" not in dhcp, (
        "site_setting.dhcpd_config must NOT reach the gateway effective"
    )


def test_site_setting_networks_not_inherited_by_gateway():
    """site_setting.networks (switch/site-namespace) must NOT appear
    in compile_gateway_device output."""
    gt = None
    ss = {"networks": {"site_net": {"vlan_id": 100}}}
    device = {}
    eff = compile_gateway_device(gt, None, ss, device)
    nets = eff.get("networks", {})
    assert "site_net" not in nets, (
        "site_setting.networks must NOT reach the gateway effective"
    )


def test_sitetemplate_dhcpd_config_not_inherited_by_gateway():
    """sitetemplate.dhcpd_config must also be stripped (sitetemplate is a
    site-level layer; only gatewaytemplate+device contribute dhcpd_config)."""
    gt = {"dhcpd_config": {"gw_scope": {"type": "local"}}}
    st = {"dhcpd_config": {"st_scope": {"type": "local", "ip_start": "172.16.0.10"}}}
    eff = compile_gateway_device(gt, st, {}, {})
    dhcp = eff.get("dhcpd_config", {})
    assert "gw_scope" in dhcp, "gatewaytemplate dhcpd_config must survive"
    assert "st_scope" not in dhcp, (
        "sitetemplate.dhcpd_config must NOT reach the gateway effective"
    )


def test_site_namespace_strip_does_not_strip_vars():
    """vars from site layers must still pass through (needed for {{var}}
    resolution), even though dhcpd_config/networks are stripped."""
    gt = {"vars": {"GW_IP": "10.0.0.1"}}
    st = {"vars": {"ST_VAR": "st_value"}}
    ss = {
        "vars": {"SS_VAR": "ss_value"},
        "dhcpd_config": {"site_scope": {"type": "local"}},
        "networks": {"site_net": {"vlan_id": 99}},
    }
    eff = compile_gateway_device(gt, st, ss, {})
    assert eff.get("vars", {}).get("GW_IP") == "10.0.0.1"
    assert eff.get("vars", {}).get("ST_VAR") == "st_value"
    assert eff.get("vars", {}).get("SS_VAR") == "ss_value"
    assert "site_scope" not in eff.get("dhcpd_config", {})
    assert "site_net" not in eff.get("networks", {})


# ---------------------------------------------------------------------------
# Original tests
# ---------------------------------------------------------------------------


def test_fold_then_device_overlay_then_vars_last():
    gt = {"vars": {"GW": "10.0.0.1"}, "ip_configs": {"corp": {"ip": "{{GW}}"}},
          "port_config": {"ge-0/0/0": {"networks": ["corp"]}}}
    st = None
    ss = {}
    device = {"port_config": {"ge-0/0/1": {"networks": ["guest"]}}}
    eff = compile_gateway_device(gt, st, ss, device)
    # device port added (DICT_MERGE), template port kept (not wiped)
    assert set(eff["port_config"]) == {"ge-0/0/0", "ge-0/0/1"}
    # vars resolved LAST, after the device overlay
    assert eff["ip_configs"]["corp"]["ip"] == "10.0.0.1"


def test_sitetemplate_one_port_does_not_wipe_template_ports():
    gt = {"port_config": {"a": {"networks": ["x"]}, "b": {"networks": ["y"]}}}
    st = {"port_config": {"a": {"networks": ["z"]}}}
    eff = compile_gateway_device(gt, st, {}, {})
    assert set(eff["port_config"]) == {"a", "b"}      # DICT_MERGE, b survives
    assert eff["port_config"]["a"]["networks"] == ["z"]  # sitetemplate wins for a


def test_gateway_with_no_port_config_does_not_crash():
    eff = compile_gateway_device({"ip_configs": {"corp": {"ip": "10.0.0.1"}}}, None, {}, {})
    assert "port_config" not in eff or eff["port_config"] == {}
    assert eff["ip_configs"]["corp"]["ip"] == "10.0.0.1"
