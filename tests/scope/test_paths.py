from digital_twin.scope.allowlist import (
    EFFECTIVE_ALLOWLIST,
    GATEWAY_EFFECTIVE_ALLOWLIST,
    RAW_ALLOWLIST,
)
from digital_twin.scope.paths import allowed, changed_leaf_paths, matches


def test_single_star_matches_exactly_one_segment():
    # '*' matches exactly one segment — it must NOT cross nesting levels.
    assert matches("networks.corp.vlan_id", "networks.*.vlan_id")
    assert not matches("networks.corp.isolation", "networks.*.vlan_id")
    # C1 regression: '*' must NOT over-match deeper-nested paths.
    # 'dhcpd_config.*.type' must only match one level of nesting under dhcpd_config,
    # NOT 'dhcpd_config.corp.options.43.type' (three levels deep).
    assert not matches("networks.corp.sub.vlan_id", "networks.*.vlan_id")


def test_double_star_matches_one_or_more_segments():
    # '**' is the one-or-more wildcard, used ONLY at the BGP neighbor-IP position.
    # IP-address keys contain literal dots: 'bgp_config.underlay.neighbors.10.0.0.2.neighbor_as'
    # is assembled from the key '10.0.0.2' — '**' must consume 1+ segments.
    assert matches(
        "bgp_config.underlay.neighbors.10.0.0.2.neighbor_as",
        "bgp_config.*.neighbors.**.neighbor_as",
    )
    assert matches(
        "bgp_config.underlay.neighbors.10.0.0.2.disabled",
        "bgp_config.*.neighbors.**.disabled",
    )
    # '**' must NOT match zero segments.
    assert not matches(
        "bgp_config.underlay.neighbors.neighbor_as",
        "bgp_config.*.neighbors.**.neighbor_as",
    )
    # '**' must not allow unrelated trailing leaves.
    assert not matches(
        "bgp_config.underlay.neighbors.10.0.0.2.auth_key",
        "bgp_config.*.neighbors.**.neighbor_as",
    )


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
    assert allowed("networks.corp.vlan_id", allowlist)
    assert allowed("vars.dhcp_ip", allowlist)
    assert not allowed("networks.corp.isolation", allowlist)


def test_c1_overmatch_regression_gatewaytemplate():
    """C1 regression: paths that were wrongly allowed by the old greedy '*' must now
    be denied.  Under greedy '*', 'dhcpd_config.*.type' matched
    'dhcpd_config.corp.options.43.type' (3 nesting levels); under '*' = exactly one
    segment it does not.  Same for vendor_encapsulated and port_config.*.disabled."""
    # dhcpd_config.<scope>.options.<n>.type — was wrongly SAFE, must be UNKNOWN
    assert not allowed("dhcpd_config.corp.options.43.type", RAW_ALLOWLIST["gatewaytemplate"])
    assert not allowed("dhcpd_config.corp.options.43.type", GATEWAY_EFFECTIVE_ALLOWLIST)
    # dhcpd_config.<scope>.vendor_encapsulated.<n>.type — same shape
    assert not allowed(
        "dhcpd_config.corp.vendor_encapsulated.1.type", RAW_ALLOWLIST["gatewaytemplate"]
    )
    assert not allowed(
        "dhcpd_config.corp.vendor_encapsulated.1.type", GATEWAY_EFFECTIVE_ALLOWLIST
    )
    # port_config.<port>.wan_source_nat.disabled — was wrongly SAFE, must be UNKNOWN
    assert not allowed(
        "port_config.ge-0/0/0.wan_source_nat.disabled", RAW_ALLOWLIST["gatewaytemplate"]
    )
    assert not allowed(
        "port_config.ge-0/0/0.wan_source_nat.disabled", GATEWAY_EFFECTIVE_ALLOWLIST
    )


def test_bgp_denied_leaves_not_overmatched():
    # Guard against '**' silently allowing a DENIED BGP leaf.
    # 'bgp_config.*.neighbors.**.neighbor_as' IS allowed; these adjacent paths
    # with structurally similar prefixes or SAME trailing leaf names are NOT.

    # bgp_config.<vrf>.networks is NOT a modeled leaf (advertised-prefix list,
    # explicitly kept out of _BGP_LEAVES to avoid false-SAFE).
    assert not allowed("bgp_config.underlay.networks", EFFECTIVE_ALLOWLIST)

    # bgp_config.<vrf>.auth_key is a secret — explicitly denied
    assert not allowed("bgp_config.underlay.auth_key", EFFECTIVE_ALLOWLIST)

    # import_policy is not a modeled leaf — denied even though it sits under
    # the neighbors subtree that the allowed 'neighbors.**.neighbor_as' touches
    assert not allowed(
        "bgp_config.underlay.neighbors.10.0.0.2.import_policy", EFFECTIVE_ALLOWLIST
    )

    # auth_key on a neighbor is also denied (peer-level secret, not neighbor_as)
    assert not allowed(
        "bgp_config.underlay.neighbors.10.0.0.2.auth_key", EFFECTIVE_ALLOWLIST
    )

    # Positive cases: the modeled BGP leaves ARE allowed
    assert allowed("bgp_config.underlay.neighbors.10.0.0.2.neighbor_as", EFFECTIVE_ALLOWLIST)
    assert allowed("bgp_config.underlay.local_as", EFFECTIVE_ALLOWLIST)
    assert allowed("bgp_config.underlay.type", EFFECTIVE_ALLOWLIST)
    assert allowed("bgp_config.underlay.neighbors.10.0.0.2.disabled", EFFECTIVE_ALLOWLIST)

    # Dotless key baseline: simple one-segment key still works
    assert matches("networks.corp.vlan_id", "networks.*.vlan_id")
    assert not matches("networks.corp.isolation", "networks.*.vlan_id")
