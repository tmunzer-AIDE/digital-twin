"""Confidence rollup across every finding (counts + the LOW/MEDIUM reasons)."""

from __future__ import annotations

from dataclasses import dataclass

from digital_twin.contracts import Finding
from digital_twin.ir import ConfidenceLevel


@dataclass(frozen=True)
class ConfidenceSummary:
    high: int
    medium: int
    low: int
    reasons: tuple[str, ...]  # why anything is below HIGH


def summarize(findings: tuple[Finding, ...]) -> ConfidenceSummary:
    high = sum(1 for f in findings if f.confidence.level is ConfidenceLevel.HIGH)
    medium = sum(1 for f in findings if f.confidence.level is ConfidenceLevel.MEDIUM)
    low = sum(1 for f in findings if f.confidence.level is ConfidenceLevel.LOW)
    reasons = tuple(
        r
        for f in findings
        if f.confidence.level is not ConfidenceLevel.HIGH
        for r in f.confidence.reasons
    )
    return ConfidenceSummary(high=high, medium=medium, low=low, reasons=reasons)
