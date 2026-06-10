from digital_twin.analysis.context import AnalysisContext
from digital_twin.ir import IRBuilder, Vlan
from tests.factories import access_port, irb, link, sw, trunk_port


def _split_ir(connected: bool):
    """A--B (carrying vlan 10) and C isolated-with-member; IRB on B."""
    b = IRBuilder()
    for d in ("A", "B", "C"):
        b.add_device(sw(d))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    b.add_port(trunk_port("A", "to-B", tagged=(10,)))
    b.add_port(trunk_port("B", "to-A", tagged=(10,)))
    b.add_link(link("A:to-B", "B:to-A"))
    b.add_port(access_port("A", "acc", 10))
    b.add_port(access_port("C", "acc", 10))
    b.add_l3intf(irb("B", 10))
    if connected:
        b.add_port(trunk_port("B", "to-C", tagged=(10,)))
        b.add_port(trunk_port("C", "to-B", tagged=(10,)))
        b.add_link(link("B:to-C", "C:to-B"))
    return b.build()


def test_components_partition_the_vlan_graph():
    comps = AnalysisContext(_split_ir(connected=False)).vlan_components(10)
    assert sorted(sorted(c.nodes) for c in comps) == [["A", "B"], ["C"]]


def test_membership_and_exit_reachability_per_component():
    comps = AnalysisContext(_split_ir(connected=False)).vlan_components(10)
    by_nodes = {tuple(sorted(c.nodes)): c for c in comps}
    ab, c = by_nodes[("A", "B")], by_nodes[("C",)]
    assert ab.has_members and ab.reaches_exit  # access port on A, IRB on B
    assert c.has_members and not c.reaches_exit  # member but stranded


def test_single_component_when_connected():
    comps = AnalysisContext(_split_ir(connected=True)).vlan_components(10)
    assert len(comps) == 1 and comps[0].reaches_exit
