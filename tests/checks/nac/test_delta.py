from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.nac.delta import NacDeltaCheck
from digital_twin.contracts import Severity
from digital_twin.ir import IRBuilder, NacRule, diff_ir


def _ir(*rules):
    b = IRBuilder()
    for r in rules:
        b.add_nacrule(r)
    return b.build()


def _run(base, prop):
    return NacDeltaCheck().run(CheckContext(
        baseline=AnalysisContext(base), proposed=AnalysisContext(prop),
        diff=diff_ir(base, prop)))


def test_modify_emits_one_warning_with_cause():
    base = _ir(NacRule(id="r1", name="r", order=1, action="allow"))
    prop = _ir(NacRule(id="r1", name="r", order=1, action="block"))
    res = _run(base, prop)
    assert res.status is Status.WARN and len(res.findings) == 1
    f = res.findings[0]
    assert f.code == "nac.rule.change" and f.severity is Severity.WARNING
    assert f.caused_by[0].ref.id == "r1" and "action" in f.caused_by[0].fields


def test_name_only_change_emits_change():
    base = _ir(NacRule(id="r1", name="old", order=1, action="allow"))
    prop = _ir(NacRule(id="r1", name="new", order=1, action="allow"))
    assert any(f.code == "nac.rule.change" for f in _run(base, prop).findings)


def test_not_matching_change_emits_change():
    base = _ir(NacRule(id="r1", name="r", order=1, action="allow"))
    prop = _ir(NacRule(id="r1", name="r", order=1, action="allow",
                       not_matching=frozenset({("auth_type", "cert")})))
    assert any(f.code == "nac.rule.change" for f in _run(base, prop).findings)


def test_add_and_remove_have_empty_fields():
    base = _ir(NacRule(id="r1", name="r", order=1, action="allow"))
    prop = _ir(NacRule(id="r2", name="r2", order=2, action="allow"))
    fields_by_id = {f.caused_by[0].ref.id: f.caused_by[0].fields for f in _run(base, prop).findings}
    assert fields_by_id["r1"] == () and fields_by_id["r2"] == ()


def test_noop_is_pass():
    base = _ir(NacRule(id="r1", name="r", order=1, action="allow"))
    assert _run(base, base).status is Status.PASS
