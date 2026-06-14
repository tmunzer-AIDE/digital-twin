"""wired.l3.ospf_withdrawal (GS26): structural withdrawal of a switch's OSPF
participation for a routed segment. Base REVIEW; UNSAFE only when the device's
last active adjacency collapses AND an affected segment has observed clients.
Comparison is by the semantic (device, vlan[, area, active]) tuple, never id."""

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, CoverageState, Status
from digital_twin.checks.wired.ospf_withdrawal import OspfWithdrawalCheck
from digital_twin.contracts import Severity
from digital_twin.ir import IRBuilder, IRCapability, Vlan, diff_ir
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
    assert r.status is Status.FAIL


def test_egress_lost_without_clients_is_warn():
    base = _ir([ospf("S", 10, name="transit")])
    prop = _ir([])
    r = _run(base, prop)
    f = next(f for f in r.findings if f.code == "wired.l3.ospf_withdrawal.egress_lost")
    assert f.severity is Severity.WARNING
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
