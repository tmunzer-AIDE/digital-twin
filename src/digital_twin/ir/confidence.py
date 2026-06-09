"""Confidence: categorical (HIGH/MEDIUM/LOW) + reasons, with MIN composition.

A derived fact's confidence is the lowest level among the facts it relied on. Reasons
explaining the floor accumulate from the lowest-level inputs. Never a float.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class ConfidenceLevel(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3


@dataclass(frozen=True)
class Confidence:
    level: ConfidenceLevel
    reasons: tuple[str, ...] = ()


def min_confidence(*confidences: Confidence) -> Confidence:
    if not confidences:
        raise ValueError("min_confidence requires at least one Confidence")
    lowest = min(c.level for c in confidences)
    reasons: tuple[str, ...] = ()
    for c in confidences:
        if c.level == lowest:
            reasons += c.reasons
    return Confidence(level=lowest, reasons=reasons)
