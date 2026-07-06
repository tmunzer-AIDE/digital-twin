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
from digital_twin.ir.confidence import ConfidenceLevel
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


def _clause_a_isolated_pair():
    """Isolates hard-eligibility clause (a) ("edge existed in baseline") from
    clauses (b)/(c)/(d). Unlike `edge_new_in_proposed` (which OMITS the
    dd04<->bb02 link from baseline entirely, so there's no baseline telemetry
    to confirm and clause (c) `agreement_clean` co-fails vacuously), this
    fixture keeps the dd04<->bb02 PHYSICAL link present and STP-confirmed on
    BOTH sides -- the baseline STP component is genuinely non-vacuous and
    clean. Only the VLAN-10 CARRIAGE on that link differs: absent in baseline
    (so the VLAN-10 edge key aa01-dd04-bb02's dd04<->bb02 segment does not
    exist in the baseline VLAN-10 graph), present in proposed (VLAN 10 is
    "added" onto an already-existing, already-STP-confirmed link). So clause
    (a) fails in isolation while (b)+(c) (clean baseline component) and (d)
    (side-local HIGH confidence) both hold."""

    def _topology(builder: IRBuilder, *, dd04_bb02_carries_v10: bool) -> None:
        builder.add_device(sw("aa01", stp_priority=0))
        builder.add_device(sw("bb02", stp_priority=4096))
        builder.add_device(sw("cc03", stp_priority=8192))
        builder.add_device(sw("dd04", stp_priority=12288))
        # aa01-cc03-bb02 path: never carries VLAN 10 (irrelevant to this test).
        builder.add_port(_v10_port("aa01", "ge-0/0/1", carry=False))
        builder.add_port(_v10_port("cc03", "ge-0/0/1", carry=False))
        builder.add_port(_v10_port("cc03", "ge-0/0/2", carry=False))
        builder.add_port(_v10_port("bb02", "ge-0/0/1", carry=False))
        # aa01-dd04 leg: carries VLAN 10 on both sides (irrelevant to clause a;
        # keeps the VLAN-10 graph connected up to dd04 on both sides).
        builder.add_port(_v10_port("aa01", "ge-0/0/2", carry=True))
        builder.add_port(_v10_port("dd04", "ge-0/0/1", carry=True))
        # dd04-bb02 leg: the link under test. Physical link + STP topology
        # identical on both sides; VLAN-10 carriage differs per the flag.
        builder.add_port(_v10_port("dd04", "ge-0/0/2", carry=dd04_bb02_carries_v10))
        builder.add_port(_v10_port("bb02", "ge-0/0/2", carry=dd04_bb02_carries_v10))
        builder.add_link(link("aa01:ge-0/0/1", "cc03:ge-0/0/1"))
        builder.add_link(link("aa01:ge-0/0/2", "dd04:ge-0/0/1"))
        builder.add_link(link("cc03:ge-0/0/2", "bb02:ge-0/0/1"))
        builder.add_link(link("dd04:ge-0/0/2", "bb02:ge-0/0/2"))
        builder.add_vlan(Vlan(vlan_id=10, name="v10", scope="s1"))
        builder.add_l3intf(irb("aa01", 10))
        builder.add_port(access_port("bb02", "acc", 10))

    baseline_builder = IRBuilder()
    _topology(baseline_builder, dd04_bb02_carries_v10=False)
    baseline_ir = baseline_builder.build()
    # Confirm the block via telemetry on baseline -> non-vacuous, clean agreement.
    baseline_ir = _set_observed(baseline_ir, _BLOCK_PORT, role="alternate", state="blocking")

    proposed_builder = IRBuilder()
    _topology(proposed_builder, dd04_bb02_carries_v10=True)
    proposed_ir = proposed_builder.build()

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


def _partial_split_pair():
    """The bridge-id topology (VLAN 10 pruned onto the losing dd04 path, as in
    `_pruned_onto_block_pair`) PLUS an extra leaf `ee05` hanging off `bb02`,
    with VLAN 10 carried onward from bb02 to ee05. No observed STP telemetry
    anywhere, so the dd04<->bb02 block is SOFT-only (vacuous agreement).

    In the hard-removed view (nothing removed, since the block isn't
    hard-eligible) the whole component {aa01, bb02, dd04, ee05} reaches the
    exit (aa01's IRB). Once the SOFT dd04<->bb02 edge is ALSO removed, the
    component SPLITS: {aa01, dd04} still reaches the exit, but {bb02, ee05}
    does not -- a PARTIAL split, unlike the single-node-stranded fixtures
    elsewhere in this file where `&` and `-` coincide. This is the
    reviewer's construction for pinning the strict `c.nodes - reaching_nodes`
    subtraction in `proposed_soft_dependent_components`: an `&`-based
    implementation (`c.nodes & reaching_nodes`) would find {aa01, dd04}
    non-empty and WRONGLY conclude the component still reaches -> false-SAFE,
    silently dropping the still-stranded {bb02, ee05}."""

    def _builder() -> IRBuilder:
        b = IRBuilder()
        b.add_device(sw("aa01", stp_priority=0))
        b.add_device(sw("bb02", stp_priority=4096))
        b.add_device(sw("cc03", stp_priority=8192))
        b.add_device(sw("dd04", stp_priority=12288))
        b.add_device(sw("ee05"))
        # aa01-cc03-bb02 path: never carries VLAN 10 (irrelevant here).
        b.add_port(_v10_port("aa01", "ge-0/0/1", carry=False))
        b.add_port(_v10_port("cc03", "ge-0/0/1", carry=False))
        b.add_port(_v10_port("cc03", "ge-0/0/2", carry=False))
        b.add_port(_v10_port("bb02", "ge-0/0/1", carry=False))
        # aa01-dd04-bb02 path: carries VLAN 10 (the losing/blocked path).
        b.add_port(_v10_port("aa01", "ge-0/0/2", carry=True))
        b.add_port(_v10_port("dd04", "ge-0/0/1", carry=True))
        b.add_port(_v10_port("dd04", "ge-0/0/2", carry=True))
        b.add_port(_v10_port("bb02", "ge-0/0/2", carry=True))
        # extra leaf ee05 hanging off bb02, carrying VLAN 10 onward.
        b.add_port(_v10_port("bb02", "ge-0/0/3", carry=True))
        b.add_port(_v10_port("ee05", "ge-0/0/1", carry=True))
        b.add_link(link("aa01:ge-0/0/1", "cc03:ge-0/0/1"))
        b.add_link(link("aa01:ge-0/0/2", "dd04:ge-0/0/1"))
        b.add_link(link("cc03:ge-0/0/2", "bb02:ge-0/0/1"))
        b.add_link(link("dd04:ge-0/0/2", "bb02:ge-0/0/2"))
        b.add_link(link("bb02:ge-0/0/3", "ee05:ge-0/0/1"))
        b.add_vlan(Vlan(vlan_id=10, name="v10", scope="s1"))
        b.add_l3intf(irb("aa01", 10))
        b.add_port(access_port("ee05", "acc", 10))
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


def test_clause_a_isolated_new_vlan_edge_on_clean_component_is_soft():
    # Isolate hard-eligibility clause (a) ("edge existed in baseline") from
    # (b)/(c)/(d): the dd04<->bb02 PHYSICAL link and its STP-confirming
    # telemetry are present and clean in baseline (unlike
    # test_new_intra_component_edge_is_soft_only's edge_new_in_proposed=True,
    # which omits the link from baseline entirely and so co-fails clause (c)
    # vacuously). Only VLAN 10's CARRIAGE on that link is new in proposed.
    base, prop = _clause_a_isolated_pair()
    sr = StpReachability(AnalysisContext(base), AnalysisContext(prop))

    # The baseline component is genuinely CLEAN and NON-VACUOUS: real matched
    # telemetry on the block port satisfies agreement_clean for real, not
    # because there's nothing to disagree with.
    report = compare_to_observed(sr._baseline.stp_tree(), sr._baseline.ir)
    b_agreement = next(a for a in report.components if any(n.startswith("bb") for n in a.nodes))
    assert b_agreement.matched_count > 0, "baseline component must have matched evidence"
    assert b_agreement.agreement_clean is True, "baseline agreement must be clean"

    # The VLAN-10 edge key for the dd04<->bb02 segment is genuinely ABSENT
    # from the baseline VLAN-10 graph -- clause (a) fails on its own.
    base_v10_keys = sr._baseline_edge_keys(10)
    dd04_bb02_key = frozenset({"dd04:ge-0/0/2", "bb02:ge-0/0/2"})
    assert dd04_bb02_key not in base_v10_keys, "edge must be new to the baseline VLAN-10 graph"

    # Clause (d): the proposed-side block is HIGH confidence (bridge_id tiebreak).
    prop_pred = sr._prop_pred[_BLOCK_PORT]
    assert prop_pred.confidence is ConfidenceLevel.HIGH

    comps = sr.proposed_components(10)
    b_comp = next(c for c in comps if any(n.startswith("bb") for n in c.nodes))
    assert b_comp.reaches_exit  # clause (a) alone fails -> soft, edge kept


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


def test_partial_split_component_is_soft_dependent():
    """Pin the strict `c.nodes - reaching_nodes` subtraction in
    `proposed_soft_dependent_components` against a regression to `&`. The
    fixture's soft-removal SPLITS the hard-view component into a reaching
    part {aa01, dd04} and a stranded part {bb02, ee05} -- both the
    intersection AND the difference against `reaching_nodes` are non-empty,
    so an `&`-based implementation would find the intersection non-empty and
    wrongly treat the whole component as still reaching (false-SAFE), silently
    dropping the still-stranded {bb02, ee05}. Single-node-stranded fixtures
    elsewhere in this file can't catch that regression because `&` and `-`
    coincide when only one node is left out.
    """
    base, prop = _partial_split_pair()
    sr = StpReachability(AnalysisContext(base), AnalysisContext(prop))

    hard, soft = sr._classify(sr._proposed, 10)
    assert hard == set(), "block must be soft-only (no baseline telemetry)"
    assert soft, "block must be predicted (soft-eligible)"

    hard_view = sr.proposed_components(10)
    b_comp = next(c for c in hard_view if any(n.startswith("bb") for n in c.nodes))
    assert b_comp.reaches_exit, "hard view: whole component reaches exit"
    assert b_comp.nodes == frozenset({"aa01", "bb02", "dd04", "ee05"})

    hardsoft_view = sr._components(sr._proposed, 10, hard | soft)
    reaching_nodes = frozenset(n for c in hardsoft_view if c.reaches_exit for n in c.nodes)
    difference = b_comp.nodes - reaching_nodes
    intersection = b_comp.nodes & reaching_nodes
    assert difference == frozenset({"bb02", "ee05"}), "partial split: some members stranded"
    assert intersection == frozenset({"aa01", "dd04"}), "partial split: some members still reach"
    # Document why `-` (not `&`) is required: both are non-empty here, so an
    # `&`-based implementation would (wrongly) see this as "still reaching".
    assert difference and intersection

    soft_dep = sr.proposed_soft_dependent_components(10)
    assert b_comp in soft_dep, "partial split must be reported soft-dependent"


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
