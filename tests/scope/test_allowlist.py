from digital_twin.scope.allowlist import (
    EFFECTIVE_ALLOWLIST,
    IGNORED_RAW_FIELDS,
    RAW_ALLOWLIST,
    SUPPORTED_OBJECT_TYPES,
)
from digital_twin.scope.paths import allowed


def test_supported_object_types_are_the_m1_pair():
    assert SUPPORTED_OBJECT_TYPES == ("site_setting", "device")


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
        # unmodeled leaves stay DENIED (GS27 owns them; deny prevents false-SAFE)
        assert not allowed("ospf_areas.0.networks.corp.metric", al)
        assert not allowed("ospf_areas.0.type", al)
        assert not allowed("ospf_areas.0.networks.corp.auth_password", al)
        assert not allowed("ospf_areas.0.networks.corp.interface_type", al)
