import dataclasses

import pytest

from digital_twin.contracts import Finding, FindingCategory, FindingSource, Severity
from digital_twin.ir import Confidence, ConfidenceLevel


def test_finding_constructs_with_spec_fields():
    f = Finding(
        source=FindingSource.ADAPTER,
        category=FindingCategory.OPERATIONAL,
        code="l0.schema.type",
        severity=Severity.ERROR,
        confidence=Confidence(level=ConfidenceLevel.HIGH),
        message="networks must be an object",
        evidence={"path": "networks"},
    )
    assert f.code == "l0.schema.type"
    assert f.affected_entities == ()  # default
    assert f.remediation is None  # default


def test_finding_is_frozen():
    f = Finding(
        source=FindingSource.CHECK,
        category=FindingCategory.NETWORK,
        code="x",
        severity=Severity.INFO,
        confidence=Confidence(level=ConfidenceLevel.LOW),
        message="m",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        f.code = "y"  # type: ignore[misc]


def test_severity_values_match_spec():
    assert [s.value for s in Severity] == ["info", "warning", "error", "critical"]
