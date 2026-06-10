"""l2.blackhole: FAIL only when a member component HAD a HIGH-confidence exit
path in IR and LOSES it in IR'; MEDIUM/LOW exit -> WARN; no locatable exit ->
INSUFFICIENT_DATA for that vlan (never PASS); pre-existing strands = context."""

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.wired.l2_blackhole import L2BlackholeCheck
from digital_twin.contracts import Severity
from digital_twin.ir import IRBuilder, IRCapability, Vlan, diff_ir
from tests.factories import access_port, irb, link, sw, trunk_port


def _ir(*, connected: bool, with_irb: bool = True, with_member: bool = True):
    """A(member)--B(IRB). connected=False cuts the link (the delta's effect)."""
    b = IRBuilder()
    b.add_device(sw("A")).add_device(sw("B"))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    if with_member:
        b.add_port(access_port("A", "acc", 10))
    b.add_port(trunk_port("A", "up", tagged=(10,)))
    b.add_port(trunk_port("B", "down", tagged=(10,)))
    if connected:
        b.add_link(link("A:up", "B:down"))
    if with_irb:
        b.add_l3intf(irb("B", 10))
    b.with_capability(IRCapability.WIRED_L2).with_capability(IRCapability.L3_EXITS)
    return b.build()


def _ctx(baseline, proposed):
    return CheckContext(
        baseline=AnalysisContext(baseline),
        proposed=AnalysisContext(proposed),
        diff=diff_ir(baseline, proposed),
    )


def test_losing_a_high_confidence_exit_fails():
    result = L2BlackholeCheck().run(_ctx(_ir(connected=True), _ir(connected=False)))
    assert result.status is Status.FAIL
    f = result.findings[0]
    assert f.severity is Severity.ERROR
    assert "10" in f.message  # names the vlan


def test_still_connected_passes():
    result = L2BlackholeCheck().run(_ctx(_ir(connected=True), _ir(connected=True)))
    assert result.status is Status.PASS


def test_no_locatable_exit_is_insufficient_data():
    base = _ir(connected=True, with_irb=False)
    prop = _ir(connected=False, with_irb=False)
    result = L2BlackholeCheck().run(_ctx(base, prop))
    assert result.status is Status.INSUFFICIENT_DATA  # exit unlocatable, never PASS


def test_newly_added_member_on_isolated_switch_fails():
    # the review's P1: the delta ADDS the first member (access port) on a switch
    # with no path to the vlan's exit — a newly INTRODUCED blackhole, not
    # "preexisting" (configured-but-empty access ports count as membership)
    base = _ir(connected=False, with_member=False)
    prop = _ir(connected=False, with_member=True)
    result = L2BlackholeCheck().run(_ctx(base, prop))
    assert result.status is Status.FAIL
    assert any(f.code == "wired.l2.blackhole.new_member_stranded" for f in result.findings)


def test_new_member_port_on_already_stranded_node_fails():
    # the review's P1 round 3: A:acc1 is already blackholed (context), but the
    # delta adds A:acc2 — a NEW member port with no path to the exit. Node-level
    # overlap must not hide it; attribution is per member PORT.
    def site(ports):
        b = IRBuilder()
        b.add_device(sw("A")).add_device(sw("CORE"))
        b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
        b.add_l3intf(irb("CORE", 10))
        b.add_port(trunk_port("CORE", "down", tagged=(10,)))
        for p in ports:
            b.add_port(access_port("A", p, 10))
        b.with_capability(IRCapability.WIRED_L2).with_capability(IRCapability.L3_EXITS)
        return b.build()

    result = L2BlackholeCheck().run(_ctx(site(["acc1"]), site(["acc1", "acc2"])))
    assert result.status is Status.FAIL
    f = next(x for x in result.findings if x.code == "wired.l2.blackhole.new_member_stranded")
    assert "A:acc2" in f.evidence["new_member_ports"]
    assert "A:acc1" not in f.evidence["new_member_ports"]  # acc1 IS pre-existing


def test_transit_only_vlan_low_exit_does_not_taint_confidence():
    # the review's P2: a vlan with NO members doesn't rely on its exit — its
    # LOW boundary-uplink confidence must not floor the whole check (and with
    # it every unrelated benign change) to REVIEW
    from digital_twin.ir import ConfidenceLevel
    from digital_twin.ir.entities import Device, DeviceRole
    from digital_twin.ir.provenance import Provenance

    def transit(suffix: str):
        b = IRBuilder()
        b.add_device(sw("A"))
        b.add_device(Device(id="GW", role=DeviceRole.GATEWAY, site="s1"))
        b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
        b.add_port(trunk_port("A", "up", tagged=(10,)))
        b.add_port(trunk_port("GW", "down", tagged=(10,)))
        b.add_link(link("A:up", "GW:down", prov=Provenance.LLDP_ONE_SIDED))  # LOW exit
        b.add_vlan(Vlan(vlan_id=99, name="x", scope="s1"))
        b.add_port(access_port("A", f"acc-{suffix}", 99))  # unrelated benign diff
        b.add_l3intf(irb("A", 99))
        b.with_capability(IRCapability.WIRED_L2).with_capability(IRCapability.L3_EXITS)
        return b.build()

    result = L2BlackholeCheck().run(_ctx(transit("a"), transit("b")))
    assert result.status is Status.PASS
    assert result.confidence is not None
    assert result.confidence.level is ConfidenceLevel.HIGH  # LOW exit not consulted


def _gs7_site(connected: bool):
    """GS7: AP with an observed vlan-30 wireless client; delta cuts the AP uplink."""
    from tests.factories import ap, wireless_client

    b = IRBuilder()
    b.add_device(sw("SW")).add_device(ap("AP1"))
    b.add_vlan(Vlan(vlan_id=30, name="voice", scope="s1"))
    b.add_port(trunk_port("SW", "to-ap", tagged=(30,)))
    b.add_port(trunk_port("AP1", "eth0", tagged=(30,)))
    if connected:
        b.add_link(link("AP1:eth0", "SW:to-ap"))
    b.add_l3intf(irb("SW", 30))
    b.add_client(wireless_client("ww:01", "AP1", vlan=30))
    for c in (IRCapability.WIRED_L2, IRCapability.L3_EXITS, IRCapability.CLIENTS_ACTIVE):
        b.with_capability(c)
    return b.build()


def test_gs7_wireless_client_isolated_by_uplink_removal_fails():
    # observed wireless clients ARE membership (observation-based, per spec):
    # cutting the AP uplink strands the vlan-30 wireless client -> FAIL
    result = L2BlackholeCheck().run(_ctx(_gs7_site(True), _gs7_site(False)))
    assert result.status is Status.FAIL
    f = next(x for x in result.findings if "exit_lost" in x.code)
    assert "AP1" in f.affected_entities


def test_wireless_membership_marks_coverage_partial():
    # spec: AP-side membership is observation-based — not-yet-connected clients
    # are a KNOWN coverage gap, so the conclusion's coverage is PARTIAL
    from digital_twin.checks.base import CoverageState

    result = L2BlackholeCheck().run(_ctx(_gs7_site(True), _gs7_site(True)))
    assert result.coverage.state is CoverageState.PARTIAL
    assert any("observation-based" in n for n in result.coverage.notes)


def test_preexisting_strand_is_context_not_failure():
    # already disconnected in baseline -> not attributed to the delta
    base = _ir(connected=False)
    prop = _ir(connected=False)
    result = L2BlackholeCheck().run(_ctx(base, prop))
    assert result.status is Status.PASS
    assert any(f.severity is Severity.INFO for f in result.findings)
