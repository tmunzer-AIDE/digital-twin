"""StpReachability (Spec-5 Task 2): pair-aware join of Spec-4 STP tree
predictions to per-VLAN graph edges, producing STP-aware reachability views
with telemetry-confirmed (hard-eligible) blocked links removed.

Motivating topology (from test_stp_tree.py::test_root_port_by_bridge_id_tiebreak_is_high):
root aa01 (prio 0) <-> transit cc03 (prio 8192) / dd04 (prio 12288) <-> leaf
bb02 (prio 4096). bb02 reaches root via two equal-cost one-hop paths through
cc03 and dd04; the bridge-id tiebreak makes bb02:ge-0/0/2 (facing dd04, the
higher bridge id) alternate/blocking at HIGH confidence. VLAN 10 is pruned
onto the LOSING (dd04) path: carried on aa01-dd04 and dd04-bb02, absent from
aa01-cc03 and cc03-bb02, with the VLAN-10 IRB exit on aa01. So bb02's only
VLAN-10 path to the exit runs through the blocked dd04<->bb02 edge; removing
that hard-eligible edge stitches an "is it load-bearing" test as membership
in the bb02-only stranded component.
"""
from __future__ import annotations

import dataclasses

from digital_twin.analysis.context import AnalysisContext
from digital_twin.analysis.stp_agreement import compare_to_observed
from digital_twin.analysis.stp_reachability import StpReachability
from digital_twin.ir import IRBuilder, Vlan
from tests.factories import access_port, irb, link, make_port, sw, trunk_port

_BLOCK_PORT = "bb02:ge-0/0/2"  # faces dd04 (the higher bridge id) -> alternate/blocking, HIGH
_ROOT_PORT = "bb02:ge-0/0/1"  # faces cc03 (the lower bridge id) -> root/forwarding, HIGH


def _set_observed(ir, pid: str, *, role: str | None, state: str | None):
    port = ir.ports[pid]
    new_port = dataclasses.replace(port, stp_role=role, stp_state=state)
    new_ports = dict(ir.ports)
    new_ports[pid] = new_port
    return dataclasses.replace(ir, ports=new_ports)


def _v10_port(did: str, name: str, *, carry: bool = False):
    """A bridge-id-topology port (observed_speed="1g" for STP costing) that
    ALSO carries VLAN 10 as a tagged trunk member when `carry` — i.e. VLAN 10
    rides the SAME physical port/link as the STP-active topology edge, so the
    STP-blocked edge and the VLAN-10 edge share one `member_ports` key."""
    port = make_port(did, name, observed_speed="1g")
    if carry:
        port = dataclasses.replace(port, tagged_vlans=(10,))
    return port


def _bridge_id_topology(
    builder: IRBuilder,
    *,
    prune_vlan10: bool = False,
    include_dd04_bb02_link: bool = True,
    carry_both_paths: bool = False,
) -> None:
    """The exact 4-switch topology from test_root_port_by_bridge_id_tiebreak_is_high.
    When `prune_vlan10`, VLAN 10 is tagged onto the aa01-dd04 and dd04-bb02
    trunks (the LOSING/blocked path) and absent from aa01-cc03/cc03-bb02, so
    the VLAN-10 edge and the predicted-blocked edge are the SAME L2 edge.
    When `include_dd04_bb02_link` is False, the dd04<->bb02 link (and VLAN-10
    carriage on it) is omitted entirely — used to build the BASELINE side of
    the `edge_new_in_proposed` variant, where that edge is new in the delta.
    When `carry_both_paths`, VLAN 10 additionally rides the WINNING (cc03) path
    too, so a forwarding path to the exit survives even with the dd04 path's
    blocked edge removed — used for the redundant-carriage soft-dependence
    negative case."""
    builder.add_device(sw("aa01", stp_priority=0))
    builder.add_device(sw("bb02", stp_priority=4096))
    builder.add_device(sw("cc03", stp_priority=8192))
    builder.add_device(sw("dd04", stp_priority=12288))
    builder.add_port(_v10_port("aa01", "ge-0/0/1", carry=carry_both_paths))
    builder.add_port(_v10_port("cc03", "ge-0/0/1", carry=carry_both_paths))
    builder.add_port(_v10_port("aa01", "ge-0/0/2", carry=prune_vlan10))
    builder.add_port(_v10_port("dd04", "ge-0/0/1", carry=prune_vlan10))
    builder.add_port(_v10_port("cc03", "ge-0/0/2", carry=carry_both_paths))
    builder.add_port(_v10_port("bb02", "ge-0/0/1", carry=carry_both_paths))
    builder.add_link(link("aa01:ge-0/0/1", "cc03:ge-0/0/1"))
    builder.add_link(link("aa01:ge-0/0/2", "dd04:ge-0/0/1"))
    builder.add_link(link("cc03:ge-0/0/2", "bb02:ge-0/0/1"))
    if include_dd04_bb02_link:
        builder.add_port(_v10_port("dd04", "ge-0/0/2", carry=prune_vlan10))
        builder.add_port(_v10_port("bb02", "ge-0/0/2", carry=prune_vlan10))
        builder.add_link(link("dd04:ge-0/0/2", "bb02:ge-0/0/2"))
    if prune_vlan10:
        builder.add_vlan(Vlan(vlan_id=10, name="v10", scope="s1"))
        builder.add_l3intf(irb("aa01", 10))
        builder.add_port(access_port("bb02", "acc", 10))


def _parallel_link_topology(builder: IRBuilder, *, prune_vlan10: bool = False) -> None:
    """test_parallel_links_same_pair_port_id_tie_low: two standalone A<->B
    links -> port_id_tie, LOW confidence. Used only for the LOW-confidence
    variant (the brief: block_confidence="low" swaps to this topology). VLAN
    10 (when pruned) rides the SAME ge-0/0/2 link that carries the LOW-conf
    blocked port, with an access member on bb02 and IRB exit on aa01."""
    builder.add_device(sw("aa01", stp_priority=0))
    builder.add_device(sw("bb02"))
    builder.add_port(make_port("aa01", "ge-0/0/1"))
    p2a = make_port("aa01", "ge-0/0/2")
    p2b = make_port("bb02", "ge-0/0/2")
    if prune_vlan10:
        p2a = dataclasses.replace(p2a, tagged_vlans=(10,))
        p2b = dataclasses.replace(p2b, tagged_vlans=(10,))
    builder.add_port(p2a)
    builder.add_port(make_port("bb02", "ge-0/0/1"))
    builder.add_port(p2b)
    builder.add_link(link("aa01:ge-0/0/1", "bb02:ge-0/0/1"))
    builder.add_link(link("aa01:ge-0/0/2", "bb02:ge-0/0/2"))
    if prune_vlan10:
        builder.add_vlan(Vlan(vlan_id=10, name="v10", scope="s1"))
        builder.add_l3intf(irb("aa01", 10))
        builder.add_port(access_port("bb02", "acc", 10))


def _pruned_onto_block_pair(
    *,
    block_confirmed: bool,
    block_confidence: str = "high",
    edge_new_in_proposed: bool = False,
    baseline_bpdu: bool = False,
    preexisting: bool = False,
    block_new_in_proposed: bool = False,
):
    """Build (baseline_ir, proposed_ir) per the brief's flags. Baseline and
    proposed share the same core bridge-id topology + VLAN-10 layer; the
    flags vary telemetry confirmation, confidence, edge novelty, or BPDU
    inconsistency as documented in task-2-brief.md.

    `block_new_in_proposed`: the dd04<->bb02 link (and its predicted-blocking
    classification) is absent from baseline entirely and present in proposed
    only — i.e. the blocked-edge KEY SET differs between sides even though
    `block_confirmed` stays False (soft-only) on the proposed side. Used by
    `blocked_edge_keys_changed` (the relevance gate), distinct from
    `edge_new_in_proposed` which is reserved for the existing-in-baseline
    hard-licence clause test."""
    is_low = block_confidence == "low"
    omit_from_baseline = edge_new_in_proposed or block_new_in_proposed

    baseline_builder = IRBuilder()
    if is_low:
        _parallel_link_topology(baseline_builder, prune_vlan10=True)
    else:
        _bridge_id_topology(
            baseline_builder, prune_vlan10=True, include_dd04_bb02_link=not omit_from_baseline
        )
    baseline_ir = baseline_builder.build()

    proposed_builder = IRBuilder()
    if is_low:
        _parallel_link_topology(proposed_builder, prune_vlan10=True)
    else:
        _bridge_id_topology(proposed_builder, prune_vlan10=True, include_dd04_bb02_link=True)
    proposed_ir = proposed_builder.build()

    block_role, block_state = "alternate", "blocking"

    if baseline_bpdu:
        # isolate the bpdu-inconsistent clause: set the block port to observed
        # role/state matching prediction (non-vacuous), then poison one OTHER
        # port in the component with bpdu-inconsistent.
        baseline_ir = _set_observed(baseline_ir, _BLOCK_PORT, role=block_role, state=block_state)
        baseline_ir = _set_observed(
            baseline_ir, _ROOT_PORT, role="disabled-bpdu-inconsistent", state=None
        )
    elif block_confirmed and not omit_from_baseline:
        # the blocked edge doesn't exist in baseline for edge_new_in_proposed /
        # block_new_in_proposed — nothing to confirm telemetry on there.
        baseline_ir = _set_observed(baseline_ir, _BLOCK_PORT, role=block_role, state=block_state)

    if preexisting and block_confirmed:
        proposed_ir = _set_observed(proposed_ir, _BLOCK_PORT, role=block_role, state=block_state)

    return baseline_ir, proposed_ir


def _redundant_both_carry_pair():
    """Same bridge-id topology, but VLAN 10 rides BOTH inter-switch paths
    (aa01-cc03-bb02 AND aa01-dd04-bb02), not just the losing dd04 path. The
    dd04<->bb02 edge is still predicted-blocking (soft-only: no telemetry
    confirmation), but a fully-forwarding VLAN-10 path to the exit survives via
    cc03 — so bb02 must NOT be reported soft-dependent. Baseline and proposed
    are identical (no delta under test here; this fixture isolates the
    negative soft-dependence case)."""

    def _builder() -> IRBuilder:
        b = IRBuilder()
        _bridge_id_topology(b, prune_vlan10=True, carry_both_paths=True)
        return b

    baseline_ir = _builder().build()
    proposed_ir = _builder().build()
    return baseline_ir, proposed_ir


def _simple_tree_pair():
    """A tree topology (no cycles, no predicted blocks) -> STP-aware view
    must equal the plain vlan_components() view."""

    def _builder() -> IRBuilder:
        b = IRBuilder()
        b.add_device(sw("aa01", stp_priority=0))
        b.add_device(sw("bb02", stp_priority=4096))
        b.add_vlan(Vlan(vlan_id=10, name="v10", scope="s1"))
        b.add_l3intf(irb("aa01", 10))
        b.add_port(access_port("bb02", "acc", 10))
        b.add_port(trunk_port("aa01", "to-bb02", tagged=(10,)))
        b.add_port(trunk_port("bb02", "to-aa01", tagged=(10,)))
        b.add_link(link("aa01:to-bb02", "bb02:to-aa01"))
        return b

    baseline_ir = _builder().build()
    proposed_ir = _builder().build()
    return baseline_ir, proposed_ir


def test_hard_block_strands_pruned_vlan_component():
    base, prop = _pruned_onto_block_pair(block_confirmed=True)
    sr = StpReachability(AnalysisContext(base), AnalysisContext(prop))
    comps = sr.proposed_components(10)
    b_comp = next(c for c in comps if any(n.startswith("bb") for n in c.nodes))
    assert not b_comp.reaches_exit  # hard-removed: the block is load-bearing


def test_no_baseline_agreement_leaves_edge_soft_not_removed():
    base, prop = _pruned_onto_block_pair(block_confirmed=False)
    sr = StpReachability(AnalysisContext(base), AnalysisContext(prop))
    comps = sr.proposed_components(10)
    b_comp = next(c for c in comps if any(n.startswith("bb") for n in c.nodes))
    assert b_comp.reaches_exit  # vacuous agreement -> soft-only -> NOT hard-removed


def test_low_confidence_block_is_soft_not_removed():
    base, prop = _pruned_onto_block_pair(block_confirmed=True, block_confidence="low")
    sr = StpReachability(AnalysisContext(base), AnalysisContext(prop))
    comps = sr.proposed_components(10)
    b_comp = next(c for c in comps if any(n.startswith("bb") for n in c.nodes))
    assert b_comp.reaches_exit  # LOW proposed confidence -> soft-only


def test_new_intra_component_edge_is_soft_only():
    base, prop = _pruned_onto_block_pair(block_confirmed=True, edge_new_in_proposed=True)
    sr = StpReachability(AnalysisContext(base), AnalysisContext(prop))
    comps = sr.proposed_components(10)
    b_comp = next(c for c in comps if any(n.startswith("bb") for n in c.nodes))
    assert b_comp.reaches_exit  # existed-in-baseline clause fails -> soft


def test_bpdu_inconsistent_component_does_not_license_hard():
    base, prop = _pruned_onto_block_pair(block_confirmed=True, baseline_bpdu=True)
    sr = StpReachability(AnalysisContext(base), AnalysisContext(prop))
    comps = sr.proposed_components(10)
    b_comp = next(c for c in comps if any(n.startswith("bb") for n in c.nodes))

    # Verify the component is NON-VACUOUS (matched_count > 0) with BPDU poison
    # (bpdu_inconsistent_count > 0): this isolates the bpdu clause from vacuity.
    report = compare_to_observed(sr._baseline.stp_tree(), sr._baseline.ir)
    b_agreement = next(a for a in report.components if any(n.startswith("bb") for n in a.nodes))
    assert b_agreement.matched_count > 0, "baseline component must have matched evidence"
    assert b_agreement.bpdu_inconsistent_count > 0, "baseline component must have bpdu poison"
    assert not b_agreement.agreement_clean, "agreement must be unclean (bpdu)"

    assert b_comp.reaches_exit  # agreement_clean False (bpdu) -> soft


def test_baseline_components_use_baseline_side_blocking():
    base, prop = _pruned_onto_block_pair(block_confirmed=True, preexisting=True)
    sr = StpReachability(AnalysisContext(base), AnalysisContext(prop))
    b_base = next(c for c in sr.baseline_components(10) if any(n.startswith("bb") for n in c.nodes))
    b_prop = next(c for c in sr.proposed_components(10) if any(n.startswith("bb") for n in c.nodes))
    assert not b_base.reaches_exit and not b_prop.reaches_exit  # symmetric strand


def test_no_predicted_blocks_matches_plain_vlan_components():
    base, prop = _simple_tree_pair()
    ctx = AnalysisContext(prop)
    sr = StpReachability(AnalysisContext(base), ctx)
    assert sr.proposed_components(10) == ctx.vlan_components(10)


def test_soft_dependence_detected_when_only_soft_block_carries_reach():
    # VLAN 10 reaches exit only via a SOFT-only blocked edge -> soft-dependent
    base, prop = _pruned_onto_block_pair(block_confirmed=False)  # soft-only
    sr = StpReachability(AnalysisContext(base), AnalysisContext(prop))
    soft = sr.proposed_soft_dependent_components(10)
    assert any(any(n.startswith("bb") for n in c.nodes) for c in soft)


def test_hard_dependence_is_not_soft_dependent():
    # a hard-eligible block already strands the component (it does NOT reach exit
    # in the hard view) -> NOT reported as soft-dependent (the hard path owns it)
    base, prop = _pruned_onto_block_pair(block_confirmed=True)
    sr = StpReachability(AnalysisContext(base), AnalysisContext(prop))
    assert sr.proposed_soft_dependent_components(10) == ()


def test_forwarding_path_is_not_soft_dependent():
    # VLAN 10 carried on BOTH links; blocking one leaves a forwarding path
    base, prop = _redundant_both_carry_pair()
    sr = StpReachability(AnalysisContext(base), AnalysisContext(prop))
    assert sr.proposed_soft_dependent_components(10) == ()


def test_blocked_edge_keys_changed_true_when_soft_set_differs():
    base, prop = _pruned_onto_block_pair(block_confirmed=False, block_new_in_proposed=True)
    sr = StpReachability(AnalysisContext(base), AnalysisContext(prop))
    assert sr.blocked_edge_keys_changed(10) is True


def test_blocked_edge_keys_changed_false_when_identical():
    base, prop = _pruned_onto_block_pair(block_confirmed=False, preexisting=True)
    sr = StpReachability(AnalysisContext(base), AnalysisContext(prop))
    assert sr.blocked_edge_keys_changed(10) is False


def _bridge_id_swap_pair():
    """IDENTICAL topology on both sides (same 4 switches, same 4 links, same
    VLAN-10 membership on ALL FOUR inter-switch links — fully redundant, so
    the predicted block is cosmetic and never strands anything). NO observed
    STP telemetry anywhere, so every predicted block is SOFT on both sides;
    the hard-eligible set is empty on both sides too.

    The root-port tiebreak among bb02's two equal-cost candidate edges
    (per test_root_port_by_bridge_id_tiebreak_is_high, deciding_factor
    "bridge_id") is keyed on the NEIGHBOR DEVICE ID STRING at each transit
    switch — the two transit switches' `stp_priority` values only affect
    which switch is elected ROOT of the whole component, and with aa01 fixed
    at priority 0 as root, cc03/dd04's own priorities never enter the root
    port comparison at bb02. So to move the tiebreak while leaving the
    topology, links, ports and VLAN membership byte-for-byte identical, the
    two transit switches SWAP DEVICE IDS (cc03 <-> dd04) between baseline and
    proposed — same physical positions, same everything else, just which
    label sits at which position:
      baseline: cc03 at aa01:ge-0/0/1 side, dd04 at aa01:ge-0/0/2 side
                -> blocks bb02:ge-0/0/2 (faces dd04, the higher bridge id)
      proposed: dd04 at aa01:ge-0/0/1 side, cc03 at aa01:ge-0/0/2 side
                -> blocks bb02:ge-0/0/1 (faces cc03, now the higher bridge id)

    Hard-removed components are IDENTICAL across sides (no telemetry -> empty
    hard set on both sides -> hard-removed view == plain vlan_components on
    both sides, and the graphs are isomorphic under the relabeling), but the
    soft-blocked edge key set MOVES from the dd04-bb02 edge to the cc03-bb02
    edge.
    """

    def _builder(*, at_port1: str, at_port2: str) -> IRBuilder:
        b = IRBuilder()
        b.add_device(sw("aa01", stp_priority=0))
        b.add_device(sw("bb02", stp_priority=4096))
        b.add_device(sw(at_port1, stp_priority=8192))
        b.add_device(sw(at_port2, stp_priority=12288))
        b.add_port(_v10_port("aa01", "ge-0/0/1", carry=True))
        b.add_port(_v10_port(at_port1, "ge-0/0/1", carry=True))
        b.add_port(_v10_port("aa01", "ge-0/0/2", carry=True))
        b.add_port(_v10_port(at_port2, "ge-0/0/1", carry=True))
        b.add_port(_v10_port(at_port1, "ge-0/0/2", carry=True))
        b.add_port(_v10_port("bb02", "ge-0/0/1", carry=True))
        b.add_port(_v10_port(at_port2, "ge-0/0/2", carry=True))
        b.add_port(_v10_port("bb02", "ge-0/0/2", carry=True))
        b.add_link(link("aa01:ge-0/0/1", f"{at_port1}:ge-0/0/1"))
        b.add_link(link("aa01:ge-0/0/2", f"{at_port2}:ge-0/0/1"))
        b.add_link(link(f"{at_port1}:ge-0/0/2", "bb02:ge-0/0/1"))
        b.add_link(link(f"{at_port2}:ge-0/0/2", "bb02:ge-0/0/2"))
        b.add_vlan(Vlan(vlan_id=10, name="v10", scope="s1"))
        b.add_l3intf(irb("aa01", 10))
        b.add_port(access_port("bb02", "acc", 10))
        return b

    baseline_ir = _builder(at_port1="cc03", at_port2="dd04").build()
    proposed_ir = _builder(at_port1="dd04", at_port2="cc03").build()
    return baseline_ir, proposed_ir


def test_blocked_edge_keys_changed_on_soft_set_change_with_identical_hard_components():
    """Pin the union property: blocked_edge_keys_changed catches a SOFT-only
    change that STP-aware _vlan_changed would MISS entirely.

    Scenario: IDENTICAL topology and IDENTICAL VLAN-10 membership (all four
    inter-switch links carry VLAN 10 -> fully redundant, nothing strands) on
    both sides, with NO observed STP telemetry anywhere -> every predicted
    block is soft, and the hard-eligible set is empty on both sides. The only
    difference is which transit switch (cc03 vs dd04) sits at which physical
    position, which moves the predicted (soft) blocking port from
    bb02:ge-0/0/2 (faces dd04) in baseline to bb02:ge-0/0/1 (faces cc03) in
    proposed.

    Because the hard-removed components are identical across sides,
    STP-aware _vlan_changed (which only compares hard-removed components)
    would see NO difference and return False. blocked_edge_keys_changed's
    UNION (hard | soft) still fires True because the soft-blocked edge key
    itself moved -- proving the union adds real coverage beyond _vlan_changed.
    """
    base, prop = _bridge_id_swap_pair()
    sr = StpReachability(AnalysisContext(base), AnalysisContext(prop))

    # Assertion 1 (the one the prior attempt dropped): hard-removed components
    # are IDENTICAL across sides -- this is exactly what _vlan_changed compares,
    # so _vlan_changed would report no change here.
    assert sr.baseline_components(10) == sr.proposed_components(10)

    # Make the mechanism explicit: the soft-blocked edge key sets differ.
    _, baseline_soft = sr._classify(AnalysisContext(base), 10)
    _, proposed_soft = sr._classify(AnalysisContext(prop), 10)
    assert baseline_soft != proposed_soft

    # Assertion 2: blocked_edge_keys_changed fires despite identical
    # hard-removed components, because the union (hard | soft) catches the
    # soft-set delta that _vlan_changed alone would miss.
    assert sr.blocked_edge_keys_changed(10) is True
