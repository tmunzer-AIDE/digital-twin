from digital_twin.scope.paths import allowed, changed_leaf_paths, matches


def test_wildcard_matches_exactly_one_segment():
    assert matches("networks.corp.vlan_id", "networks.*.vlan_id")
    assert not matches("networks.corp.isolation", "networks.*.vlan_id")
    assert not matches("networks.corp.sub.vlan_id", "networks.*.vlan_id")


def test_trailing_star_matches_whole_subtree_including_root():
    assert matches("vars", "vars.*")
    assert matches("vars.x", "vars.*")
    assert matches("vars.x.y", "vars.*")
    assert not matches("varsx", "vars.*")


def test_bare_entry_matches_exactly():
    assert matches("name", "name")
    assert not matches("name.sub", "name")


def test_added_subtree_descends_to_leaves():
    # adding a whole network surfaces its LEAVES, so each gates individually
    cur = {"networks": {"corp": {"vlan_id": 10}}}
    new = {"networks": {"corp": {"vlan_id": 10}, "lab": {"vlan_id": 99, "isolation": True}}}
    assert changed_leaf_paths(cur, new) == ("networks.lab.isolation", "networks.lab.vlan_id")


def test_removed_subtree_descends_to_leaves():
    cur = {"dhcpd_config": {"corp": {"ip": "10.0.0.2"}}}
    assert changed_leaf_paths(cur, {}) == ("dhcpd_config.corp.ip",)


def test_allowed_checks_any_entry():
    allowlist = ("networks.*.vlan_id", "vars.*")
    assert allowed("networks.corp.vlan_id", allowlist)
    assert allowed("vars.dhcp_ip", allowlist)
    assert not allowed("networks.corp.isolation", allowlist)
