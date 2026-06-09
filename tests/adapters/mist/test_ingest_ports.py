from digital_twin.adapters.mist.ingest.ports import expand_port_members, usage_vlans
from tests.adapters.mist.fixtures import SITE_EFFECTIVE


def test_expand_single_port():
    assert expand_port_members("ge-0/0/47") == ["ge-0/0/47"]


def test_expand_trailing_range():
    assert expand_port_members("ge-0/0/0-2") == ["ge-0/0/0", "ge-0/0/1", "ge-0/0/2"]


def test_expand_comma_list_mixed():
    assert expand_port_members("ge-0/0/0,ge-0/0/5-6") == ["ge-0/0/0", "ge-0/0/5", "ge-0/0/6"]


def test_usage_vlans_access():
    native, tagged = usage_vlans(
        SITE_EFFECTIVE["port_usages"]["office"], SITE_EFFECTIVE["networks"]
    )
    assert native == 10 and tagged == ()


def test_usage_vlans_trunk_with_named_networks():
    native, tagged = usage_vlans(
        SITE_EFFECTIVE["port_usages"]["uplink"], SITE_EFFECTIVE["networks"]
    )
    assert native == 10 and tagged == (30,)


def test_usage_vlans_trunk_all_networks():
    native, tagged = usage_vlans(SITE_EFFECTIVE["port_usages"]["all"], SITE_EFFECTIVE["networks"])
    assert native is None and set(tagged) == {10, 30}


def test_native_is_excluded_from_tagged_with_all_networks():
    # the native network is carried UNTAGGED — it must not also appear tagged
    usage = {"mode": "trunk", "all_networks": True, "port_network": "corp"}
    native, tagged = usage_vlans(usage, SITE_EFFECTIVE["networks"])
    assert native == 10 and tagged == (30,)


def test_native_is_excluded_from_tagged_with_named_networks():
    usage = {"mode": "trunk", "port_network": "corp", "networks": ["corp", "voice"]}
    native, tagged = usage_vlans(usage, SITE_EFFECTIVE["networks"])
    assert native == 10 and tagged == (30,)
