from digital_twin.analysis.context import AnalysisContext
from digital_twin.analysis.exits import ExitKind
from digital_twin.ir import ConfidenceLevel, IRBuilder, Vlan
from digital_twin.ir.entities import Device, DeviceRole
from digital_twin.ir.provenance import Provenance
from tests.factories import irb, link, sw, trunk_port


def _base(with_irb: bool, with_gateway: bool = False, gw_one_sided: bool = False):
    b = IRBuilder()
    b.add_device(sw("A"))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    b.add_port(trunk_port("A", "down", tagged=(10,)))
    if with_irb:
        b.add_l3intf(irb("A", 10))
    if with_gateway:
        b.add_device(Device(id="GW", role=DeviceRole.GATEWAY, site="s1"))
        b.add_port(trunk_port("A", "up", tagged=(10,)))
        b.add_port(trunk_port("GW", "down", tagged=(10,)))
        prov = Provenance.LLDP_ONE_SIDED if gw_one_sided else Provenance.LLDP_TWO_SIDED
        b.add_link(link("A:up", "GW:down", prov=prov))
    return b.build()


def test_rule1_irb_is_high_confidence_exit():
    res = AnalysisContext(_base(with_irb=True)).exit_for(10)
    assert res.kind is ExitKind.IRB
    assert res.nodes == ("A",)
    assert res.confidence is not None and res.confidence.level is ConfidenceLevel.HIGH


def test_rule2_boundary_uplink_two_sided_is_high():
    res = AnalysisContext(_base(with_irb=False, with_gateway=True)).exit_for(10)
    assert res.kind is ExitKind.BOUNDARY_UPLINK
    assert res.nodes == ("GW",)
    assert res.confidence is not None and res.confidence.level is ConfidenceLevel.HIGH


def test_rule2_one_sided_uplink_is_low():
    res = AnalysisContext(_base(with_irb=False, with_gateway=True, gw_one_sided=True)).exit_for(10)
    assert res.kind is ExitKind.BOUNDARY_UPLINK
    assert res.confidence is not None and res.confidence.level is ConfidenceLevel.LOW


def test_rule1_wins_over_rule2():
    res = AnalysisContext(_base(with_irb=True, with_gateway=True)).exit_for(10)
    assert res.kind is ExitKind.IRB


def test_rule3_no_exit_found():
    res = AnalysisContext(_base(with_irb=False)).exit_for(10)
    assert res.kind is ExitKind.NONE
    assert res.confidence is None  # absent -> INSUFFICIENT_DATA at the check
