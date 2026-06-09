import networkx as nx

from digital_twin.ir.confidence import ConfidenceLevel
from digital_twin.ir.model import IRBuilder
from digital_twin.representations.graph_data import L2Edge, VlanNode
from digital_twin.representations.l2_graph import build_l2_graph
from digital_twin.representations.vlan_graph import build_vlan_graph
from tests.factories import access_port, irb, link, sw, trunk_port


def _cyclomatic(g: nx.MultiGraph) -> int:
    return g.number_of_edges() - g.number_of_nodes() + nx.number_connected_components(g)


def _node(g: nx.MultiGraph, n: str) -> VlanNode:
    return g.nodes[n]["data"]


def test_excludes_pure_non_member_nodes():
    pa, pb = trunk_port("d1", "u1", (30,)), trunk_port("d2", "u1", (30,))
    pc, pd = trunk_port("d1", "u2", (40,)), trunk_port("d3", "u1", (40,))
    ir = (
        IRBuilder()
        .add_device(sw("d1"))
        .add_device(sw("d2"))
        .add_device(sw("d3"))
        .add_port(pa)
        .add_port(pb)
        .add_port(pc)
        .add_port(pd)
        .add_link(link(pa.id, pb.id))
        .add_link(link(pc.id, pd.id))
        .build()
    )
    v30 = build_vlan_graph(ir, build_l2_graph(ir), 30)
    assert set(v30.nodes) == {"d1", "d2"}


def test_annotates_access_ports_and_exits():
    acc = access_port("d2", "ge-0/0/9", 30)
    i = irb("d1", 30, "10.0.30.0/24")
    pa, pb = trunk_port("d1", "u1", (30,)), trunk_port("d2", "u1", (30,))
    ir = (
        IRBuilder()
        .add_device(sw("d1"))
        .add_device(sw("d2"))
        .add_port(pa)
        .add_port(pb)
        .add_port(acc)
        .add_l3intf(i)
        .add_link(link(pa.id, pb.id))
        .build()
    )
    v30 = build_vlan_graph(ir, build_l2_graph(ir), 30)
    assert _node(v30, "d2").access_ports == [acc.id]
    assert _node(v30, "d2").is_member is True
    assert _node(v30, "d1").exits == [i.id]
    assert _node(v30, "d1").is_exit is True


def test_isolated_member_included_and_marked():
    acc = access_port("d2", "ge-0/0/9", 30)
    ir = (
        IRBuilder()
        .add_device(sw("d1"))
        .add_device(sw("d2"))
        .add_port(trunk_port("d1", "u1", (30,)))
        .add_port(acc)
        .build()
    )
    v30 = build_vlan_graph(ir, build_l2_graph(ir), 30)
    assert "d2" in v30.nodes
    assert _node(v30, "d2").is_member is True
    assert v30.degree("d2") == 0  # isolated -> blackhole candidate for later checks


def test_edge_payload_preserved_and_ring_is_a_cycle():
    ports = {
        ("d1", "a"): trunk_port("d1", "a", (30,)),
        ("d2", "a"): trunk_port("d2", "a", (30,)),
        ("d2", "b"): trunk_port("d2", "b", (30,)),
        ("d3", "a"): trunk_port("d3", "a", (30,)),
        ("d3", "b"): trunk_port("d3", "b", (30,)),
        ("d1", "b"): trunk_port("d1", "b", (30,)),
    }
    b = IRBuilder().add_device(sw("d1")).add_device(sw("d2")).add_device(sw("d3"))
    for p in ports.values():
        b.add_port(p)
    b.add_link(link(ports[("d1", "a")].id, ports[("d2", "a")].id))
    b.add_link(link(ports[("d2", "b")].id, ports[("d3", "a")].id))
    b.add_link(link(ports[("d3", "b")].id, ports[("d1", "b")].id))
    ir = b.build()
    v30 = build_vlan_graph(ir, build_l2_graph(ir), 30)
    assert _cyclomatic(v30) == 1
    edge = next(iter(v30.edges(data=True)))[2]["data"]
    assert isinstance(edge, L2Edge)
    assert edge.confidence.level is ConfidenceLevel.HIGH
