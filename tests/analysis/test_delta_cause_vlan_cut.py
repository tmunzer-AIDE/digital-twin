"""Family-2 rule 1: graph-based vlan_cut / vlan_split / blackhole cause mapping.

Each fixture builds a baseline + proposed IR whose per-VLAN graph cuts/splits in a
specific place, with a SPECIFIC port/link/l3intf in the delta — the tests assert the
mapping functions name exactly the delta-present boundary/separating entity and nothing
else (no internal edges, no over-naming of removed leaves).
"""

from digital_twin.analysis.context import AnalysisContext
from digital_twin.analysis.delta_cause import (
    causes_for_blackhole,
    causes_for_vlan_cut,
    causes_for_vlan_split,
    delta_index,
)
from digital_twin.checks.base import CheckContext
from digital_twin.ir import IRBuilder, Vlan
from digital_twin.ir.diff import diff_ir
from tests.factories import access_port, irb, link, sw, trunk_port


def _ctx(base_ir, prop_ir):
    diff = diff_ir(base_ir, prop_ir)
    return CheckContext(AnalysisContext(base_ir), AnalysisContext(prop_ir), diff, delta_index(diff))


def _ids(causes):
    return sorted((c.ref.kind, c.ref.id) for c in causes)


def _stranded(ctx, vid):
    return next(c for c in ctx.proposed.vlan_components(vid) if not c.reaches_exit)


# --- 1. vlan_cut names the boundary trunk port that dropped the vlan -----------------


def _cut_ir(cut: bool):
    """A -- B -- C, vlan 7 on all trunks, exit IRB on A. In the cut variant B's port to
    C drops vlan 7, stranding fragment {C}."""
    b = IRBuilder()
    for d in ("A", "B", "C"):
        b.add_device(sw(d))
    b.add_vlan(Vlan(vlan_id=7, name="v7", scope="s1"))
    b.add_port(trunk_port("A", "to-B", tagged=(7,)))
    b.add_port(trunk_port("B", "to-A", tagged=(7,)))
    b.add_link(link("A:to-B", "B:to-A"))
    b.add_port(trunk_port("B", "to-C", tagged=() if cut else (7,)))
    b.add_port(trunk_port("C", "to-B", tagged=(7,)))
    b.add_link(link("B:to-C", "C:to-B"))
    b.add_port(access_port("C", "acc", 7))
    b.add_l3intf(irb("A", 7))
    return b.build()


def test_vlan_cut_names_boundary_port():
    ctx = _ctx(_cut_ir(cut=False), _cut_ir(cut=True))
    stranded = _stranded(ctx, 7)
    assert sorted(stranded.nodes) == ["C"]
    assert _ids(causes_for_vlan_cut(ctx, 7, stranded)) == [("port", "B:to-C")]


# --- 2. ambiguous: component stranded by EXIT loss, no boundary edge cut -> () --------


def _exitloss_ir(rm_irb: bool):
    """A -- B fully connected on vlan 7; exit IRB on A removed in proposed. The single
    component stays connected (no boundary edge lost) but loses its exit."""
    b = IRBuilder()
    for d in ("A", "B"):
        b.add_device(sw(d))
    b.add_vlan(Vlan(vlan_id=7, name="v7", scope="s1"))
    b.add_port(trunk_port("A", "to-B", tagged=(7,)))
    b.add_port(trunk_port("B", "to-A", tagged=(7,)))
    b.add_link(link("A:to-B", "B:to-A"))
    b.add_port(access_port("B", "acc", 7))
    if not rm_irb:
        b.add_l3intf(irb("A", 7))
    return b.build()


def test_vlan_cut_empty_when_no_boundary_edge_lost():
    ctx = _ctx(_exitloss_ir(rm_irb=False), _exitloss_ir(rm_irb=True))
    comp = ctx.proposed.vlan_components(7)[0]
    assert not comp.reaches_exit and not ctx.diff.is_empty()  # the delta IS non-empty
    assert causes_for_vlan_cut(ctx, 7, comp) == ()  # but no carriage cut to attribute


# --- 3a. per-vid scoping: disjoint vlan domains stay attributed to their own port ----


def _twocut_ir(cut: bool):
    """Disjoint vlan domains: vlan 7 (A--B, exit A, cut at A:p7) and vlan 8 (C--D,
    exit C, cut at C:p8). The two cuts must stay attributed to their own port. NB: this
    only exercises PER-VID scoping — the per-vid graphs share no nodes, so even a naive
    whole-graph-per-vid impl would pass. Component-locality is tested in 3b below."""
    b = IRBuilder()
    for d in ("A", "B", "C", "D"):
        b.add_device(sw(d))
    b.add_vlan(Vlan(vlan_id=7, name="v7", scope="s1"))
    b.add_vlan(Vlan(vlan_id=8, name="v8", scope="s1"))
    b.add_port(trunk_port("A", "p7", tagged=() if cut else (7,)))
    b.add_port(trunk_port("B", "p7", tagged=(7,)))
    b.add_link(link("A:p7", "B:p7"))
    b.add_port(access_port("B", "m7", 7))
    b.add_l3intf(irb("A", 7))
    b.add_port(trunk_port("C", "p8", tagged=() if cut else (8,)))
    b.add_port(trunk_port("D", "p8", tagged=(8,)))
    b.add_link(link("C:p8", "D:p8"))
    b.add_port(access_port("D", "m8", 8))
    b.add_l3intf(irb("C", 8))
    return b.build()


def test_disjoint_vlan_cuts_are_per_vid():
    ctx = _ctx(_twocut_ir(cut=False), _twocut_ir(cut=True))
    s7, s8 = _stranded(ctx, 7), _stranded(ctx, 8)
    assert _ids(causes_for_vlan_cut(ctx, 7, s7)) == [("port", "A:p7")]
    assert _ids(causes_for_vlan_cut(ctx, 8, s8)) == [("port", "C:p8")]


# --- 3b. COMPONENT-LOCALITY: two cuts on ONE vid graph, each fragment names only its --
#         own boundary cut (NOT the other fragment's) -----------------------------------


def _two_fragments_one_vid_ir(cut: bool):
    """ONE vlan-7 graph: F1 -- [P1] -- CO(exit) -- [P2] -- F2. In the cut variant CO's
    port to F1 drops vlan 7 (P1) AND CO's port to F2 drops vlan 7 (P2). Core keeps the
    exit IRB and reaches it; F1 and F2 each become a SEPARATE stranded component. The
    cause for F1's component must be ONLY P1 (CO:to-F1), and F2's ONLY P2 (CO:to-F2).
    A whole-graph impl that names every lost edge in the vid graph would blame BOTH P1
    and P2 for each fragment — so this fixture discriminates component-locality."""
    b = IRBuilder()
    for d in ("F1", "CO", "F2"):
        b.add_device(sw(d))
    b.add_vlan(Vlan(vlan_id=7, name="v7", scope="s1"))
    b.add_l3intf(irb("CO", 7))  # the single exit lives on the core, untouched
    # F1 -- CO, cut on the core side (P1)
    b.add_port(trunk_port("CO", "to-F1", tagged=() if cut else (7,)))
    b.add_port(trunk_port("F1", "to-CO", tagged=(7,)))
    b.add_link(link("CO:to-F1", "F1:to-CO"))
    b.add_port(access_port("F1", "acc", 7))
    # CO -- F2, cut on the core side (P2)
    b.add_port(trunk_port("CO", "to-F2", tagged=() if cut else (7,)))
    b.add_port(trunk_port("F2", "to-CO", tagged=(7,)))
    b.add_link(link("CO:to-F2", "F2:to-CO"))
    b.add_port(access_port("F2", "acc", 7))
    return b.build()


def test_vlan_cut_is_component_local():
    ctx = _ctx(_two_fragments_one_vid_ir(cut=False), _two_fragments_one_vid_ir(cut=True))
    # After the delta there are exactly TWO non-exit-reaching components (the core, which
    # keeps its exit, is the only reaching one); each stranded fragment is a separate one.
    stranded = [c for c in ctx.proposed.vlan_components(7) if not c.reaches_exit]
    by_node = {n: c for c in stranded for n in c.nodes}
    assert len(stranded) == 2
    assert {frozenset(c.nodes) for c in stranded} == {frozenset({"F1"}), frozenset({"F2"})}
    frag1, frag2 = by_node["F1"], by_node["F2"]
    # Component-local boundary: F1 names ONLY its own cut P1, F2 names ONLY its own cut P2.
    assert _ids(causes_for_vlan_cut(ctx, 7, frag1)) == [("port", "CO:to-F1")]
    assert _ids(causes_for_vlan_cut(ctx, 7, frag2)) == [("port", "CO:to-F2")]


# --- 4. internal edge loss inside the stranded fragment is NOT named ------------------


def _internal_ir(cut: bool):
    """A -- B -- C, plus C and D doubly-linked. Boundary B--C drops vlan 7 (the real
    cut) AND one of the two internal C--D links drops vlan 7 too. The stranded fragment
    {C,D} stays connected via the surviving internal link; only the boundary port is
    named, never the internal one (validates the exactly-one-endpoint XOR rule)."""
    b = IRBuilder()
    for d in ("A", "B", "C", "D"):
        b.add_device(sw(d))
    b.add_vlan(Vlan(vlan_id=7, name="v7", scope="s1"))
    b.add_l3intf(irb("A", 7))
    b.add_port(trunk_port("A", "to-B", tagged=(7,)))
    b.add_port(trunk_port("B", "to-A", tagged=(7,)))
    b.add_link(link("A:to-B", "B:to-A"))
    b.add_port(trunk_port("B", "to-C", tagged=() if cut else (7,)))  # boundary cut
    b.add_port(trunk_port("C", "to-B", tagged=(7,)))
    b.add_link(link("B:to-C", "C:to-B"))
    b.add_port(trunk_port("C", "to-D1", tagged=(7,)))  # internal link 1 (survives)
    b.add_port(trunk_port("D", "to-C1", tagged=(7,)))
    b.add_link(link("C:to-D1", "D:to-C1"))
    b.add_port(trunk_port("C", "to-D2", tagged=() if cut else (7,)))  # internal link 2 dropped
    b.add_port(trunk_port("D", "to-C2", tagged=(7,)))
    b.add_link(link("C:to-D2", "D:to-C2"))
    b.add_port(access_port("D", "m", 7))
    return b.build()


def test_internal_edge_loss_not_named():
    ctx = _ctx(_internal_ir(cut=False), _internal_ir(cut=True))
    stranded = _stranded(ctx, 7)
    assert sorted(stranded.nodes) == ["C", "D"]
    # both B:to-C (boundary) and C:to-D2 (internal) dropped vlan 7, but only the boundary names
    assert ("port", "C:to-D2") not in _ids(diff_caused := causes_for_vlan_cut(ctx, 7, stranded))
    assert _ids(diff_caused) == [("port", "B:to-C")]


# --- 5. vlan_split: separating edge named; link-only variant exercises the link path --


def _split_port_ir(cut: bool):
    """A -- B -- C, every node holds an access port on vlan 7 (all participate even after
    a cut). Dropping vlan 7 from B's port to C fragments {A,B} | {C}."""
    b = IRBuilder()
    for d in ("A", "B", "C"):
        b.add_device(sw(d))
    b.add_vlan(Vlan(vlan_id=7, name="v7", scope="s1"))
    b.add_port(trunk_port("A", "to-B", tagged=(7,)))
    b.add_port(trunk_port("B", "to-A", tagged=(7,)))
    b.add_link(link("A:to-B", "B:to-A"))
    b.add_port(trunk_port("B", "to-C", tagged=() if cut else (7,)))
    b.add_port(trunk_port("C", "to-B", tagged=(7,)))
    b.add_link(link("B:to-C", "C:to-B"))
    for d in ("A", "B", "C"):
        b.add_port(access_port(d, "acc", 7))
    return b.build()


def _split_link_ir(cut: bool):
    """Same split, but the proposed REMOVES the B--C link entirely (ports unchanged):
    the separating delta is a removed link, exercising _edge_causes' link path."""
    b = IRBuilder()
    for d in ("A", "B", "C"):
        b.add_device(sw(d))
    b.add_vlan(Vlan(vlan_id=7, name="v7", scope="s1"))
    b.add_port(trunk_port("A", "to-B", tagged=(7,)))
    b.add_port(trunk_port("B", "to-A", tagged=(7,)))
    b.add_link(link("A:to-B", "B:to-A"))
    b.add_port(trunk_port("B", "to-C", tagged=(7,)))
    b.add_port(trunk_port("C", "to-B", tagged=(7,)))
    if not cut:
        b.add_link(link("B:to-C", "C:to-B"))
    for d in ("A", "B", "C"):
        b.add_port(access_port(d, "acc", 7))
    return b.build()


def test_vlan_split_names_separating_port():
    ctx = _ctx(_split_port_ir(cut=False), _split_port_ir(cut=True))
    assert [sorted(c.nodes) for c in ctx.proposed.vlan_components(7)] == [["A", "B"], ["C"]]
    assert _ids(causes_for_vlan_split(ctx, 7)) == [("port", "B:to-C")]


def test_vlan_split_names_separating_link():
    ctx = _ctx(_split_link_ir(cut=False), _split_link_ir(cut=True))
    assert [sorted(c.nodes) for c in ctx.proposed.vlan_components(7)] == [["A", "B"], ["C"]]
    assert _ids(causes_for_vlan_split(ctx, 7)) == [("link", "B:to-C__C:to-B")]


# --- 6. vlan_split over-naming guard: removed leaf node not blamed --------------------


def _split_with_leaf_ir(prop: bool):
    """Real split at B--C (port drop) PLUS, only in baseline, a leaf switch D hanging off
    A removed entirely in proposed. D's lost boundary edge must NOT be blamed for the
    split — both endpoints of a separating edge must survive in proposed."""
    b = IRBuilder()
    for d in ("A", "B", "C"):
        b.add_device(sw(d))
    b.add_vlan(Vlan(vlan_id=7, name="v7", scope="s1"))
    b.add_port(trunk_port("A", "to-B", tagged=(7,)))
    b.add_port(trunk_port("B", "to-A", tagged=(7,)))
    b.add_link(link("A:to-B", "B:to-A"))
    b.add_port(trunk_port("B", "to-C", tagged=() if prop else (7,)))  # the real split edge
    b.add_port(trunk_port("C", "to-B", tagged=(7,)))
    b.add_link(link("B:to-C", "C:to-B"))
    for d in ("A", "B", "C"):
        b.add_port(access_port(d, "acc", 7))
    if not prop:  # leaf D present only in baseline -> removed in proposed
        b.add_device(sw("D"))
        b.add_port(trunk_port("A", "to-D", tagged=(7,)))
        b.add_port(trunk_port("D", "to-A", tagged=(7,)))
        b.add_link(link("A:to-D", "D:to-A"))
        b.add_port(access_port("D", "acc", 7))
    return b.build()


def test_vlan_split_does_not_name_removed_leaf():
    ctx = _ctx(_split_with_leaf_ir(prop=False), _split_with_leaf_ir(prop=True))
    # D's removal IS in the delta, but only the real separating edge is named
    assert _ids(causes_for_vlan_split(ctx, 7)) == [("port", "B:to-C")]


# --- 7. blackhole: removed exit l3intf named (carriage unchanged), and combined w/ cut


def _blackhole_irb_ir(rm_irb: bool):
    """A -- B carrying vlan 7 (carriage UNCHANGED); the only exit IRB on A removed in
    proposed -> component {A,B} blackholed by exit removal alone."""
    b = IRBuilder()
    for d in ("A", "B"):
        b.add_device(sw(d))
    b.add_vlan(Vlan(vlan_id=7, name="v7", scope="s1"))
    b.add_port(trunk_port("A", "to-B", tagged=(7,)))
    b.add_port(trunk_port("B", "to-A", tagged=(7,)))
    b.add_link(link("A:to-B", "B:to-A"))
    b.add_port(access_port("B", "acc", 7))
    if not rm_irb:
        b.add_l3intf(irb("A", 7))
    return b.build()


def test_blackhole_names_removed_l3intf():
    ctx = _ctx(_blackhole_irb_ir(rm_irb=False), _blackhole_irb_ir(rm_irb=True))
    comp = ctx.proposed.vlan_components(7)[0]
    assert not comp.reaches_exit
    assert _ids(causes_for_blackhole(ctx, 7, comp)) == [("l3intf", "A:l3:irb:7")]


def _blackhole_both_ir(prop: bool):
    """A -- B -- C, exit IRB on A. In proposed: B's port to C drops vlan 7 (carriage cut
    stranding {C}) AND the IRB on A is removed. The stranded fragment {C} is attributed
    BOTH its boundary port and the removed exit l3intf."""
    b = IRBuilder()
    for d in ("A", "B", "C"):
        b.add_device(sw(d))
    b.add_vlan(Vlan(vlan_id=7, name="v7", scope="s1"))
    b.add_port(trunk_port("A", "to-B", tagged=(7,)))
    b.add_port(trunk_port("B", "to-A", tagged=(7,)))
    b.add_link(link("A:to-B", "B:to-A"))
    b.add_port(trunk_port("B", "to-C", tagged=() if prop else (7,)))
    b.add_port(trunk_port("C", "to-B", tagged=(7,)))
    b.add_link(link("B:to-C", "C:to-B"))
    b.add_port(access_port("C", "acc", 7))
    if not prop:
        b.add_l3intf(irb("A", 7))
    return b.build()


def test_blackhole_combines_carriage_cut_and_removed_l3intf():
    ctx = _ctx(_blackhole_both_ir(prop=False), _blackhole_both_ir(prop=True))
    # with the exit gone too, BOTH fragments lack an exit; pick the carriage-cut one {C}
    stranded = next(c for c in ctx.proposed.vlan_components(7) if c.nodes == frozenset({"C"}))
    assert _ids(causes_for_blackhole(ctx, 7, stranded)) == [
        ("l3intf", "A:l3:irb:7"),
        ("port", "B:to-C"),
    ]
