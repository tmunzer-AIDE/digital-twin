import pytest

from digital_twin.engine.capability_check import CapabilityGapError, validate_supply


def test_ok_when_all_requirements_have_producers():
    validate_supply(produced=frozenset({"wired.l2", "stp.state"}), required=frozenset({"wired.l2"}))


def test_missing_producer_raises_with_names():
    with pytest.raises(CapabilityGapError) as e:
        validate_supply(
            produced=frozenset({"wired.l2"}),
            required=frozenset({"wired.l2", "analysis.reachability"}),
        )
    assert "analysis.reachability" in str(e.value)


def test_explicit_not_yet_supported_is_allowed():
    validate_supply(
        produced=frozenset(),
        required=frozenset({"wan.tunnels"}),
        not_yet_supported=frozenset({"wan.tunnels"}),
    )
