"""Spec-6 e2e goldens: STP policy SAFE grants through the FULL verdict
(CheckRegistry.run_all -> assemble/decide):

  bulk root-protect on validated inter-switch designated downlinks -> SAFE
  stp_required enable on a validated pair -> SAFE, coverage COMPLETE (R2-P1:
      the provisional "peer unobserved" note is DISCARDED, not outvoted)
  same bulk plan + one telemetry-dark port -> REVIEW
  observed-designated NON-TREE access port -> REVIEW (R1-P1-2 boundary)
  stp_p2p change -> REVIEW, byte-identical to Spec-2

Fixture: the fully-observed bridge-id topology (all 8 rows matched, one clean
component); VLAN 10 redundantly carried on both paths so no blackhole/loop
finding interferes with the SAFE assertions.
"""
from __future__ import annotations

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, CoverageState
from digital_twin.checks.registry import CheckRegistry
from digital_twin.checks.wired import ALL_WIRED_CHECKS
from digital_twin.ir import IRBuilder, IRCapability, diff_ir
from digital_twin.verdict.decision import Decision, DecisionInputs
from digital_twin.verdict.verdict import assemble
from tests.analysis.test_stp_inertness import _fully_observed, _with_policy
from tests.analysis.test_stp_reachability import _bridge_id_topology, _set_observed

_DESIGNATED_DOWNLINKS = ("aa01:ge-0/0/1", "aa01:ge-0/0/2", "cc03:ge-0/0/2", "dd04:ge-0/0/2")


def _validated_ir():
    # CLIENTS_ACTIVE models a SUCCESSFUL zero-client fetch (plan-review P1:
    # without it, wired.client.impact returns INSUFFICIENT_DATA on any port
    # diff and blackhole adds a missing-client note — both force REVIEW and
    # would make the SAFE assertions unreachable)
    b = IRBuilder()
    _bridge_id_topology(b, prune_vlan10=True, carry_both_paths=True)
    ir = (
        b.with_capability(IRCapability.WIRED_L2)
        .with_capability(IRCapability.L3_EXITS)
        .with_capability(IRCapability.CLIENTS_ACTIVE)
        .build()
    )
    return _fully_observed(ir)


def _verdict(base, prop):
    # exact harness shape from tests/golden/test_stp_reachability_scenarios.py:_run
    diff = diff_ir(base, prop)
    ctx = CheckContext(
        baseline=AnalysisContext(base), proposed=AnalysisContext(prop), diff=diff,
    )
    results = CheckRegistry(ALL_WIRED_CHECKS).run_all(ctx)
    verdict = assemble(
        inputs=DecisionInputs(
            rejections=(),
            l0_fatal=False,
            baseline_unavailable=False,
            check_results=results,
        ),
        ir_diff=diff,
    )
    return verdict, results


def test_bulk_root_protect_on_designated_downlinks_is_safe():
    base = _validated_ir()
    prop = base
    for pid in _DESIGNATED_DOWNLINKS:
        prop = _with_policy(prop, pid, stp_no_root_port=True)
    verdict, results = _verdict(base, prop)
    assert verdict.decision is Decision.SAFE
    policy = next(r for r in results if r.check_id == "wired.stp.policy")
    grants = [f for f in policy.findings if f.code.endswith("inert_change")]
    assert {f.subject.id for f in grants} == set(_DESIGNATED_DOWNLINKS)


def test_required_enable_on_validated_pair_is_safe_with_complete_coverage():
    base = _validated_ir()
    prop = _with_policy(base, "cc03:ge-0/0/2", stp_required=True)
    verdict, results = _verdict(base, prop)
    assert verdict.decision is Decision.SAFE
    policy = next(r for r in results if r.check_id == "wired.stp.policy")
    assert policy.coverage.state is CoverageState.COMPLETE
    assert not any("peer unobserved" in n for n in policy.coverage.notes)


def test_bulk_plan_with_one_dark_port_is_review():
    base_all = _validated_ir()
    base = _set_observed(base_all, "dd04:ge-0/0/2", role=None, state=None)
    prop = base
    for pid in _DESIGNATED_DOWNLINKS:
        prop = _with_policy(prop, pid, stp_no_root_port=True)
    verdict, _ = _verdict(base, prop)
    assert verdict.decision is Decision.REVIEW


def test_non_tree_access_port_is_review_even_observed_designated():
    base = _validated_ir()
    base = _set_observed(base, "bb02:acc", role="designated", state="forwarding")
    prop = _with_policy(base, "bb02:acc", stp_no_root_port=True)
    verdict, _ = _verdict(base, prop)
    assert verdict.decision is Decision.REVIEW


def test_stp_p2p_change_stays_review():
    base = _validated_ir()
    prop = _with_policy(base, "cc03:ge-0/0/2", stp_p2p=True)
    verdict, _ = _verdict(base, prop)
    assert verdict.decision is Decision.REVIEW
