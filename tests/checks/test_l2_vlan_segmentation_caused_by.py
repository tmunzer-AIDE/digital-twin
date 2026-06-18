"""Cause attribution (CA-T12) for wired.l2.vlan_segmentation:
- a `.split` finding names the changed boundary trunk port that separated the
  domain;
- a `.reshape` (INFO) finding carries no cause (caused_by == ()).
"""

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext
from digital_twin.checks.wired.l2_vlan_segmentation import L2VlanSegmentationCheck
from digital_twin.ir import IRBuilder, IRCapability, Vlan, diff_ir
from tests.factories import access_port, link, sw, trunk_port


def _ctx(baseline, proposed):
    return CheckContext(
        baseline=AnalysisContext(baseline),
        proposed=AnalysisContext(proposed),
        diff=diff_ir(baseline, proposed),
    )


def _ids(causes):
    return sorted((c.ref.kind, c.ref.id) for c in causes)


def _split_ir(cut: bool):
    """A -- B -- C, all hold a vlan-10 access port. The delta drops vlan 10 from
    B's port toward C -> {A,B} | {C} split, attributed to B:to-C."""
    b = IRBuilder()
    for d in ("A", "B", "C"):
        b.add_device(sw(d))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    b.add_port(trunk_port("A", "to-B", tagged=(10,)))
    b.add_port(trunk_port("B", "to-A", tagged=(10,)))
    b.add_link(link("A:to-B", "B:to-A"))
    b.add_port(trunk_port("B", "to-C", tagged=() if cut else (10,)))
    b.add_port(trunk_port("C", "to-B", tagged=(10,)))
    b.add_link(link("B:to-C", "C:to-B"))
    for d in ("A", "B", "C"):
        b.add_port(access_port(d, "acc", 10))
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def test_split_finding_names_changed_boundary_port():
    result = L2VlanSegmentationCheck().run(_ctx(_split_ir(cut=False), _split_ir(cut=True)))
    f = next(f for f in result.findings if f.code.endswith(".split"))
    assert _ids(f.caused_by) == [("port", "B:to-C")]


def _reshape_ir(devs):
    b = IRBuilder()
    for d in devs:
        b.add_device(sw(d))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    for d in devs:
        b.add_port(access_port(d, "acc", 10))
    pairs = [("A", "B"), ("B", "C")] if len(devs) == 3 else [("A", "B")]
    for a, c in pairs:
        if a in devs and c in devs:
            b.add_port(trunk_port(a, f"to-{c}", tagged=(10,)))
            b.add_port(trunk_port(c, f"to-{a}", tagged=(10,)))
            b.add_link(link(f"{a}:to-{c}", f"{c}:to-{a}"))
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def test_reshape_finding_has_no_cause():
    # domain contracts (C dropped entirely) without a split -> INFO reshape, no cause
    base = _reshape_ir(("A", "B", "C"))
    prop = _reshape_ir(("A", "B"))
    result = L2VlanSegmentationCheck().run(_ctx(base, prop))
    f = next(f for f in result.findings if f.code.endswith(".reshape"))
    assert f.caused_by == ()
