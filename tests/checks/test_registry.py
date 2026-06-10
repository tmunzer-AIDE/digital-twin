from dataclasses import dataclass, field

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState, Status
from digital_twin.checks.registry import CheckRegistry
from digital_twin.contracts import FindingCategory, Severity
from digital_twin.ir import IRBuilder, IRCapability, diff_ir
from tests.factories import sw


def _ctx(*caps: str) -> CheckContext:
    b1, b2 = IRBuilder(), IRBuilder()
    b1.add_device(sw("A"))
    b2.add_device(sw("A")).add_device(sw("B"))  # diff: device B added
    for c in caps:
        b1.with_capability(c)
        b2.with_capability(c)
    ir1, ir2 = b1.build(), b2.build()
    return CheckContext(
        baseline=AnalysisContext(ir1), proposed=AnalysisContext(ir2), diff=diff_ir(ir1, ir2)
    )


@dataclass
class FakeCheck:
    id: str = "test.fake"
    title: str = "fake"
    domain: str = "test"
    default_severity: Severity = Severity.ERROR
    applies: bool = True
    needs: frozenset = field(default_factory=frozenset)
    boom: bool = False
    ran: bool = False

    def requires(self):
        return self.needs

    def applies_to(self, diff):
        return self.applies

    def run(self, ctx):
        self.ran = True
        if self.boom:
            raise RuntimeError("kaboom")
        return CheckResult(
            check_id=self.id,
            status=Status.PASS,
            findings=(),
            coverage=Coverage(state=CoverageState.COMPLETE),
            confidence=None,
            reasoning="ok",
        )


def test_not_applicable_short_circuits_before_requires():
    # gating order: applies_to FIRST — a non-applicable check with missing caps
    # is NOT_APPLICABLE, never INSUFFICIENT_DATA
    check = FakeCheck(applies=False, needs=frozenset({IRCapability.STP_STATE}))
    (result,) = CheckRegistry([check]).run_all(_ctx())
    assert result.status is Status.NOT_APPLICABLE
    assert check.ran is False


def test_missing_capability_is_insufficient_data():
    check = FakeCheck(needs=frozenset({IRCapability.STP_STATE}))
    (result,) = CheckRegistry([check]).run_all(_ctx())  # ctx has NO capabilities
    assert result.status is Status.INSUFFICIENT_DATA
    assert check.ran is False
    assert result.coverage.state is CoverageState.INSUFFICIENT


def test_capability_present_runs_the_check():
    check = FakeCheck(needs=frozenset({IRCapability.WIRED_L2}))
    (result,) = CheckRegistry([check]).run_all(_ctx(IRCapability.WIRED_L2))
    assert result.status is Status.PASS and check.ran


def test_crash_is_isolated_to_check_error_with_operational_finding():
    boom, ok = FakeCheck(id="test.boom", boom=True), FakeCheck(id="test.ok")
    results = CheckRegistry([boom, ok]).run_all(_ctx())
    by_id = {r.check_id: r for r in results}
    assert by_id["test.boom"].status is Status.CHECK_ERROR
    f = by_id["test.boom"].findings[0]
    assert f.category is FindingCategory.OPERATIONAL  # crash != network breakage
    assert "kaboom" in str(f.evidence.get("error"))
    assert by_id["test.ok"].status is Status.PASS  # one bad check cannot sink the rest
