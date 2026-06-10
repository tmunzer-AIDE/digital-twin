"""l2.loop spec table: cycle + all-STP = PASS; + STP disabled = FAIL(HIGH);
+ STP unknown = WARN(LOW). Only NEW cycles are attributed to the delta."""

from dataclasses import replace

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.wired.l2_loop import L2LoopCheck
from digital_twin.contracts import FindingCategory, Severity
from digital_twin.ir import ConfidenceLevel, IRBuilder, IRCapability, Vlan, diff_ir
from tests.factories import link, sw, trunk_port


def _ring_ir(stp: bool | None, parallel: bool):
    """A-B with one link (tree) or two standalone links (cycle)."""
    b = IRBuilder()
    b.add_device(sw("A")).add_device(sw("B"))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    for dev, peer in (("A", "B"), ("B", "A")):
        p1 = trunk_port(dev, f"to-{peer}-1", tagged=(10,))
        b.add_port(replace(p1, stp_enabled=stp))
        if parallel:
            p2 = trunk_port(dev, f"to-{peer}-2", tagged=(10,))
            b.add_port(replace(p2, stp_enabled=stp))
    b.add_link(link("A:to-B-1", "B:to-A-1"))
    if parallel:
        b.add_link(link("A:to-B-2", "B:to-A-2"))
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def _ctx(baseline, proposed) -> CheckContext:
    return CheckContext(
        baseline=AnalysisContext(baseline),
        proposed=AnalysisContext(proposed),
        diff=diff_ir(baseline, proposed),
    )


def test_new_cycle_with_stp_everywhere_passes():
    ctx = _ctx(_ring_ir(stp=True, parallel=False), _ring_ir(stp=True, parallel=True))
    result = L2LoopCheck().run(ctx)
    assert result.status is Status.PASS  # protected redundancy, not a loop


def test_new_cycle_with_stp_disabled_fails_high():
    ctx = _ctx(_ring_ir(stp=False, parallel=False), _ring_ir(stp=False, parallel=True))
    result = L2LoopCheck().run(ctx)
    assert result.status is Status.FAIL
    f = result.findings[0]
    assert f.severity is Severity.ERROR and f.category is FindingCategory.NETWORK
    assert f.confidence.level is ConfidenceLevel.HIGH


def test_new_cycle_with_stp_unknown_warns_low():
    ctx = _ctx(_ring_ir(stp=None, parallel=False), _ring_ir(stp=None, parallel=True))
    result = L2LoopCheck().run(ctx)
    assert result.status is Status.WARN
    assert result.findings[0].confidence.level is ConfidenceLevel.LOW


def test_stp_regression_on_existing_cycle_fails():
    # the cycle's NODE SET is unchanged, but the delta disables STP on its
    # ports — the spec's attributable condition is "cycle + STP disabled",
    # which IS newly introduced here. Must FAIL, not hide behind "preexisting".
    ctx = _ctx(_ring_ir(stp=True, parallel=True), _ring_ir(stp=False, parallel=True))
    result = L2LoopCheck().run(ctx)
    assert result.status is Status.FAIL
    assert any(f.code == "wired.l2.loop.unprotected" for f in result.findings)


def test_stp_becoming_unknown_on_existing_cycle_warns():
    ctx = _ctx(_ring_ir(stp=True, parallel=True), _ring_ir(stp=None, parallel=True))
    result = L2LoopCheck().run(ctx)
    assert result.status is Status.WARN


def test_preexisting_cycle_is_context_not_failure():
    same = _ring_ir(stp=False, parallel=True)
    ctx = _ctx(same, _ring_ir(stp=False, parallel=True))
    result = L2LoopCheck().run(ctx)
    assert result.status is Status.PASS  # not introduced by the delta
    assert any(f.severity is Severity.INFO for f in result.findings)  # reported as context


def test_applies_to_link_and_port_changes_only():
    check = L2LoopCheck()
    base, prop = _ring_ir(True, False), _ring_ir(True, True)
    assert check.applies_to(diff_ir(base, prop)) is True
    assert check.applies_to(diff_ir(base, base)) is False
