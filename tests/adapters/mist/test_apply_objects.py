from digital_twin.adapters.mist.apply.objects import get_object, replace_object
from tests.adapters.mist.fixtures import raw_site

RAW = raw_site()  # SWITCH_A (id "dev-a") + AP_1, scope site_id "s1"


def test_get_site_setting_returns_the_setting():
    obj = get_object(RAW, "site_setting", RAW.scope.site_id)
    assert obj is RAW.setting


def test_get_device_by_id():
    obj = get_object(RAW, "device", "dev-a")
    assert obj is not None and obj["mac"] == "aa0000000001"


def test_get_unknown_returns_none():
    assert get_object(RAW, "device", "ghost") is None
    assert get_object(RAW, "site_setting", "not-this-site") is None


def test_replace_site_setting_swaps_whole_object():
    new = replace_object(
        RAW, "site_setting", RAW.scope.site_id, {"networks": {"only": {"vlan_id": 9}}}
    )
    assert new.setting["networks"] == {"only": {"vlan_id": 9}}
    assert "port_usages" not in new.setting  # full replacement, not a merge
    assert RAW.setting != new.setting  # original untouched (immutability)


def test_replace_device_preserves_identity_fields():
    # Mist PUT never lets a payload change server-managed identity; ingest needs mac/type
    new = replace_object(
        RAW, "device", "dev-a", {"name": "renamed", "mac": "evil", "port_config": {}}
    )
    dev = next(d for d in new.devices if d.get("id") == "dev-a")
    assert dev["name"] == "renamed"
    assert dev["mac"] == "aa0000000001"  # identity preserved over payload's attempt
    assert dev["type"] == "switch"
    assert "other_ip_configs" not in dev  # non-identity config replaced away


def test_replace_does_not_touch_other_devices():
    new = replace_object(RAW, "device", "dev-a", {"name": "x"})
    assert any(d.get("id") == "dev-ap1" and d.get("model") == "AP45" for d in new.devices)
