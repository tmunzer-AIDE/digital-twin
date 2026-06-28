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
    assert "port_config_overwrite.*.speed" in device  # SP2: resolver-honored + modeled
    assert "port_config_overwrite.*.mac_limit" in device  # SP4: resolver-honored + modeled
    assert "port_config_overwrite.*.poe_keep_state_when_reboot" not in device  # still unmodeled


def test_l1_attrs_in_scope():
    dev = set(RAW_ALLOWLIST["device"])
    for leaf in (
        "port_config.*.speed", "port_config.*.duplex", "port_config.*.disable_autoneg",
        "local_port_config.*.speed", "local_port_config.*.duplex",
        "local_port_config.*.disable_autoneg",
        "port_config_overwrite.*.speed", "port_config_overwrite.*.duplex",
    ):
        assert leaf in dev, leaf


def test_overwrite_has_no_disable_autoneg():
    # OAS: port_config_overwrite carries speed+duplex but NOT disable_autoneg
    assert "port_config_overwrite.*.disable_autoneg" not in set(RAW_ALLOWLIST["device"])


def test_usage_l1_in_scope():
    site = set(RAW_ALLOWLIST["site_setting"])
    eff = set(EFFECTIVE_ALLOWLIST)
    for a in ("speed", "duplex", "disable_autoneg"):
        assert f"port_usages.*.{a}" in site, a
        assert f"port_usages.*.{a}" in eff, a


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


def test_org_object_types_includes_all_fanout_types():
    assert set(ORG_OBJECT_TYPES) == {
        "networktemplate",
        "gatewaytemplate",
        "sitetemplate",
        "wlan",
    }


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


def test_disabled_in_scope_on_overwrite_and_local():
    dev = set(RAW_ALLOWLIST["device"])
    assert "port_config_overwrite.*.disabled" in dev
    assert "local_port_config.*.disabled" in dev
    assert "port_config_overwrite.*.disabled" in set(EFFECTIVE_ALLOWLIST)


def test_disabled_not_in_scope_on_port_config():
    assert "port_config.*.disabled" not in set(RAW_ALLOWLIST["device"])


def test_no_local_overwrite_stays_out_of_scope():
    # a lone no_local_overwrite flip could activate unmodeled local leaves -> UNKNOWN
    assert "port_config.*.no_local_overwrite" not in set(RAW_ALLOWLIST["device"])


def test_local_dynamic_usage_still_out_of_scope():
    # P1 regression: adding `disabled` must NOT reintroduce local dynamic_usage,
    # which PR #14 deliberately narrowed out (it's a port_config-only pointer)
    assert "local_port_config.*.dynamic_usage" not in set(RAW_ALLOWLIST["device"])


def test_auth_usage_leaves_in_scope_everywhere_usages_live():
    from digital_twin.scope.allowlist import EFFECTIVE_ALLOWLIST
    for coll in (RAW_ALLOWLIST["site_setting"], RAW_ALLOWLIST["device"],
                 RAW_ALLOWLIST["networktemplate"], EFFECTIVE_ALLOWLIST):
        s = set(coll)
        for a in ("port_auth", "enable_mac_auth", "dynamic_vlan_networks", "guest_network"):
            assert f"port_usages.*.{a}" in s, a


def test_auth_local_leaves_device_only():
    assert "local_port_config.*.port_auth" in set(RAW_ALLOWLIST["device"])
    # site_setting / networktemplate have NO local_port_config map
    assert "local_port_config.*.port_auth" not in set(RAW_ALLOWLIST["site_setting"])
    assert "local_port_config.*.port_auth" not in set(RAW_ALLOWLIST["networktemplate"])


def test_auth_not_on_port_config_or_overwrite():
    dev = set(RAW_ALLOWLIST["device"])
    assert "port_config.*.port_auth" not in dev
    assert "port_config_overwrite.*.port_auth" not in dev


def test_voip_network_in_scope_usage_and_local_not_port_config():
    site, dev = set(RAW_ALLOWLIST["site_setting"]), set(RAW_ALLOWLIST["device"])
    assert "port_usages.*.voip_network" in site and "port_usages.*.voip_network" in dev
    assert "local_port_config.*.voip_network" in dev
    assert "local_port_config.*.voip_network" not in site
    assert "port_config.*.voip_network" not in dev


def test_mac_limit_in_scope_usage_local_overwrite_not_port_config():
    dev = set(RAW_ALLOWLIST["device"])
    assert "port_usages.*.mac_limit" in dev and "local_port_config.*.mac_limit" in dev
    assert "port_config_overwrite.*.mac_limit" in dev
    assert "port_config.*.mac_limit" not in dev


def test_misc_knobs_in_scope_usage_local_not_port_config():
    dev = set(RAW_ALLOWLIST["device"])
    for a in ("inter_switch_link", "storm_control", "enable_qos"):
        assert f"port_usages.*.{a}" in dev and f"local_port_config.*.{a}" in dev
        assert f"port_config.*.{a}" not in dev
        assert f"port_config_overwrite.*.{a}" not in dev
