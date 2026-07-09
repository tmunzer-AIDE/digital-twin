"""CheckContext.stp_agreement (Spec-6 Task 1): ONE shared baseline
StpAgreementReport memo, passed into StpReachability (and later StpInertness)
so cache behavior lives in exactly one place."""
from __future__ import annotations

from digital_twin.analysis.context import AnalysisContext
from digital_twin.analysis.stp_agreement import compare_to_observed
from digital_twin.checks.base import CheckContext
from digital_twin.ir import IRBuilder, IRCapability, diff_ir
from tests.factories import link, make_port, sw


def _ctx() -> CheckContext:
    b = IRBuilder().add_device(sw("aa01", stp_priority=0)).add_device(sw("bb02"))
    b.add_port(make_port("aa01", "ge-0/0/1", observed_speed="1g"))
    b.add_port(make_port("bb02", "ge-0/0/1", observed_speed="1g"))
    b.add_link(link("aa01:ge-0/0/1", "bb02:ge-0/0/1"))
    ir = b.with_capability(IRCapability.WIRED_L2).build()
    return CheckContext(
        baseline=AnalysisContext(ir), proposed=AnalysisContext(ir), diff=diff_ir(ir, ir)
    )


def test_stp_agreement_is_memoized_same_object():
    ctx = _ctx()
    assert ctx.stp_agreement is ctx.stp_agreement


def test_stp_agreement_equals_direct_comparator_run():
    ctx = _ctx()
    direct = compare_to_observed(ctx.baseline.stp_tree(), ctx.baseline.ir)
    assert ctx.stp_agreement == direct


def test_stp_reachability_receives_the_shared_report():
    ctx = _ctx()
    report = ctx.stp_agreement
    # the memoized StpReachability must hold the SAME object, not a recompute
    assert ctx.stp_reachability._base_agreements is report.components
