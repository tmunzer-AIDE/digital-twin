from digital_twin.analysis.context import AnalysisContext
from digital_twin.ir import IRBuilder, Vlan
from tests.factories import access_port, irb, link, sw, trunk_port


def _ir():
    b = IRBuilder()
    b.add_device(sw("A")).add_device(sw("B"))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    b.add_port(trunk_port("A", "ge-0/0/0", tagged=(10,)))
    b.add_port(trunk_port("B", "ge-0/0/0", tagged=(10,)))
    b.add_port(access_port("A", "ge-0/0/1", 10))
    b.add_l3intf(irb("B", 10))
    b.add_link(link("A:ge-0/0/0", "B:ge-0/0/0"))
    return b.build()


def test_l2_graph_is_memoized():
    ctx = AnalysisContext(_ir())
    assert ctx.l2_graph() is ctx.l2_graph()  # same object, built once


def test_vlan_graph_memoized_per_vlan():
    ctx = AnalysisContext(_ir())
    assert ctx.vlan_graph(10) is ctx.vlan_graph(10)
    assert set(ctx.vlan_graph(10).nodes) == {"A", "B"}


def test_ir_and_capabilities_exposed():
    ir = _ir()
    ctx = AnalysisContext(ir)
    assert ctx.ir is ir
    assert ctx.capabilities == ir.capabilities
