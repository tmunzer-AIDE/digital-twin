"""Golden test: 13 real TM-LAB org NAC rules (redacted representative fixture).

tmlab_nacrules.json is a redacted representative fixture modeled on the real TM-LAB org
shape — real order/name/action/enabled; representative matching fields; all UUIDs replaced
with stable synthetic ids (r0..r12 for rules, t0..t10 for tag references).
"""
import json
from pathlib import Path

from digital_twin.adapters.mist.ingest.nac import build_nac_ir
from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext
from digital_twin.checks.nac.shadowing import NacShadowingCheck, is_provable
from digital_twin.ir import diff_ir

_RULES = json.loads((Path(__file__).parent / "fixtures" / "tmlab_nacrules.json").read_text())


def _ir(rules):
    ir, _ = build_nac_ir(rules, [])
    return ir


def test_real_rules_have_no_introduced_shadows_when_unchanged():
    ir = _ir(_RULES)
    res = NacShadowingCheck().run(CheckContext(
        baseline=AnalysisContext(ir), proposed=AnalysisContext(ir), diff=diff_ir(ir, ir)))
    # baseline == proposed → no diff → applies_to False path is exercised in the pipeline;
    # here we assert the check itself produces zero INTRODUCED findings (only pre-existing)
    assert not any(f.code.endswith("introduced") for f in res.findings)


def test_perturbation_earliest_catch_all_buries_every_provable_rule():
    # the promised perturbed case: prepend an earliest catch-all (no criteria). Under
    # conservative coverage a catch-all covers everything, so the introduced set is
    # exactly the enabled+provable real rules — a fixture-content-independent hand-check.
    catch_all = {"id": "z-catchall", "name": "z", "order": -1, "enabled": True,
                 "action": "allow", "matching": {}, "apply_tags": []}
    base = _ir(_RULES)
    prop = _ir([catch_all, *_RULES])
    res = NacShadowingCheck().run(CheckContext(
        baseline=AnalysisContext(base), proposed=AnalysisContext(prop),
        diff=diff_ir(base, prop)))
    introduced = {f.subject.id for f in res.findings if f.code.endswith("introduced")}
    expected = {r.id for r in prop.nacrules
                if r.id != "z-catchall" and r.enabled and is_provable(r)}
    assert expected and introduced == expected
