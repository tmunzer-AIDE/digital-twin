from digital_twin.analysis.context import AnalysisContext
from digital_twin.ir import ConfidenceLevel, IRBuilder, Vlan
from digital_twin.ir.entities import LinkKind
from digital_twin.ir.provenance import Provenance
from tests.factories import link, sw, trunk_port


def _builder(*devs):
    b = IRBuilder()
    for d in devs:
        b.add_device(sw(d))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    return b


def _trunk(b, dev, name):
    b.add_port(trunk_port(dev, name, tagged=(10,)))


def test_triangle_is_one_cycle():
    b = _builder("A", "B", "C")
    for dev, peers in (("A", ("B", "C")), ("B", ("A", "C")), ("C", ("A", "B"))):
        for p in peers:
            _trunk(b, dev, f"to-{p}")
    b.add_link(link("A:to-B", "B:to-A"))
    b.add_link(link("B:to-C", "C:to-B"))
    b.add_link(link("A:to-C", "C:to-A"))
    cycles = AnalysisContext(b.build()).cycles(10)
    assert len(cycles) == 1
    assert set(cycles[0].nodes) == {"A", "B", "C"}
    assert len(cycles[0].member_ports) == 6  # every port on the ring


def test_parallel_standalone_links_are_a_two_node_cycle():
    b = _builder("A", "B")
    for dev, peer in (("A", "B"), ("B", "A")):
        _trunk(b, dev, f"to-{peer}-1")
        _trunk(b, dev, f"to-{peer}-2")
    b.add_link(link("A:to-B-1", "B:to-A-1"))
    b.add_link(link("A:to-B-2", "B:to-A-2"))
    cycles = AnalysisContext(b.build()).cycles(10)
    assert len(cycles) == 1
    assert set(cycles[0].nodes) == {"A", "B"}


def test_lag_bundle_is_not_a_cycle():
    # two LAG members collapse to ONE logical edge (Plan 1 contract)
    b = _builder("A", "B")
    for dev, peer in (("A", "B"), ("B", "A")):
        _trunk(b, dev, f"to-{peer}-1")
        _trunk(b, dev, f"to-{peer}-2")
    b.add_link(link("A:to-B-1", "B:to-A-1", kind=LinkKind.LAG, bundle="ae0"))
    b.add_link(link("A:to-B-2", "B:to-A-2", kind=LinkKind.LAG, bundle="ae0"))
    assert AnalysisContext(b.build()).cycles(10) == ()


def test_cycle_confidence_is_min_of_edge_confidences():
    b = _builder("A", "B")
    for dev, peer in (("A", "B"), ("B", "A")):
        _trunk(b, dev, f"to-{peer}-1")
        _trunk(b, dev, f"to-{peer}-2")
    b.add_link(link("A:to-B-1", "B:to-A-1"))  # two-sided -> HIGH
    b.add_link(link("A:to-B-2", "B:to-A-2", prov=Provenance.LLDP_ONE_SIDED))  # LOW
    (cycle,) = AnalysisContext(b.build()).cycles(10)
    assert cycle.confidence.level is ConfidenceLevel.LOW  # weakest input governs


def test_no_cycle_on_a_tree():
    b = _builder("A", "B")
    _trunk(b, "A", "to-B")
    _trunk(b, "B", "to-A")
    b.add_link(link("A:to-B", "B:to-A"))
    assert AnalysisContext(b.build()).cycles(10) == ()
