from digital_twin.adapters.mist.apply.objects import get_object, replace_object
from tests.adapters.mist.fixtures import SWITCH_A, raw_site

RAW = raw_site()  # SWITCH_A (id "dev-a") + AP_1, scope site_id "s1"
SWITCH_A_IP_CONFIGS = SWITCH_A["other_ip_configs"]


def test_get_site_setting_returns_the_setting():
    obj = get_object(RAW, "site_setting", RAW.scope.site_id)
    assert obj is RAW.setting


def test_get_device_by_id():
    obj = get_object(RAW, "device", "dev-a")
    assert obj is not None and obj["mac"] == "aa0000000001"


def test_get_unknown_returns_none():
    assert get_object(RAW, "device", "ghost") is None
    assert get_object(RAW, "site_setting", "not-this-site") is None


def test_update_replaces_present_roots_and_keeps_omitted_roots():
    # Mist PUT semantics (confirmed): a root present in the payload is replaced
    # WHOLESALE; a root omitted from the payload PERSISTS unchanged
    new = replace_object(
        RAW, "site_setting", RAW.scope.site_id, {"networks": {"only": {"vlan_id": 9}}}
    )
    assert new.setting["networks"] == {"only": {"vlan_id": 9}}  # replaced wholesale
    assert new.setting["port_usages"] == RAW.setting["port_usages"]  # omitted -> kept
    assert RAW.setting["networks"] != new.setting["networks"]  # original untouched


def test_dash_marker_deletes_a_root_attribute():
    # deletion is EXPLICIT: {"-attribute_name": ""}
    assert "port_usages" in RAW.setting
    new = replace_object(RAW, "site_setting", RAW.scope.site_id, {"-port_usages": ""})
    assert "port_usages" not in new.setting
    assert "-port_usages" not in new.setting  # the marker itself never lands
    assert new.setting["networks"] == RAW.setting["networks"]  # everything else kept


def test_replace_device_preserves_identity_fields():
    # Mist PUT never lets a payload change server-managed identity; ingest needs mac/type
    new = replace_object(
        RAW, "device", "dev-a", {"name": "renamed", "mac": "evil", "port_config": {}}
    )
    dev = next(d for d in new.devices if d.get("id") == "dev-a")
    assert dev["name"] == "renamed"
    assert dev["mac"] == "aa0000000001"  # identity preserved over payload's attempt
    assert dev["type"] == "switch"
    assert dev["other_ip_configs"] == SWITCH_A_IP_CONFIGS  # omitted root persists


def test_conflicting_set_and_delete_markers_detected():
    from digital_twin.adapters.mist.apply.objects import update_conflicts

    assert update_conflicts({"dhcpd_config": {}, "-dhcpd_config": ""}) == ["dhcpd_config"]
    assert update_conflicts({"networks": {}, "-vars": ""}) == []


def test_replace_does_not_touch_other_devices():
    new = replace_object(RAW, "device", "dev-a", {"name": "x"})
    assert any(d.get("id") == "dev-ap1" and d.get("model") == "AP45" for d in new.devices)
