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
    return {member: (usage, name) for member, usage, name, _res in resolve_effective_ports(eff)}


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
        port_config={"ge-0/0/7": {"usage": "office", "no_local_overwrite": False}},
        local_port_config={"ge-0/0/7": {"usage": "uplink"}},
    )
    usage, name = _resolved(eff)["ge-0/0/7"]
    assert name == "uplink" and usage_vlans(usage, NETWORKS) == (10, (30,))


def test_local_override_targets_one_member_of_a_range():
    eff = _eff(
        port_config={"ge-0/0/0-3": {"usage": "office", "no_local_overwrite": False}},
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


def test_port_config_overwrite_can_disable_poe():
    # schema-confirmed: port_config_overwrite carries poe_disabled; the IR
    # models PoE now, so the overwrite layer must honor it (else a valid PoE
    # change through overwrite is invisible -> UNKNOWN)
    eff = _eff(
        port_config={"ge-0/0/9": {"usage": "office"}},
        port_config_overwrite={"ge-0/0/9": {"poe_disabled": True}},
    )
    usage, _name = _resolved(eff)["ge-0/0/9"]
    assert usage.get("poe_disabled") is True


def test_resolve_port_bases_merges_local_over_port_config_and_keeps_dynamic_flag():
    eff = {
        "port_config": {
            "ge-0/0/0": {"usage": "office", "dynamic_usage": "dynamic"},
            "ge-0/0/1-2": {"usage": "office", "no_local_overwrite": False},
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


# -- system-defined usages + unresolved names (real-world 2026-06-10) ------------
# Mist ships system-defined port usages (ap/uplink/default/disabled) that appear
# in NO config object — not the template, not the device, not even the derived
# setting. A port referencing one must resolve to the documented semantics
# (marked "system" -> INFERRED confidence downstream); a name with NO definition
# anywhere is "unresolved" (carriage unknown, NEVER silently empty).


def _resolution(eff):
    return {m: (usage, name, res) for m, usage, name, res in resolve_effective_ports(eff)}


def test_system_uplink_resolves_trunk_all_networks():
    eff = _eff(port_usages={}, port_config={"ge-0/0/46": {"usage": "uplink"}})
    usage, name, res = _resolution(eff)["ge-0/0/46"]
    assert res == "system" and name == "uplink"
    assert usage["mode"] == "trunk" and usage["all_networks"] is True


def test_system_disabled_resolves_to_no_vlans():
    eff = _eff(port_config={"ge-0/0/43": {"usage": "disabled"}})
    usage, _, res = _resolution(eff)["ge-0/0/43"]
    assert res == "system"
    assert usage_vlans(usage, NETWORKS) == (None, ())


def test_explicit_definition_wins_over_system():
    eff = _eff(
        port_usages={**USAGES, "uplink2": {"mode": "access", "port_network": "corp"}},
        port_config={"ge-0/0/1": {"usage": "uplink2"}},
    )
    usage, _, res = _resolution(eff)["ge-0/0/1"]
    assert res == "explicit" and usage["mode"] == "access"


def test_unknown_usage_name_is_unresolved():
    eff = _eff(port_config={"ge-0/0/2": {"usage": "iot"}})
    usage, name, res = _resolution(eff)["ge-0/0/2"]
    assert res == "unresolved" and name == "iot"
    assert usage_vlans(usage, NETWORKS) == (None, ())


def test_no_system_defined_port_usages_flag_disables_injection():
    eff = _eff(
        port_usages={},
        no_system_defined_port_usages=True,
        port_config={"ge-0/0/46": {"usage": "uplink"}},
    )
    assert _resolution(eff)["ge-0/0/46"][2] == "unresolved"


def test_disabled_system_defined_list_disables_one():
    eff = _eff(
        port_usages={},
        disabled_system_defined_port_usages=["uplink"],
        port_config={"ge-0/0/46": {"usage": "uplink"}, "ge-0/0/43": {"usage": "disabled"}},
    )
    out = _resolution(eff)
    assert out["ge-0/0/46"][2] == "unresolved"
    assert out["ge-0/0/43"][2] == "system"


# -- no_local_overwrite gate (Task 1) -------------------------------------------


def test_local_discarded_when_no_local_overwrite_defaults_true():
    # port_config present, no_local_overwrite absent (OAS default true) -> local DISCARDED
    eff = _eff(
        port_config={"ge-0/0/7": {"usage": "office"}},
        local_port_config={"ge-0/0/7": {"usage": "uplink"}},
    )
    assert _resolved(eff)["ge-0/0/7"][1] == "office"  # local ignored


def test_local_applies_when_no_local_overwrite_false():
    eff = _eff(
        port_config={"ge-0/0/7": {"usage": "office", "no_local_overwrite": False}},
        local_port_config={"ge-0/0/7": {"usage": "uplink"}},
    )
    assert _resolved(eff)["ge-0/0/7"][1] == "uplink"


def test_standalone_local_applies_without_port_config_entry():
    eff = _eff(local_port_config={"ge-0/0/8": {"usage": "uplink"}})
    assert _resolved(eff)["ge-0/0/8"][1] == "uplink"


def test_local_outranks_port_config_overwrite():
    # both set port_network; local is the highest layer -> local wins
    eff = _eff(
        port_config={"ge-0/0/9": {"usage": "office", "no_local_overwrite": False}},
        port_config_overwrite={"ge-0/0/9": {"port_network": "voice"}},
        local_port_config={"ge-0/0/9": {"port_network": "corp"}},
    )
    usage, _name = _resolved(eff)["ge-0/0/9"]
    assert usage["port_network"] == "corp"


def test_overwrite_only_member_is_resolved():
    # a port present ONLY in port_config_overwrite still yields a resolved port
    eff = _eff(port_config_overwrite={"ge-0/0/12": {"port_network": "voice"}})
    res = _resolved(eff)
    assert "ge-0/0/12" in res
    assert res["ge-0/0/12"][0]["port_network"] == "voice"
    assert res["ge-0/0/12"][1] is None  # no usage name resolves


# -- disabled attribute (Task 2) -----------------------------------------------


def test_overwrite_disabled_marks_effective_disabled():
    # the bug-report shape: overwrite-only members carry disabled with no port_config entry
    eff = _eff(port_config_overwrite={"mge-0/0/0-3": {"disabled": True}})
    res = _resolved(eff)
    assert res["mge-0/0/0"][0].get("disabled") is True
    assert res["mge-0/0/3"][0].get("disabled") is True


def test_local_disabled_honored_when_overridable():
    eff = _eff(
        port_config={"ge-0/0/5": {"usage": "office", "no_local_overwrite": False}},
        local_port_config={"ge-0/0/5": {"disabled": True}},
    )
    assert _resolved(eff)["ge-0/0/5"][0].get("disabled") is True


def test_port_config_disabled_is_ignored():
    # disabled is NOT valid on port_config (OAS) -> the resolver must not honor it
    eff = _eff(port_config={"ge-0/0/6": {"usage": "office", "disabled": True}})
    assert _resolved(eff)["ge-0/0/6"][0].get("disabled") is None
