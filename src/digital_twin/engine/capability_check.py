"""Capability supply/demand validation (the silent-blind-spot killer).

Every required capability must have a producer, or be explicitly declared
not-yet-supported. Run as a test/startup assertion so a consumer added without
its producer fails LOUDLY instead of returning INSUFFICIENT_DATA forever.
Consumers (checks/analyzers) arrive in Plan 4 and feed `required`.
"""

from __future__ import annotations

from digital_twin.ir import Capability


class CapabilityGapError(RuntimeError):
    pass


def validate_supply(
    produced: frozenset[Capability],
    required: frozenset[Capability],
    not_yet_supported: frozenset[Capability] = frozenset(),
) -> None:
    missing = required - produced - not_yet_supported
    if missing:
        raise CapabilityGapError(
            "required capabilities with no producer: " + ", ".join(sorted(missing))
        )
