"""L0 sub-check: flag payload attributes not documented in the committed Mist OAS.

This is the SOLE place "not in the OAS" becomes a finding (WARNING -> REVIEW). It
reads the FAITHFUL schema (the jsonschema validator strips `additionalProperties:
false` to stay permissive; this walker keeps it, so closedness is owned here).

A documented object (has `properties`, no explicit-open `additionalProperties`) is
treated as a CLOSED set: any present, non-null key not in `properties` is reported.
Map nodes (`additionalProperties` is a schema) and explicitly-open nodes
(`additionalProperties: true`) are not flagged; we recurse into them. An object
node with NO `properties` and no `additionalProperties` is UNDOCUMENTED and is not
judged — but an explicit `additionalProperties: false` allows NO keys, so it flags
every extra key even with no `properties`. Composition is resolved conservatively
(`anyOf`/`oneOf` most permissive, `allOf` most restrictive); duplicate property/map
sub-schemas across branches are COMPOSED, not overwritten, so a nested key
documented in any branch is accepted.

Server-managed / GET-only top-level roots (`IGNORED_RAW_FIELDS`: device status,
map placement, inventory) are skipped at the ROOT only — never per-segment, so a
nested unknown like `networks.foo.id` still surfaces. Secret-bearing paths are
never surfaced. Input is expected null-stripped by the caller; the walk also skips
None defensively.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from digital_twin.contracts import Finding, FindingCategory, FindingSource, Severity
from digital_twin.ir import Confidence, ConfidenceLevel
from digital_twin.redaction import STRIP_KEY_PARTS
from digital_twin.scope.allowlist import IGNORED_RAW_FIELDS

# Object types NOT enforced (their committed OAS is too thin, or they are deferred
# pending OAS/allowlist reconciliation). The single SCOPE LEVER — for device-only
# v1 this also lists networktemplate / site_setting / gatewaytemplate.
OAS_UNKNOWN_KEY_SKIP: frozenset[str] = frozenset(
    {"wlan", "nacrule", "sitetemplate", "networktemplate", "site_setting", "gatewaytemplate"}
)

_MAX_FINDINGS = 50  # same cap as schema.py L0 violations — don't flood the verdict
_HIGH = Confidence(level=ConfidenceLevel.HIGH)
# Node states, valued by PERMISSIVENESS (higher = allows more unknown keys), so
# allOf takes the min (most restrictive) and anyOf/oneOf the max (most permissive).
# Order OPEN > MAP > ABSENT > CLOSED matches the anyOf rule: a MAP branch wins over
# a plain/absent branch, so the dynamic keys it allows are not flagged. _ABSENT (no
# `additionalProperties` keyword) is distinct from _CLOSED (explicit
# `additionalProperties: false`): _ABSENT+no-properties is UNDOCUMENTED (skip), but
# _CLOSED allows NO keys, so it flags even with no properties.
_OPEN, _MAP, _ABSENT, _CLOSED = 3, 2, 1, 0


def _self_state(schema: Mapping[str, Any]) -> tuple[int, Mapping[str, Any] | None]:
    if "additionalProperties" not in schema:
        return _ABSENT, None
    ap = schema["additionalProperties"]
    if ap is True:
        return _OPEN, None
    if isinstance(ap, Mapping):
        return _MAP, ap
    return _CLOSED, None  # explicit False (or non-true/non-dict) -> no keys allowed


def _merge_props(into: dict[str, Any], branch: Mapping[str, Any], combinator: str) -> None:
    """Compose duplicate property sub-schemas instead of overwriting, so recursion
    sees EVERY branch's view of a shared key (conservative union -> no false flag)."""
    for k, v in branch.items():
        into[k] = {combinator: [into[k], v]} if k in into else v


def _norm_node(
    schema: Mapping[str, Any],
) -> tuple[dict[str, Any], int, Mapping[str, Any] | None]:
    """Resolve an object node (incl. allOf/anyOf/oneOf) to (props, state, map_schema).
    Duplicate property/map sub-schemas across branches are COMPOSED, not overwritten:
    `anyOf` for union branches, `allOf` for intersection branches — so a nested key
    documented in ANY branch is accepted. (NOTE: an `allOf` whose branches set their
    OWN `additionalProperties` is only approximated; Task 1's refresh gate FAILS if a
    refreshed schema introduces that shape, so it cannot land silently.)"""
    props: dict[str, Any] = dict(schema.get("properties") or {})
    state, map_schema = _self_state(schema)

    for branch in schema.get("allOf") or []:
        if isinstance(branch, Mapping):
            b_props, b_state, b_map = _norm_node(branch)
            _merge_props(props, b_props, "allOf")
            if b_state < state:  # intersection: most restrictive
                state, map_schema = b_state, b_map
            elif b_state == _MAP and state == _MAP:  # both map constraints apply
                map_schema = {"allOf": [map_schema, b_map]}

    for key in ("anyOf", "oneOf"):
        for branch in schema.get(key) or []:
            if isinstance(branch, Mapping):
                b_props, b_state, b_map = _norm_node(branch)
                _merge_props(props, b_props, "anyOf")
                if b_state > state:  # union: most permissive (avoid false positives)
                    state, map_schema = b_state, b_map
                elif b_state == _MAP and state == _MAP:  # union of map value-schemas
                    map_schema = {"anyOf": [map_schema, b_map]}

    return props, state, map_schema


def _is_secret_path(path: str) -> bool:
    return any(part in seg for seg in path.lower().split(".") for part in STRIP_KEY_PARTS)


def _finding(path: str, object_type: str) -> Finding:
    return Finding(
        source=FindingSource.ADAPTER,
        category=FindingCategory.OPERATIONAL,
        code="l0.schema.unknown_attribute",
        severity=Severity.WARNING,
        confidence=_HIGH,
        message=f"attribute {path!r} is not documented in the {object_type} OAS schema",
        evidence={"path": path, "object_type": object_type},
    )


def _descend(
    value: Any, schema: Mapping[str, Any], path: str, object_type: str, out: list[Finding]
) -> None:
    if len(out) >= _MAX_FINDINGS:
        return
    if isinstance(value, Mapping):
        _walk(value, schema, path, object_type, out)
    elif isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, Mapping):
            for i, elem in enumerate(value):
                _descend(elem, items, f"{path}.{i}", object_type, out)


def _walk(
    payload: Mapping[str, Any],
    schema: Mapping[str, Any],
    path: str,
    object_type: str,
    out: list[Finding],
) -> None:
    props, state, map_schema = _norm_node(schema)
    for key, value in payload.items():
        if len(out) >= _MAX_FINDINGS:
            return
        if value is None:  # null == absent
            continue
        child = f"{path}.{key}" if path else key
        if key in props:
            _descend(value, props[key], child, object_type, out)
        elif state == _OPEN:
            continue
        elif state == _MAP:
            if map_schema is not None:
                _descend(value, map_schema, child, object_type, out)
        elif state == _ABSENT and not props:
            continue  # undocumented node (no `additionalProperties`, no properties) -> can't judge
        elif _is_secret_path(child):
            continue
        else:  # _CLOSED (explicit false; flags even with no props), or _ABSENT + documented props
            out.append(_finding(child, object_type))


def unknown_attribute_findings(
    schema: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    object_type: str,
    scope_roots: Collection[str] | None,
) -> tuple[Finding, ...]:
    """Findings for payload keys not documented in `schema`. Empty for skip-listed
    (thin / deferred) object types. Server-managed top-level roots
    (`IGNORED_RAW_FIELDS`) are skipped (root-level only). `scope_roots` limits the
    walk to those top-level roots (None = whole object, the --l0-full-object mode)."""
    if object_type in OAS_UNKNOWN_KEY_SKIP or not isinstance(payload, Mapping):
        return ()
    payload = {k: v for k, v in payload.items() if k not in IGNORED_RAW_FIELDS}
    if scope_roots is not None:
        payload = {k: v for k, v in payload.items() if k in scope_roots}
    out: list[Finding] = []
    _walk(payload, schema, "", object_type, out)
    return tuple(out)
