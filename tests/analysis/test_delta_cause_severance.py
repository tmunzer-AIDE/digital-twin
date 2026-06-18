"""Family-2 rule 2: causes_for_severance — physical L2 cut cause attribution.

Each fixture builds a baseline + proposed IR where the physical L2 graph loses a
boundary edge of an island (a connected component whose uplink is severed). The tests
assert that causes_for_severance names exactly the delta-present port or link that
caused the L2 edge to drop, and nothing else.

Port-id format: "device:name"
Link-id format: sorted(port_a, port_b) joined by "__"  (see link_id() in entities.py)
"""

from dataclasses import replace

from digital_twin.analysis.context import AnalysisContext
from digital_twin.analysis.delta_cause import causes_for_severance, delta_index
from digital_twin.checks.base import CheckContext
from digital_twin.ir import IRBuilder, Vlan
from digital_twin.ir.diff import diff_ir
from tests.factories import access_port, link, sw, trunk_port


def _ctx(base_ir, prop_ir):
    diff = diff_ir(base_ir, prop_ir)
    return CheckContext(AnalysisContext(base_ir), AnalysisContext(prop_ir), diff, delta_index(diff))


def _ids(causes):
    return sorted((c.ref.kind, c.ref.id) for c in causes)


# --- 1. disabled-port severance: port disabled -> L2 edge drops -> island isolated -----


def _disabled_port_ir(*, disabled: bool):
    """idf --[up/down]-- core. In the proposed variant idf's uplink port is disabled,
    causing the physical L2 edge to disappear and idf to become an isolated island.

    Baseline and proposed share the same trunk VLAN configuration; only the
    `disabled` field changes on idf:up, so diff_ir emits a 'port' modification with
    changed field 'disabled'."""
    b = IRBuilder()
    b.add_device(sw("idf"))
    b.add_device(sw("core"))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    idf_up = trunk_port("idf", "up", tagged=(10,))
    if disabled:
        idf_up = replace(idf_up, disabled=True)
    b.add_port(idf_up)
    b.add_port(trunk_port("core", "down", tagged=(10,)))
    b.add_link(link("idf:up", "core:down"))
    b.add_port(access_port("idf", "acc", 10))
    return b.build()


def test_disabled_port_severance_names_the_disabled_port():
    ctx = _ctx(_disabled_port_ir(disabled=False), _disabled_port_ir(disabled=True))

    # verify the L2 edge is gone in proposed: idf is isolated
    prop_l2 = ctx.proposed.l2_graph()
    assert not prop_l2.has_edge("idf", "core"), "expected the L2 edge to drop when port disabled"

    # island = {idf}  (the node set that is now disconnected)
    island = frozenset({"idf"})
    assert _ids(causes_for_severance(ctx, island)) == [("port", "idf:up")]


# --- 2. pre-existing island (no delta) -> () ------------------------------------------


def test_preexisting_severance_yields_empty():
    """If idf is already isolated in BOTH baseline and proposed, there is no delta
    to name — the boundary edge was lost BEFORE this plan."""
    ir = _disabled_port_ir(disabled=True)   # already severed in both snapshots
    ctx = _ctx(ir, ir)
    island = frozenset({"idf"})
    assert causes_for_severance(ctx, island) == ()


# --- 3. boundary-local: unrelated severance elsewhere does NOT appear ------------------


def _two_islands_ir(*, disable_idf: bool, disable_leaf: bool):
    """Two independent uplinks:
      idf  --[idf:up / core:port-idf]-- core
      leaf --[leaf:up / core:port-leaf]-- core

    In proposed, one or both may be disabled.  When only idf is disabled, only idf's
    island should be attributed; leaf's boundary edge is NOT lost so it must not appear.
    When only leaf is disabled, the idf island naming test (island = {leaf}) must see
    ONLY leaf's port, not idf's."""
    b = IRBuilder()
    b.add_device(sw("idf"))
    b.add_device(sw("leaf"))
    b.add_device(sw("core"))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))

    idf_up = trunk_port("idf", "up", tagged=(10,))
    if disable_idf:
        idf_up = replace(idf_up, disabled=True)
    b.add_port(idf_up)
    b.add_port(trunk_port("core", "port-idf", tagged=(10,)))
    b.add_link(link("idf:up", "core:port-idf"))
    b.add_port(access_port("idf", "acc", 10))

    leaf_up = trunk_port("leaf", "up", tagged=(10,))
    if disable_leaf:
        leaf_up = replace(leaf_up, disabled=True)
    b.add_port(leaf_up)
    b.add_port(trunk_port("core", "port-leaf", tagged=(10,)))
    b.add_link(link("leaf:up", "core:port-leaf"))
    b.add_port(access_port("leaf", "acc", 10))

    return b.build()


def test_unrelated_severance_does_not_appear_in_island_causes():
    """Both idf and leaf severances are in the delta, but each island query is
    boundary-local: idf's island only names idf:up, leaf's island only names leaf:up."""
    ctx = _ctx(
        _two_islands_ir(disable_idf=False, disable_leaf=False),
        _two_islands_ir(disable_idf=True, disable_leaf=True),
    )

    idf_island = frozenset({"idf"})
    leaf_island = frozenset({"leaf"})

    idf_causes = _ids(causes_for_severance(ctx, idf_island))
    leaf_causes = _ids(causes_for_severance(ctx, leaf_island))

    assert idf_causes == [("port", "idf:up")]
    assert leaf_causes == [("port", "leaf:up")]
    # cross-contamination check: each island only sees its own cause
    assert ("port", "leaf:up") not in idf_causes
    assert ("port", "idf:up") not in leaf_causes


# --- 4. link-only removal: removed link named (exercises _edge_causes' link path) ------


def _link_removal_ir(*, removed: bool):
    """idf --[idf:up / core:down]-- core, with the link itself removed in proposed
    (both ports unchanged). The delta-cause must be the removed link, not a port."""
    b = IRBuilder()
    b.add_device(sw("idf"))
    b.add_device(sw("core"))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    b.add_port(trunk_port("idf", "up", tagged=(10,)))
    b.add_port(trunk_port("core", "down", tagged=(10,)))
    if not removed:
        b.add_link(link("idf:up", "core:down"))
    b.add_port(access_port("idf", "acc", 10))
    return b.build()


def test_link_removal_names_the_removed_link():
    ctx = _ctx(_link_removal_ir(removed=False), _link_removal_ir(removed=True))

    # verify the L2 edge is gone in proposed
    assert not ctx.proposed.l2_graph().has_edge("idf", "core")

    island = frozenset({"idf"})
    # link_id("idf:up", "core:down") -> "core:down__idf:up" (sorted)
    assert _ids(causes_for_severance(ctx, island)) == [("link", "core:down__idf:up")]
