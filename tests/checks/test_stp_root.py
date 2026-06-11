"""wired.stp.root_change: the delta moves the predicted root bridge of an L2
component (bridge_priority change or topology reshape) -> reconvergence, every
blocked port re-elects -> WARNING/REVIEW. Election: lowest (priority, mac)
among the component's switches; absent priority = platform default 32768
(assumed -> MEDIUM confidence)."""

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.wired.stp_root import StpRootChangeCheck
from digital_twin.contracts import Severity
from digital_twin.ir import ConfidenceLevel, IRBuilder, IRCapability, diff_ir
from tests.factories import link, sw, trunk_port


def _ir(*, a_prio=None, b_prio=None):
    b = IRBuilder()
    b.add_device(sw("aa01", stp_priority=a_prio)).add_device(sw("bb02", stp_priority=b_prio))
    b.add_port(trunk_port("aa01", "ge-0/0/1", tagged=(20,), native=10))
    b.add_port(trunk_port("bb02", "ge-0/0/1", tagged=(20,), native=10))
    b.add_link(link("aa01:ge-0/0/1", "bb02:ge-0/0/1"))
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def _run(base, prop):
    return StpRootChangeCheck().run(
        CheckContext(
            baseline=AnalysisContext(base), proposed=AnalysisContext(prop), diff=diff_ir(base, prop)
        )
    )


def test_priority_change_that_moves_the_root_is_a_warning():
    # baseline: both default 32768 -> lowest mac aa01 wins; proposed: bb02
    # gets 4096 -> root moves -> reconvergence
    result = _run(_ir(), _ir(b_prio=4096))
    assert result.status is Status.WARN
    f = result.findings[0]
    assert f.code == "wired.stp.root_change.moved"
    assert f.severity is Severity.WARNING
    assert f.evidence["baseline_root"] == "aa01" and f.evidence["proposed_root"] == "bb02"
    # a default-32768 assumption is involved -> the claim caps at MEDIUM
    assert f.confidence.level is ConfidenceLevel.MEDIUM


def test_explicit_priorities_on_both_roots_give_high_confidence():
    result = _run(_ir(a_prio=4096, b_prio=8192), _ir(a_prio=16384, b_prio=8192))
    assert result.status is Status.WARN
    assert result.findings[0].confidence.level is ConfidenceLevel.HIGH


def test_priority_zero_is_the_highest_priority_not_default():
    # review regression (3f442f3): 0 is a VALID priority — the strongest one.
    # `or _DEFAULT_PRIORITY` swallowed it as falsy -> false negative.
    result = _run(_ir(), _ir(b_prio=0))
    assert result.status is Status.WARN
    f = result.findings[0]
    assert f.code == "wired.stp.root_change.moved"
    assert f.evidence["proposed_root"] == "bb02"


def test_no_root_movement_is_silent():
    assert _run(_ir(), _ir()).findings == ()
    # priority change that does NOT move the root (aa01 stays lowest)
    assert _run(_ir(a_prio=4096), _ir(a_prio=8192)).findings == ()


def test_single_switch_component_is_silent():
    def lone(prio):
        b = IRBuilder().add_device(sw("aa01", stp_priority=prio))
        b.add_port(trunk_port("aa01", "ge-0/0/1", tagged=(20,)))
        b.with_capability(IRCapability.WIRED_L2)
        return b.build()

    assert _run(lone(None), lone(4096)).findings == ()
