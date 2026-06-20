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

from collections.abc import Collection, Mapping
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
    "networktemplate": "networktemplate.schema.json",
    "gatewaytemplate": "gatewaytemplate.schema.json",
    # NOTE: the Mist OAS `site_template` component is thin (auto_upgrade/name/vars
    # only) and does NOT yet document the switch-config surface a real sitetemplate
    # carries (confirmed with the domain owner; OAS fix is upstream). The committed
    # schema has no `additionalProperties: false`, so L0 stays PERMISSIVE for those
    # fields (no false-reject) — the field gate + compile + checks still cover them.
    "sitetemplate": "sitetemplate.schema.json",
    # thin/permissive WLAN schema: types the modeled lint leaves so a `wlan` op
    # L0-validates instead of fatal-rejecting; scoped L0 (changed roots) means a
    # partial WLAN update only validates the touched root.
    "wlan": "wlan.schema.json",
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


def _in_scope(err: jsonschema.ValidationError, scope_roots: Collection[str] | None) -> bool:
    """When `scope_roots` is given, report only violations on those top-level
    roots (plus object-level violations with an EMPTY path, e.g. a root
    `required`, which aren't tied to any one root). Mist's PUT is a root-level
    merge: roots OMITTED from the change persist unchanged and Mist does not
    re-validate them — so a stale committed-OAS type on an untouched root must
    not surface as a violation of THIS change. `scope_roots=None` validates the
    whole object (the opt-in legacy mode)."""
    if scope_roots is None:
        return True
    path = err.absolute_path
    return not path or str(path[0]) in scope_roots


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


def validate_payload(
    object_type: str,
    payload: Mapping[str, Any],
    *,
    scope_roots: Collection[str] | None = None,
) -> L0Result:
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
        if not _touches_secret(err) and _in_scope(err, scope_roots)
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
