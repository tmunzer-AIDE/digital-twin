from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, CoverageState, Status
from digital_twin.checks.wired.vlan_collision import VlanCollisionCheck
from digital_twin.contracts import Severity
from digital_twin.ir import IRCapability, Vlan
from digital_twin.ir.diff import diff_ir
from digital_twin.ir.model import IRBuilder


def _ir(*vlans):
    b = IRBuilder().with_capability(IRCapability.WIRED_L2)
    for v in vlans:
        b.add_vlan(v)
    return b.build()


def _ctx(base, prop):
    return CheckContext(baseline=AnalysisContext(base), proposed=AnalysisContext(prop),
                        diff=diff_ir(base, prop))


def test_introduced_collision_warns():
    base = _ir(Vlan(vlan_id=10, collisions=()))
    prop = _ir(Vlan(vlan_id=10, collisions=("guest",)))   # a second name now claims vlan 10
    res = VlanCollisionCheck().run(_ctx(base, prop))
    assert res.status is Status.WARN
    assert res.findings[0].code.endswith(".introduced")
    assert res.coverage.state is CoverageState.COMPLETE


def test_no_collision_is_clean():
    ir = _ir(Vlan(vlan_id=10, collisions=()), Vlan(vlan_id=20, collisions=()))
    assert VlanCollisionCheck().run(_ctx(ir, ir)).status is Status.PASS


def test_preexisting_collision_is_info():
    ir = _ir(Vlan(vlan_id=10, collisions=("guest",)))
    res = VlanCollisionCheck().run(_ctx(ir, ir))
    assert res.status is Status.PASS
    assert res.findings[0].severity is Severity.INFO
    assert res.findings[0].code.endswith(".preexisting")


def test_changed_collision_set_reads_as_introduced():
    # the KEY carries the collision facts -> a CHANGED set is a new violation, not pre-existing
    base = _ir(Vlan(vlan_id=10, collisions=("guest",)))
    prop = _ir(Vlan(vlan_id=10, collisions=("guest", "lab")))
    res = VlanCollisionCheck().run(_ctx(base, prop))
    assert res.status is Status.WARN and res.findings[0].code.endswith(".introduced")
