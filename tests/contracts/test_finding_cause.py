from digital_twin.contracts import (
    Cause,
    Finding,
    FindingCategory,
    FindingSource,
    ObjectRef,
    Severity,
)
from digital_twin.ir import Confidence, ConfidenceLevel


def _f(**kw):
    base = dict(
        source=FindingSource.CHECK, category=FindingCategory.NETWORK, code="x",
        severity=Severity.WARNING, confidence=Confidence(level=ConfidenceLevel.HIGH), message="m",
    )
    base.update(kw)
    return Finding(**base)


def test_caused_by_defaults_empty():
    assert _f().caused_by == ()


def test_cause_carries_ref_and_fields():
    c = Cause(ref=ObjectRef("port", "p1"), fields=("native_vlan",))
    f = _f(caused_by=(c,))
    assert f.caused_by[0].ref.id == "p1"
    assert f.caused_by[0].fields == ("native_vlan",)


def test_cause_fields_default_empty():
    assert Cause(ref=ObjectRef("link", "l1")).fields == ()
