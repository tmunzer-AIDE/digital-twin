"""wired.l3.gateway_gap (MVP: ROUTE-GW): a ROUTED network (subnet declared)
must have an L3 interface somewhere — switch IRB or gateway ip_config. The
delta removing the only modeled one -> ERROR (UNSAFE at HIGH); newly-declared
routed intent with no modeled interface -> WARNING/MEDIUM (could live on an
unmodeled box); the same gap already in the baseline -> INFO context."""

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.wired.gateway_gap import GatewayGapCheck
from digital_twin.contracts import Severity
from digital_twin.ir import ConfidenceLevel, IRBuilder, IRCapability, Vlan, diff_ir
from tests.factories import irb, sw


def _ir(*, routed=True, with_irb=True):
    b = IRBuilder().add_device(sw("S"))
    b.add_vlan(Vlan(vlan_id=10, name="corp", subnet="198.51.100.0/24" if routed else None))
    if with_irb:
        b.add_l3intf(irb("S", 10, subnet="198.51.100.0/24"))
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def _run(base, prop):
    return GatewayGapCheck().run(
        CheckContext(
            baseline=AnalysisContext(base), proposed=AnalysisContext(prop), diff=diff_ir(base, prop)
        )
    )


def test_removing_the_only_l3_interface_of_a_routed_network_is_unsafe():
    result = _run(_ir(with_irb=True), _ir(with_irb=False))
    assert result.status is Status.FAIL
    f = result.findings[0]
    assert f.code == "wired.l3.gateway_gap.removed"
    assert f.severity is Severity.ERROR and f.confidence.level is ConfidenceLevel.HIGH
    assert f.evidence["vlan"] == 10


def test_newly_routed_network_without_modeled_l3_is_a_warning():
    # the interface could live on an unmodeled box -> MEDIUM, REVIEW not UNSAFE
    result = _run(_ir(routed=False, with_irb=False), _ir(routed=True, with_irb=False))
    assert result.status is Status.WARN
    f = result.findings[0]
    assert f.code == "wired.l3.gateway_gap.unserved"
    assert f.severity is Severity.WARNING and f.confidence.level is ConfidenceLevel.MEDIUM


def test_preexisting_gap_is_info_context():
    result = _run(_ir(with_irb=False), _ir(with_irb=False))
    assert result.status is Status.PASS
    f = result.findings[0]
    assert f.code == "wired.l3.gateway_gap.preexisting" and f.severity is Severity.INFO


def test_served_routed_network_is_silent():
    assert _run(_ir(), _ir()).findings == ()


def test_unrouted_vlan_never_fires():
    assert _run(_ir(routed=False, with_irb=False), _ir(routed=False, with_irb=False)).findings == ()