"""Per-domain coverage rollup: domain -> counts of check coverage states."""

from __future__ import annotations

from dataclasses import dataclass

from digital_twin.checks.base import CheckResult


@dataclass(frozen=True)
class DomainCoverage:
    complete: int = 0
    partial: int = 0
    insufficient: int = 0
    not_applicable: int = 0


def rollup(results: tuple[CheckResult, ...], domains: dict[str, str]) -> dict[str, DomainCoverage]:
    """domains: check_id -> domain (from the registered checks)."""
    counts: dict[str, dict[str, int]] = {}
    for res in results:
        domain = domains.get(res.check_id, "unknown")
        c = counts.setdefault(
            domain, {"complete": 0, "partial": 0, "insufficient": 0, "not_applicable": 0}
        )
        c[res.coverage.state.value] += 1
    return {d: DomainCoverage(**c) for d, c in counts.items()}
