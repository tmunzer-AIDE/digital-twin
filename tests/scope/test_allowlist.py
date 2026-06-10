from digital_twin.scope.allowlist import (
    EFFECTIVE_ALLOWLIST,
    IGNORED_RAW_FIELDS,
    RAW_ALLOWLIST,
    SUPPORTED_OBJECT_TYPES,
)


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


def test_effective_allowlist_is_leaf_level():
    assert "networks.*.vlan_id" in EFFECTIVE_ALLOWLIST
    assert "vars.*" in EFFECTIVE_ALLOWLIST
    assert "port_config.*.usage" in EFFECTIVE_ALLOWLIST
    assert "networks.*" not in EFFECTIVE_ALLOWLIST  # subtree entries are gone


def test_server_metadata_is_ignored_in_raw_diffs():
    for f in ("id", "org_id", "site_id", "created_time", "modified_time"):
        assert f in IGNORED_RAW_FIELDS
