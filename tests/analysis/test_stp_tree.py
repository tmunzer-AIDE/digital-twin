"""Engine-side pins for the relocated election helper + the active-topology
preparation layer (Spec-4 Task 2): pseudo-edge synthesis, port/link exclusion,
and LAG member-end costing, ahead of election/roles (later tasks)."""
from digital_twin.analysis.stp_tree import (
    ABSTAIN,
    DEFAULT_PRIORITY,
    active_topology,
    component_rpc,
    predict_stp_tree,
    root_of,
)
from digital_twin.ir import ConfidenceLevel, IRBuilder
from digital_twin.ir.entities import LinkKind
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


def test_pseudo_edge_excluded_when_one_claimed_port_is_bpdu_filtered():
    dev = "aa0000000001"
    pa = make_port(dev, "ge-0/0/8", self_loop_peer=f"{dev}:ge-0/0/9", bpdu_filter=True)
    pb = make_port(dev, "ge-0/0/9", self_loop_peer=f"{dev}:ge-0/0/8")
    ir = IRBuilder().add_device(sw(dev)).add_port(pa).add_port(pb).build()
    top = active_topology(ir)
    assert not top.pseudo_edges


def test_pseudo_edges_deterministically_sorted_by_insertion_order():
    """Pseudo-edges must be ordered by (node, port_a, port_b) regardless of
    port insertion order. Build TWO reciprocal pairs added in REVERSE name order:
    ge-0/0/8+9 added AFTER ge-0/0/1+2. Verify pseudo_edges come out sorted."""
    dev = "aa0000000001"
    builder = IRBuilder().add_device(sw(dev))

    # Add ge-0/0/8 and ge-0/0/9 FIRST (reverse lexicographic to ge-0/0/1+2)
    p8 = make_port(dev, "ge-0/0/8", self_loop_peer=f"{dev}:ge-0/0/9")
    p9 = make_port(dev, "ge-0/0/9", self_loop_peer=f"{dev}:ge-0/0/8")
    builder.add_port(p8).add_port(p9)

    # Add ge-0/0/1 and ge-0/0/2 SECOND (should sort first)
    p1 = make_port(dev, "ge-0/0/1", self_loop_peer=f"{dev}:ge-0/0/2")
    p2 = make_port(dev, "ge-0/0/2", self_loop_peer=f"{dev}:ge-0/0/1")
    builder.add_port(p1).add_port(p2)

    ir = builder.build()
    top = active_topology(ir)

    # Should have TWO pseudo-edges
    assert len(top.pseudo_edges) == 2

    # port_a values should be sorted: ge-0/0/1 should come before ge-0/0/8
    # despite ge-0/0/8 being inserted first
    assert top.pseudo_edges[0].port_a == f"{dev}:ge-0/0/1"
    assert top.pseudo_edges[1].port_a == f"{dev}:ge-0/0/8"


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


# ---------- speed-disagreement honesty cap (spec Cost-model rule) -----------


def test_known_ends_speed_disagreement_notes_and_caps_medium():
    # aa01 end observed 1g, bb02 end observed 10g — BOTH known (not defaulted)
    # but disagreeing: the spec mandates a note + a MEDIUM confidence cap,
    # while directional costs stay exactly as computed (never collapsed to min).
    b = IRBuilder()
    b.add_device(sw("aa01", stp_priority=0)).add_device(sw("bb02", stp_priority=4096))
    b.add_port(make_port("aa01", "ge-0/0/1", observed_speed="1g"))
    b.add_port(make_port("bb02", "ge-0/0/1", observed_speed="10g"))
    b.add_link(link("aa01:ge-0/0/1", "bb02:ge-0/0/1"))
    ir = b.build()

    top = active_topology(ir)
    edge = top.edges[0]
    a_end = edge.a if edge.a.node == "aa01" else edge.b
    b_end = edge.b if edge.a.node == "aa01" else edge.a
    # directional costs untouched — never collapsed to min
    assert a_end.cost == 20_000  # 1g
    assert b_end.cost == 2_000  # 10g
    assert edge.link_confidence is ConfidenceLevel.MEDIUM
    all_notes = top.notes + tuple(
        n for n in predict_stp_tree(ir).notes if n not in top.notes
    )
    assert any("speed disagreement" in n for n in all_notes)

    prediction = predict_stp_tree(ir)
    comp = prediction.components[0]
    # every port prediction on this edge caps at MEDIUM, never HIGH
    for port in comp.ports.values():
        assert port.confidence is not ConfidenceLevel.HIGH
    assert any("speed disagreement" in n for n in prediction.notes)


def test_matching_known_speeds_no_disagreement_note_stays_high():
    b = IRBuilder()
    b.add_device(sw("aa01", stp_priority=0)).add_device(sw("bb02", stp_priority=4096))
    b.add_port(make_port("aa01", "ge-0/0/1", observed_speed="10g"))
    b.add_port(make_port("bb02", "ge-0/0/1", observed_speed="10g"))
    b.add_link(link("aa01:ge-0/0/1", "bb02:ge-0/0/1"))
    ir = b.build()
    top = active_topology(ir)
    assert top.edges[0].link_confidence is ConfidenceLevel.HIGH
    assert not any("speed disagreement" in n for n in top.notes)

    prediction = predict_stp_tree(ir)
    comp = prediction.components[0]
    root_port = comp.ports["bb02:ge-0/0/1"]
    assert root_port.confidence is ConfidenceLevel.HIGH
    assert not any("speed disagreement" in n for n in prediction.notes)


def test_one_end_unknown_speed_stays_defaulted_low_not_disagreement():
    # one end unknown -> existing defaulted->LOW path, NOT the disagreement note
    b = IRBuilder()
    b.add_device(sw("aa01", stp_priority=0)).add_device(sw("bb02"))
    b.add_port(make_port("aa01", "ge-0/0/1", observed_speed="10g"))
    b.add_port(make_port("bb02", "ge-0/0/1"))  # unknown -> defaulted 1g
    b.add_link(link("aa01:ge-0/0/1", "bb02:ge-0/0/1"))
    ir = b.build()
    top = active_topology(ir)
    assert top.edges[0].link_confidence is ConfidenceLevel.HIGH  # cap is NOT link-level here
    assert not any("speed disagreement" in n for n in top.notes)

    prediction = predict_stp_tree(ir)
    comp = prediction.components[0]
    root_port = comp.ports["bb02:ge-0/0/1"]
    assert root_port.deciding_factor == "sole_path"
    assert root_port.confidence is ConfidenceLevel.LOW
    assert not any("speed disagreement" in n for n in prediction.notes)


# ---------- component_rpc: election + directed tainted RPC (Task 3) ---------


def test_rpc_uses_receiving_port_cost_directionally():
    # A(root, 1g port) -- B(10g port): RPC(B) = cost(B's port) = 2_000, NOT 20_000
    b = IRBuilder()
    b.add_device(sw("aa01", stp_priority=0)).add_device(sw("bb02", stp_priority=4096))
    b.add_port(make_port("aa01", "ge-0/0/1", observed_speed="1g"))
    b.add_port(make_port("bb02", "ge-0/0/1", observed_speed="10g"))
    b.add_link(link("aa01:ge-0/0/1", "bb02:ge-0/0/1"))
    ir = b.build()
    top = active_topology(ir)
    election = component_rpc(ir, top, frozenset({"aa01", "bb02"}))
    assert election.root == "aa01"
    assert election.root_assumed_default is False
    assert election.rpc["aa01"].cost == 0
    assert election.rpc["bb02"].cost == 2_000  # bb02's OWN port cost (10g), not aa01's (1g)


def test_rpc_taint_propagates_default_cost():
    # unknown speed on the path -> rpc.defaulted True downstream
    b = IRBuilder()
    b.add_device(sw("aa01", stp_priority=0)).add_device(sw("bb02")).add_device(sw("cc03"))
    b.add_port(make_port("aa01", "ge-0/0/1", observed_speed="10g"))
    b.add_port(make_port("bb02", "ge-0/0/1", observed_speed="10g"))
    b.add_port(make_port("bb02", "ge-0/0/2"))  # no speed -> defaulted
    b.add_port(make_port("cc03", "ge-0/0/1"))  # no speed -> defaulted too, but irrelevant
    b.add_link(link("aa01:ge-0/0/1", "bb02:ge-0/0/1"))
    b.add_link(link("bb02:ge-0/0/2", "cc03:ge-0/0/1"))
    ir = b.build()
    top = active_topology(ir)
    election = component_rpc(ir, top, frozenset({"aa01", "bb02", "cc03"}))
    assert election.root == "aa01"
    assert election.rpc["aa01"].defaulted is False
    assert election.rpc["bb02"].defaulted is False  # aa01->bb02 leg has known speeds both ends
    assert election.rpc["cc03"].defaulted is True  # bb02->cc03 leg carries an unknown-speed end


def test_abstain_component_has_no_roles_and_a_note():
    from digital_twin.ir.entities import Device, DeviceRole

    b = IRBuilder()
    b.add_device(
        Device(id="aa01", role=DeviceRole.SWITCH, site="s1", stp_priority_invalid=True)
    ).add_device(sw("bb02"))
    b.add_port(make_port("aa01", "ge-0/0/1"))
    b.add_port(make_port("bb02", "ge-0/0/1"))
    b.add_link(link("aa01:ge-0/0/1", "bb02:ge-0/0/1"))
    ir = b.build()
    top = active_topology(ir)
    election = component_rpc(ir, top, frozenset({"aa01", "bb02"}))
    assert election.root is None
    assert election.abstained is True
    assert election.rpc == {}
    assert election.note is not None


def test_trivial_root_single_switch_with_pseudo_edge():
    # engine-local: root = the switch, root_assumed_default False
    dev = "aa0000000001"
    pa = make_port(dev, "ge-0/0/8", self_loop_peer=f"{dev}:ge-0/0/9")
    pb = make_port(dev, "ge-0/0/9", self_loop_peer=f"{dev}:ge-0/0/8")
    ir = IRBuilder().add_device(sw(dev)).add_port(pa).add_port(pb).build()
    top = active_topology(ir)
    election = component_rpc(ir, top, frozenset({dev}))
    assert election.root == dev
    assert election.root_assumed_default is False
    assert election.abstained is False
    assert election.rpc[dev].cost == 0


def test_equal_cost_parallel_paths_never_compare_payload():
    # P2 pin AT THE DIJKSTRA LAYER (not only role assignment): two identical-
    # cost/same-node-pair standalone links -> RPC computes without TypeError
    # and deterministically (edge_key + counter break the tie)
    b = IRBuilder()
    b.add_device(sw("aa01", stp_priority=0)).add_device(sw("bb02"))
    b.add_port(make_port("aa01", "ge-0/0/1"))
    b.add_port(make_port("aa01", "ge-0/0/2"))
    b.add_port(make_port("bb02", "ge-0/0/1"))
    b.add_port(make_port("bb02", "ge-0/0/2"))
    b.add_link(link("aa01:ge-0/0/1", "bb02:ge-0/0/1"))
    b.add_link(link("aa01:ge-0/0/2", "bb02:ge-0/0/2"))
    ir = b.build()
    top = active_topology(ir)
    # must not raise TypeError from comparing _ActiveEdge payloads on a cost tie
    election = component_rpc(ir, top, frozenset({"aa01", "bb02"}))
    assert election.root == "aa01"
    assert election.rpc["bb02"].cost == 20_000  # single 1g hop either parallel link


def test_equal_cost_tie_merges_taint_pessimistically():
    # Two parallel aa01<->bb02 links, both costed at 20_000 (a tie): link #1 is
    # clean (both ends observed 1g); link #2's bb02-side speed is UNSET, so it
    # defaults to 1g (same 20_000 cost) but IS tainted. Whichever path the heap
    # settles bb02 on first, the merge must still surface defaulted=True — a
    # real switch could equally have taken the tainted parallel path.
    b = IRBuilder()
    b.add_device(sw("aa01", stp_priority=0)).add_device(sw("bb02"))
    b.add_port(make_port("aa01", "ge-0/0/1", observed_speed="1g"))
    b.add_port(make_port("bb02", "ge-0/0/1", observed_speed="1g"))  # clean leg
    b.add_port(make_port("aa01", "ge-0/0/2", observed_speed="1g"))
    b.add_port(make_port("bb02", "ge-0/0/2"))  # no speed -> defaults to 1g, tainted
    b.add_link(link("aa01:ge-0/0/1", "bb02:ge-0/0/1"))
    b.add_link(link("aa01:ge-0/0/2", "bb02:ge-0/0/2"))
    ir = b.build()
    top = active_topology(ir)
    election = component_rpc(ir, top, frozenset({"aa01", "bb02"}))
    assert election.root == "aa01"
    assert election.rpc["bb02"].cost == 20_000
    assert election.rpc["bb02"].defaulted is True  # pessimistic: OR over equal-cost paths


def test_equal_cost_tie_merges_link_confidence_pessimistically():
    # Mirror case for link_conf: two parallel equal-cost links, one HIGH
    # confidence (default two-sided LLDP), one MEDIUM (INFERRED provenance).
    # The settled RPC must take the MIN across both equal-cost paths, not
    # whichever happened to settle first.
    from digital_twin.ir.provenance import Provenance

    b = IRBuilder()
    b.add_device(sw("aa01", stp_priority=0)).add_device(sw("bb02"))
    b.add_port(make_port("aa01", "ge-0/0/1", observed_speed="1g"))
    b.add_port(make_port("bb02", "ge-0/0/1", observed_speed="1g"))
    b.add_port(make_port("aa01", "ge-0/0/2", observed_speed="1g"))
    b.add_port(make_port("bb02", "ge-0/0/2", observed_speed="1g"))
    b.add_link(link("aa01:ge-0/0/1", "bb02:ge-0/0/1"))  # HIGH (default prov)
    b.add_link(link("aa01:ge-0/0/2", "bb02:ge-0/0/2", prov=Provenance.INFERRED))  # MEDIUM
    ir = b.build()
    top = active_topology(ir)
    election = component_rpc(ir, top, frozenset({"aa01", "bb02"}))
    assert election.root == "aa01"
    assert election.rpc["bb02"].cost == 20_000
    assert election.rpc["bb02"].link_conf is ConfidenceLevel.MEDIUM  # pessimistic: min


# ---------- predict_stp_tree: role assignment + confidence (Task 4) ---------


def test_root_bridge_ports_designated_except_self_loop_pair():
    # P2 pin: root's self-loop -> designated/backup, its other ports designated
    dev = "aa0000000001"
    pa = make_port(dev, "ge-0/0/8", self_loop_peer=f"{dev}:ge-0/0/9")
    pb = make_port(dev, "ge-0/0/9", self_loop_peer=f"{dev}:ge-0/0/8")
    p_uplink = make_port(dev, "ge-0/0/1", observed_speed="1g")
    b = IRBuilder().add_device(sw(dev, stp_priority=0)).add_device(sw("bb02", stp_priority=4096))
    b.add_port(pa).add_port(pb).add_port(p_uplink)
    b.add_port(make_port("bb02", "ge-0/0/1", observed_speed="1g"))
    b.add_link(link(f"{dev}:ge-0/0/1", "bb02:ge-0/0/1"))
    ir = b.build()
    prediction = predict_stp_tree(ir)
    assert len(prediction.components) == 1
    comp = prediction.components[0]
    assert comp.root == dev
    loop_a = comp.ports[f"{dev}:ge-0/0/8"]
    loop_b = comp.ports[f"{dev}:ge-0/0/9"]
    assert (loop_a.role, loop_a.state) == ("designated", "forwarding")
    assert (loop_b.role, loop_b.state) == ("backup", "blocking")
    assert loop_a.deciding_factor == "port_id_tie" and loop_a.confidence is ConfidenceLevel.LOW
    assert loop_b.deciding_factor == "port_id_tie" and loop_b.confidence is ConfidenceLevel.LOW
    uplink = comp.ports[f"{dev}:ge-0/0/1"]
    assert (uplink.role, uplink.state) == ("designated", "forwarding")
    assert uplink.deciding_factor == "root_bridge"
    assert uplink.confidence is ConfidenceLevel.HIGH


def test_root_port_by_cost_is_high_confidence():
    # aa01 = root; bb02 has two candidate paths to root of DIFFERENT cost:
    # a direct 10g link to aa01 (cost 2_000) and a 1g link via cc03 (cost
    # 20_000 + 20_000). The direct link wins by cost.
    b = IRBuilder()
    b.add_device(sw("aa01", stp_priority=0))
    b.add_device(sw("bb02", stp_priority=4096)).add_device(sw("cc03", stp_priority=8192))
    b.add_port(make_port("aa01", "ge-0/0/1", observed_speed="10g"))
    b.add_port(make_port("bb02", "ge-0/0/1", observed_speed="10g"))
    b.add_port(make_port("aa01", "ge-0/0/2", observed_speed="1g"))
    b.add_port(make_port("cc03", "ge-0/0/1", observed_speed="1g"))
    b.add_port(make_port("bb02", "ge-0/0/2", observed_speed="1g"))
    b.add_port(make_port("cc03", "ge-0/0/2", observed_speed="1g"))
    b.add_link(link("aa01:ge-0/0/1", "bb02:ge-0/0/1"))
    b.add_link(link("aa01:ge-0/0/2", "cc03:ge-0/0/1"))
    b.add_link(link("bb02:ge-0/0/2", "cc03:ge-0/0/2"))
    ir = b.build()
    prediction = predict_stp_tree(ir)
    comp = prediction.components[0]
    assert comp.root == "aa01"
    root_port = comp.ports["bb02:ge-0/0/1"]
    assert (root_port.role, root_port.state) == ("root", "forwarding")
    assert root_port.deciding_factor == "cost"
    assert root_port.confidence is ConfidenceLevel.HIGH


def test_root_port_by_bridge_id_tiebreak_is_high():
    # bb02 reaches root aa01 via two EQUAL-cost one-hop paths through two
    # different neighbor bridges (cc03, dd04) that both directly connect to
    # root at the same cost -> tie broken by neighbor bridge id (cc03 < dd04).
    b = IRBuilder()
    b.add_device(sw("aa01", stp_priority=0))
    b.add_device(sw("bb02", stp_priority=4096))
    b.add_device(sw("cc03", stp_priority=8192)).add_device(sw("dd04", stp_priority=12288))
    b.add_port(make_port("aa01", "ge-0/0/1", observed_speed="1g"))
    b.add_port(make_port("cc03", "ge-0/0/1", observed_speed="1g"))
    b.add_port(make_port("aa01", "ge-0/0/2", observed_speed="1g"))
    b.add_port(make_port("dd04", "ge-0/0/1", observed_speed="1g"))
    b.add_port(make_port("cc03", "ge-0/0/2", observed_speed="1g"))
    b.add_port(make_port("bb02", "ge-0/0/1", observed_speed="1g"))
    b.add_port(make_port("dd04", "ge-0/0/2", observed_speed="1g"))
    b.add_port(make_port("bb02", "ge-0/0/2", observed_speed="1g"))
    b.add_link(link("aa01:ge-0/0/1", "cc03:ge-0/0/1"))
    b.add_link(link("aa01:ge-0/0/2", "dd04:ge-0/0/1"))
    b.add_link(link("cc03:ge-0/0/2", "bb02:ge-0/0/1"))
    b.add_link(link("dd04:ge-0/0/2", "bb02:ge-0/0/2"))
    ir = b.build()
    prediction = predict_stp_tree(ir)
    comp = prediction.components[0]
    assert comp.root == "aa01"
    root_port = comp.ports["bb02:ge-0/0/1"]  # faces cc03, the lower bridge id
    assert (root_port.role, root_port.state) == ("root", "forwarding")
    assert root_port.deciding_factor == "bridge_id"
    assert root_port.confidence is ConfidenceLevel.HIGH
    loser = comp.ports["bb02:ge-0/0/2"]
    assert (loser.role, loser.state) == ("alternate", "blocking")
    assert loser.deciding_factor == "bridge_id"


def test_parallel_links_same_pair_port_id_tie_low():
    # two standalone links A<->B: far side gets ONE root port (port_id_tie,
    # LOW), the other end alternate/blocking — node-level SPT cannot express
    # this distinction.
    b = IRBuilder()
    b.add_device(sw("aa01", stp_priority=0)).add_device(sw("bb02"))
    b.add_port(make_port("aa01", "ge-0/0/1"))
    b.add_port(make_port("aa01", "ge-0/0/2"))
    b.add_port(make_port("bb02", "ge-0/0/1"))
    b.add_port(make_port("bb02", "ge-0/0/2"))
    b.add_link(link("aa01:ge-0/0/1", "bb02:ge-0/0/1"))
    b.add_link(link("aa01:ge-0/0/2", "bb02:ge-0/0/2"))
    ir = b.build()
    prediction = predict_stp_tree(ir)
    comp = prediction.components[0]
    assert comp.root == "aa01"
    winner = comp.ports["bb02:ge-0/0/1"]
    loser = comp.ports["bb02:ge-0/0/2"]
    assert (winner.role, winner.state) == ("root", "forwarding")
    assert winner.deciding_factor == "port_id_tie"
    assert winner.confidence is ConfidenceLevel.LOW
    assert (loser.role, loser.state) == ("alternate", "blocking")
    assert loser.deciding_factor == "port_id_tie"


def test_sole_path_deciding_factor():
    b = IRBuilder()
    b.add_device(sw("aa01", stp_priority=0)).add_device(sw("bb02", stp_priority=4096))
    b.add_port(make_port("aa01", "ge-0/0/1", observed_speed="1g"))
    b.add_port(make_port("bb02", "ge-0/0/1", observed_speed="1g"))
    b.add_link(link("aa01:ge-0/0/1", "bb02:ge-0/0/1"))
    ir = b.build()
    prediction = predict_stp_tree(ir)
    comp = prediction.components[0]
    root_port = comp.ports["bb02:ge-0/0/1"]
    assert (root_port.role, root_port.state) == ("root", "forwarding")
    assert root_port.deciding_factor == "sole_path"
    assert root_port.confidence is ConfidenceLevel.HIGH


def test_alternate_ends_block():
    b = IRBuilder()
    b.add_device(sw("aa01", stp_priority=0)).add_device(sw("bb02"))
    b.add_port(make_port("aa01", "ge-0/0/1"))
    b.add_port(make_port("aa01", "ge-0/0/2"))
    b.add_port(make_port("bb02", "ge-0/0/1"))
    b.add_port(make_port("bb02", "ge-0/0/2"))
    b.add_link(link("aa01:ge-0/0/1", "bb02:ge-0/0/1"))
    b.add_link(link("aa01:ge-0/0/2", "bb02:ge-0/0/2"))
    ir = b.build()
    prediction = predict_stp_tree(ir)
    comp = prediction.components[0]
    # aa01's second port is the designated end of the losing parallel link
    designated_loser_side = comp.ports["aa01:ge-0/0/2"]
    assert (designated_loser_side.role, designated_loser_side.state) == (
        "designated",
        "forwarding",
    )
    loser = comp.ports["bb02:ge-0/0/2"]
    assert (loser.role, loser.state) == ("alternate", "blocking")


def test_self_loop_designated_backup_low():
    dev = "aa0000000001"
    pa = make_port(dev, "ge-0/0/8", self_loop_peer=f"{dev}:ge-0/0/9")
    pb = make_port(dev, "ge-0/0/9", self_loop_peer=f"{dev}:ge-0/0/8")
    ir = IRBuilder().add_device(sw(dev)).add_port(pa).add_port(pb).build()
    prediction = predict_stp_tree(ir)
    comp = prediction.components[0]
    assert comp.root == dev
    winner = comp.ports[f"{dev}:ge-0/0/8"]
    loser = comp.ports[f"{dev}:ge-0/0/9"]
    assert (winner.role, winner.state) == ("designated", "forwarding")
    assert (loser.role, loser.state) == ("backup", "blocking")
    assert winner.deciding_factor == "port_id_tie" and winner.confidence is ConfidenceLevel.LOW
    assert loser.deciding_factor == "port_id_tie" and loser.confidence is ConfidenceLevel.LOW


def test_assumed_default_caps_component_medium():
    # aa01's stp_priority unset -> defaults to 32768, LOSES the election to
    # bb02's explicit 4096 -> root = bb02, but root_assumed_default is still
    # True (any elector's priority being assumed taints the whole election) ->
    # component-wide confidence cap MEDIUM even for an otherwise-HIGH decision.
    b = IRBuilder()
    b.add_device(sw("aa01")).add_device(sw("bb02", stp_priority=4096))
    b.add_port(make_port("aa01", "ge-0/0/1", observed_speed="1g"))
    b.add_port(make_port("bb02", "ge-0/0/1", observed_speed="1g"))
    b.add_link(link("aa01:ge-0/0/1", "bb02:ge-0/0/1"))
    ir = b.build()
    prediction = predict_stp_tree(ir)
    comp = prediction.components[0]
    assert comp.root == "bb02"
    assert comp.root_assumed_default is True
    root_port = comp.ports["aa01:ge-0/0/1"]
    assert root_port.deciding_factor == "sole_path"  # would be HIGH but for the cap
    assert root_port.confidence is ConfidenceLevel.MEDIUM
    designated = comp.ports["bb02:ge-0/0/1"]
    assert designated.confidence is ConfidenceLevel.MEDIUM


def test_link_confidence_caps_prediction():
    # MEDIUM link on the deciding comparison -> prediction MEDIUM
    from digital_twin.ir.provenance import Provenance

    b = IRBuilder()
    b.add_device(sw("aa01", stp_priority=0)).add_device(sw("bb02", stp_priority=4096))
    b.add_port(make_port("aa01", "ge-0/0/1", observed_speed="1g"))
    b.add_port(make_port("bb02", "ge-0/0/1", observed_speed="1g"))
    b.add_link(link("aa01:ge-0/0/1", "bb02:ge-0/0/1", prov=Provenance.INFERRED))
    ir = b.build()
    prediction = predict_stp_tree(ir)
    comp = prediction.components[0]
    root_port = comp.ports["bb02:ge-0/0/1"]
    assert root_port.deciding_factor == "sole_path"
    assert root_port.confidence is ConfidenceLevel.MEDIUM
    designated = comp.ports["aa01:ge-0/0/1"]
    assert designated.confidence is ConfidenceLevel.MEDIUM


def test_defaulted_speed_caps_low():
    b = IRBuilder()
    b.add_device(sw("aa01", stp_priority=0)).add_device(sw("bb02"))
    b.add_port(make_port("aa01", "ge-0/0/1"))  # no speed -> defaulted
    b.add_port(make_port("bb02", "ge-0/0/1"))  # no speed -> defaulted
    b.add_link(link("aa01:ge-0/0/1", "bb02:ge-0/0/1"))
    ir = b.build()
    prediction = predict_stp_tree(ir)
    comp = prediction.components[0]
    root_port = comp.ports["bb02:ge-0/0/1"]
    assert root_port.deciding_factor == "sole_path"
    assert root_port.confidence is ConfidenceLevel.LOW


def test_lag_members_share_bundle_role_capped_medium():
    b = IRBuilder()
    b.add_device(sw("aa01", stp_priority=0)).add_device(sw("bb02", stp_priority=4096))
    b.add_port(make_port("aa01", "ge-0/0/1", observed_speed="1g"))
    b.add_port(make_port("aa01", "ge-0/0/2", observed_speed="1g"))
    b.add_port(make_port("bb02", "ge-0/0/1", observed_speed="1g"))
    b.add_port(make_port("bb02", "ge-0/0/2", observed_speed="1g"))
    b.add_link(link("aa01:ge-0/0/1", "bb02:ge-0/0/1", kind=LinkKind.LAG, bundle="ae0"))
    b.add_link(link("aa01:ge-0/0/2", "bb02:ge-0/0/2", kind=LinkKind.LAG, bundle="ae0"))
    ir = b.build()
    prediction = predict_stp_tree(ir)
    comp = prediction.components[0]
    member1 = comp.ports["bb02:ge-0/0/1"]
    member2 = comp.ports["bb02:ge-0/0/2"]
    assert (member1.role, member1.state) == ("root", "forwarding")
    assert (member2.role, member2.state) == ("root", "forwarding")
    assert member1.deciding_factor == "sole_path" and member2.deciding_factor == "sole_path"
    assert member1.confidence is ConfidenceLevel.MEDIUM
    assert member2.confidence is ConfidenceLevel.MEDIUM
    assert any("lag" in n.lower() for n in member1.notes)


def test_stp_edge_on_switch_link_elected_normally_with_note():
    b = IRBuilder()
    b.add_device(sw("aa01", stp_priority=0)).add_device(sw("bb02"))
    b.add_port(make_port("aa01", "ge-0/0/1", stp_edge=True))
    b.add_port(make_port("bb02", "ge-0/0/1"))
    b.add_link(link("aa01:ge-0/0/1", "bb02:ge-0/0/1"))
    ir = b.build()
    prediction = predict_stp_tree(ir)
    comp = prediction.components[0]
    edge_port = comp.ports["aa01:ge-0/0/1"]
    # elected normally: root is aa01, so this end is designated, NOT forced
    assert (edge_port.role, edge_port.state) == ("designated", "forwarding")
    assert edge_port.deciding_factor == "root_bridge"
    assert any("edge" in n.lower() for n in edge_port.notes)


def test_determinism_same_ir_identical_prediction():
    b = IRBuilder()
    b.add_device(sw("aa01", stp_priority=0)).add_device(sw("bb02")).add_device(sw("cc03"))
    b.add_port(make_port("aa01", "ge-0/0/1"))
    b.add_port(make_port("bb02", "ge-0/0/1"))
    b.add_port(make_port("bb02", "ge-0/0/2"))
    b.add_port(make_port("cc03", "ge-0/0/1"))
    b.add_link(link("aa01:ge-0/0/1", "bb02:ge-0/0/1"))
    b.add_link(link("bb02:ge-0/0/2", "cc03:ge-0/0/1"))
    ir = b.build()
    first = predict_stp_tree(ir)
    second = predict_stp_tree(ir)
    assert first == second


# ---------- AnalysisContext memoization (Task 5) ---------------------------


def test_context_memoizes_stp_tree():
    from digital_twin.analysis.context import AnalysisContext

    b = IRBuilder()
    b.add_device(sw("aa01", stp_priority=0)).add_device(sw("bb02", stp_priority=4096))
    b.add_port(make_port("aa01", "ge-0/0/1", observed_speed="1g"))
    b.add_port(make_port("bb02", "ge-0/0/1", observed_speed="1g"))
    b.add_link(link("aa01:ge-0/0/1", "bb02:ge-0/0/1"))
    ir = b.build()
    ctx = AnalysisContext(ir)
    assert ctx.stp_tree() is ctx.stp_tree()
