from digital_twin.scope.allowlist import (
    EFFECTIVE_ALLOWLIST,
    IGNORED_RAW_FIELDS,
    RAW_ALLOWLIST,
    SUPPORTED_OBJECT_TYPES,
)


def test_supported_object_types_are_the_m1_pair():
    assert SUPPORTED_OBJECT_TYPES == ("site_setting", "device")


def test_raw_allowlist_matches_spec_table():
    assert RAW_ALLOWLIST["site_setting"] == ("networks.*", "port_usages.*", "vars.*")
    assert RAW_ALLOWLIST["device"] == (
        "port_config.*",
        "networks.*",
        "port_usages.*",
        "name",
        "notes",
    )


def test_effective_allowlist_covers_what_the_ir_consumes():
    # everything resolve_effective_ports/vlans read, and vars (the allowed input)
    for f in (
        "networks",
        "port_usages",
        "vars",
        "port_config",
        "local_port_config",
        "port_config_overwrite",
    ):
        assert f in EFFECTIVE_ALLOWLIST


def test_server_metadata_is_ignored_in_raw_diffs():
    for f in ("id", "org_id", "site_id", "created_time", "modified_time"):
        assert f in IGNORED_RAW_FIELDS
