from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, CoverageState, Status
from digital_twin.checks.wired.subnet_overlap import SubnetOverlapCheck
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


def test_introduced_overlap_warns():
    base = _ir(Vlan(vlan_id=10, subnet="10.0.0.0/24"))
    prop = _ir(Vlan(vlan_id=10, subnet="10.0.0.0/24"),
               Vlan(vlan_id=20, subnet="10.0.0.0/25"))   # overlaps vlan 10
    res = SubnetOverlapCheck().run(_ctx(base, prop))
    assert res.status is Status.WARN and res.findings[0].code.endswith(".introduced")


def test_disjoint_subnets_clean():
    ir = _ir(Vlan(vlan_id=10, subnet="10.0.0.0/24"), Vlan(vlan_id=20, subnet="10.1.0.0/24"))
    assert SubnetOverlapCheck().run(_ctx(ir, ir)).status is Status.PASS


def test_unresolved_subnet_skipped_relevance_scoped():
    # an untouched unresolved subnet must NOT taint -> COMPLETE coverage, no finding
    ir = _ir(Vlan(vlan_id=10, subnet="10.0.0.0/24"),
             Vlan(vlan_id=20, subnet="{{var}}", subnet_unresolved=True))
    res = SubnetOverlapCheck().run(_ctx(ir, ir))
    assert res.status is Status.PASS and res.coverage.state is CoverageState.COMPLETE


def test_touched_unparseable_subnet_is_partial_note():
    # a present-but-unparseable CIDR on a DELTA-TOUCHED vlan -> PARTIAL note (not silent PASS)
    base = _ir(Vlan(vlan_id=10, subnet="10.0.0.0/24"))
    prop = _ir(Vlan(vlan_id=10, subnet="10.0.0.0/24"), Vlan(vlan_id=20, subnet="not-a-cidr"))
    res = SubnetOverlapCheck().run(_ctx(base, prop))
    assert res.coverage.state is CoverageState.PARTIAL
    assert any("20" in n for n in res.coverage.notes)
