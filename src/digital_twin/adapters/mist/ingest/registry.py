"""Run registered domain ingesters in order; collect the capabilities EARNED.

A crashing ingester is ISOLATED into a value (IngestFailure) — per the spec's
component contract, a domain ingester failure becomes a named UNKNOWN (mapped
by the Plan-3+ pipeline), never an unhandled exception. A failed ingester
contributes no capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass

from digital_twin.ir import Capability

from .base import IngestContext, Ingester


@dataclass(frozen=True)
class IngestFailure:
    ingester: str
    error: str


@dataclass(frozen=True)
class IngestReport:
    """Outcome of an ingest run.

    CONTRACT: when ``ok`` is False, the builder/IR is DIAGNOSTIC-ONLY — the
    failed ingester may have left partial mutations behind (no rollback is
    attempted). The pipeline must map a non-ok report to decision UNKNOWN
    (named per failed ingester) and must NOT run checks against that IR.
    """

    produced: frozenset[Capability]
    failures: tuple[IngestFailure, ...]

    @property
    def ok(self) -> bool:
        return not self.failures


class IngesterRegistry:
    def __init__(self, ingesters: list[Ingester]) -> None:
        self._ingesters = list(ingesters)

    def potential_supply(self) -> frozenset[Capability]:
        """Union of declared produces() — for capability_check wiring validation."""
        out: set[Capability] = set()
        for ingester in self._ingesters:
            out |= ingester.produces()
        return frozenset(out)

    def run(self, ctx: IngestContext) -> IngestReport:
        produced: set[Capability] = set()
        failures: list[IngestFailure] = []
        for ingester in self._ingesters:
            try:
                produced |= ingester.ingest(ctx)  # earned, not declared
            except Exception as e:  # noqa: BLE001 — isolated into a value (spec contract)
                failures.append(IngestFailure(ingester=ingester.name, error=str(e)))
        for cap in produced:
            ctx.builder.with_capability(cap)
        return IngestReport(produced=frozenset(produced), failures=tuple(failures))
