"""Tier-1 equivalence: OAS-driven FULL-ATTRIBUTE precedence tests.

Generates payloads covering every schema leaf (including features no real org
has configured) and asserts our merge semantics per leaf: site wins on overlap,
template-only survives, DICT_MERGE fields merge per key. Validates OUR semantics
for self-consistency; Tier 2 (live gate) validates them against Mist's engine.
"""

from __future__ import annotations

from typing import Any

import pytest

from digital_twin.adapters.mist.compile.merge import MergePolicy, merge_site_effective
from digital_twin.adapters.mist.oas import load_schema, norm_schema


def _load(name: str) -> dict[str, Any]:
    return load_schema(name)


def _marker(schema: dict[str, Any], tag: str) -> Any:
    """A type-valid value for a leaf schema, distinguishable by `tag`."""
    t = schema.get("type")
    if "enum" in schema:
        options = schema["enum"]
        return options[0] if tag == "tpl" else options[-1]
    if t == "string":
        return tag
    if t in ("integer", "number"):
        return 1 if tag == "tpl" else 2
    if t == "boolean":
        return tag != "tpl"
    return tag  # untyped: treat as string


def _gen(schema: dict[str, Any], tag: str, depth: int = 0) -> Any:
    """Generate a payload populating EVERY property of the schema."""
    if depth > 12:
        return _marker({"type": "string"}, tag)
    schema = norm_schema(schema)
    t = schema.get("type")
    if t == "object" or "properties" in schema or "additionalProperties" in schema:
        out: dict[str, Any] = {}
        for prop, sub in (schema.get("properties") or {}).items():
            out[prop] = _gen(sub, tag, depth + 1)
        ap = schema.get("additionalProperties")
        if isinstance(ap, dict):  # keyed collection (networks, port_usages, ...)
            out[f"key_{tag}"] = _gen(ap, tag, depth + 1)
            out["key_shared"] = _gen(ap, tag, depth + 1)
        return out
    if t == "array":
        items = schema.get("items") or {"type": "string"}
        return [_gen(items, tag, depth + 1)]
    return _marker(schema, tag)


def _leaves(node: Any, path: str = "") -> dict[str, Any]:
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for k, v in node.items():
            out.update(_leaves(v, f"{path}.{k}" if path else k))
        return out
    return {path: node}


@pytest.fixture(scope="module")
def schemas() -> tuple[dict[str, Any], dict[str, Any]]:
    return _load("networktemplate.schema.json"), _load("site_setting.schema.json")


def test_site_wins_on_every_overlapping_leaf(schemas):
    nt_schema, st_schema = schemas
    tpl = _gen(nt_schema, "tpl")
    site = _gen(st_schema, "site")
    out = merge_site_effective(tpl, site)
    out_leaves = _leaves(out)
    for path, value in _leaves(site).items():
        top = path.split(".", 1)[0]
        if MergePolicy.for_field(top) is MergePolicy.DICT_MERGE and ".key_tpl" in f".{path}":
            continue  # template-only keys of merged dicts are not in `site`
        assert out_leaves.get(path) == value, f"site value lost at {path}"


def test_template_only_fields_survive(schemas):
    nt_schema, _ = schemas
    tpl = _gen(nt_schema, "tpl")
    out = merge_site_effective(tpl, {})
    assert _leaves(out) == _leaves(tpl)


def test_dict_merge_unions_keys_from_both_sides(schemas):
    nt_schema, st_schema = schemas
    tpl = _gen(nt_schema, "tpl")
    site = _gen(st_schema, "site")
    out = merge_site_effective(tpl, site)
    for field, policy_field in (("networks", "networks"), ("port_usages", "port_usages")):
        if field in tpl and field in site:
            assert MergePolicy.for_field(policy_field) is MergePolicy.DICT_MERGE
            assert "key_tpl" in out[field], f"{field}: template-only key lost"
            assert "key_site" in out[field], f"{field}: site key lost"
            assert out[field]["key_shared"] == site[field]["key_shared"], f"{field}: site must win"
