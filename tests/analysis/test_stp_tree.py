"""Engine-side pins for the relocated election helper + the active-topology
preparation layer (Spec-4 Task 2): pseudo-edge synthesis, port/link exclusion,
and LAG member-end costing, ahead of election/roles (later tasks)."""
from digital_twin.analysis.stp_tree import (
    ABSTAIN,
    DEFAULT_PRIORITY,
    active_topology,
    root_of,
)
from digital_twin.ir import ConfidenceLevel, IRBuilder
from tests.factories import ap, link, make_port, sw


def test_root_of_semantics_pinned_at_new_home():
    # <2 switches -> None; else min (priority ?? 32768, device_id); assumed flag
    b = IRBuilder()
    b.add_device(sw("aa01", stp_priority=4096)).add_device(sw("bb02"))  # None -> 32768
    ir = b.build()
    assert root_of(ir, frozenset({"aa01"})) is None
    assert root_of(ir, frozenset({"aa01", "bb02"})) == ("aa01", True)
    assert DEFAULT_PRIORITY == 32768 and ABSTAIN == "abstain"


# ---------- active_topology: pseudo-edges (self-loops) ----------------------


def _loop_ports(dev, a="ge-0/0/8", b="ge-0/0/9", reciprocal=True):
    pa = make_port(dev, a, self_loop_peer=f"{dev}:{b}")
    pb = make_port(dev, b, self_loop_peer=f"{dev}:{a}" if reciprocal else None)
    return pa, pb


def _ir_with_loop_ports(dev, a="ge-0/0/8", b="ge-0/0/9", reciprocal=True):
    pa, pb = _loop_ports(dev, a, b, reciprocal=reciprocal)
    builder = IRBuilder().add_device(sw(dev))
    builder.add_port(pa).add_port(pb)
    return builder.build()


def test_pseudo_edges_synthesized_without_links():
    # reciprocal claim, NO Link minted (Spec-3 same-device skip) -> one pseudo-edge
    top = active_topology(_ir_with_loop_ports("aa0000000001"))
    assert len(top.pseudo_edges) == 1  # deduped frozenset pair
    assert top.pseudo_edges[0].node == "aa0000000001"
    # deterministic: min(port name) first
    assert top.pseudo_edges[0].port_a == "aa0000000001:ge-0/0/8"
    assert top.pseudo_edges[0].port_b == "aa0000000001:ge-0/0/9"


def test_one_sided_claim_synthesizes_nothing_but_notes():
    top = active_topology(_ir_with_loop_ports("aa0000000001", reciprocal=False))
    assert not top.pseudo_edges
    assert any("one-sided" in n for n in top.notes)


def test_pseudo_edge_excluded_when_one_claimed_port_is_disabled():
    dev = "aa0000000001"
    pa = make_port(dev, "ge-0/0/8", self_loop_peer=f"{dev}:ge-0/0/9", disabled=True)
    pb = make_port(dev, "ge-0/0/9", self_loop_peer=f"{dev}:ge-0/0/8")
    ir = IRBuilder().add_device(sw(dev)).add_port(pa).add_port(pb).build()
    top = active_topology(ir)
    assert not top.pseudo_edges


# ---------- active_topology: link/port exclusion -----------------------------


def test_disabled_and_bpdu_filter_ends_excluded():
    # a link whose far end is bpdu_filter'd contributes NO edge and NO port ends
    b = IRBuilder()
    b.add_device(sw("aa01")).add_device(sw("bb02"))
    b.add_port(make_port("aa01", "ge-0/0/1"))
    b.add_port(make_port("bb02", "ge-0/0/1", bpdu_filter=True))
    b.add_link(link("aa01:ge-0/0/1", "bb02:ge-0/0/1"))
    top = active_topology(b.build())
    assert top.edges == ()


def test_disabled_end_excludes_the_link():
    b = IRBuilder()
    b.add_device(sw("aa01")).add_device(sw("bb02"))
    b.add_port(make_port("aa01", "ge-0/0/1", disabled=True))
    b.add_port(make_port("bb02", "ge-0/0/1"))
    b.add_link(link("aa01:ge-0/0/1", "bb02:ge-0/0/1"))
    top = active_topology(b.build())
    assert top.edges == ()


def test_non_switch_ends_excluded():
    # switch<->AP link -> not in the active subgraph
    b = IRBuilder()
    b.add_device(sw("aa01")).add_device(ap("cc03"))
    b.add_port(make_port("aa01", "ge-0/0/1"))
    b.add_port(make_port("cc03", "eth0"))
    b.add_link(link("aa01:ge-0/0/1", "cc03:eth0"))
    top = active_topology(b.build())
    assert top.edges == ()


def test_stp_edge_port_stays_in_the_active_subgraph():
    # stp_edge is a role hint, NOT an exclusion criterion — a modeled
    # switch<->switch link with an edge-configured port still participates.
    b = IRBuilder()
    b.add_device(sw("aa01")).add_device(sw("bb02"))
    b.add_port(make_port("aa01", "ge-0/0/1", stp_edge=True))
    b.add_port(make_port("bb02", "ge-0/0/1"))
    b.add_link(link("aa01:ge-0/0/1", "bb02:ge-0/0/1"))
    top = active_topology(b.build())
    assert len(top.edges) == 1


def test_normal_two_switch_link_produces_one_active_edge():
    b = IRBuilder()
    b.add_device(sw("aa01")).add_device(sw("bb02"))
    b.add_port(make_port("aa01", "ge-0/0/1"))
    b.add_port(make_port("bb02", "ge-0/0/1"))
    b.add_link(link("aa01:ge-0/0/1", "bb02:ge-0/0/1"))
    top = active_topology(b.build())
    assert len(top.edges) == 1
    edge = top.edges[0]
    assert {edge.a.node, edge.b.node} == {"aa01", "bb02"}
    assert edge.a.ports == ("aa01:ge-0/0/1",)
    assert edge.b.ports == ("bb02:ge-0/0/1",)


# ---------- active_topology: LAG bundle ends + cost ladder -------------------


def test_lag_bundle_is_one_logical_edge_with_member_ends():
    # two member links, one bundle_id -> ONE ActiveEdge; ends carry BOTH member
    # ports per side; lag=True
    b = IRBuilder()
    b.add_device(sw("aa01")).add_device(sw("bb02"))
    b.add_port(make_port("aa01", "ge-0/0/1"))
    b.add_port(make_port("aa01", "ge-0/0/2"))
    b.add_port(make_port("bb02", "ge-0/0/1"))
    b.add_port(make_port("bb02", "ge-0/0/2"))
    from digital_twin.ir.entities import LinkKind

    b.add_link(link("aa01:ge-0/0/1", "bb02:ge-0/0/1", kind=LinkKind.LAG, bundle="ae0"))
    b.add_link(link("aa01:ge-0/0/2", "bb02:ge-0/0/2", kind=LinkKind.LAG, bundle="ae0"))
    top = active_topology(b.build())
    assert len(top.edges) == 1
    edge = top.edges[0]
    a_end = edge.a if edge.a.node == "aa01" else edge.b
    b_end = edge.b if edge.a.node == "aa01" else edge.a
    assert set(a_end.ports) == {"aa01:ge-0/0/1", "aa01:ge-0/0/2"}
    assert set(b_end.ports) == {"bb02:ge-0/0/1", "bb02:ge-0/0/2"}
    assert a_end.lag and b_end.lag


def test_lag_member_excluded_drops_only_that_member():
    # one bpdu_filter'd member -> that member drops out of the end, the other
    # member keeps the end (and the edge) alive
    b = IRBuilder()
    b.add_device(sw("aa01")).add_device(sw("bb02"))
    b.add_port(make_port("aa01", "ge-0/0/1"))
    b.add_port(make_port("aa01", "ge-0/0/2", bpdu_filter=True))
    b.add_port(make_port("bb02", "ge-0/0/1"))
    b.add_port(make_port("bb02", "ge-0/0/2"))
    from digital_twin.ir.entities import LinkKind

    b.add_link(link("aa01:ge-0/0/1", "bb02:ge-0/0/1", kind=LinkKind.LAG, bundle="ae0"))
    b.add_link(link("aa01:ge-0/0/2", "bb02:ge-0/0/2", kind=LinkKind.LAG, bundle="ae0"))
    top = active_topology(b.build())
    assert len(top.edges) == 1
    edge = top.edges[0]
    a_end = edge.a if edge.a.node == "aa01" else edge.b
    assert a_end.ports == ("aa01:ge-0/0/1",)  # the filtered member is gone


def test_lag_all_members_excluded_drops_the_whole_edge():
    b = IRBuilder()
    b.add_device(sw("aa01")).add_device(sw("bb02"))
    b.add_port(make_port("aa01", "ge-0/0/1", bpdu_filter=True))
    b.add_port(make_port("aa01", "ge-0/0/2", disabled=True))
    b.add_port(make_port("bb02", "ge-0/0/1"))
    b.add_port(make_port("bb02", "ge-0/0/2"))
    from digital_twin.ir.entities import LinkKind

    b.add_link(link("aa01:ge-0/0/1", "bb02:ge-0/0/1", kind=LinkKind.LAG, bundle="ae0"))
    b.add_link(link("aa01:ge-0/0/2", "bb02:ge-0/0/2", kind=LinkKind.LAG, bundle="ae0"))
    top = active_topology(b.build())
    assert top.edges == ()


def test_cost_ladder_uses_observed_speed_over_configured_speed():
    b = IRBuilder()
    b.add_device(sw("aa01")).add_device(sw("bb02"))
    b.add_port(make_port("aa01", "ge-0/0/1", speed="1g", observed_speed="10g"))
    b.add_port(make_port("bb02", "ge-0/0/1"))
    b.add_link(link("aa01:ge-0/0/1", "bb02:ge-0/0/1"))
    top = active_topology(b.build())
    edge = top.edges[0]
    a_end = edge.a if edge.a.node == "aa01" else edge.b
    assert a_end.cost == 2_000  # 10g ladder value, not the 1g config value
    assert not a_end.cost_defaulted


def test_cost_defaults_to_1g_and_flags_when_speed_unknown():
    b = IRBuilder()
    b.add_device(sw("aa01")).add_device(sw("bb02"))
    b.add_port(make_port("aa01", "ge-0/0/1"))  # no speed, no observed_speed
    b.add_port(make_port("bb02", "ge-0/0/1"))
    b.add_link(link("aa01:ge-0/0/1", "bb02:ge-0/0/1"))
    top = active_topology(b.build())
    edge = top.edges[0]
    a_end = edge.a if edge.a.node == "aa01" else edge.b
    assert a_end.cost == 20_000  # 1g default
    assert a_end.cost_defaulted


def test_lag_end_cost_is_min_over_member_ends():
    b = IRBuilder()
    b.add_device(sw("aa01")).add_device(sw("bb02"))
    b.add_port(make_port("aa01", "ge-0/0/1", observed_speed="1g"))
    b.add_port(make_port("aa01", "ge-0/0/2", observed_speed="10g"))
    b.add_port(make_port("bb02", "ge-0/0/1"))
    b.add_port(make_port("bb02", "ge-0/0/2"))
    from digital_twin.ir.entities import LinkKind

    b.add_link(link("aa01:ge-0/0/1", "bb02:ge-0/0/1", kind=LinkKind.LAG, bundle="ae0"))
    b.add_link(link("aa01:ge-0/0/2", "bb02:ge-0/0/2", kind=LinkKind.LAG, bundle="ae0"))
    top = active_topology(b.build())
    edge = top.edges[0]
    a_end = edge.a if edge.a.node == "aa01" else edge.b
    assert a_end.cost == 2_000  # min(20_000, 2_000) — the 10g member wins


def test_link_confidence_captured_on_active_edge():
    b = IRBuilder()
    b.add_device(sw("aa01")).add_device(sw("bb02"))
    b.add_port(make_port("aa01", "ge-0/0/1"))
    b.add_port(make_port("bb02", "ge-0/0/1"))
    b.add_link(link("aa01:ge-0/0/1", "bb02:ge-0/0/1"))
    top = active_topology(b.build())
    assert top.edges[0].link_confidence is ConfidenceLevel.HIGH
