"""Leaf-path diffing + allowlist matching, shared by the field and derived gates.

changed_leaf_paths(): dot-paths of every LEAF that differs between two mappings.
Added/removed subtrees are DESCENDED into, so "a network was added" surfaces as
its individual leaves (networks.corp2.vlan_id, networks.corp2.isolation, ...)
and each leaf is gated on its own — the spec's leaf-tightened allowlist needs
exactly this (a new network with only vlan_id is in scope; one that also sets
isolation is not).

matches(): allowlist entry syntax —
  - '*' matches ONE OR MORE dot-separated path segments ('networks.*.vlan_id',
    'bgp_config.*.neighbors.*.neighbor_as' where neighbor keys are IPs like
    10.0.0.2 that contain literal dots and expand to multiple dot-path segments)
  - a trailing '.*' matches the WHOLE subtree,
    including the root key itself              ('vars.*' allows vars and below)
  - bare entries match exactly                 ('name')

Note on IP-address keys: Mist BGP neighbor dicts are keyed by IP (e.g.
'10.0.0.2').  The `_walk` path-builder joins keys with '.' so such a key
contributes FOUR dot-segments to the path.  '*' in an allowlist pattern must
therefore be allowed to consume one OR MORE path segments — the `_matches_segs`
helper implements backtracking to handle this.
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


def _matches_segs(entry_segs: list[str], path_segs: list[str]) -> bool:
    """Backtracking segment match.  A '*' in the entry consumes ONE OR MORE path
    segments so that IP-address dictionary keys (e.g. '10.0.0.2', which expands
    to four dot-separated path segments) are still matched by a single '*'."""
    ei, pi = 0, 0
    while ei < len(entry_segs) and pi < len(path_segs):
        if entry_segs[ei] == "*":
            # try consuming 1..N path segments for this wildcard
            ei += 1
            for consume in range(1, len(path_segs) - pi + 1):
                if _matches_segs(entry_segs[ei:], path_segs[pi + consume:]):
                    return True
            return False
        if entry_segs[ei] != path_segs[pi]:
            return False
        ei += 1
        pi += 1
    return ei == len(entry_segs) and pi == len(path_segs)


def matches(path: str, entry: str) -> bool:
    if entry.endswith(".*"):
        root = entry[:-2]
        return path == root or path.startswith(root + ".")
    return _matches_segs(entry.split("."), path.split("."))


def allowed(path: str, allowlist: tuple[str, ...]) -> bool:
    return any(matches(path, entry) for entry in allowlist)
