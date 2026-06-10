import networkx as nx

from digital_twin.ir.confidence import ConfidenceLevel
from digital_twin.ir.entities import LinkKind, Port, PortMode
from digital_twin.ir.model import IRBuilder
from digital_twin.ir.provenance import Provenance
from digital_twin.representations.graph_data import L2Edge
from digital_twin.representations.l2_graph import build_l2_graph, link_carried_vlans
from tests.factories import link, sw, trunk_port


def _trunk(pid: str, native: int | None, tagged: tuple[int, ...]) -> Port:
    return Port(
        id=pid,
        device_id=pid.split(":")[0],
        name="p",
        mode=PortMode.TRUNK,
        native_vlan=native,
        tagged_vlans=tagged,
    )


def _access(pid: str, native: int) -> Port:
    return Port(
        id=pid, device_id=pid.split(":")[0], name="p", mode=PortMode.ACCESS, native_vlan=native
    )


def test_trunk_to_trunk_tagged_intersection_plus_matching_native():
    assert link_carried_vlans(_trunk("d1:p", 1, (10, 30)), _trunk("d2:p", 1, (30, 40))) == {1, 30}


def test_trunk_native_mismatch_drops_native():
    assert link_carried_vlans(_trunk("d1:p", 1, (30,)), _trunk("d2:p", 99, (30,))) == {30}


def test_access_match_carries_native_only():
    assert link_carried_vlans(_access("d1:p", 30), _access("d2:p", 30)) == {30}


def test_access_mismatch_carries_nothing():
    assert link_carried_vlans(_access("d1:p", 10), _access("d2:p", 20)) == set()


def test_access_joins_trunk_only_via_native_not_tagged():
    a = _access("d1:p", 10)
    assert link_carried_vlans(a, _trunk("d2:p", 1, (10, 30))) == set()
    assert link_carried_vlans(a, _trunk("d2:p", 10, (30,))) == {10}


def _edge(g: nx.MultiGraph, u: str, v: str) -> L2Edge:
    return next(iter(g.get_edge_data(u, v).values()))["data"]


def test_single_trunk_one_edge_with_ports_and_vlans():
    pa, pb = trunk_port("d1", "ge-0/0/1", (30,)), trunk_port("d2", "ge-0/0/1", (30,))
    ir = (
        IRBuilder()
        .add_device(sw("d1"))
        .add_device(sw("d2"))
        .add_port(pa)
        .add_port(pb)
        .add_link(link(pa.id, pb.id))
        .build()
    )
    g = build_l2_graph(ir)
    assert g.number_of_edges() == 1
    e = _edge(g, "d1", "d2")
    assert e.vlans == {30}
    assert set(e.member_ports) == {pa.id, pb.id}


def test_two_independent_physical_links_parallel():
    pa1, pb1 = trunk_port("d1", "ge-0/0/1", (30,)), trunk_port("d2", "ge-0/0/1", (30,))
    pa2, pb2 = trunk_port("d1", "ge-0/0/2", (30,)), trunk_port("d2", "ge-0/0/2", (30,))
    ir = (
        IRBuilder()
        .add_device(sw("d1"))
        .add_device(sw("d2"))
        .add_port(pa1)
        .add_port(pb1)
        .add_port(pa2)
        .add_port(pb2)
        .add_link(link(pa1.id, pb1.id))
        .add_link(link(pa2.id, pb2.id))
        .build()
    )
    assert build_l2_graph(ir).number_of_edges() == 2


def test_one_lag_bundle_collapses_unions_vlans_mins_confidence():
    pa1, pb1 = trunk_port("d1", "ae0a", (30,)), trunk_port("d2", "ae0a", (30,))
    pa2, pb2 = trunk_port("d1", "ae0b", (40,)), trunk_port("d2", "ae0b", (40,))
    ir = (
        IRBuilder()
        .add_device(sw("d1"))
        .add_device(sw("d2"))
        .add_port(pa1)
        .add_port(pb1)
        .add_port(pa2)
        .add_port(pb2)
        .add_link(link(pa1.id, pb1.id, LinkKind.LAG, "ae0", Provenance.LLDP_TWO_SIDED))
        .add_link(link(pa2.id, pb2.id, LinkKind.LAG, "ae0", Provenance.LLDP_ONE_SIDED))
        .build()
    )
    g = build_l2_graph(ir)
    assert g.number_of_edges() == 1
    e = _edge(g, "d1", "d2")
    assert e.vlans == {30, 40}
    assert e.confidence.level is ConfidenceLevel.LOW


def test_mclag_bundle_preserves_kind():
    pa, pb = trunk_port("d1", "ae0a", (30,)), trunk_port("d2", "ae0a", (30,))
    ir = (
        IRBuilder()
        .add_device(sw("d1"))
        .add_device(sw("d2"))
        .add_port(pa)
        .add_port(pb)
        .add_link(link(pa.id, pb.id, LinkKind.MCLAG, "ae0"))
        .build()
    )
    assert _edge(build_l2_graph(ir), "d1", "d2").kind == "mclag"


def test_two_independent_lags_same_pair_stay_two_edges():
    pa1, pb1 = trunk_port("d1", "ae0a", (30,)), trunk_port("d2", "ae0a", (30,))
    pa2, pb2 = trunk_port("d1", "ae1a", (30,)), trunk_port("d2", "ae1a", (30,))
    ir = (
        IRBuilder()
        .add_device(sw("d1"))
        .add_device(sw("d2"))
        .add_port(pa1)
        .add_port(pb1)
        .add_port(pa2)
        .add_port(pb2)
        .add_link(link(pa1.id, pb1.id, LinkKind.LAG, "ae0"))
        .add_link(link(pa2.id, pb2.id, LinkKind.LAG, "ae1"))
        .build()
    )
    assert build_l2_graph(ir).number_of_edges() == 2


def test_vc_internal_link_dropped_and_member_folded():
    pa, pb = trunk_port("d1", "vcp0", (30,)), trunk_port("d1b", "vcp1", (30,))
    ir = (
        IRBuilder()
        .add_device(sw("d1", vc_members=("d1b",)))
        .add_device(sw("d1b"))
        .add_port(pa)
        .add_port(pb)
        .add_link(link(pa.id, pb.id, LinkKind.VC))
        .build()
    )
    g = build_l2_graph(ir)
    assert g.number_of_edges() == 0
    assert "d1" in g.nodes and "d1b" not in g.nodes


def test_ap_uplink_edge_carries_switch_side_vlans():
    # an AP is a VLAN-TRANSPARENT bridge: its eth port has no vlan facts (the
    # lldp ingester cannot invent them), so the SWITCH side defines delivery
    from digital_twin.ir.entities import Port, PortMode
    from tests.factories import ap

    b = IRBuilder()
    b.add_device(sw("SW")).add_device(ap("AP1"))
    b.add_port(trunk_port("SW", "to-ap", tagged=(30, 40), native=1))
    b.add_port(Port(id="AP1:eth0", device_id="AP1", name="eth0", mode=PortMode.TRUNK))
    b.add_link(link("AP1:eth0", "SW:to-ap"))
    g = build_l2_graph(b.build())
    edge = next(iter(g.edges(data=True)))[2]["data"]
    assert edge.vlans == {1, 30, 40}


def test_switch_to_switch_edges_unchanged_by_transparency():
    b = IRBuilder()
    b.add_device(sw("A")).add_device(sw("B"))
    b.add_port(trunk_port("A", "up", tagged=(30, 40)))
    b.add_port(trunk_port("B", "down", tagged=(30,)))
    b.add_link(link("A:up", "B:down"))
    g = build_l2_graph(b.build())
    edge = next(iter(g.edges(data=True)))[2]["data"]
    assert edge.vlans == {30}  # intersection semantics stay exact
