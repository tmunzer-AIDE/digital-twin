from digital_twin.adapters.mist.ingest.ports import (
    expand_port_members,
    resolve_effective_ports,
    resolve_port_bases,
    usage_vlans,
)
from tests.adapters.mist.fixtures import SITE_EFFECTIVE

NETWORKS = SITE_EFFECTIVE["networks"]  # corp=10, voice=30
USAGES = SITE_EFFECTIVE["port_usages"]  # office: access/corp; uplink: trunk/corp+voice


def _eff(**kw):
    return {"networks": NETWORKS, "port_usages": USAGES, **kw}


def _resolved(eff):
    return {member: (usage, name) for member, usage, name in resolve_effective_ports(eff)}


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


# -- resolve_effective_ports: per-port override layering (P1 #2) ----------------


def test_resolve_assigns_usage_to_each_range_member():
    r = _resolved(_eff(port_config={"ge-0/0/0-2": {"usage": "office"}}))
    assert set(r) == {"ge-0/0/0", "ge-0/0/1", "ge-0/0/2"}
    usage, name = r["ge-0/0/1"]
    assert name == "office" and usage_vlans(usage, NETWORKS) == (10, ())


def test_inline_port_network_overrides_the_usage_vlan():
    # office usage carries corp(10); the port pins port_network=voice(30) inline
    eff = _eff(port_config={"ge-0/0/5": {"usage": "office", "port_network": "voice"}})
    usage, _ = _resolved(eff)["ge-0/0/5"]
    assert usage_vlans(usage, NETWORKS) == (30, ())


def test_local_port_config_reassigns_usage():
    eff = _eff(
        port_config={"ge-0/0/7": {"usage": "office"}},
        local_port_config={"ge-0/0/7": {"usage": "uplink"}},
    )
    usage, name = _resolved(eff)["ge-0/0/7"]
    assert name == "uplink" and usage_vlans(usage, NETWORKS) == (10, (30,))


def test_local_override_targets_one_member_of_a_range():
    eff = _eff(
        port_config={"ge-0/0/0-3": {"usage": "office"}},
        local_port_config={"ge-0/0/2": {"usage": "uplink"}},
    )
    r = _resolved(eff)
    assert r["ge-0/0/0"][1] == "office" and r["ge-0/0/2"][1] == "uplink"


def test_port_config_overwrite_moves_the_access_vlan_without_a_new_usage():
    # the reviewer's case: overwrite port_network -> the port's VLAN changes even
    # though the named profile is untouched. Old code dropped this entirely.
    eff = _eff(
        port_config={"ge-0/0/9": {"usage": "office"}},  # office -> corp(10)
        port_config_overwrite={"ge-0/0/9": {"port_network": "voice"}},
    )
    usage, name = _resolved(eff)["ge-0/0/9"]
    assert name == "office"  # profile name unchanged
    assert usage_vlans(usage, NETWORKS) == (30, ())  # but VLAN is now voice(30)


def test_resolve_port_bases_merges_local_over_port_config_and_keeps_dynamic_flag():
    eff = {
        "port_config": {
            "ge-0/0/0": {"usage": "office", "dynamic_usage": "dynamic"},
            "ge-0/0/1-2": {"usage": "office"},
        },
        "local_port_config": {"ge-0/0/1": {"usage": "uplink"}},
    }
    bases = resolve_port_bases(eff)
    assert bases["ge-0/0/0"]["dynamic_usage"] == "dynamic"  # flag preserved per member
    assert bases["ge-0/0/1"]["usage"] == "uplink"  # local override wins per member
    assert bases["ge-0/0/2"]["usage"] == "office"


def test_port_present_only_in_local_config_still_resolves():
    assert _resolved(_eff(local_port_config={"ge-0/0/11": {"usage": "office"}}))["ge-0/0/11"][
        1
    ] == ("office")
