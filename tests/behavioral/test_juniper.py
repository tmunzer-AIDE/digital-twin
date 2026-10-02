"""Documentation counterexamples, not independent live-device calibration."""

from dataclasses import replace

import pytest

from digital_twin.behavioral import (
    Domain,
    Match,
    Node,
    Outcome,
    Program,
    Query,
    Rule,
    Space,
    Status,
)
from digital_twin.behavioral.juniper import (
    NatSet,
    Route,
    SrxSession,
    ex_egress,
    ex_ingress,
    lacp_eligibility,
    route_lookup,
    srx_flow,
)
from digital_twin.behavioral.ssr import (
    Path,
    Service,
    Transport,
    build_fib,
    eligible_paths,
    fib_node,
    tenant_includes,
)


def done():
    return Node("done", (Rule(outcome=Outcome.DELIVERED),))


def evaluate(nodes, space, entry="start", **kwargs):
    return Program((*nodes, done())).evaluate(Query("q", entry, space, "done"), **kwargs)


def packet(**changes):
    values = dict(
        src=Domain.ip("192.0.2.10"),
        dst=Domain.ip("203.0.113.10"),
        src_port=12000,
        dst_port=8443,
        protocol="tcp",
        vrf="default",
        src_zone="untrust",
        src_interface="ge-0/0/0",
    )
    values.update(changes)
    return Space.of(**values)


def nat(name, source_field, source, matches, writes, destination=None):
    return NatSet(
        name,
        Match(source_field, Domain.value(source)),
        (Rule(matches=matches, writes=writes),),
        destination,
    )


@pytest.mark.parametrize(
    "control,status",
    [
        ("lldp", Status.SATISFIED),
        ("lacp", Status.SATISFIED),
        ("data", Status.VIOLATED),
    ],
)
def test_els_trunk_untagged_control_without_native_vlan(control, status):
    node = ex_ingress(
        "start", mode="trunk", native_vlan=None, vlans=(10,), destination="done", els=True
    )
    result = evaluate((node,), Space.of(tagged=False, control=control))
    assert result.status is status


def test_native_classification_and_egress_tagging_are_distinct_transfers():
    ingress = ex_ingress(
        "start", mode="trunk", native_vlan=10, vlans=(10, 20), destination="egress", els=True
    )
    egress = ex_egress("egress", native_vlan=20, tagged_vlans=(10,), destination="done")
    result = evaluate((ingress, egress), Space.of(tagged=False, control="data"))
    assert result.status is Status.SATISFIED
    assert result.traces[0].space.witness()["vlan"] == 10
    assert result.traces[0].space.witness()["tagged"] is True
    assert evaluate((ingress, egress), Space.of(tagged=True, vlan=30)).status is Status.VIOLATED


def test_els_native_vlan_without_logical_membership_cannot_be_certified():
    node = ex_ingress(
        "start", mode="trunk", native_vlan=10, vlans=(20,), destination="done", els=True
    )
    assert evaluate((node,), Space.of(tagged=False, control="data")).status is Status.UNKNOWN


def test_access_tagging_needs_explicit_platform_profile_and_priority_tags_are_opaque():
    node = ex_ingress(
        "start", mode="access", native_vlan=10, vlans=(10,), destination="done", els=True
    )
    assert evaluate((node,), Space.of(tagged=True, vlan=10)).status is Status.UNKNOWN
    profile = ex_ingress(
        "start",
        mode="access",
        native_vlan=10,
        vlans=(10,),
        destination="done",
        els=True,
        access_accepts_tagged=True,
    )
    assert evaluate((profile,), Space.of(tagged=True, vlan=10)).status is Status.SATISFIED
    assert evaluate((profile,), Space.of(tagged=True, vlan=0)).status is Status.UNKNOWN


@pytest.mark.parametrize(
    "local,peer,force_up,expected",
    [
        ("passive", "passive", False, False),
        ("active", "passive", False, True),
        ("passive", "active", False, True),
        ("active", "active", False, True),
        ("passive", "passive", True, True),
    ],
)
def test_lacp_active_passive(local, peer, force_up, expected):
    assert lacp_eligibility(local, peer, force_up=force_up) is expected


def test_longest_prefix_vrf_preference_and_dual_stack():
    routes = (
        Route("0.0.0.0/0", "default", "wan", "untrust"),
        Route("203.0.113.0/24", "default", "old", "old", preference=20),
        Route("203.0.113.0/24", "default", "specific", "trust", preference=10),
        Route("203.0.113.0/24", "blue", "blue", "blue"),
        Route("2001:db8::/32", "default", "ipv6", "trust"),
    )
    node = route_lookup("start", routes, "done")
    result = evaluate((node,), packet())
    assert result.status is Status.SATISFIED
    assert result.traces[0].space.witness()["dst_interface"] == "specific"
    assert (
        evaluate((node,), packet(vrf="blue")).traces[0].space.witness()["dst_interface"] == "blue"
    )
    assert evaluate((node,), packet(dst=Domain.ip("2001:db8::1"))).status is Status.SATISFIED


def test_missing_resolution_and_ecmp_do_not_become_reachability_proofs():
    route = Route("203.0.113.0/24", "default", "wan", "untrust", resolved=None)
    assert evaluate((route_lookup("start", (route,), "done"),), packet()).status is Status.UNKNOWN
    a = replace(route, resolved=True)
    b = replace(a, interface="wan2")
    assert evaluate((route_lookup("start", (a, b), "done"),), packet()).status is Status.UNKNOWN


def translated_flow(**features):
    dnat = nat(
        "publish",
        "src_zone",
        "untrust",
        (Match("dst", Domain.ip("203.0.113.10")),),
        (("dst", Domain.ip("10.10.0.20")), ("dst_port", Domain.value(443))),
    )
    snat = nat("source", "src_zone", "untrust", (), (("src", Domain.ip("10.10.0.1")),))
    policy = Rule(
        matches=(
            Match("dst_zone", Domain.value("trust")),
            Match("dst", Domain.ip("10.10.0.20")),
            Match("dst_port", Domain.value(443)),
        ),
        outcome=Outcome.DELIVERED,
    )
    defaults = dict(
        routes=(Route("10.10.0.0/24", "default", "inside", "trust"),),
        policies=(policy,),
        destination="done",
        dnat=(dnat,),
        snat=(snat,),
    )
    defaults.update(features)
    return srx_flow("start", **defaults)


def test_srx_destination_nat_precedes_route_and_security_policy_source_nat_follows():
    result = evaluate(translated_flow(), packet())
    assert result.status is Status.SATISFIED
    witness = result.traces[0].space.witness()
    assert (witness["dst"], witness["dst_port"], witness["dst_zone"], witness["src"]) == (
        "10.10.0.20",
        443,
        "trust",
        "10.10.0.1",
    )
    assert witness["original_dst"] == "203.0.113.10"
    assert witness["original_dst_port"] == 8443
    assert result.traces[0].path.index("start:dnat") < result.traces[0].path.index("start:route")
    assert result.traces[0].path.index("start:policy") < result.traces[0].path.index("start:snat")
    deny_original = Rule(
        matches=(Match("dst_port", Domain.value(8443)),), outcome=Outcome.DELIVERED
    )
    assert evaluate(translated_flow(policies=(deny_original,)), packet()).status is Status.VIOLATED


def test_srx_static_nat_and_reverse_static_take_precedence_over_dynamic_translations():
    static = nat(
        "static",
        "src_zone",
        "untrust",
        (),
        (("dst", Domain.ip("10.10.0.30")), ("dst_port", Domain.value(443))),
    )
    reverse = nat("reverse", "src_zone", "untrust", (), (("src", Domain.ip("198.51.100.5")),))
    permit = Rule(matches=(Match("dst", Domain.ip("10.10.0.30")),), outcome=Outcome.DELIVERED)
    result = evaluate(
        translated_flow(static=(static,), reverse_static=(reverse,), policies=(permit,)), packet()
    )
    assert result.status is Status.SATISFIED
    witness = result.traces[0].space.witness()
    assert witness["dst"] == "10.10.0.30"
    assert witness["src"] == "198.51.100.5"
    assert "start:dnat" not in result.traces[0].path
    assert "start:snat" not in result.traces[0].path


def test_srx_specific_rule_set_with_no_matching_rule_does_not_fall_back():
    specific = nat(
        "specific",
        "src_interface",
        "ge-0/0/0",
        (Match("dst_port", Domain.value(9999)),),
        (("dst", Domain.ip("10.10.0.20")),),
    )
    broad = nat("broad", "src_zone", "untrust", (), (("dst", Domain.ip("10.10.0.20")),))
    result = evaluate(translated_flow(dnat=(broad, specific)), packet())
    assert result.status is Status.VIOLATED
    assert "start:dnat:specific" in result.traces[0].path
    assert "start:dnat:broad" not in result.traces[0].path
    assert result.traces[0].space.witness()["dst"] == "203.0.113.10"


def test_srx_return_session_uses_translated_five_tuple_and_restores_original():
    original = packet()
    trace = evaluate(translated_flow(), original).traces[0]
    session = SrxSession.establish(original, trace)
    response = session.response_node("start", "done")
    reply = Space.of(
        src=Domain.ip("10.10.0.20"),
        dst=Domain.ip("10.10.0.1"),
        src_port=443,
        dst_port=12000,
        protocol="tcp",
        vrf="default",
    )
    result = evaluate((response,), reply)
    assert result.status is Status.SATISFIED
    assert result.traces[0].space.witness() == {
        "src": "203.0.113.10",
        "dst": "192.0.2.10",
        "src_port": 8443,
        "dst_port": 12000,
        "protocol": "tcp",
        "vrf": "default",
    }
    for field, value in (("dst_port", Domain.value(12001)), ("vrf", Domain.value("blue"))):
        assert evaluate((response,), reply.with_field(field, value)).status is Status.VIOLATED
    with pytest.raises(ValueError, match="exact five-tuple"):
        SrxSession(original.with_field("src_port", Domain.range(12000, 12001)), trace.space)


def test_srx_unsupported_features_and_symbolic_session_copies_are_unknown():
    assert (
        evaluate(translated_flow(unsupported_features=("ALG",)), packet()).status is Status.UNKNOWN
    )
    assert evaluate(translated_flow(), packet(src_port=Domain.range(12000, 13000))).status is (
        Status.UNKNOWN
    )


def test_ssr_documented_tcp_443_no_next_hop_despite_rib_route_and_any_match_fix():
    # Official FIB construction example: s1 covers TCP while more-specific
    # s6 only covers UDP. best-match-only never installs a next hop for s1.
    services = (
        Service("s1", "10.1.0.0/16", ("t0",), (Transport("tcp"),)),
        Service("s6", "10.1.1.0/24", ("t0",), (Transport("udp", Domain.range(1000, 2000)),)),
    )
    routes = (Route("10.1.1.0/24", "default", "g1", "wan"),)
    space = Space.of(dst=Domain.ip("10.1.1.10"), protocol="tcp", dst_port=443, tenant="t0")
    for mode, expected in (("best-match-only", Status.VIOLATED), ("any-match", Status.SATISFIED)):
        entries = build_fib(services, routes, tenants=("t0",), mode=mode)
        assert evaluate((fib_node("start", entries, "done"),), space).status is expected


def test_ssr_transport_collision_excludes_all_transports_of_losing_service():
    services = (
        Service("s6", "10.1.1.0/24", ("t0",), (Transport("udp", Domain.range(3000, 4000)),)),
        Service(
            "s7",
            "10.1.0.0/16",
            ("t0",),
            (
                Transport("udp", Domain.range(3500, 4500)),
                Transport("tcp", Domain.range(1000, 2000)),
            ),
        ),
    )
    entries = build_fib(
        services, (Route("10.1.1.0/24", "default", "g1", "wan"),), tenants=("t0",), mode="any-match"
    )
    assert not any(e.service.name == "s7" and e.next_hop for e in entries)
    tcp = Space.of(dst=Domain.ip("10.1.1.10"), protocol="tcp", dst_port=1500, tenant="t0")
    assert evaluate((fib_node("start", entries, "done"),), tcp).status is Status.VIOLATED


def test_ssr_equal_prefix_lexical_overlap_winner_is_independent_of_input_order():
    services = (
        Service("b", "10.1.0.0/16", ("t0",), (Transport("tcp"), Transport("udp"))),
        Service("a", "10.1.0.0/16", ("t0",), (Transport("tcp"),)),
    )
    route = Route("10.1.1.0/24", "default", "g1", "wan")
    a = build_fib(services, (route,), tenants=("t0",))
    assert a == build_fib(tuple(reversed(services)), (route,), tenants=("t0",))
    assert {e.service.name for e in a if e.next_hop} == {"a"}


def test_ssr_tenant_hierarchy_vrf_and_unknown_broader_route_expansion():
    assert tenant_includes("corp", "engineering.corp")
    assert not tenant_includes("engineering.corp", "corp")
    services = (Service("svc", "10.1.0.0/16", ("corp",), (Transport("tcp"),)),)
    routes = (Route("10.1.1.0/24", "blue", "blue", "wan"),)
    entries = build_fib(
        services, routes, tenants=("engineering.corp", "guest"), vrfs=(("corp", "blue"),)
    )
    assert {e.tenant for e in entries} == {"engineering.corp"}
    space = Space.of(dst=Domain.ip("10.1.1.10"), protocol="tcp", tenant="engineering.corp")
    assert evaluate((fib_node("start", entries, "done"),), space).status is Status.SATISFIED
    unmapped = build_fib(services, routes, tenants=("engineering.corp",))
    assert evaluate((fib_node("start", unmapped, "done"),), space).status is Status.VIOLATED
    expanded = build_fib(
        services, (Route("0.0.0.0/0", "default", "wan", "wan"),), tenants=("corp",)
    )
    assert (
        evaluate(
            (fib_node("start", expanded, "done"),), space.with_field("tenant", Domain.value("corp"))
        ).status
        is Status.UNKNOWN
    )


def test_ssr_uncertainty_is_not_erased_by_later_route_update():
    services = (Service("svc", "10.1.0.0/16", ("t0",)),)
    routes = (
        Route("10.1.1.0/24", "default", "a", "wan"),
        Route("10.1.1.0/24", "default", "b", "wan"),
        Route("10.1.1.0/24", "default", "a", "wan"),
    )
    entries = build_fib(services, routes, tenants=("t0",))
    assert (
        evaluate(
            (fib_node("start", entries, "done"),), Space.of(dst=Domain.ip("10.1.1.10"), tenant="t0")
        ).status
        is Status.UNKNOWN
    )


def test_ssr_new_session_sla_eligibility_and_explicit_best_effort():
    paths = (Path("a", 1, True, False), Path("b", 2, True, False))
    assert eligible_paths(paths, best_effort=False) == ()
    assert eligible_paths(paths, best_effort=True) == ("a",)
    assert eligible_paths((replace(paths[0], meets_sla=None),), best_effort=True) is None
    healthy = (replace(paths[1], meets_sla=True), *paths)
    assert eligible_paths(healthy, best_effort=True) == ("b",)
    with pytest.raises(ValueError):
        eligible_paths(paths, best_effort=1)
