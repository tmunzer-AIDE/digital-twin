"""Extracted Mist OAS schemas (data) + shared schema-normalization helpers.

norm_schema() resolves composition so BOTH the Tier-1 payload generator and the
Tier-2 attribute-coverage walker see the same leaves: allOf is merged, the FIRST
variant of anyOf/oneOf is taken (the Mist OAS uses these mostly for
int-or-{{var}} unions), nullable type-arrays collapse to their non-null type.
Unknown constructs raise UnsupportedSchema LOUDLY — silently under-generating
would fake full coverage.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_DIR = Path(__file__).parent

_KNOWN_KEYS = {
    "type",
    "properties",
    "additionalProperties",
    "items",
    "enum",
    "default",
    "description",
    "format",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "pattern",
    "example",
    "examples",
    "required",
    "nullable",
    "deprecated",
    "readOnly",
    "writeOnly",
    "title",
    "minItems",
    "maxItems",
    "uniqueItems",
    "allOf",
    "anyOf",
    "oneOf",
    "const",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "x-deprecation-note",
}


class UnsupportedSchema(ValueError):
    """An OAS construct the tooling does not understand — fail loudly."""


def load_schema(filename: str) -> dict[str, Any]:
    return json.loads((_DIR / filename).read_text())  # type: ignore[no-any-return]


def norm_schema(schema: dict[str, Any]) -> dict[str, Any]:
    unknown = set(schema) - _KNOWN_KEYS
    if unknown:
        raise UnsupportedSchema(f"unsupported OAS constructs: {sorted(unknown)}")
    if "allOf" in schema:
        merged: dict[str, Any] = {}
        props: dict[str, Any] = {}
        for sub in schema["allOf"]:
            sub = norm_schema(sub)
            props.update(sub.get("properties") or {})
            merged.update({k: v for k, v in sub.items() if k != "properties"})
        if props:
            merged["properties"] = props
        return norm_schema({**merged, **{k: v for k, v in schema.items() if k != "allOf"}})
    for comb in ("anyOf", "oneOf"):
        if comb in schema:
            variants = schema[comb]
            # Mist uses these for SCALAR unions (int-or-{{var}}), where every
            # variant has the same (empty) leaf set, so first-variant is safe.
            # An object-shaped non-first variant would hide leaves from both
            # the Tier-1 generator and Tier-2 coverage — fail loudly instead.
            for extra in variants[1:]:
                if isinstance(extra, dict) and (
                    "properties" in extra or "additionalProperties" in extra
                ):
                    raise UnsupportedSchema(
                        f"{comb} with object-shaped non-first variant (leaves would be hidden)"
                    )
            return norm_schema(variants[0])  # first variant, deterministic
    t = schema.get("type")
    if isinstance(t, list):  # nullable type arrays, e.g. ["integer", "null"]
        non_null = [x for x in t if x != "null"]
        return {**schema, "type": non_null[0] if non_null else "string"}
    return schema
