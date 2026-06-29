"""wired.dhcp.snooping (GS25): snooping enabled on a switch whose every known
egress path toward the vlan's modeled DHCP source is UNTRUSTED -> offers are
dropped at lease renewal (WARNING/REVIEW). One trusted path silences; unknown
trust or unknowable placement abstains (PARTIAL) — never a dropped-offer
conclusion from blindness. "site" sources are unlocatable by design."""

from dataclasses import replace

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, CoverageState, Status
from digital_twin.checks.wired.snooping import DhcpSnoopingCheck
from digital_twin.contracts import Severity
from digital_twin.ir import IRBuilder, IRCapability, Vlan, diff_ir
from digital_twin.ir.entities import Device, DeviceRole
from tests.factories import link, sw, trunk_port


def _ir(*, snooping=("corp",), trust=True, sources=("GW",), gw_blind=False,
        linked=True):
    b = IRBuilder()
    b.add_device(replace(sw("S"), dhcp_snooping=snooping))
    b.add_device(Device(id="GW", role=DeviceRole.GATEWAY, site="s1",
                        l3_unmodeled=gw_blind))
    b.add_port(replace(trunk_port("S", "ge-0/0/0", tagged=(10,)), dhcp_trusted=trust))
    b.add_port(trunk_port("GW", "ge-0/0/0", tagged=(10,)))
    if linked:
        b.add_link(link("S:ge-0/0/0", "GW:ge-0/0/0"))
    b.add_vlan(Vlan(vlan_id=10, name="corp", dhcp_sources=sources))
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def _run(base, prop):
    return DhcpSnoopingCheck().run(
        CheckContext(
            baseline=AnalysisContext(base), proposed=AnalysisContext(prop), diff=diff_ir(base, prop)
        )
    )


def test_snooping_with_only_untrusted_path_to_source_is_warning():
    r = _run(_ir(snooping=None, trust=False), _ir(trust=False))
    assert r.status is Status.WARN
    f = r.findings[0]
    assert f.code == "wired.dhcp.snooping.untrusted_path"
    assert f.severity is Severity.WARNING


def test_one_trusted_path_is_enough():
    r = _run(_ir(snooping=None), _ir(trust=True))
    assert r.status is Status.PASS and not r.findings


def test_unknown_trust_abstains_partial_never_warns():
    r = _run(_ir(snooping=None, trust=None), _ir(trust=None))
    assert not r.findings
    assert r.coverage.state is CoverageState.PARTIAL


def test_site_source_placement_is_unlocatable_partial():
    r = _run(_ir(snooping=None, sources=("site",)), _ir(sources=("site",)))
    assert not r.findings
    assert r.coverage.state is CoverageState.PARTIAL
    assert any("site" in n for n in r.coverage.notes)


def test_mixed_sources_hedged_finding_plus_partial():
    base = _ir(snooping=None, trust=False, sources=("GW", "site"))
    prop = _ir(trust=False, sources=("GW", "site"))
    r = _run(base, prop)
    f = r.findings[0]
    assert "may still serve" in f.message
    assert r.coverage.state is CoverageState.PARTIAL


def test_preexisting_blocked_snooping_is_info():
    r = _run(_ir(trust=False), _ir(trust=False))  # snooped+blocked in BOTH
    assert [f.severity for f in r.findings] == [Severity.INFO]


def test_preexisting_blocked_with_blind_gateway_stays_complete():
    # GS22 rule, regressed once: PARTIAL keys off CONCLUSIONS — a blind
    # gateway behind an INFO-demoted pre-existing blockage must not drag
    # REVIEW via the coverage side door (decision floors PARTIAL to REVIEW)
    r = _run(_ir(trust=False, gw_blind=True), _ir(trust=False, gw_blind=True))
    assert [f.severity for f in r.findings] == [Severity.INFO]
    assert r.coverage.state is CoverageState.COMPLETE


def test_preexisting_site_abstention_stays_complete():
    # GS22 extended to ABSTENTIONS: a "site" source unlocatable in BOTH baseline
    # and proposed is an ambient blind spot, not delta-introduced — it must not
    # floor an unrelated change to REVIEW via PARTIAL coverage.
    r = _run(_ir(sources=("site",)), _ir(sources=("site",)))  # snooped+site on BOTH
    assert not r.findings
    assert r.coverage.state is CoverageState.COMPLETE


def test_preexisting_unknown_trust_abstention_stays_complete():
    r = _run(_ir(trust=None), _ir(trust=None))  # snooped+unknown trust on BOTH
    assert not r.findings
    assert r.coverage.state is CoverageState.COMPLETE


def test_preexisting_unreachable_abstention_stays_complete():
    r = _run(_ir(trust=False, linked=False), _ir(trust=False, linked=False))
    assert r.coverage.state is CoverageState.COMPLETE


def test_trusted_port_going_untrusted_under_existing_snooping_is_introduced():
    # ACTIVITY not pair (native-mismatch lesson): the snooping was on, but the
    # delta removed the last trusted path
    r = _run(_ir(trust=True), _ir(trust=False))
    assert r.findings[0].severity is Severity.WARNING


def test_blind_gateway_source_caps_confidence_medium():
    from digital_twin.ir import ConfidenceLevel
    r = _run(_ir(snooping=None, trust=False, gw_blind=True), _ir(trust=False, gw_blind=True))
    assert r.findings[0].confidence.level is ConfidenceLevel.MEDIUM
    assert r.coverage.state is CoverageState.PARTIAL
    assert any("GW" in n and "unmodeled" in n.lower() for n in r.coverage.notes)


def test_dhcp_unresolved_source_also_caps_medium():
    from digital_twin.ir import ConfidenceLevel

    def ir(snooping):
        b = IRBuilder()
        b.add_device(replace(sw("S"), dhcp_snooping=snooping))
        b.add_device(Device(id="GW", role=DeviceRole.GATEWAY, site="s1",
                            dhcp_unresolved=True))
        b.add_port(replace(trunk_port("S", "ge-0/0/0", tagged=(10,)), dhcp_trusted=False))
        b.add_port(trunk_port("GW", "ge-0/0/0", tagged=(10,)))
        b.add_link(link("S:ge-0/0/0", "GW:ge-0/0/0"))
        b.add_vlan(Vlan(vlan_id=10, name="corp", dhcp_sources=("GW",)))
        b.with_capability(IRCapability.WIRED_L2)
        return b.build()

    r = _run(ir(None), ir(("corp",)))
    assert r.findings[0].confidence.level is ConfidenceLevel.MEDIUM


def test_source_not_reachable_in_graph_abstains():
    r = _run(_ir(snooping=None, trust=False, linked=False),
             _ir(trust=False, linked=False))
    assert not [f for f in r.findings if f.code.endswith("untrusted_path")]
    assert r.coverage.state is CoverageState.PARTIAL


def test_all_networks_star_snoops_every_sourced_vlan():
    base = _ir(snooping=None, trust=False)
    prop = _ir(snooping=("*",), trust=False)
    assert _run(base, prop).findings


def test_baseline_parity_folds_with_the_baseline_vc_map():
    # r3 regression: the snooping switch S is a VC MEMBER (folded under R) in
    # baseline and standalone in proposed; the blocked path exists on BOTH
    # sides. Parity must fold baseline node ids with the BASELINE map — if it
    # reuses proposed-folded ids, "S" is not a baseline graph node, the
    # baseline probe reads "unreachable" instead of "blocked", and the
    # pre-existing blockage is wrongly re-reported as introduced (WARNING).
    def ir_vc(folded):
        b = IRBuilder()
        b.add_device(sw("R", vc_members=("S",) if folded else ()))
        b.add_device(replace(sw("S"), dhcp_snooping=("corp",)))
        b.add_device(Device(id="GW", role=DeviceRole.GATEWAY, site="s1"))
        b.add_port(replace(trunk_port("S", "ge-0/0/0", tagged=(10,)), dhcp_trusted=False))
        b.add_port(trunk_port("GW", "ge-0/0/0", tagged=(10,)))
        b.add_link(link("S:ge-0/0/0", "GW:ge-0/0/0"))
        b.add_vlan(Vlan(vlan_id=10, name="corp", dhcp_sources=("GW",)))
        b.with_capability(IRCapability.WIRED_L2)
        return b.build()

    r = _run(ir_vc(folded=True), ir_vc(folded=False))
    assert [f.severity for f in r.findings] == [Severity.INFO]


def test_edge_not_carrying_the_vlan_is_unreachable_not_ok():
    # review P1 r2: the LOCAL port carries vlan 10 but the peer end does not
    # -> the EDGE does not carry it -> the source is unreachable on the vlan
    # graph: abstain (PARTIAL), never a trusted "ok" through a dead path
    def ir_asym(snooping):
        b = IRBuilder()
        b.add_device(replace(sw("S"), dhcp_snooping=snooping))
        b.add_device(Device(id="GW", role=DeviceRole.GATEWAY, site="s1"))
        b.add_port(replace(trunk_port("S", "ge-0/0/0", tagged=(10,)), dhcp_trusted=True))
        b.add_port(trunk_port("GW", "ge-0/0/0", tagged=(99,)))  # peer drops 10
        b.add_link(link("S:ge-0/0/0", "GW:ge-0/0/0"))
        b.add_vlan(Vlan(vlan_id=10, name="corp", dhcp_sources=("GW",)))
        b.with_capability(IRCapability.WIRED_L2)
        return b.build()

    r = _run(ir_asym(None), ir_asym(("corp",)))
    assert not [f for f in r.findings if f.code.endswith("untrusted_path")]
    assert r.coverage.state is CoverageState.PARTIAL


# --- caused_by attribution tests ---


def test_conclusion_finding_caused_by_names_changed_device():
    # Device S gains snooping in proposed (snooping=None->("corp",)) → it IS in
    # the delta as a modified entity. The WARNING finding's caused_by must name it.
    r = _run(_ir(snooping=None, trust=False), _ir(trust=False))
    f = next(x for x in r.findings if x.severity is Severity.WARNING)
    assert len(f.caused_by) == 1
    assert f.caused_by[0].ref.kind == "device"
    assert f.caused_by[0].ref.id == "S"


def test_preexisting_info_finding_has_empty_caused_by():
    # Identical snooping on both sides → severity INFO → caused_by must be ().
    r = _run(_ir(trust=False), _ir(trust=False))
    f = next(x for x in r.findings if x.severity is Severity.INFO)
    assert f.caused_by == ()


def test_port_trust_flip_names_port_in_caused_by():
    # PR #5 review gap: the last trusted egress port flips dhcp_trusted True→False
    # while device snooping config is UNCHANGED (both sides have snooping=("corp",)).
    # Before the fix the port was missing from caused_by (only the device was
    # returned via delta_index.cause("device", did)). The port S:ge-0/0/0 IS in
    # the delta as a Modified entity (dhcp_trusted field changed), so the fix
    # (*ctx.delta_index.causes("port", blocked)) must name it.
    base = _ir(trust=True)   # snooping already on; trusted port
    prop = _ir(trust=False)  # snooping unchanged; port flips untrusted
    r = _run(base, prop)
    f = next(x for x in r.findings if x.severity is Severity.WARNING)
    assert f.code == "wired.dhcp.snooping.untrusted_path"
    port_cause_ids = {c.ref.id for c in f.caused_by if c.ref.kind == "port"}
    assert "S:ge-0/0/0" in port_cause_ids, (
        f"expected port S:ge-0/0/0 in caused_by, got: {[c.ref for c in f.caused_by]}"
    )


def test_vlan_source_change_names_vlan_in_caused_by():
    # PR #5 review gap: a snooped vlan whose dhcp_sources goes from () → ("GW",)
    # introduces an untrusted_path (device snooping config + port trust UNCHANGED;
    # both sides have snooping=("corp",) and trust=False). The vlan entity IS in
    # the delta as a Modified entity (dhcp_sources field changed), so
    # *(c for c in (ctx.delta_index.cause("vlan", str(vlan)),) if c is not None)
    # must name vlan "10" in caused_by.
    base = _ir(trust=False, sources=())   # snooping on; no dhcp source → no finding
    prop = _ir(trust=False, sources=("GW",))  # source added → untrusted_path fires
    r = _run(base, prop)
    f = next(
        x for x in r.findings
        if x.code == "wired.dhcp.snooping.untrusted_path" and x.severity is Severity.WARNING
    )
    vlan_cause_ids = {c.ref.id for c in f.caused_by if c.ref.kind == "vlan"}
    assert "10" in vlan_cause_ids, (
        f"expected vlan '10' in caused_by, got: {[c.ref for c in f.caused_by]}"
    )


def test_added_link_names_link_in_caused_by():
    # PR #5 review gap: a new egress path to the DHCP source is created by ADDING
    # a link (linked=False → True). Device snooping config + port trust UNCHANGED
    # (both sides snooping=("corp",), trust=False). The link entity IS in the delta
    # as an Added entity; path_links collects its id ("GW:ge-0/0/0__S:ge-0/0/0"),
    # so *ctx.delta_index.causes("link", path_links) must name it in caused_by.
    base = _ir(trust=False, linked=False)  # no link → source unreachable → no finding
    prop = _ir(trust=False, linked=True)   # link added → untrusted_path fires
    r = _run(base, prop)
    f = next(
        x for x in r.findings
        if x.code == "wired.dhcp.snooping.untrusted_path" and x.severity is Severity.WARNING
    )
    # link_id("S:ge-0/0/0", "GW:ge-0/0/0") → sorted → "GW:ge-0/0/0__S:ge-0/0/0"
    expected_link_id = "GW:ge-0/0/0__S:ge-0/0/0"
    link_cause_ids = {c.ref.id for c in f.caused_by if c.ref.kind == "link"}
    assert expected_link_id in link_cause_ids, (
        f"expected link '{expected_link_id}' in caused_by, got: {[c.ref for c in f.caused_by]}"
    )
