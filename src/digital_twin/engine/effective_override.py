"""Report lower-layer changes absent from final compiled switch configuration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from digital_twin.checks.base import CheckResult, Coverage, CoverageState, Status
from digital_twin.contracts import Finding, FindingCategory, FindingSource, ObjectRef, Severity
from digital_twin.ir import Confidence, ConfidenceLevel
from digital_twin.scope.allowlist import EFFECTIVE_ALLOWLIST
from digital_twin.scope.paths import allowed_tokens, leaf_changes

_HIGH = Confidence(level=ConfidenceLevel.HIGH)


def _overlaps(path: tuple[str, ...], other: tuple[str, ...]) -> bool:
    # A scalar/object replacement may diff at a parent rather than its old leaf.
    return path[:len(other)] == other or other[:len(path)] == path


def effective_override_result(
    *,
    site_id: str,
    baseline_lower: Mapping[str, Any],
    proposed_lower: Mapping[str, Any],
    baseline_devices: Mapping[str, Mapping[str, Any]],
    proposed_devices: Mapping[str, Mapping[str, Any]],
) -> CheckResult | None:
    lower_changed = [
        delta
        for delta in leaf_changes(baseline_lower, proposed_lower)
        if allowed_tokens(delta.tokens, EFFECTIVE_ALLOWLIST) and delta.tokens[0] != "vars"
    ]
    devices = sorted(baseline_devices.keys() & proposed_devices.keys())
    if not lower_changed or not devices:
        return None
    changes = {
        did: leaf_changes(baseline_devices[did], proposed_devices[did]) for did in devices
    }
    fully: dict[str, list[str]] = {}
    partially: dict[str, list[str]] = {}
    applied: dict[str, list[str]] = {}
    for delta in lower_changed:
        path = delta.path
        masked = [did for did in devices if not any(
            _overlaps(delta.tokens, p.tokens) for p in changes[did]
        )]
        if not masked:
            continue
        if len(masked) == len(devices):
            fully[path] = masked
        else:
            partially[path] = masked
            applied[path] = [did for did in devices if did not in masked]
    findings = []
    for label, rows in (("fully", fully), ("partially", partially)):
        if not rows:
            continue
        findings.append(
            Finding(
                source=FindingSource.CHECK,
                category=FindingCategory.OPERATIONAL,
                code=f"scope.effective_noop.{label}_overridden",
                severity=Severity.WARNING,
                confidence=_HIGH,
                message="lower-layer changes leave compiled device values unchanged for: "
                + ", ".join(rows),
                subject=ObjectRef("site_setting", site_id),
                affected_entities=tuple(sorted({did for ids in rows.values() for did in ids})),
                evidence={
                    "paths": sorted(rows),
                    "overridden_devices_by_path": rows,
                    "applied_devices_by_path": {p: applied[p] for p in rows if p in applied},
                },
            )
        )
    if not findings:
        return None
    return CheckResult(
        check_id="scope.effective_noop",
        status=Status.WARN,
        findings=tuple(findings),
        coverage=Coverage(CoverageState.COMPLETE),
        confidence=_HIGH,
        reasoning="compared changed lower-layer leaves with final compiled per-device changes",
    )
