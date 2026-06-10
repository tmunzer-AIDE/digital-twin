"""L0: thin structural payload validation against the COMMITTED Mist OAS.

Types, enums, required, and machine-readably encoded conditionals — exactly what
jsonschema can assert from the extracted schemas (OAS-only keywords like
`nullable` are unknown keywords to jsonschema and simply don't constrain).
Deterministic -> every finding is HIGH confidence, source=adapter,
category=operational (a payload Mist would reject is not network breakage).
`fatal` means the run cannot meaningfully continue (payload not an object /
no schema for the type) -> the engine short-circuits to UNKNOWN.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from typing import Any

import jsonschema

from digital_twin.adapters.mist.oas import load_schema
from digital_twin.contracts import Finding, FindingCategory, FindingSource, Severity
from digital_twin.ir import Confidence, ConfidenceLevel

_SCHEMA_FILES: dict[str, str] = {
    "site_setting": "site_setting.schema.json",
    "device": "device_switch.schema.json",
}
_MAX_FINDINGS = 50
_HIGH = Confidence(level=ConfidenceLevel.HIGH)


@dataclass(frozen=True)
class L0Result:
    findings: tuple[Finding, ...]
    fatal: bool  # structurally fatal -> engine short-circuits to UNKNOWN


def _finding(code: str, message: str, path: str = "") -> Finding:
    return Finding(
        source=FindingSource.ADAPTER,
        category=FindingCategory.OPERATIONAL,
        code=code,
        severity=Severity.ERROR,
        confidence=_HIGH,
        message=message,
        evidence={"path": path} if path else {},
    )


@cache
def _validator(object_type: str) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(load_schema(_SCHEMA_FILES[object_type]))


def validate_payload(object_type: str, payload: Mapping[str, Any]) -> L0Result:
    if object_type not in _SCHEMA_FILES:
        return L0Result(
            findings=(
                _finding(
                    "l0.schema.unknown_type", f"no OAS schema for object_type {object_type!r}"
                ),
            ),
            fatal=True,
        )
    if not isinstance(payload, Mapping):
        return L0Result(
            findings=(
                _finding(
                    "l0.schema.not_an_object",
                    "payload must be a JSON object (full-object PUT body)",
                ),
            ),
            fatal=True,
        )
    findings = tuple(
        _finding(
            "l0.schema.violation",
            err.message,
            path=".".join(str(p) for p in err.absolute_path),
        )
        for _, err in zip(
            range(_MAX_FINDINGS), _validator(object_type).iter_errors(dict(payload)), strict=False
        )
    )
    return L0Result(findings=findings, fatal=False)
