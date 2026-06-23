from digital_twin.scope.allowlist import (
    EFFECTIVE_ALLOWLIST,
    GATEWAY_EFFECTIVE_ALLOWLIST,
    IGNORED_RAW_FIELDS,
    ORG_OBJECT_TYPES,
    RAW_ALLOWLIST,
    SUPPORTED_OBJECT_TYPES,
)
from digital_twin.scope.paths import allowed


def test_supported_object_types_are_the_m1_pair():
    assert SUPPORTED_OBJECT_TYPES == ("site_setting", "device", "wlan")


def test_raw_allowlist_is_leaf_tightened_to_modeled_fields():
    # spec: "named subtrees, LEAF-tightened" — only IR-modeled leaves, never
    # whole networks/port_usages subtrees (which carry isolation/allow_dhcpd/...)
    site = RAW_ALLOWLIST["site_setting"]
    assert "networks.*.vlan_id" in site and "vars.*" in site
    assert "networks.*" not in site and "port_usages.*" not in site
    for attr in ("mode", "port_network", "networks", "all_networks"):
        assert f"port_usages.*.{attr}" in site

    device = RAW_ALLOWLIST["device"]
    assert "name" in device and "notes" in device
    assert "port_config.*.usage" in device and "port_config.*" not in device
    # resolver-modeled override maps (compile/switch + ingest/ports): in scope,
    # leaf-tightened to exactly what the resolver honors
    assert "local_port_config.*.usage" in device
    assert "port_config_overwrite.*.port_network" in device
    assert "port_config_overwrite.*.speed" not in device  # not resolver-honored


def test_effective_allowlist_is_leaf_level():
    assert "networks.*.vlan_id" in EFFECTIVE_ALLOWLIST
    assert "vars.*" in EFFECTIVE_ALLOWLIST
    assert "port_config.*.usage" in EFFECTIVE_ALLOWLIST
    assert "networks.*" not in EFFECTIVE_ALLOWLIST  # subtree entries are gone


def test_server_metadata_is_ignored_in_raw_diffs():
    for f in ("id", "org_id", "site_id", "created_time", "modified_time"):
        assert f in IGNORED_RAW_FIELDS


def test_ospf_allowlist_is_leaf_tightened():
    for al in (RAW_ALLOWLIST["device"], RAW_ALLOWLIST["site_setting"], EFFECTIVE_ALLOWLIST):
        # modeled + acted-on leaves are in scope
        assert allowed("ospf_config.enabled", al)
        assert allowed("ospf_areas.0.networks.corp.passive", al)
        assert allowed("ospf_areas.0.networks.corp.metric", al)  # GS27-T1
        # unmodeled leaves stay DENIED (deny prevents false-SAFE)
        assert not allowed("ospf_areas.0.type", al)
        assert not allowed("ospf_areas.0.networks.corp.auth_password", al)
        assert not allowed("ospf_areas.0.networks.corp.interface_type", al)


def test_networktemplate_allowlist_equals_site_setting_exactly():
    from digital_twin.scope.allowlist import RAW_ALLOWLIST
    assert RAW_ALLOWLIST["networktemplate"] == RAW_ALLOWLIST["site_setting"]


def test_org_object_types_includes_all_three():
    assert set(ORG_OBJECT_TYPES) == {"networktemplate", "gatewaytemplate", "sitetemplate"}


def test_gatewaytemplate_raw_allowlist_is_modeled_leaves_only():
    gw = set(RAW_ALLOWLIST["gatewaytemplate"])
    assert "port_config.*.disabled" in gw and "ip_configs.*.ip" in gw
    assert "vars.*" in gw                        # a vars edit must pass the RAW field
    # gate so the derived gate can evaluate the ripple (mirrors site_setting)
    assert "port_config.*.usage" not in gw      # inert -> excluded
    assert "networks.*.vlan_id" not in gw       # org-namespace -> excluded


def test_sitetemplate_raw_allowlist_is_union():
    st = set(RAW_ALLOWLIST["sitetemplate"])
    assert set(RAW_ALLOWLIST["site_setting"]).issubset(st)        # switch/site surface
    assert "ip_configs.*.ip" in st                                # + gateway leaves


def test_gateway_effective_allowlist_includes_disabled_ip_and_vars():
    gw = set(GATEWAY_EFFECTIVE_ALLOWLIST)
    assert {"port_config.*.disabled", "ip_configs.*.ip", "vars.*"} <= gw
    assert "port_config.*.disabled" not in set(EFFECTIVE_ALLOWLIST)  # switch lacks it
