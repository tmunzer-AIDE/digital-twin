"""L0: thin structural payload validation against the COMMITTED Mist OAS.

Types, enums, required, and machine-readably encoded conditionals — exactly what
jsonschema can assert from the extracted schemas. OAS 3.0 `nullable: true` is
NOT a JSON-Schema keyword (jsonschema would ignore it and then `type: string`
would falsely reject an explicit null), so schemas are normalized first:
nullable -> type list with "null" (+ None appended to any enum).
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

# Violations on secret-bearing keys are SUPPRESSED: the twin never stores or
# simulates secrets (replay fixtures strip them by design — see the redaction
# manifest in observability/replay/redaction.py, kept in sync), and Mist's own
# API still validates real payloads at apply time.
_SECRET_KEY_PARTS: tuple[str, ...] = (
    "psk",
    "password",
    "passphrase",
    "secret",
    "token",
    "community",
    "private_key",
    "cert",
)


def _without_nulls(obj: Any) -> Any:
    """null == absent (the project-wide canon: Mist GETs return null for unset
    optional fields) — strip None-valued keys deeply before schema validation,
    since the EFFECTIVE object inherits them from the current state."""
    if isinstance(obj, dict):
        return {k: _without_nulls(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_without_nulls(v) for v in obj]
    return obj


def _touches_secret(err: jsonschema.ValidationError) -> bool:
    path_keys = [str(p).lower() for p in err.absolute_path]
    blob = " ".join((*path_keys, err.message.lower()))
    return any(part in blob for part in _SECRET_KEY_PARTS)


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


def _absorb_nullable(node: Any) -> None:
    """In place: OAS `nullable: true` -> JSON-Schema `type: [..., "null"]` (+ null
    in any enum) — recursively. load_schema returns a fresh object each call, so
    mutating here never touches shared state."""
    if isinstance(node, dict):
        if node.get("nullable") is True:
            t = node.get("type")
            if isinstance(t, str):
                node["type"] = [t, "null"]
            elif isinstance(t, list) and "null" not in t:
                node["type"] = [*t, "null"]
            enum = node.get("enum")
            if isinstance(enum, list) and None not in enum:
                node["enum"] = [*enum, None]
        for value in node.values():
            _absorb_nullable(value)
    elif isinstance(node, list):
        for item in node:
            _absorb_nullable(item)


@cache
def _validator(object_type: str) -> jsonschema.Draft202012Validator:
    schema = load_schema(_SCHEMA_FILES[object_type])
    _absorb_nullable(schema)
    return jsonschema.Draft202012Validator(schema)


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
    errors = (
        err
        for err in _validator(object_type).iter_errors(_without_nulls(dict(payload)))
        if not _touches_secret(err)
    )
    findings = tuple(
        _finding(
            "l0.schema.violation",
            err.message,
            path=".".join(str(p) for p in err.absolute_path),
        )
        for _, err in zip(range(_MAX_FINDINGS), errors, strict=False)
    )
    return L0Result(findings=findings, fatal=False)
