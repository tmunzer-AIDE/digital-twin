from digital_twin.adapters.mist.compile.fold import MergePolicy
from digital_twin.adapters.mist.compile.merge import SWITCH_POLICY, merge_site_effective


def test_site_scalar_overrides_template():
    tpl = {"ospf_areas": {"0": {"x": 1}}, "mtu": 1500}
    site = {"mtu": 9000}
    out = merge_site_effective(tpl, site)
    assert out["mtu"] == 9000
    assert out["ospf_areas"] == {"0": {"x": 1}}  # template-only survives


def test_dict_merge_fields_merge_per_key_site_wins():
    tpl = {"networks": {"corp": {"vlan_id": 10}, "voice": {"vlan_id": 20}}}
    site = {"networks": {"voice": {"vlan_id": 21}, "guest": {"vlan_id": 30}}}
    out = merge_site_effective(tpl, site)
    assert out["networks"] == {
        "corp": {"vlan_id": 10},
        "voice": {"vlan_id": 21},  # site wins per key
        "guest": {"vlan_id": 30},
    }


def test_dict_merge_applies_to_port_usages_and_vars_too():
    tpl = {"port_usages": {"ap": {"mode": "trunk"}}, "vars": {"a": "1"}}
    site = {"port_usages": {"office": {"mode": "access"}}, "vars": {"b": "2"}}
    out = merge_site_effective(tpl, site)
    assert set(out["port_usages"]) == {"ap", "office"}
    assert out["vars"] == {"a": "1", "b": "2"}


def test_replace_fields_replace_wholesale():
    # dhcp_snooping is REPLACE policy: site value replaces the whole object
    tpl = {"dhcp_snooping": {"enabled": True, "networks": ["corp"]}}
    site = {"dhcp_snooping": {"enabled": False}}
    out = merge_site_effective(tpl, site)
    assert out["dhcp_snooping"] == {"enabled": False}


def test_none_template_means_site_only():
    assert merge_site_effective(None, {"mtu": 1}) == {"mtu": 1}


def test_inputs_are_not_mutated():
    tpl = {"networks": {"corp": {"vlan_id": 10}}}
    site = {"networks": {"corp": {"vlan_id": 11}}}
    merge_site_effective(tpl, site)
    assert tpl["networks"]["corp"]["vlan_id"] == 10
    assert site["networks"]["corp"]["vlan_id"] == 11


def test_policy_table_is_data():
    assert SWITCH_POLICY.get("networks") is MergePolicy.DICT_MERGE
    assert SWITCH_POLICY.get("unknown_future_field", MergePolicy.REPLACE) is MergePolicy.REPLACE
