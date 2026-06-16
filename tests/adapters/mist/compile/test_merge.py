from digital_twin.adapters.mist.compile.merge import SWITCH_POLICY, merge_site_effective


def test_merge_site_effective_unchanged_without_sitetemplate():
    nt = {"networks": {"corp": {"vlan_id": 10}}}
    ss = {"networks": {"corp": {"vlan_id": 11}}}
    assert merge_site_effective(nt, ss)["networks"]["corp"]["vlan_id"] == 11


def test_merge_site_effective_folds_sitetemplate_between_nt_and_site():
    # sitetemplate sits between networktemplate (base) and site_setting (wins)
    nt = {"networks": {"corp": {"vlan_id": 10}}}
    st = {"networks": {"corp": {"vlan_id": 20}, "guest": {"vlan_id": 30}}}
    ss = {"networks": {"guest": {"vlan_id": 31}}}
    out = merge_site_effective(nt, ss, sitetemplate=st)
    assert out["networks"]["corp"]["vlan_id"] == 20   # from sitetemplate
    assert out["networks"]["guest"]["vlan_id"] == 31   # site_setting wins


def test_switch_policy_dict_merge_fields():
    for f in ("networks", "port_usages", "vars", "dhcpd_config"):
        assert SWITCH_POLICY[f].value == "dict_merge"
