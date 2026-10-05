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
    # Reboot-time power behavior has no validated model and stays denied.
    assert "port_config_overwrite.*.poe_keep_state_when_reboot" not in device


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
        "wlantemplate",
    }
    assert "wlantemplate" not in set(SUPPORTED_OBJECT_TYPES)


def test_gatewaytemplate_raw_allowlist_is_modeled_leaves_only():
    gw = set(RAW_ALLOWLIST["gatewaytemplate"])
    assert "port_config.*.disabled" in gw and "ip_configs.*.ip" in gw
    assert {"ip_configs.*.type", "ip_configs.*.netmask"} <= gw
    assert {"dhcpd_config.*.dns_servers", "dhcpd_config.*.lease_time"} <= gw
    assert "vars.*" in gw                        # a vars edit must pass the RAW field
    # gate so the derived gate can evaluate the ripple (mirrors site_setting)
    assert "port_config.*.usage" in gw          # gateway WAN redundancy classifier
    assert "networks.*.vlan_id" not in gw       # org-namespace -> excluded


def test_sitetemplate_raw_allowlist_is_union():
    st = set(RAW_ALLOWLIST["sitetemplate"])
    assert set(RAW_ALLOWLIST["site_setting"]).issubset(st)        # switch/site surface
    assert "ip_configs.*.ip" in st                                # + gateway leaves


def test_gateway_effective_allowlist_includes_disabled_ip_and_vars():
    gw = set(GATEWAY_EFFECTIVE_ALLOWLIST)
    assert {"port_config.*.disabled", "ip_configs.*.ip", "vars.*"} <= gw
    assert {"ip_configs.*.type", "ip_configs.*.netmask"} <= gw
    assert {"dhcpd_config.*.dns_servers", "dhcpd_config.*.lease_time"} <= gw
    assert "port_config.*.disabled" not in set(EFFECTIVE_ALLOWLIST)  # switch lacks it


def test_disabled_in_scope_on_overwrite_and_local():
    dev = set(RAW_ALLOWLIST["device"])
    assert "port_config_overwrite.*.disabled" in dev
    assert "local_port_config.*.disabled" in dev
    assert "port_config_overwrite.*.disabled" in set(EFFECTIVE_ALLOWLIST)


def test_disabled_not_in_scope_on_port_config():
    assert "port_config.*.disabled" not in set(RAW_ALLOWLIST["device"])


def test_no_local_overwrite_is_in_scope():
    # no_local_overwrite is modeled (resolve_effective_ports/_overridable). A lone
    # flip activating an UNMODELED local leaf is caught by field_gate's
    # _local_overwrite_ripple, not by blanket-gating the flag itself.
    assert "port_config.*.no_local_overwrite" in set(RAW_ALLOWLIST["device"])


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
    for a in ("inter_switch_link", "storm_control"):
        assert f"port_usages.*.{a}" in dev and f"local_port_config.*.{a}" in dev
        assert f"port_config.*.{a}" not in dev
        assert f"port_config_overwrite.*.{a}" not in dev


def test_only_ui_leaf_is_benign_and_operational_effects_remain_denied():
    from digital_twin.scope.allowlist import (
        DEVICE_PROFILE_OVERRIDABLE_LEAVES_BY_ROLE,
        EFFECTIVE_ALLOWLIST,
        RAW_ALLOWLIST,
    )

    benign = ("port_usages.*.ui_evpntopo_id",)
    operational = (
        "port_usages.*.enable_qos",
        "port_usages.*.poe_keep_state_when_reboot",
        "port_usages.*.server_fail_retry_interval",
    )
    operational_device = (
        "local_port_config.*.enable_qos",
        "port_config_overwrite.*.poe_keep_state_when_reboot",
    )
    for leaf in benign:
        assert leaf in RAW_ALLOWLIST["site_setting"], leaf
    for leaf in benign:
        assert leaf in RAW_ALLOWLIST["device"], leaf
        assert leaf in EFFECTIVE_ALLOWLIST, leaf
        # benign = ignored by IR; a device-profile overriding it changes nothing,
        # so it must NOT taint profiled switches to UNKNOWN (review P1 round 2:
        # the local/overwrite benign leaves must not leak in through
        # _DEVICE_PORT_LEAVES either)
        assert leaf not in DEVICE_PROFILE_OVERRIDABLE_LEAVES_BY_ROLE["switch"], leaf
    for leaf in operational:
        assert leaf not in RAW_ALLOWLIST["site_setting"]
    for leaf in (*operational, *operational_device):
        assert leaf not in RAW_ALLOWLIST["device"]
        assert leaf not in EFFECTIVE_ALLOWLIST


def test_spec1_reviewed_leaves_are_in_all_three_gates():
    from digital_twin.scope.allowlist import (
        DEVICE_PROFILE_OVERRIDABLE_LEAVES_BY_ROLE,
        EFFECTIVE_ALLOWLIST,
        RAW_ALLOWLIST,
    )

    reviewed = (
        "bypass_auth_when_server_down_for_voip", "poe_priority", "community_vlan_id",
        "inter_isolation_network_link", "stp_required", "stp_no_root_port",
        "stp_p2p", "use_vstp",
    )
    for attr in reviewed:
        leaf = f"port_usages.*.{attr}"
        assert leaf in RAW_ALLOWLIST["site_setting"], leaf
        assert leaf in EFFECTIVE_ALLOWLIST, leaf
        # the third gate — absent here, a below-profile edit on a profiled
        # switch would resolve REVIEW/SAFE instead of UNKNOWN (false-SAFE)
        assert leaf in DEVICE_PROFILE_OVERRIDABLE_LEAVES_BY_ROLE["switch"], leaf


def test_spec1_usage_only_leaves_are_not_dead_allowed_on_local():
    # refreshed OAS: these do NOT exist on local_port_config — allowing them
    # there would violate the placement contract (spec: "allowlisted only on
    # the maps documented")
    from digital_twin.scope.allowlist import RAW_ALLOWLIST

    for attr in ("bypass_auth_when_server_down_for_voip", "poe_priority",
                 "community_vlan_id", "inter_isolation_network_link", "stp_required"):
        assert f"local_port_config.*.{attr}" not in RAW_ALLOWLIST["device"], attr


def test_every_device_allowlist_root_reaches_the_compiled_effective():
    """An admitted device leaf the compiler drops never reaches the IR, so the
    change would diff to nothing and resolve SAFE without being simulated."""
    from digital_twin.adapters.mist.compile import switch as compile_switch

    compiled = {
        *compile_switch._DEVICE_DICT_MERGE_FIELDS,
        *compile_switch._DEVICE_OWN_FIELDS,
    }
    inert = {"name", "notes"}
    roots = {path.split(".", 1)[0] for path in RAW_ALLOWLIST["device"]}
    assert roots - inert <= compiled


_BENIGN_DHCP_PATHS = (
    "dhcpd_config.lan.dns_suffix",
    "dhcpd_config.lan.options.15.type",
    "dhcpd_config.lan.options.15.value",
    "dhcpd_config.lan.options.119.type",
    "dhcpd_config.lan.options.119.value",
)


def test_dhcp_naming_leaves_are_benign_on_gateway_and_switch_scope_rows():
    from digital_twin.scope.allowlist import DEVICE_PROFILE_OVERRIDABLE_LEAVES_BY_ROLE
    from digital_twin.scope.paths import allowed

    for leaf in _BENIGN_DHCP_PATHS:
        for object_type in ("gatewaytemplate", "site_setting", "networktemplate", "sitetemplate"):
            assert allowed(leaf, RAW_ALLOWLIST[object_type]), (object_type, leaf)
        assert allowed(leaf, GATEWAY_EFFECTIVE_ALLOWLIST), leaf
        assert allowed(leaf, EFFECTIVE_ALLOWLIST), leaf
        # benign = ignored by the IR: a device profile overriding it changes
        # nothing, so it must not taint profiled devices to UNKNOWN
        for role in ("gateway", "switch"):
            assert not allowed(leaf, DEVICE_PROFILE_OVERRIDABLE_LEAVES_BY_ROLE[role]), (role, leaf)
        # device-level switch dhcpd_config stays unmodeled as a whole
        assert not allowed(leaf, RAW_ALLOWLIST["device"]), leaf


def test_other_dhcp_options_stay_denied():
    from digital_twin.scope.paths import allowed

    for leaf in (
        "dhcpd_config.lan.options",            # an option map: empty-map rule, not the allowlist
        "dhcpd_config.lan.options.15",         # option 15 without type/value leaves
        "dhcpd_config.lan.options.3.value",    # router
        "dhcpd_config.lan.options.6.value",    # DNS servers
        "dhcpd_config.lan.options.42.value",   # NTP servers
        "dhcpd_config.lan.options.43.value",   # vendor-specific (AP/phone discovery)
        "dhcpd_config.lan.options.66.value",   # TFTP server (phone/PXE provisioning)
        "dhcpd_config.lan.options.101.value",  # timezone: clock-driven behaviour
        "dhcpd_config.lan.options.121.value",  # classless static routes
        "dhcpd_config.lan.options.252.value",  # WPAD proxy auto-config
        "dhcpd_config.lan.vendor_encapsulated.1.value",
    ):
        for allowlist in (RAW_ALLOWLIST["gatewaytemplate"], RAW_ALLOWLIST["site_setting"],
                          GATEWAY_EFFECTIVE_ALLOWLIST, EFFECTIVE_ALLOWLIST):
            assert not allowed(leaf, allowlist), leaf
