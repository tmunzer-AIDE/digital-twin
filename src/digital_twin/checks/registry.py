"""Run checks under the spec's STRICT gating order, with crash isolation.

Per check: (1) applies_to(diff) -> NOT_APPLICABLE and stop (checked FIRST, so a
cosmetic change is never INSUFFICIENT_DATA); (2) requires() vs the IRs'
capabilities (INTERSECTION of baseline+proposed — a comparison needs facts on
both sides) -> INSUFFICIENT_DATA; (3) run, exceptions isolated to CHECK_ERROR +
an OPERATIONAL finding (a crash is never network breakage -> REVIEW, not UNSAFE).
"""

from __future__ import annotations

from digital_twin.contracts import Finding, FindingCategory, FindingSource, Severity
from digital_twin.ir import Capability, Confidence, ConfidenceLevel

from .base import Check, CheckContext, CheckResult, Coverage, CoverageState, Status


class CheckRegistry:
    def __init__(self, checks: list[Check]) -> None:
        self._checks = list(checks)

    def run_all(self, ctx: CheckContext) -> tuple[CheckResult, ...]:
        capabilities = ctx.baseline.capabilities & ctx.proposed.capabilities
        return tuple(self._run_one(check, ctx, capabilities) for check in self._checks)

    def _run_one(
        self, check: Check, ctx: CheckContext, capabilities: frozenset[Capability]
    ) -> CheckResult:
        if not check.applies_to(ctx.diff):
            return CheckResult(
                check_id=check.id,
                status=Status.NOT_APPLICABLE,
                findings=(),
                coverage=Coverage(state=CoverageState.NOT_APPLICABLE),
                confidence=None,
                reasoning="delta does not touch this check's domain",
            )
        missing = check.requires() - capabilities
        if missing:
            return CheckResult(
                check_id=check.id,
                status=Status.INSUFFICIENT_DATA,
                findings=(),
                coverage=Coverage(
                    state=CoverageState.INSUFFICIENT,
                    notes=tuple(f"missing capability: {m}" for m in sorted(missing)),
                ),
                confidence=None,
                reasoning=f"applicable but lacking capabilities: {sorted(missing)}",
            )
        try:
            return check.run(ctx)
        except Exception as e:  # noqa: BLE001 — isolated per the spec's component contract
            return CheckResult(
                check_id=check.id,
                status=Status.CHECK_ERROR,
                findings=(
                    Finding(
                        source=FindingSource.CHECK,
                        category=FindingCategory.OPERATIONAL,
                        code=f"{check.id}.check_error",
                        severity=Severity.ERROR,
                        confidence=Confidence(level=ConfidenceLevel.HIGH),
                        message=f"check {check.id} crashed; result unavailable",
                        evidence={"error": str(e)},
                    ),
                ),
                coverage=Coverage(state=CoverageState.INSUFFICIENT, notes=("check crashed",)),
                confidence=None,
                reasoning=f"crashed: {e}",
            )
