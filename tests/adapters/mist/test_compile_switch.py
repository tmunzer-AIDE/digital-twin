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
