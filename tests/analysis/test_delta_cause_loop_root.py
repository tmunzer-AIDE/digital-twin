"""Family-2 rules 4/5: causes_for_loop + causes_for_root_move cause mapping.

LOOP fixtures build a REAL cycle (a triangle on one vlan graph) and assert the loop
cause is the entity that ARMED the cycle: a cycle member port whose `stp_enabled`
flipped or that was newly added, or an added/removed cycle link. An unrelated field
change on a cycle member (e.g. `stp_edge`, which the loop check does NOT read) is
filtered out.

ROOT-MOVE fixtures move a connected component's predicted STP root and assert the
cause is an election-relevant device priority change (old/new root only) or a
boundary edge LOST / a MERGING edge GAINED. The MERGE case is the one a pure-XOR
boundary test would miss: the joining edge has BOTH endpoints inside the proposed
merged component.

Port-id format: "device:name"; link-id format: sorted(a, b) joined by "__".
"""

from dataclasses import replace

import networkx as nx

from digital_twin.analysis.context import AnalysisContext
from digital_twin.analysis.delta_cause import (
    causes_for_loop,
    causes_for_root_move,
    delta_index,
)
from digital_twin.checks.base import CheckContext
from digital_twin.checks.wired.stp_root import _root_of
from digital_twin.ir import IRBuilder, Vlan
from digital_twin.ir.diff import diff_ir
from tests.factories import access_port, link, sw, trunk_port


def _ctx(base_ir, prop_ir):
    diff = diff_ir(base_ir, prop_ir)
    return CheckContext(AnalysisContext(base_ir), AnalysisContext(prop_ir), diff, delta_index(diff))


def _ids(causes):
    return sorted((c.ref.kind, c.ref.id) for c in causes)


def _triangle(vid: int, *, mutate=None):
    """A 3-node cycle A--B--C--A on `vid`, every node also an access member of vid.
    `mutate(port_factory)` may swap a trunk port for an edited variant (keyed by port id)."""
    edits = {} if mutate is None else mutate
    b = IRBuilder()
    for d in ("A", "B", "C"):
        b.add_device(sw(d))
    b.add_vlan(Vlan(vlan_id=vid, name=f"v{vid}", scope="s1"))
    for x, y in (("A", "B"), ("B", "C"), ("C", "A")):
        px = trunk_port(x, f"to-{y}", tagged=(vid,))
        py = trunk_port(y, f"to-{x}", tagged=(vid,))
        b.add_port(edits.get(px.id, px))
        b.add_port(edits.get(py.id, py))
        b.add_link(link(f"{x}:to-{y}", f"{y}:to-{x}"))
    for d in ("A", "B", "C"):
        b.add_port(access_port(d, "acc", vid))
    return b.build()


# --- LOOP 1. a cycle member port whose stp_enabled flipped is named -------------------


def test_loop_names_stp_enabled_flip():
    # baseline: cycle port A:to-B has STP enabled; proposed: STP disabled on it.
    p = trunk_port("A", "to-B", tagged=(7,))
    base = _triangle(7, mutate={"A:to-B": replace(p, stp_enabled=True)})
    prop = _triangle(7, mutate={"A:to-B": replace(p, stp_enabled=False)})
    ctx = _ctx(base, prop)
    cycle = next(c for c in ctx.proposed.cycles(7))
    # the flip is the only delta; A:to-B is named (its stp_enabled is a loop field)
    assert _ids(causes_for_loop(ctx, cycle)) == [("port", "A:to-B")]


# --- LOOP 2. over-naming guard: added link arms; an unrelated stp_edge-only port isn't -


def _triangle_open(*, closed: bool, edge_port_id: str | None = None):
    """A--B--C path (always) plus the C--A closing link only when `closed`. Adding the
    closing link in proposed ARMS the cycle. Optionally give one EXISTING cycle member
    an stp_edge-only flip via `edge_port_id` (a field the loop check never reads)."""
    b = IRBuilder()
    for d in ("A", "B", "C"):
        b.add_device(sw(d))
    b.add_vlan(Vlan(vlan_id=7, name="v7", scope="s1"))

    def _port(did, name):
        p = trunk_port(did, name, tagged=(7,))
        if edge_port_id == p.id:
            p = replace(p, stp_edge=True)
        return p

    for x, y in (("A", "B"), ("B", "C")):
        b.add_port(_port(x, f"to-{y}"))
        b.add_port(_port(y, f"to-{x}"))
        b.add_link(link(f"{x}:to-{y}", f"{y}:to-{x}"))
    # closing edge C--A (the cycle-arming link), only in the closed variant
    b.add_port(_port("C", "to-A"))
    b.add_port(_port("A", "to-C"))
    if closed:
        b.add_link(link("C:to-A", "A:to-C"))
    for d in ("A", "B", "C"):
        b.add_port(access_port(d, "acc", 7))
    return b.build()


def test_loop_added_link_armed_not_unrelated_stp_edge():
    # baseline: open path, no cycle. proposed: closing link added AND B:to-C gets an
    # stp_edge-only change (loop check ignores stp_edge), so B:to-C must NOT be named.
    base = _triangle_open(closed=False)
    prop = _triangle_open(closed=True, edge_port_id="B:to-C")
    ctx = _ctx(base, prop)
    cycle = next(c for c in ctx.proposed.cycles(7))
    ids = _ids(causes_for_loop(ctx, cycle))
    # the added closing link IS named; the stp_edge-only port is NOT
    assert ("link", "A:to-C__C:to-A") in ids
    assert ("port", "B:to-C") not in ids
    assert ids == [("link", "A:to-C__C:to-A")]


# --- ROOT scenarios: two A--B--... components / merges -------------------------------


def _root_chain(*, b_prio: int, c_prio: int | None = None, merge: bool = False,
                cut: bool = False, third_prio: int | None = None):
    """Component 1: A(100)--B(b_prio). Component 2: C(c_prio)--D(300) when c_prio set.
    `merge` adds a B--C link joining the two components. `cut` removes the A--B link.
    `third_prio` overrides D's priority (a non-root election-irrelevant device)."""
    b = IRBuilder()
    b.add_device(sw("A", stp_priority=100))
    b.add_device(sw("B", stp_priority=b_prio))
    b.add_vlan(Vlan(vlan_id=10, name="v", scope="s1"))
    b.add_port(trunk_port("A", "to-B", tagged=(10,)))
    b.add_port(trunk_port("B", "to-A", tagged=(10,)))
    if not cut:
        b.add_link(link("A:to-B", "B:to-A"))
    if c_prio is not None:
        b.add_device(sw("C", stp_priority=c_prio))
        b.add_device(sw("D", stp_priority=300 if third_prio is None else third_prio))
        b.add_port(trunk_port("C", "to-D", tagged=(10,)))
        b.add_port(trunk_port("D", "to-C", tagged=(10,)))
        b.add_link(link("C:to-D", "D:to-C"))
        b.add_port(trunk_port("B", "to-C", tagged=(10,)))
        b.add_port(trunk_port("C", "to-B", tagged=(10,)))
        if merge:
            b.add_link(link("B:to-C", "C:to-B"))
    return b.build()


def _prop_comp(ctx, node):
    for c in nx.connected_components(ctx.proposed.l2_graph()):
        if node in c:
            return frozenset(c)
    raise AssertionError(node)


# --- ROOT 1. priority move: new root because its stp_priority changed ------------------


def test_root_priority_move_names_new_root():
    # baseline B=200 (root A); proposed B=50 (root becomes B).
    base = _root_chain(b_prio=200)
    prop = _root_chain(b_prio=50)
    ctx = _ctx(base, prop)
    comp = _prop_comp(ctx, "A")
    base_root, _ = _root_of(ctx.baseline.ir, comp)
    prop_root, _ = _root_of(ctx.proposed.ir, comp)
    assert (base_root, prop_root) == ("A", "B")
    assert _ids(causes_for_root_move(ctx, comp, base_root, prop_root)) == [("device", "B")]


# --- ROOT 2. over-naming guard: a non-root third switch priority change is NOT named ---


def test_root_priority_move_does_not_name_non_root():
    # A--B--C--D chain in one component. Baseline B=200 (root A). Proposed: B=50 (root
    # becomes B) AND D changes priority too (300->250) but D is neither old nor new root.
    base = _root_chain(b_prio=200, c_prio=150, merge=True, third_prio=300)
    prop = _root_chain(b_prio=50, c_prio=150, merge=True, third_prio=250)
    ctx = _ctx(base, prop)
    comp = _prop_comp(ctx, "A")
    base_root, _ = _root_of(ctx.baseline.ir, comp)
    prop_root, _ = _root_of(ctx.proposed.ir, comp)
    assert (base_root, prop_root) == ("A", "B")
    ids = _ids(causes_for_root_move(ctx, comp, base_root, prop_root))
    assert ("device", "D") not in ids  # D changed priority but is election-irrelevant
    assert ids == [("device", "B")]


# --- ROOT 3. split/removal: a boundary edge loss moves a fragment's root ---------------


def test_root_split_names_boundary_link():
    # A(100)--B(50) one component, root B. Proposed REMOVES the A--B link: A and B split
    # into singletons. The {A} fragment's baseline root was B; its proposed root (alone)
    # is None — but for a fragment whose root MOVES we attribute the lost boundary link.
    # Use a 3-node component so the fragment still elects after the cut.
    def chain(cut: bool):
        b = IRBuilder()
        b.add_device(sw("A", stp_priority=100))
        b.add_device(sw("B", stp_priority=200))
        b.add_device(sw("R", stp_priority=10))  # global root, sits past the cut
        b.add_vlan(Vlan(vlan_id=10, name="v", scope="s1"))
        # A--B
        b.add_port(trunk_port("A", "to-B", tagged=(10,)))
        b.add_port(trunk_port("B", "to-A", tagged=(10,)))
        b.add_link(link("A:to-B", "B:to-A"))
        # B--R (the boundary link cut in proposed)
        b.add_port(trunk_port("B", "to-R", tagged=(10,)))
        b.add_port(trunk_port("R", "to-B", tagged=(10,)))
        if not cut:
            b.add_link(link("B:to-R", "R:to-B"))
        return b.build()

    ctx = _ctx(chain(cut=False), chain(cut=True))
    # proposed fragment containing A,B (R is now severed off)
    comp = _prop_comp(ctx, "A")
    assert comp == frozenset({"A", "B"})
    base_comp = next(
        frozenset(c) for c in nx.connected_components(ctx.baseline.l2_graph()) if "A" in c
    )
    base_root, _ = _root_of(ctx.baseline.ir, base_comp)
    prop_root, _ = _root_of(ctx.proposed.ir, comp)
    assert base_root == "R" and prop_root == "A"  # root moved from R (lost) to A
    # the lost B--R boundary link is named
    assert _ids(causes_for_root_move(ctx, comp, base_root, prop_root)) == [
        ("link", "B:to-R__R:to-B")
    ]


# --- ROOT 4. MERGE: an added link joins two baseline components; merged root differs ---


def test_root_merge_names_added_link():
    # Baseline: {A(100),B(200)} root A ; {C(50),D(300)} root C. Proposed adds B--C link,
    # merging into {A,B,C,D} whose root is C. For the {A,B} half, root moves A->C. The
    # MERGE link B--C has BOTH endpoints inside the proposed merged component, so a pure
    # XOR boundary test returns () — only _gained_merging_edges catches it.
    base = _root_chain(b_prio=200, c_prio=50, merge=False)
    prop = _root_chain(b_prio=200, c_prio=50, merge=True)
    ctx = _ctx(base, prop)
    comp = _prop_comp(ctx, "A")
    assert comp == frozenset({"A", "B", "C", "D"})
    base_comp = next(
        frozenset(c) for c in nx.connected_components(ctx.baseline.l2_graph()) if "A" in c
    )
    base_root, _ = _root_of(ctx.baseline.ir, base_comp)  # root of the {A,B} baseline half
    prop_root, _ = _root_of(ctx.proposed.ir, comp)
    assert base_root == "A" and prop_root == "C"  # root moved A -> C via the merge
    ids = _ids(causes_for_root_move(ctx, comp, base_root, prop_root))
    # the added merging link is named (both endpoints inside the merged component)
    assert ids == [("link", "B:to-C__C:to-B")]


def test_root_merge_would_fail_against_xor_only():
    # GUARD: prove the merge edge is invisible to a pure-XOR boundary test, so this
    # scenario genuinely exercises _gained_merging_edges (not _boundary_lost_edges).
    base = _root_chain(b_prio=200, c_prio=50, merge=False)
    prop = _root_chain(b_prio=200, c_prio=50, merge=True)
    ctx = _ctx(base, prop)
    comp = _prop_comp(ctx, "A")
    base_l2, prop_l2 = ctx.baseline.l2_graph(), ctx.proposed.l2_graph()
    # the added edge B--C: both endpoints are inside the proposed component -> XOR=False
    assert "B" in comp and "C" in comp
    # a pure-XOR boundary-lost test finds NO lost boundary edge here
    from digital_twin.analysis.delta_cause import _boundary_lost_edges

    assert _boundary_lost_edges(base_l2, prop_l2, comp) == []
