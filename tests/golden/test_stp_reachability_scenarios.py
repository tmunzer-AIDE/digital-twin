"""Spec-5 Task 6: end-to-end scenario goldens for STP blocked-link reachability
taint. These drive the FULL verdict (CheckRegistry.run_all -> assemble/decide),
not the blackhole check in isolation, proving decision precedence carries the
hard/soft/pre-existing outcomes through correctly:

  hard strand (telemetry-confirmed HIGH block, load-bearing)  -> Decision.UNSAFE
  pre-existing symmetric block (unrelated delta)               -> not UNSAFE (INFO)
  soft low-confidence / unconfirmed block                      -> Decision.REVIEW, no CRITICAL
  disagreement (telemetry contradicts the predicted block)     -> not UNSAFE (soft only)
  vacuous agreement (no observed telemetry at all)              -> Decision.REVIEW
  new intra-component edge (not baseline-licensed)               -> not UNSAFE (soft)

Entry point: reuse the Task-2/3/4 fixture builders from
tests/analysis/test_stp_reachability.py (the exact pruned-onto-block bridge-id
topology + `_set_observed` telemetry helper) to build baseline/proposed IRs,
then drive them through the SAME production path `simulate()` uses internally:
CheckRegistry(ALL_WIRED_CHECKS).run_all(CheckContext(...)) -> assemble(inputs=
DecisionInputs(check_results=...)). This exercises the real decide() precedence
without going through the raw-JSON ingest harness, which would be disproportionate
scaffolding for topology already expressed precisely at the IR level by the
existing builders.
"""
from __future__ import annotations

import dataclasses

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext
from digital_twin.checks.registry import CheckRegistry
from digital_twin.checks.wired import ALL_WIRED_CHECKS
from digital_twin.contracts import Severity
from digital_twin.ir import IRBuilder, IRCapability, diff_ir
from digital_twin.verdict.decision import Decision, DecisionInputs
from digital_twin.verdict.verdict import Verdict, assemble
from tests.analysis.test_stp_reachability import _bridge_id_topology, _set_observed

_BLOCK_PORT = "bb02:ge-0/0/2"  # faces dd04 (higher bridge id) -> alternate/blocking, HIGH


def _with_caps(builder: IRBuilder) -> IRBuilder:
    return builder.with_capability(IRCapability.WIRED_L2).with_capability(IRCapability.L3_EXITS)


def _bridge_id_ir(*, carry_both_paths: bool):
    b = IRBuilder()
    _bridge_id_topology(b, prune_vlan10=True, carry_both_paths=carry_both_paths)
    return _with_caps(b).build()


def _pruned_onto_block_plan(
    *,
    block_confirmed: bool,
    preexisting: bool = False,
    unrelated_delta: bool = False,
    telemetry_contradicts: bool = False,
    edge_new_in_proposed: bool = False,
):
    """Build (baseline_ir, proposed_ir) for the Task-2 bridge-id pruned-onto-block
    topology, with capabilities wired for the full check registry.

    Not `preexisting`: baseline carries VLAN 10 on BOTH inter-switch paths
    (redundant) while proposed prunes it back onto ONLY the dd04 (blocked)
    path — a normal VLAN-10-irrelevant delta severs the cc03 carriage, so
    bb02's only path now runs through the hard-blocked edge -> newly stranded.

    `preexisting=True`: the SAME pruned-onto-block topology, unchanged, on
    both sides (`unrelated_delta` adds a cosmetic vlan-99 change so the
    delta is non-empty but VLAN 10's structure is untouched) -> symmetric,
    not delta-caused.

    `telemetry_contradicts=True`: observed stp_state on the block port
    disagrees with the HIGH prediction (forwarding instead of blocking) on
    BOTH sides -> baseline agreement is unclean -> the license fails ->
    soft-only regardless of `block_confirmed`.

    `edge_new_in_proposed=True`: the dd04<->bb02 link is absent from baseline
    entirely and only appears in proposed -> fails the "edge existed in
    baseline" hard-eligibility clause -> soft-only even if confirmed.
    """
    if preexisting:
        base = _bridge_id_ir(carry_both_paths=False)
        prop = _bridge_id_ir(carry_both_paths=False)
        if block_confirmed:
            base = _set_observed(base, _BLOCK_PORT, role="alternate", state="blocking")
            prop = _set_observed(prop, _BLOCK_PORT, role="alternate", state="blocking")
        if unrelated_delta:
            prop = _add_unrelated_vlan(prop)
        return base, prop

    if edge_new_in_proposed:
        base_builder = IRBuilder()
        _bridge_id_topology(base_builder, prune_vlan10=True, include_dd04_bb02_link=False)
        base = _with_caps(base_builder).build()
        prop = _bridge_id_ir(carry_both_paths=False)
        if block_confirmed:
            # nothing to confirm telemetry on in baseline (edge doesn't exist there);
            # proposed-side telemetry still confirms the block on that side alone.
            prop = _set_observed(prop, _BLOCK_PORT, role="alternate", state="blocking")
        return base, prop

    base = _bridge_id_ir(carry_both_paths=True)
    prop = _bridge_id_ir(carry_both_paths=False)

    if telemetry_contradicts:
        # both sides observe the block port as FORWARDING (root), contradicting
        # the HIGH alternate/blocking prediction -> disagreement -> soft-only
        base = _set_observed(base, _BLOCK_PORT, role="root", state="forwarding")
        prop = _set_observed(prop, _BLOCK_PORT, role="root", state="forwarding")
    elif block_confirmed:
        base = _set_observed(base, _BLOCK_PORT, role="alternate", state="blocking")
        prop = _set_observed(prop, _BLOCK_PORT, role="alternate", state="blocking")

    return base, prop


def _add_unrelated_vlan(ir):
    """Add a fully-formed, self-contained vlan-99 (access member + its own IRB
    exit, both on aa01) so the delta is genuinely benign — non-empty but not
    itself a new blackhole/unlocatable-exit finding — without touching VLAN
    10's structure. This is the `unrelated_delta` fixture."""
    from digital_twin.ir import Vlan
    from tests.factories import access_port, irb

    new_port = access_port("aa01", "acc-99", 99)
    new_intf = irb("aa01", 99)
    new_ports = dict(ir.ports)
    new_ports[new_port.id] = new_port
    new_vlans = dict(ir.vlans)
    v99 = Vlan(vlan_id=99, name="x", scope="s1")
    new_vlans[v99.vlan_id] = v99
    new_l3intfs = (*ir.l3intfs, new_intf)
    return dataclasses.replace(ir, ports=new_ports, vlans=new_vlans, l3intfs=new_l3intfs)


def _run(pair) -> Verdict:
    baseline_ir, proposed_ir = pair
    diff = diff_ir(baseline_ir, proposed_ir)
    ctx = CheckContext(
        baseline=AnalysisContext(baseline_ir),
        proposed=AnalysisContext(proposed_ir),
        diff=diff,
    )
    results = CheckRegistry(ALL_WIRED_CHECKS).run_all(ctx)
    return assemble(
        inputs=DecisionInputs(
            rejections=(),
            l0_fatal=False,
            baseline_unavailable=False,
            check_results=results,
        ),
        ir_diff=diff,
    )


def test_scenario_hard_strand_is_unsafe():
    verdict = _run(_pruned_onto_block_plan(block_confirmed=True))
    assert verdict.decision is Decision.UNSAFE
    assert any(f.code == "wired.l2.blackhole.exit_lost" for f in verdict.findings)


def test_scenario_preexisting_symmetric_unchanged():
    verdict = _run(
        _pruned_onto_block_plan(block_confirmed=True, preexisting=True, unrelated_delta=True)
    )
    assert verdict.decision is not Decision.UNSAFE  # INFO context only


def test_scenario_soft_low_confidence_is_review_not_unsafe():
    verdict = _run(_pruned_onto_block_plan(block_confirmed=False))
    assert verdict.decision is Decision.REVIEW
    assert not any(f.severity is Severity.CRITICAL for f in verdict.findings)


def test_scenario_disagreement_stays_soft():
    verdict = _run(_pruned_onto_block_plan(block_confirmed=True, telemetry_contradicts=True))
    assert verdict.decision is not Decision.UNSAFE  # mismatch -> soft only


def test_scenario_vacuous_agreement_stays_soft():
    verdict = _run(_pruned_onto_block_plan(block_confirmed=False))  # no telemetry
    assert verdict.decision is Decision.REVIEW


def test_scenario_new_intra_component_edge_soft():
    verdict = _run(_pruned_onto_block_plan(block_confirmed=True, edge_new_in_proposed=True))
    assert verdict.decision is not Decision.UNSAFE  # not baseline-licensed -> soft
