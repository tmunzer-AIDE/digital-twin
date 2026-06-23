from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.nac.shadowing import NacShadowingCheck
from digital_twin.contracts import Severity
from digital_twin.ir import IRBuilder, NacRule, diff_ir


def _ir(*rules):
    b = IRBuilder()
    for r in rules:
        b.add_nacrule(r)
    return b.build()


def _ctx(base, prop):
    return CheckContext(baseline=AnalysisContext(base), proposed=AnalysisContext(prop),
                        diff=diff_ir(base, prop))


def _run(base, prop):
    return NacShadowingCheck().run(_ctx(base, prop))


def test_introduced_shadow_by_reorder_warns():
    a = NacRule(id="a", order=1, enabled=True, action="allow")          # catch-all
    b = NacRule(id="b", order=2, enabled=True, action="allow",
                auth_types=frozenset({"cert"}))
    base = _ir(NacRule(id="a", order=3, enabled=True, action="allow"), b)  # a was LATER
    prop = _ir(a, b)                                                       # a now earlier
    res = _run(base, prop)
    f = next(f for f in res.findings if f.code.endswith("introduced"))
    assert f.severity is Severity.WARNING and f.subject.id == "b"
    assert f.evidence["shadower"]["id"] == "a"
    assert res.status is Status.WARN


def test_enabled_flip_is_a_cause():
    base = _ir(NacRule(id="a", order=1, enabled=False, action="allow"),
               NacRule(id="b", order=2, enabled=True, action="allow"))
    prop = _ir(NacRule(id="a", order=1, enabled=True, action="allow"),
               NacRule(id="b", order=2, enabled=True, action="allow"))
    res = _run(base, prop)
    f = next(f for f in res.findings if f.code.endswith("introduced"))
    assert f.subject.id == "b"
    cause_a = next(c for c in f.caused_by if c.ref.id == "a")
    assert "enabled" in cause_a.fields            # the field that introduced the shadow


def test_preexisting_is_info():
    base = _ir(NacRule(id="a", order=1, enabled=True, action="allow"),
               NacRule(id="b", order=2, enabled=True, action="block"))
    prop = _ir(NacRule(id="a", order=1, enabled=True, action="allow"),
               NacRule(id="b", order=2, enabled=True, action="allow"))  # b.action changed
    res = _run(base, prop)
    assert any(f.code.endswith("preexisting") and f.severity is Severity.INFO
               for f in res.findings)


def test_indeterminate_baseline_is_suppressed():
    # b shadowed in proposed; in baseline a is opaque → indeterminate → no finding for b
    base = _ir(NacRule(id="a", order=1, enabled=True, opaque_digest="x"),
               NacRule(id="b", order=2, enabled=True, action="allow"))
    prop = _ir(NacRule(id="a", order=1, enabled=True, action="allow"),
               NacRule(id="b", order=2, enabled=True, action="allow"))
    res = _run(base, prop)
    assert not any(f.subject and f.subject.id == "b" for f in res.findings)
