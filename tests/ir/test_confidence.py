from dataclasses import FrozenInstanceError

import pytest

from digital_twin.ir.confidence import Confidence, ConfidenceLevel, min_confidence


def test_levels_are_ordered():
    assert ConfidenceLevel.LOW < ConfidenceLevel.MEDIUM < ConfidenceLevel.HIGH


def test_single_confidence_returned_as_is():
    c = Confidence(ConfidenceLevel.HIGH, ("two-sided LLDP",))
    assert min_confidence(c) == c


def test_min_picks_lowest_and_keeps_lowest_reasons():
    high = Confidence(ConfidenceLevel.HIGH, ("configured",))
    low_a = Confidence(ConfidenceLevel.LOW, ("one-sided LLDP",))
    low_b = Confidence(ConfidenceLevel.LOW, ("uncorroborated",))
    result = min_confidence(high, low_a, low_b)
    assert result.level is ConfidenceLevel.LOW
    assert result.reasons == ("one-sided LLDP", "uncorroborated")
    assert "configured" not in result.reasons


def test_min_requires_at_least_one_argument():
    with pytest.raises(ValueError):
        min_confidence()


def test_confidence_is_frozen():
    c = Confidence(ConfidenceLevel.MEDIUM)
    with pytest.raises(FrozenInstanceError):
        c.level = ConfidenceLevel.HIGH  # type: ignore[misc]
