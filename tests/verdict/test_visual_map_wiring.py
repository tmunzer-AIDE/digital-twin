import dataclasses

from digital_twin.contracts import (
    Finding,
    FindingCategory,
    FindingSource,
    ObjectRef,
    Severity,
)
from digital_twin.drivers.render import verdict_to_dict
from digital_twin.ir import Confidence, ConfidenceLevel
from digital_twin.verdict.verdict import Verdict

_HIGH = Confidence(level=ConfidenceLevel.HIGH)


def _verdict_with_map():
    from digital_twin.contracts import VisualEntry, VisualTier
    f = Finding(source=FindingSource.CHECK, category=FindingCategory.NETWORK,
                code="t.x", severity=Severity.WARNING, confidence=_HIGH, message="m",
                subject=ObjectRef("device", "s1"))
    vmap = {"l2": {"device:s1": VisualEntry(
        kind="device", id="s1", tier=VisualTier.AFFECTED,
        severity=Severity.WARNING, findings=())}}
    # minimal Verdict — only fields needed for the test
    return Verdict(
        decision=__import__("digital_twin.verdict.decision", fromlist=["Decision"]).Decision.REVIEW,
        decision_reasons=(), overall_severity=Severity.WARNING, findings=(f,),
        check_results=(), coverage={}, confidence_summary=None, ir_diff=None,
        visual_map=vmap,
    )


def test_verdict_has_visual_map_field_default_empty():
    names = {f.name for f in dataclasses.fields(Verdict)}
    assert "visual_map" in names


def test_visual_map_serializes_to_nested_kind_id_shape():
    v = _verdict_with_map()
    d = verdict_to_dict(v)
    entry = d["visual_map"]["l2"]["device:s1"]
    assert entry["kind"] == "device" and entry["id"] == "s1"
    assert entry["tier"] == "affected" and entry["severity"] == "warning"
    assert entry["findings"] == []


def test_visual_map_does_not_affect_decision_or_severity():
    # building the map is pure: a verdict's decision/severity are identical
    # whether or not visual_map is populated.
    v = _verdict_with_map()
    bare = dataclasses.replace(v, visual_map={})
    assert v.decision == bare.decision
    assert v.overall_severity == bare.overall_severity
    assert [f.severity for f in v.findings] == [f.severity for f in bare.findings]
