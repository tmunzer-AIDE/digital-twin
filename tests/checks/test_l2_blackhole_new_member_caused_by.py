"""Cause attribution (CA-T13) for wired.l2.blackhole.new_member_stranded:
- a newly-configured access port stranded on an isolated node names that port
  (direct access-port attribution);
- a WLAN-only new member with NO L2 delta is honest-empty (caused_by == ()):
  there is no changed port/link to blame, and severance fallback finds no lost
  boundary edge.
"""

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.wired.l2_blackhole import L2BlackholeCheck
from digital_twin.ir import IRBuilder, IRCapability, Vlan, diff_ir
from tests.factories import access_port, ap, irb, sw, trunk_port


def _ctx(baseline, proposed):
    return CheckContext(
        baseline=AnalysisContext(baseline),
        proposed=AnalysisContext(proposed),
        diff=diff_ir(baseline, proposed),
    )


def _ids(causes):
    return sorted((c.ref.kind, c.ref.id) for c in causes)


def _new_access_ir(with_member: bool):
    """Switch A is physically isolated (no uplink at all) but has the vlan-10
    exit IRB elsewhere it cannot reach. A's vlan-10 domain never reaches the
    exit. The delta ADDS a new access port A:acc on the isolated node ->
    new_member_stranded, attributed to the added port A:acc."""
    b = IRBuilder()
    b.add_device(sw("A")).add_device(sw("CORE"))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    b.add_l3intf(irb("CORE", 10))
    b.add_port(trunk_port("CORE", "down", tagged=(10,)))  # exit lives on CORE, A unreachable
    if with_member:
        b.add_port(access_port("A", "acc", 10))
    b.with_capability(IRCapability.WIRED_L2).with_capability(IRCapability.L3_EXITS)
    return b.build()


def test_new_access_port_strand_names_the_port():
    result = L2BlackholeCheck().run(_ctx(_new_access_ir(False), _new_access_ir(True)))
    assert result.status is Status.FAIL
    f = next(f for f in result.findings if f.code == "wired.l2.blackhole.new_member_stranded")
    assert _ids(f.caused_by) == [("port", "A:acc")]


def _wlan_only_ir(*, require: bool):
    """AP1 is physically isolated (no link to anything), so vlan-30 has the IRB
    exit on SW which AP1 can't reach. The delta adds ONLY a WLAN VLAN-30
    requirement on AP1 (a new wlan_member) with NO port/link L2 change ->
    new_member_stranded with nothing in the delta to blame -> caused_by ()."""
    b = IRBuilder()
    b.add_device(sw("SW")).add_device(ap("AP1"))
    b.add_vlan(Vlan(vlan_id=30, name="voice", scope="s1"))
    b.add_l3intf(irb("SW", 30))
    b.add_port(trunk_port("SW", "down", tagged=(30,)))
    if require:
        b.require_ap_vlans("AP1", frozenset({30}))
    for c in (IRCapability.WIRED_L2, IRCapability.L3_EXITS):
        b.with_capability(c)
    return b.build()


def test_wlan_only_new_member_is_honest_empty():
    result = L2BlackholeCheck().run(_ctx(_wlan_only_ir(require=False), _wlan_only_ir(require=True)))
    f = next(f for f in result.findings if f.code == "wired.l2.blackhole.new_member_stranded")
    assert f.caused_by == ()
