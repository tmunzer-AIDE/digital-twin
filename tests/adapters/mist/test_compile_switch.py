from digital_twin.adapters.mist.compile.switch import compile_device, compile_site, merge_only


def test_compile_site_merges_then_resolves_vars():
    tpl = {"networks": {"corp": {"vlan_id": "{{corp_vlan}}"}}}
    setting = {"vars": {"corp_vlan": "30"}, "port_usages": {"office": {"mode": "access"}}}
    eff = compile_site(tpl, setting)
    assert eff["networks"]["corp"]["vlan_id"] == "30"
    assert eff["port_usages"]["office"]["mode"] == "access"


def test_merge_only_keeps_vars_unresolved_for_the_oracle():
    # getSiteSettingDerived does NOT resolve vars (confirmed) — the Tier-2 gate
    # compares this artifact, with {{...}} intact.
    tpl = {"networks": {"corp": {"vlan_id": "{{corp_vlan}}"}}}
    merged = merge_only(tpl, {"vars": {"corp_vlan": "30"}})
    assert merged["networks"]["corp"]["vlan_id"] == "{{corp_vlan}}"


def test_compile_site_without_vars_skips_resolution():
    eff = compile_site(None, {"networks": {"corp": {"vlan_id": 30}}})
    assert eff["networks"]["corp"]["vlan_id"] == 30


def test_compile_device_layers_overrides_then_resolves_site_vars():
    # device-level config can reference SITE vars; resolution happens ONCE,
    # after the device overlay (devices have no vars of their own).
    tpl = {"networks": {"corp": {"vlan_id": "{{corp_vlan}}"}}}
    setting = {
        "vars": {"corp_vlan": "30", "lab_vlan": "99"},
        "port_usages": {"office": {"mode": "access", "port_network": "corp"}},
    }
    device = {
        "mac": "aabbcc001122",
        "port_config": {"ge-0/0/1": {"usage": "office", "description": "desk-{{corp_vlan}}"}},
        "networks": {"lab": {"vlan_id": "{{lab_vlan}}"}},
    }
    dev_eff = compile_device(tpl, setting, device)
    assert dev_eff["networks"]["corp"]["vlan_id"] == "30"  # site leaf resolved
    assert dev_eff["networks"]["lab"]["vlan_id"] == "99"  # device leaf resolved w/ site vars
    assert dev_eff["port_config"]["ge-0/0/1"]["description"] == "desk-30"


def test_compile_device_does_not_mutate_inputs():
    setting = {"networks": {"corp": {"vlan_id": 30}}}
    device = {"networks": {"lab": {"vlan_id": 99}}}
    compile_device(None, setting, device)
    assert "lab" not in setting["networks"]


def test_compile_device_merges_port_config_per_key_not_wholesale():
    # a device overriding ONE port range must not wipe the inherited assignments
    setting = {"port_config": {"ge-0/0/0": {"usage": "a"}, "ge-0/0/1": {"usage": "b"}}}
    device = {"port_config": {"ge-0/0/1": {"usage": "c"}}}
    eff = compile_device(None, setting, device)
    assert eff["port_config"]["ge-0/0/0"] == {"usage": "a"}  # inherited key survives
    assert eff["port_config"]["ge-0/0/1"] == {"usage": "c"}  # device wins its key


def test_compile_device_applies_switch_matching_base():
    # a matched rule's port_config becomes the BASE; the device's own port_config
    # is layered on top (both ports present in the effective config)
    tpl = {
        "switch_matching": {
            "enable": True,
            "rules": [
                {"match_model[0:6]": "EX4100", "port_config": {"ge-0/0/44": {"usage": "iot"}}}
            ],
        }
    }
    device = {"model": "EX4100-48MP", "port_config": {"ge-0/0/0": {"usage": "office"}}}
    eff = compile_device(tpl, {}, device)
    assert eff["port_config"]["ge-0/0/44"]["usage"] == "iot"  # from the rule
    assert eff["port_config"]["ge-0/0/0"]["usage"] == "office"  # from the device


def test_compile_device_port_overrides_switch_matching_rule_per_port():
    tpl = {
        "switch_matching": {
            "enable": True,
            "rules": [
                {"match_model": "EX4100-48MP", "port_config": {"ge-0/0/5": {"usage": "rule"}}}
            ],
        }
    }
    device = {"model": "EX4100-48MP", "port_config": {"ge-0/0/5": {"usage": "device"}}}
    eff = compile_device(tpl, {}, device)
    assert eff["port_config"]["ge-0/0/5"]["usage"] == "device"  # device wins


def test_compile_device_ignores_switch_matching_when_disabled():
    tpl = {
        "switch_matching": {
            "enable": False,
            "rules": [
                {"match_model": "EX4100-48MP", "port_config": {"ge-0/0/9": {"usage": "iot"}}}
            ],
        }
    }
    eff = compile_device(tpl, {}, {"model": "EX4100-48MP"})
    assert "ge-0/0/9" not in (eff.get("port_config") or {})


def test_compile_device_carries_local_and_overwrite_maps():
    # old code dropped these device fields entirely -> the resolver never saw them
    device = {
        "port_config": {"ge-0/0/0": {"usage": "office"}},
        "local_port_config": {"ge-0/0/0": {"usage": "uplink"}},
        "port_config_overwrite": {"ge-0/0/0": {"port_network": "voice"}},
    }
    eff = compile_device(None, {}, device)
    assert eff["local_port_config"]["ge-0/0/0"]["usage"] == "uplink"
    assert eff["port_config_overwrite"]["ge-0/0/0"]["port_network"] == "voice"


def test_compile_device_carries_device_level_ospf():
    site = {"networks": {"corp": {"vlan_id": 10}}}
    device = {
        "ospf_config": {"enabled": True},
        "ospf_areas": {"0": {"networks": {"corp": {}}}},
    }
    out = compile_device(None, site, device)
    assert out["ospf_config"] == {"enabled": True}
    assert out["ospf_areas"] == {"0": {"networks": {"corp": {}}}}
