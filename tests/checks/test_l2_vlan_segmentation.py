"""l2.vlan_segmentation: split -> WARN(WARNING, HIGH); expansion/contraction
-> PASS with INFO. Purely structural — no intent judged."""

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.wired.l2_vlan_segmentation import L2VlanSegmentationCheck
from digital_twin.contracts import Severity
from digital_twin.ir import ConfidenceLevel, IRBuilder, IRCapability, Vlan, diff_ir
from tests.factories import access_port, link, sw, trunk_port


def _chain_ir(*links: tuple[str, str], devs=("A", "B", "C")):
    b = IRBuilder()
    for d in devs:
        b.add_device(sw(d))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    for d in devs:
        b.add_port(access_port(d, "acc", 10))
    for a, c in links:
        b.add_port(trunk_port(a, f"to-{c}", tagged=(10,)))
        b.add_port(trunk_port(c, f"to-{a}", tagged=(10,)))
        b.add_link(link(f"{a}:to-{c}", f"{c}:to-{a}"))
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def _ctx(baseline, proposed):
    return CheckContext(
        baseline=AnalysisContext(baseline),
        proposed=AnalysisContext(proposed),
        diff=diff_ir(baseline, proposed),
    )


def test_split_warns_high_confidence():
    base = _chain_ir(("A", "B"), ("B", "C"))  # one domain A-B-C
    prop = _chain_ir(("A", "B"))  # C cut off -> 2 components
    result = L2VlanSegmentationCheck().run(_ctx(base, prop))
    assert result.status is Status.WARN
    f = result.findings[0]
    assert f.severity is Severity.WARNING
    assert f.confidence.level is ConfidenceLevel.HIGH


def test_contraction_without_split_is_info_pass():
    base = _chain_ir(("A", "B"))
    prop = _chain_ir(("A", "B"), devs=("A", "B"))  # C (isolated member) gone entirely
    result = L2VlanSegmentationCheck().run(_ctx(base, prop))
    assert result.status is Status.PASS
    assert all(f.severity is Severity.INFO for f in result.findings)


def test_no_structural_change_passes_quietly():
    base = _chain_ir(("A", "B"), ("B", "C"))
    result = L2VlanSegmentationCheck().run(_ctx(base, _chain_ir(("A", "B"), ("B", "C"))))
    assert result.status is Status.PASS and result.findings == ()
