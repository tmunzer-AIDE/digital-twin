"""Leaf-path diffing + allowlist matching, shared by the field and derived gates.

changed_leaf_paths(): dot-paths of every LEAF that differs between two mappings.
Added/removed subtrees are DESCENDED into, so "a network was added" surfaces as
its individual leaves (networks.corp2.vlan_id, networks.corp2.isolation, ...)
and each leaf is gated on its own — the spec's leaf-tightened allowlist needs
exactly this (a new network with only vlan_id is in scope; one that also sets
isolation is not).

matches(): allowlist entry syntax —
  - '*' matches exactly ONE key segment        ('networks.*.vlan_id')
  - a trailing '.*' matches the WHOLE subtree,
    including the root key itself              ('vars.*' allows vars and below)
  - bare entries match exactly                 ('name')
Keys containing literal dots are unsupported (Mist keys here are port ranges,
network names, var names — none use dots).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_MISSING = object()


def changed_leaf_paths(
    current: Mapping[str, Any],
    new: Mapping[str, Any],
    ignore_top: tuple[str, ...] = (),
) -> tuple[str, ...]:
    out: list[str] = []
    _walk(dict(current), dict(new), "", out, ignore_top)
    return tuple(sorted(out))


def _walk(cur: Any, new: Any, path: str, out: list[str], ignore_top: tuple[str, ...]) -> None:
    if isinstance(cur, dict) and isinstance(new, dict):
        for key in sorted(set(cur) | set(new)):
            if not path and key in ignore_top:
                continue
            sub = f"{path}.{key}" if path else key
            cv, nv = cur.get(key, _MISSING), new.get(key, _MISSING)
            # null == absent (Mist PUT semantics, same canon as compile equivalence)
            if cv is _MISSING and nv is None or nv is _MISSING and cv is None:
                continue
            # descend into an added/removed SUBTREE so its leaves gate individually
            if cv is _MISSING and isinstance(nv, dict):
                cv = {}
            if nv is _MISSING and isinstance(cv, dict):
                nv = {}
            if cv is _MISSING or nv is _MISSING:
                out.append(sub)  # scalar/list added or removed = changed (PUT semantics)
            else:
                _walk(cv, nv, sub, out, ignore_top)
        return
    if _normalized(cur) != _normalized(new):
        out.append(path)


def _normalized(value: Any) -> Any:
    """null==absent must hold DEEPLY: lists compare atomically, so None-valued
    dict keys inside list elements are stripped before comparison."""
    if isinstance(value, dict):
        return {k: _normalized(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_normalized(v) for v in value]
    return value


def matches(path: str, entry: str) -> bool:
    if entry.endswith(".*"):
        root = entry[:-2]
        return path == root or path.startswith(root + ".")
    entry_segs = entry.split(".")
    path_segs = path.split(".")
    if len(entry_segs) != len(path_segs):
        return False
    return all(e in ("*", p) for e, p in zip(entry_segs, path_segs, strict=True))


def allowed(path: str, allowlist: tuple[str, ...]) -> bool:
    return any(matches(path, entry) for entry in allowlist)
