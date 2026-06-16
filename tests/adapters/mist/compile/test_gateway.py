from digital_twin.adapters.mist.compile.gateway import compile_gateway_device


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
