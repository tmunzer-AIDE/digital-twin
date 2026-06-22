"""wired.l3.ospf_withdrawal (GS26): structural withdrawal of a switch's OSPF
participation for a routed segment. Base REVIEW; UNSAFE only when the device's
last active adjacency collapses AND an affected segment has observed clients.
Comparison is by the semantic (device, vlan[, area, active]) tuple, never id."""

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, CoverageState, Status
from digital_twin.checks.wired.ospf_withdrawal import OspfWithdrawalCheck
from digital_twin.contracts import Severity
from digital_twin.ir import ConfidenceLevel, IRBuilder, IRCapability, Vlan, diff_ir
from digital_twin.ir.entities import AttachKind, Client, ClientKind
from tests.factories import access_port, irb, ospf, sw


def _ir(ospf_rows, *, clients=(), routed=(10, 20, 30), with_clients_cap=True):
    b = IRBuilder().add_device(sw("S"))
    for vid in routed:
        b.add_vlan(Vlan(vlan_id=vid, subnet=f"198.51.{vid}.0/24"))
        b.add_l3intf(irb("S", vid, subnet=f"198.51.{vid}.0/24"))
    if clients:
        b.add_port(access_port("S", "p1", vlan=routed[0]))
    for row in ospf_rows:
        b.add_ospf_intf(row)
    for mac, vid in clients:
        b.add_client(
            Client(mac=mac, kind=ClientKind.WIRED, attach_kind=AttachKind.PORT,
                   attach_id="S:p1", vlan=vid)
        )
    b.with_capability(IRCapability.WIRED_L2).with_capability(IRCapability.L3_EXITS)
    if with_clients_cap:
        b.with_capability(IRCapability.CLIENTS_ACTIVE)
    return b.build()


def _run(base, prop):
    return OspfWithdrawalCheck().run(
        CheckContext(baseline=AnalysisContext(base), proposed=AnalysisContext(prop),
                     diff=diff_ir(base, prop))
    )


def test_requires_and_not_applicable_without_ospf_diff():
    assert OspfWithdrawalCheck().requires() == frozenset(
        {IRCapability.WIRED_L2, IRCapability.L3_EXITS}
    )
    base = _ir([])
    prop = _ir([])
    assert OspfWithdrawalCheck().applies_to(diff_ir(base, prop)) is False


def test_egress_lost_with_clients_is_fail():
    base = _ir(
        [ospf("S", 10, name="transit"), ospf("S", 20, name="corp", passive=True)],
        clients=[("aa:bb", 20)],
    )
    prop = _ir([ospf("S", 20, name="corp", passive=True)], clients=[("aa:bb", 20)])
    r = _run(base, prop)
    f = next(f for f in r.findings if f.code == "wired.l3.ospf_withdrawal.egress_lost")
    assert f.severity is Severity.ERROR
    assert f.confidence.level is ConfidenceLevel.HIGH  # observed-client UNSAFE -> HIGH
    assert r.status is Status.FAIL


def test_egress_lost_without_clients_is_warn():
    base = _ir([ospf("S", 10, name="transit")])
    prop = _ir([])
    r = _run(base, prop)
    f = next(f for f in r.findings if f.code == "wired.l3.ospf_withdrawal.egress_lost")
    assert f.severity is Severity.WARNING
    # REVIEW case carries MEDIUM, not HIGH — the impact is unconfirmed
    assert f.confidence.level is ConfidenceLevel.MEDIUM
    assert r.status is Status.WARN


def test_egress_lost_clients_unfetched_stays_warn_and_partial():
    base = _ir([ospf("S", 10, name="transit")], clients=[("aa:bb", 10)], with_clients_cap=False)
    prop = _ir([], with_clients_cap=False)
    r = _run(base, prop)
    f = next(f for f in r.findings if f.code == "wired.l3.ospf_withdrawal.egress_lost")
    assert f.severity is Severity.WARNING
    assert r.coverage.state is CoverageState.PARTIAL


def test_disable_ospf_collapses_all():
    base = _ir([ospf("S", 10, name="a"), ospf("S", 20, name="b")], clients=[("aa:bb", 10)])
    prop = _ir([])
    r = _run(base, prop)
    f = next(f for f in r.findings if f.code == "wired.l3.ospf_withdrawal.egress_lost")
    assert f.severity is Severity.ERROR


def test_advertised_removed_when_device_keeps_adjacency():
    base = _ir([ospf("S", 10, name="transit"), ospf("S", 20, name="corp", passive=True)])
    prop = _ir([ospf("S", 10, name="transit")])
    r = _run(base, prop)
    codes = {f.code for f in r.findings}
    assert "wired.l3.ospf_withdrawal.advertised_removed" in codes
    assert "wired.l3.ospf_withdrawal.egress_lost" not in codes
    assert r.status is Status.WARN


def test_addition_and_unrelated_are_silent():
    base = _ir([ospf("S", 10, name="transit")])
    prop = _ir([ospf("S", 10, name="transit"), ospf("S", 20, name="corp", passive=True)])
    r = _run(base, prop)
    assert r.findings == ()
    assert r.status is Status.PASS


def test_transit_mutation_on_noncollapsing_passive_flip():
    base = _ir([ospf("S", 10, name="a"), ospf("S", 20, name="b")])
    prop = _ir([ospf("S", 10, name="a", passive=True), ospf("S", 20, name="b")])
    r = _run(base, prop)
    f = next(f for f in r.findings if f.code == "wired.l3.ospf_withdrawal.transit_mutation")
    assert f.severity is Severity.WARNING
    assert r.status is Status.WARN


def test_pure_rename_is_silent():
    base = _ir([ospf("S", 10, name="corp")])
    prop = _ir([ospf("S", 10, name="corp2")])
    r = _run(base, prop)
    assert r.findings == ()
    assert r.status is Status.PASS


def test_area_move_is_transit_mutation_not_withdrawal():
    base = _ir([ospf("S", 10, name="corp", area="0")])
    prop = _ir([ospf("S", 10, name="corp", area="1")])
    r = _run(base, prop)
    codes = {f.code for f in r.findings}
    assert codes == {"wired.l3.ospf_withdrawal.transit_mutation"}


def test_unresolved_withdrawal_abstains_partial_never_unsafe():
    base = _ir([ospf("S", None, name="ghost", unresolved=True)])
    prop = _ir([])
    r = _run(base, prop)
    assert r.status is not Status.FAIL
    assert r.coverage.state is CoverageState.PARTIAL
    assert any("does not resolve" in n for n in r.coverage.notes)


def _two_switch_ir(*, s1_on_20, s2_20_passive):
    # S1 and S2 both participate on vlan 20; S2 also has an active vlan 30 so it
    # never collapses. Used to prove egress_lost on S1 does NOT suppress an
    # independent transit_mutation on S2 (the per-(device,vlan) suppression).
    b = IRBuilder().add_device(sw("S1")).add_device(sw("S2"))
    for vid in (20, 30):
        b.add_vlan(Vlan(vlan_id=vid, subnet=f"198.51.{vid}.0/24"))
    b.add_l3intf(irb("S1", 20, subnet="198.51.20.0/24"))
    b.add_l3intf(irb("S2", 20, subnet="198.51.20.0/24"))
    b.add_l3intf(irb("S2", 30, subnet="198.51.30.0/24"))
    if s1_on_20:
        b.add_ospf_intf(ospf("S1", 20, name="s1transit"))
    b.add_ospf_intf(ospf("S2", 20, name="s2corp", passive=s2_20_passive))
    b.add_ospf_intf(ospf("S2", 30, name="s2transit"))
    b.with_capability(IRCapability.WIRED_L2).with_capability(IRCapability.L3_EXITS)
    b.with_capability(IRCapability.CLIENTS_ACTIVE)
    return b.build()


def test_collapse_on_one_device_does_not_suppress_mutation_on_another():
    # S1 loses its only active OSPF interface on vlan 20 (collapse -> egress_lost),
    # while S2 INDEPENDENTLY flips its own vlan-20 row active->passive (no collapse,
    # S2 keeps vlan 30 active). The S2 mutation must still surface — suppression is
    # per (device, vlan), not per vlan.
    base = _two_switch_ir(s1_on_20=True, s2_20_passive=False)
    prop = _two_switch_ir(s1_on_20=False, s2_20_passive=True)
    r = _run(base, prop)
    codes = {f.code for f in r.findings}
    assert "wired.l3.ospf_withdrawal.egress_lost" in codes  # S1 collapsed on vlan 20
    tm = [f for f in r.findings if f.code == "wired.l3.ospf_withdrawal.transit_mutation"]
    assert len(tm) == 1 and tm[0].evidence["device"] == "S2"  # S2's mutation not suppressed


def test_advertised_removed_is_per_device_not_global_vlan():
    # S1 withdraws its OWN vlan-20 advertisement while keeping vlan 30 active (no
    # collapse); S2 still advertises vlan 20. Per-(device,vlan): S1's withdrawal
    # is a real REVIEW finding, NOT silently masked by S2 still carrying the vlan
    # (the global base_vlans - prop_vlans form returned PASS — the P1 bug).
    def build(*, s1_has_20):
        b = IRBuilder().add_device(sw("S1")).add_device(sw("S2"))
        for vid in (20, 30):
            b.add_vlan(Vlan(vlan_id=vid, subnet=f"198.51.{vid}.0/24"))
        b.add_l3intf(irb("S1", 20, subnet="198.51.20.0/24"))
        b.add_l3intf(irb("S1", 30, subnet="198.51.30.0/24"))
        b.add_l3intf(irb("S2", 20, subnet="198.51.20.0/24"))
        if s1_has_20:
            b.add_ospf_intf(ospf("S1", 20, name="s1corp"))
        b.add_ospf_intf(ospf("S1", 30, name="s1transit"))  # S1 keeps an adjacency
        b.add_ospf_intf(ospf("S2", 20, name="s2corp"))  # S2 still advertises vlan 20
        b.with_capability(IRCapability.WIRED_L2).with_capability(IRCapability.L3_EXITS)
        b.with_capability(IRCapability.CLIENTS_ACTIVE)
        return b.build()

    r = _run(build(s1_has_20=True), build(s1_has_20=False))
    ar = [f for f in r.findings if f.code == "wired.l3.ospf_withdrawal.advertised_removed"]
    assert len(ar) == 1
    assert ar[0].evidence["device"] == "S1" and ar[0].evidence["vlan"] == 20
    assert r.status is Status.WARN  # REVIEW, never a silent PASS


# --- caused_by attribution (CA Task 11) ---


def test_egress_lost_attributes_the_collapsed_devices_active_ospf_intf():
    base = _ir([ospf("S", 10, name="transit")], clients=[("aa:bb", 10)])
    prop = _ir([])
    r = _run(base, prop)
    f = next(f for f in r.findings if f.code.endswith(".egress_lost"))
    causes = {(c.ref.kind, c.ref.id) for c in f.caused_by}
    assert causes == {("ospf_intf", "S:ospf:0:transit")}


def test_advertised_removed_attributes_the_changed_ospf_intf():
    base = _ir([ospf("S", 10, name="transit"), ospf("S", 20, name="corp", passive=True)])
    prop = _ir([ospf("S", 10, name="transit")])
    r = _run(base, prop)
    f = next(f for f in r.findings if f.code.endswith(".advertised_removed"))
    causes = {(c.ref.kind, c.ref.id) for c in f.caused_by}
    assert causes == {("ospf_intf", "S:ospf:0:corp")}


def test_transit_mutation_attributes_the_changed_ospf_intf():
    base = _ir([ospf("S", 10, name="a"), ospf("S", 20, name="b")])
    prop = _ir([ospf("S", 10, name="a", passive=True), ospf("S", 20, name="b")])
    r = _run(base, prop)
    f = next(f for f in r.findings if f.code.endswith(".transit_mutation"))
    causes = {(c.ref.kind, c.ref.id) for c in f.caused_by}
    # the passive flip on vlan 10 reuses the same id (name unchanged) -> modified
    assert causes == {("ospf_intf", "S:ospf:0:a")}


def test_two_devices_same_vlan_advertised_removed_names_only_its_own_intf():
    # REGRESSION (round 8): A and B both have OSPF deltas on vlan 50. A's
    # advertised_removed must name ONLY A's ospf_intf, never B's. Without the
    # `oi.device_id == did` match the (*base,*prop) ospf_intfs on vlan 50 would
    # include B's row and cross-blame it.
    def build(*, a_has_50, b_passive_50):
        b = IRBuilder().add_device(sw("A")).add_device(sw("B"))
        for vid in (50, 60):
            b.add_vlan(Vlan(vlan_id=vid, subnet=f"198.51.{vid}.0/24"))
        b.add_l3intf(irb("A", 50, subnet="198.51.50.0/24"))
        b.add_l3intf(irb("A", 60, subnet="198.51.60.0/24"))
        b.add_l3intf(irb("B", 50, subnet="198.51.50.0/24"))
        if a_has_50:
            b.add_ospf_intf(ospf("A", 50, name="a50"))
        b.add_ospf_intf(ospf("A", 60, name="a60"))  # A keeps an adjacency (no collapse)
        # B independently flips its own vlan-50 row active->passive (a delta on
        # B's vlan-50 ospf_intf id, same vlan as A's withdrawal)
        b.add_ospf_intf(ospf("B", 50, name="b50", passive=b_passive_50))
        b.with_capability(IRCapability.WIRED_L2).with_capability(IRCapability.L3_EXITS)
        b.with_capability(IRCapability.CLIENTS_ACTIVE)
        return b.build()

    base = build(a_has_50=True, b_passive_50=False)
    prop = build(a_has_50=False, b_passive_50=True)
    r = _run(base, prop)
    ar = next(f for f in r.findings if f.code.endswith(".advertised_removed")
              and f.evidence["device"] == "A")
    causes = {c.ref.id for c in ar.caused_by}
    assert causes == {"A:ospf:0:a50"}
    assert "B:ospf:0:b50" not in causes  # B's changed intf must NOT be cross-blamed


def test_participation_by_area_and_ambiguity():
    from digital_twin.checks.wired.ospf_withdrawal import _participation
    from tests.factories import ospf
    # two networks on the SAME (S, vlan 10, area 0) with DIFFERENT metric (needs the
    # ospf(metric=) factory extension from the Test harness note). vlan 10 is in _ir's
    # default routed=(10,20,30) so it already has a subnet.
    ir = _ir([ospf("S", 10, area="0", name="a", metric=5),
              ospf("S", 10, area="0", name="b", metric=9)])
    seg = _participation(ir).by_dev_vlan[("S", 10)]
    assert "0" in seg.ambiguous_areas    # differing metric -> ambiguous, no last-win


def test_egress_lost_names_only_active_intf_not_an_unrelated_passive_one():
    # REGRESSION (round 9): a device's ACTIVE adjacency collapses while an
    # unrelated PASSIVE ospf row on the SAME device also changes (a passive row
    # withdrawn). egress_lost must name ONLY the active intf, never the passive
    # one (the `not oi.passive` filter). Without it, the removed passive row
    # would be cross-blamed for the adjacency collapse.
    base = _ir(
        [ospf("S", 10, name="transit"), ospf("S", 20, name="stub", passive=True)],
        clients=[("aa:bb", 10)],
    )
    # both rows removed: the active collapse is egress_lost; the passive row's
    # removal is a separate event that must NOT be attributed to the collapse
    prop = _ir([], clients=[("aa:bb", 10)])
    r = _run(base, prop)
    f = next(f for f in r.findings if f.code.endswith(".egress_lost"))
    causes = {c.ref.id for c in f.caused_by}
    assert causes == {"S:ospf:0:transit"}
    assert "S:ospf:0:stub" not in causes  # the passive row is not the adjacency cause
