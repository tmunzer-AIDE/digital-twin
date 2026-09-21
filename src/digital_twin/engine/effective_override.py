"""Report lower-layer edits masked by higher-precedence device configuration.

This is not an intent parser.  It compares the compiled lower-layer artifact with
each device's final effective artifact.  A path changed by a template/site edit but
unchanged on a device is provably overridden on that device.

The same comparison also covers device profiles once that layer is present in the
compiler.  Until then, the existing device-profile coverage gate remains the honest
UNKNOWN rail for profiled devices.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from digital_twin.checks.base import CheckResult, Coverage, CoverageState, Status
from digital_twin.contracts import (
    Finding,
    FindingCategory,
    FindingSource,
    ObjectRef,
    Severity,
)
from digital_twin.ir import Confidence, ConfidenceLevel
from digital_twin.scope.allowlist import EFFECTIVE_ALLOWLIST
from digital_twin.scope.paths import allowed, changed_leaf_paths

JsonObj = Mapping[str, Any]
_HIGH = Confidence(level=ConfidenceLevel.HIGH)


def effective_override_result(
    *,
    site_id: str,
    baseline_lower: JsonObj,
    proposed_lower: JsonObj,
    baseline_devices: Mapping[str, JsonObj],
    proposed_devices: Mapping[str, JsonObj],
) -> CheckResult | None:
    """Return override findings for changed lower-layer paths, if any.

    Devices absent from either side are excluded: creation/deletion is not an
    inheritance comparison.  Exact leaf paths are used so keyed-map overrides do
    not incorrectly mask their unaffected siblings.
    """
    lower_changed = tuple(
        path
        for path in changed_leaf_paths(baseline_lower, proposed_lower)
        if allowed(path, EFFECTIVE_ALLOWLIST)
    )
    device_ids = tuple(sorted(set(baseline_devices) & set(proposed_devices)))
    if not lower_changed or not device_ids:
        return None

    final_changed = {
        device_id: frozenset(
            changed_leaf_paths(
                baseline_devices[device_id], proposed_devices[device_id]
            )
        )
        for device_id in device_ids
    }
    fully: dict[str, tuple[str, ...]] = {}
    partially: dict[str, tuple[str, ...]] = {}
    partial_applied: dict[str, tuple[str, ...]] = {}
    for path in lower_changed:
        overridden = tuple(
            device_id for device_id in device_ids if path not in final_changed[device_id]
        )
        if not overridden:
            continue
        applied = tuple(device_id for device_id in device_ids if device_id not in overridden)
        if not applied:
            fully[path] = overridden
        else:
            partially[path] = overridden
            partial_applied[path] = applied

    findings: list[Finding] = []
    if fully:
        affected = tuple(sorted({device for devices in fully.values() for device in devices}))
        findings.append(Finding(
            source=FindingSource.CHECK,
            category=FindingCategory.OPERATIONAL,
            code="scope.effective_noop.fully_overridden",
            severity=Severity.WARNING,
            confidence=_HIGH,
            message=(
                "the proposed lower-layer change is overridden on every affected "
                "device for: " + ", ".join(sorted(fully))
            ),
            affected_entities=affected,
            subject=ObjectRef("site", site_id),
            evidence={
                "paths": sorted(fully),
                "overridden_devices_by_path": {
                    path: list(devices) for path, devices in sorted(fully.items())
                },
            },
        ))
    if partially:
        affected = tuple(
            sorted({device for devices in partially.values() for device in devices})
        )
        findings.append(Finding(
            source=FindingSource.CHECK,
            category=FindingCategory.OPERATIONAL,
            code="scope.effective_noop.partially_overridden",
            severity=Severity.WARNING,
            confidence=_HIGH,
            message=(
                "the proposed lower-layer change is overridden on some affected "
                "devices for: " + ", ".join(sorted(partially))
            ),
            affected_entities=affected,
            subject=ObjectRef("site", site_id),
            evidence={
                "paths": sorted(partially),
                "overridden_devices_by_path": {
                    path: list(devices) for path, devices in sorted(partially.items())
                },
                "applied_devices_by_path": {
                    path: list(partial_applied[path]) for path in sorted(partially)
                },
            },
        ))
    if not findings:
        return None
    return CheckResult(
        check_id="scope.effective_noop",
        status=Status.WARN,
        findings=tuple(findings),
        coverage=Coverage(CoverageState.COMPLETE),
        confidence=_HIGH,
        reasoning=(
            f"{len(fully)} fully overridden and {len(partially)} partially "
            "overridden effective path(s)"
        ),
    )
