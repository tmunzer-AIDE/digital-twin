from digital_twin.analysis.context import AnalysisContext
from digital_twin.analysis.delta_cause import DeltaIndex, delta_index
from digital_twin.checks.base import CheckContext
from digital_twin.ir import IRBuilder, IRDiff


def _empty_ir():
    return IRBuilder().build()


def test_check_context_has_delta_index_default():
    ir = _empty_ir()
    diff = IRDiff((), (), ())
    ctx = CheckContext(baseline=AnalysisContext(ir), proposed=AnalysisContext(ir), diff=diff)
    assert isinstance(ctx.delta_index, DeltaIndex)  # default built from the empty diff


def test_check_context_accepts_explicit_index():
    ir = _empty_ir()
    di = delta_index(IRDiff((), (), ()))
    ctx = CheckContext(
        baseline=AnalysisContext(ir),
        proposed=AnalysisContext(ir),
        diff=IRDiff((), (), ()),
        delta_index=di,
    )
    assert ctx.delta_index is di
