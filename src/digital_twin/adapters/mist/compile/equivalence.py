"""Tier-2 equivalence: our merge_only() site-level merge vs getSiteSettingDerived.

(merge_only, NOT compile_site: derived does not resolve {{vars}}, so the oracle
comparison uses the pre-vars artifact.) Comparison rules (from the spec):
canonical normalization (numeric strings, absent==null==empty), per-path diff
reporting, a catalogued-divergence list (data: divergences.json — every entry
justified), and an attribute-coverage report so a green gate states explicitly
which schema leaves real data exercised.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from digital_twin.adapters.mist.oas import norm_schema

_DIVERGENCES = Path(__file__).parent / "divergences.json"

# The M1 in-scope SITE-setting fields (the switch L2 config the compiler/IR use).
# The gate's "100% on in-scope fields" rule (spec) applies to THESE subtrees;
# out-of-scope domains (radio_config, marvis, password_policy, RF templates,
# server-injected metadata/defaults) are validated by the Tier-1 OAS tests.
#
# NOTE: per-port assignment (port_config/local_port_config/port_config_overwrite)
# is DEVICE-level and absent from getSiteSettingDerived, so this site oracle does
# NOT cover the port->VLAN projection. That projection is validated separately by
# the gate's port-usage cross-check (compiled vs observed) and by Tier-1 unit tests.
IN_SCOPE_FIELDS: tuple[str, ...] = ("networks", "port_usages", "vars")


def restrict_to_scope(config: dict[str, Any]) -> dict[str, Any]:
    return {k: config[k] for k in IN_SCOPE_FIELDS if k in config}


def restrict_schema_to_scope(schema: dict[str, Any]) -> dict[str, Any]:
    props = norm_schema(schema).get("properties") or {}
    return {"type": "object", "properties": {k: props[k] for k in IN_SCOPE_FIELDS if k in props}}


@dataclass(frozen=True)
class Diff:
    path: str
    ours: Any
    derived: Any


@dataclass(frozen=True)
class CompareResult:
    diffs: tuple[Diff, ...]
    catalogued_diffs: tuple[Diff, ...]

    @property
    def passed(self) -> bool:
        return not self.diffs


@dataclass(frozen=True)
class Coverage:
    covered: frozenset[str]
    uncovered: frozenset[str]


def load_catalogued() -> tuple[str, ...]:
    """Paths of catalogued divergences; every entry must carry a reason."""
    entries = json.loads(_DIVERGENCES.read_text())["entries"]
    missing = [e for e in entries if not e.get("reason")]
    if missing:
        raise ValueError(f"divergence entries without a reason: {missing}")
    return tuple(e["path"] for e in entries)


def _norm(v: Any) -> Any:
    if isinstance(v, str) and v.lstrip("-").isdigit():
        return int(v)
    if v in (None, {}, []):
        return None
    return v


def _walk_diffs(ours: Any, derived: Any, path: str, out: list[Diff]) -> None:
    if isinstance(ours, dict) or isinstance(derived, dict):
        o = ours if isinstance(ours, dict) else {}
        d = derived if isinstance(derived, dict) else {}
        for key in sorted(set(o) | set(d)):
            _walk_diffs(o.get(key), d.get(key), f"{path}.{key}" if path else key, out)
        return
    if isinstance(ours, list) and isinstance(derived, list):
        if [_norm(x) for x in ours] != [_norm(x) for x in derived]:
            out.append(Diff(path, ours, derived))
        return
    if _norm(ours) != _norm(derived):
        out.append(Diff(path, ours, derived))


def compare_effective(
    ours: dict[str, Any], derived: dict[str, Any], catalogued: tuple[str, ...] | None = None
) -> CompareResult:
    catalogued = load_catalogued() if catalogued is None else catalogued
    all_diffs: list[Diff] = []
    _walk_diffs(ours, derived, "", all_diffs)
    known = tuple(d for d in all_diffs if d.path in catalogued)
    real = tuple(d for d in all_diffs if d.path not in catalogued)
    return CompareResult(diffs=real, catalogued_diffs=known)


def _schema_leaves(schema: dict[str, Any], path: str = "", depth: int = 0) -> set[str]:
    if depth > 12:
        return {path} if path else set()
    schema = norm_schema(schema)  # SAME normalization as the Tier-1 generator —
    # leaves hidden behind allOf/anyOf/oneOf must count in coverage too
    props = schema.get("properties")
    if props:
        out: set[str] = set()
        for k, sub in props.items():
            out |= _schema_leaves(sub, f"{path}.{k}" if path else k, depth + 1)
        return out
    ap = schema.get("additionalProperties")
    if isinstance(ap, dict):
        return _schema_leaves(ap, f"{path}.*" if path else "*", depth + 1)
    return {path} if path else set()


def _data_leaves(node: Any, path: str = "") -> set[str]:
    if isinstance(node, dict):
        out: set[str] = set()
        for k, v in node.items():
            out |= _data_leaves(v, f"{path}.{k}" if path else k)
        return out
    return {path} if path else set()


def attribute_coverage(schema: dict[str, Any], samples: list[dict[str, Any]]) -> Coverage:
    """Which schema leaves did real data exercise? (wildcard-aware for keyed dicts)"""
    schema_leaves = _schema_leaves(schema)
    seen: set[str] = set()
    for sample in samples:
        seen |= _data_leaves(sample)

    def exercised(leaf: str) -> bool:
        if "*" not in leaf:
            return leaf in seen
        import re

        pattern = re.compile("^" + re.escape(leaf).replace("\\*", "[^.]+") + "$")
        return any(pattern.match(s) for s in seen)

    covered = frozenset(leaf for leaf in schema_leaves if exercised(leaf))
    return Coverage(covered=covered, uncovered=frozenset(schema_leaves) - covered)
