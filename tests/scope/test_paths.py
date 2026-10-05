from digital_twin.scope.allowlist import (
    EFFECTIVE_ALLOWLIST,
    GATEWAY_EFFECTIVE_ALLOWLIST,
    RAW_ALLOWLIST,
)
from digital_twin.scope.paths import allowed_tokens, changed_leaf_paths, leaf_changes


def test_single_star_matches_exactly_one_segment():
    # '*' matches exactly one segment — it must NOT cross nesting levels.
    assert allowed_tokens(("networks", "corp", "vlan_id"), ("networks.*.vlan_id",))
    assert not allowed_tokens(("networks", "corp", "isolation"), ("networks.*.vlan_id",))
    # C1 regression: '*' must NOT over-match deeper-nested paths.
    # 'dhcpd_config.*.type' must only match one level of nesting under dhcpd_config,
    # NOT 'dhcpd_config.corp.options.43.type' (three levels deep).
    assert not allowed_tokens(("networks", "corp", "sub", "vlan_id"), ("networks.*.vlan_id",))


def test_double_star_matches_one_original_map_key():
    # The legacy '**' spelling consumes one original JSON map key.
    # A literal IP address remains one token, never multiple nesting levels.
    assert allowed_tokens(
        ("bgp_config", "underlay", "neighbors", "10.0.0.2", "neighbor_as"),
        ("bgp_config.*.neighbors.**.neighbor_as",),
    )
    assert allowed_tokens(
        ("bgp_config", "underlay", "neighbors", "10.0.0.2", "disabled"),
        ("bgp_config.*.neighbors.**.disabled",),
    )
    # '**' must NOT match zero keys.
    assert not allowed_tokens(
        ("bgp_config", "underlay", "neighbors", "neighbor_as"),
        ("bgp_config.*.neighbors.**.neighbor_as",),
    )
    # '**' must not allow unrelated trailing leaves.
    assert not allowed_tokens(
        ("bgp_config", "underlay", "neighbors", "10.0.0.2", "auth_key"),
        ("bgp_config.*.neighbors.**.neighbor_as",),
    )
    assert not allowed_tokens(
        ("bgp_config", "underlay", "neighbors", "10.0.0.2", "nested", "neighbor_as"),
        ("bgp_config.*.neighbors.**.neighbor_as",),
    )


def test_trailing_star_matches_whole_subtree_including_root():
    assert allowed_tokens(("vars",), ("vars.*",))
    assert allowed_tokens(("vars", "x"), ("vars.*",))
    assert allowed_tokens(("vars", "x", "y"), ("vars.*",))
    assert not allowed_tokens(("varsx",), ("vars.*",))


def test_bare_entry_matches_exactly():
    assert allowed_tokens(("name",), ("name",))
    assert not allowed_tokens(("name", "sub"), ("name",))


def test_added_subtree_descends_to_leaves():
    # adding a whole network surfaces its LEAVES, so each gates individually
    cur = {"networks": {"corp": {"vlan_id": 10}}}
    new = {"networks": {"corp": {"vlan_id": 10}, "lab": {"vlan_id": 99, "isolation": True}}}
    assert changed_leaf_paths(cur, new) == ("networks.lab.isolation", "networks.lab.vlan_id")


def test_removed_subtree_descends_to_leaves():
    cur = {"dhcpd_config": {"corp": {"ip": "10.0.0.2"}}}
    assert changed_leaf_paths(cur, {}) == ("dhcpd_config.corp.ip",)


def test_null_and_absent_are_equivalent():
    # Mist PUT semantics (and the compile-equivalence canon): sending null and
    # omitting the key are the same statement — not a change. Matters for
    # payloads derived from REDACTED fixtures, where secrets are nulled out.
    cur = {"radius_config": {"secret": None, "port": 1812}, "x": None}
    new = {"radius_config": {"port": 1812}}
    assert changed_leaf_paths(cur, new) == ()
    assert changed_leaf_paths(new, cur) == ()  # symmetric


def test_null_absent_equivalence_applies_inside_lists():
    # lists compare atomically, so the rule must hold DEEPLY: a nulled secret
    # inside a list element (auth_servers[]) is not a change when omitted
    cur = {"radius_config": {"auth_servers": [{"host": "h", "secret": None}]}}
    new = {"radius_config": {"auth_servers": [{"host": "h"}]}}
    assert changed_leaf_paths(cur, new) == ()


def test_allowed_checks_any_entry():
    allowlist = ("networks.*.vlan_id", "vars.*")
    assert allowed_tokens(("networks", "corp", "vlan_id"), allowlist)
    assert allowed_tokens(("vars", "dhcp_ip"), allowlist)
    assert not allowed_tokens(("networks", "corp", "isolation"), allowlist)


def test_c1_overmatch_regression_gatewaytemplate():
    """C1 regression: paths that were wrongly allowed by the old greedy '*' must now
    be denied.  Under greedy '*', 'dhcpd_config.*.type' matched
    'dhcpd_config.corp.options.43.type' (3 nesting levels); under '*' = exactly one
    segment it does not.  Same for vendor_encapsulated and port_config.*.disabled."""
    # dhcpd_config.<scope>.options.<n>.type — was wrongly SAFE, must be UNKNOWN
    assert not allowed_tokens(
        ("dhcpd_config", "corp", "options", "43", "type"), RAW_ALLOWLIST["gatewaytemplate"]
    )
    assert not allowed_tokens(
        ("dhcpd_config", "corp", "options", "43", "type"), GATEWAY_EFFECTIVE_ALLOWLIST
    )
    # dhcpd_config.<scope>.vendor_encapsulated.<n>.type — same shape
    assert not allowed_tokens(
        ("dhcpd_config", "corp", "vendor_encapsulated", "1", "type"),
        RAW_ALLOWLIST["gatewaytemplate"],
    )
    assert not allowed_tokens(
        ("dhcpd_config", "corp", "vendor_encapsulated", "1", "type"), GATEWAY_EFFECTIVE_ALLOWLIST
    )
    # port_config.<port>.wan_source_nat.disabled — was wrongly SAFE, must be UNKNOWN
    assert not allowed_tokens(
        ("port_config", "ge-0/0/0", "wan_source_nat", "disabled"), RAW_ALLOWLIST["gatewaytemplate"]
    )
    assert not allowed_tokens(
        ("port_config", "ge-0/0/0", "wan_source_nat", "disabled"), GATEWAY_EFFECTIVE_ALLOWLIST
    )


def test_bgp_denied_leaves_not_overmatched():
    # Guard against '**' silently allowing a DENIED BGP leaf.
    # 'bgp_config.*.neighbors.**.neighbor_as' IS allowed; these adjacent paths
    # with structurally similar prefixes or SAME trailing leaf names are NOT.

    # bgp_config.<vrf>.networks is NOT a modeled leaf (advertised-prefix list,
    # explicitly kept out of _BGP_LEAVES to avoid false-SAFE).
    assert not allowed_tokens(("bgp_config", "underlay", "networks"), EFFECTIVE_ALLOWLIST)

    # bgp_config.<vrf>.auth_key is a secret — explicitly denied
    assert not allowed_tokens(("bgp_config", "underlay", "auth_key"), EFFECTIVE_ALLOWLIST)

    # import_policy is not a modeled leaf — denied even though it sits under
    # the neighbors subtree that the allowed 'neighbors.**.neighbor_as' touches
    assert not allowed_tokens(
        ("bgp_config", "underlay", "neighbors", "10.0.0.2", "import_policy"), EFFECTIVE_ALLOWLIST
    )

    # auth_key on a neighbor is also denied (peer-level secret, not neighbor_as)
    assert not allowed_tokens(
        ("bgp_config", "underlay", "neighbors", "10.0.0.2", "auth_key"), EFFECTIVE_ALLOWLIST
    )

    # Positive cases: the modeled BGP leaves ARE allowed
    assert allowed_tokens(
        ("bgp_config", "underlay", "neighbors", "10.0.0.2", "neighbor_as"), EFFECTIVE_ALLOWLIST
    )
    assert allowed_tokens(("bgp_config", "underlay", "local_as"), EFFECTIVE_ALLOWLIST)
    assert allowed_tokens(("bgp_config", "underlay", "type"), EFFECTIVE_ALLOWLIST)
    assert allowed_tokens(
        ("bgp_config", "underlay", "neighbors", "10.0.0.2", "disabled"), EFFECTIVE_ALLOWLIST
    )

    # Dotless key baseline: simple one-segment key still works
    assert allowed_tokens(("networks", "corp", "vlan_id"), ("networks.*.vlan_id",))
    assert not allowed_tokens(("networks", "corp", "isolation"), ("networks.*.vlan_id",))


def test_leaf_changes_added_removed_changed():
    cur = {"a": 1, "b": 2, "d": {"x": 1}}
    new = {"a": 1, "b": 3, "c": 9, "d": {}}
    by = {d.path: d for d in leaf_changes(cur, new)}
    assert by["b"].kind == "changed" and by["b"].before == 2 and by["b"].after == 3
    assert by["c"].kind == "added" and by["c"].before is None and by["c"].after == 9
    assert by["d.x"].kind == "removed" and by["d.x"].before == 1 and by["d.x"].after is None


def test_leaf_changes_list_is_atomic():
    by = {d.path: d for d in leaf_changes({"t": [1, 2]}, {"t": [1, 2, 3]})}
    assert set(by) == {"t"}
    assert by["t"].before == [1, 2] and by["t"].after == [1, 2, 3]


def test_leaf_changes_null_equals_absent():
    assert leaf_changes({"a": None}, {}) == ()
    assert leaf_changes({}, {"a": None}) == ()


def test_leaf_changes_ignore_top():
    paths = [
        d.path for d in leaf_changes({"meta": 1, "a": 1}, {"meta": 2, "a": 2}, ignore_top=("meta",))
    ]
    assert paths == ["a"]


def test_changed_leaf_paths_parity_with_leaf_changes():
    cur = {"a": 1, "b": {"x": 2}, "c": [1]}
    new = {"a": 9, "b": {"x": 2, "y": 3}, "c": [1, 2]}
    assert changed_leaf_paths(cur, new) == tuple(d.path for d in leaf_changes(cur, new))
    assert changed_leaf_paths(cur, new) == ("a", "b.y", "c")  # sorted, unchanged behavior


def test_added_and_removed_empty_objects_are_visible_to_the_gate():
    assert changed_leaf_paths({}, {"routing_policies": {}}) == ("routing_policies",)
    assert changed_leaf_paths({"evpn_options": {}}, {}) == ("evpn_options",)
    assert changed_leaf_paths({}, {"matching": {"future_filter": {}}}) == (
        "matching.future_filter",
    )
    assert changed_leaf_paths({"routing_policies": {}}, {"routing_policies": {}}) == ()


def test_json_boolean_number_changes_are_not_lost_inside_atomic_lists():
    assert changed_leaf_paths({"enabled": True}, {"enabled": 1}) == ("enabled",)
    assert changed_leaf_paths({"rules": [{"enabled": False}]}, {"rules": [{"enabled": 0}]}) == (
        "rules",
    )
    assert changed_leaf_paths({"metric": 1}, {"metric": 1.0}) == ()


def test_new_or_removed_null_only_object_retains_structural_presence():
    assert changed_leaf_paths({}, {"routing_policies": {"option": None}}) == ("routing_policies",)
    assert changed_leaf_paths({"routing_policies": {"option": None}}, {}) == ("routing_policies",)
