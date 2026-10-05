"""Leaf-path diffing + allowlist matching, shared by the field and derived gates.

changed_leaf_paths(): dot-paths of every LEAF that differs between two mappings.
Added/removed subtrees are DESCENDED into, so "a network was added" surfaces as
its individual leaves (networks.corp2.vlan_id, networks.corp2.isolation, ...)
and each leaf is gated on its own — the spec's leaf-tightened allowlist needs
exactly this (a new network with only vlan_id is in scope; one that also sets
isolation is not).

matches(): allowlist entry syntax —
  - '*' matches EXACTLY ONE dot-separated path segment ('networks.*.vlan_id')
  - '**' matches ONE OR MORE dot-separated path segments, used ONLY where dict
    keys contain literal dots — BGP neighbors are keyed by IP (e.g. '10.0.0.2'),
    which the path walker joins with '.', expanding to four dot-segments, so the
    neighbor position in an allowlist entry must use '**'
    ('bgp_config.*.neighbors.**.neighbor_as')
  - a trailing '.*' matches the WHOLE subtree,
    including the root key itself              ('vars.*' allows vars and below)
  - bare entries match exactly                 ('name')

Safety invariant: '*' matches exactly one segment, so a pattern like
'dhcpd_config.*.type' cannot cross nesting levels and match deeper paths such
as 'dhcpd_config.corp.options.43.type'.  '**' is reserved for the neighbor-IP
position and must NOT be used elsewhere.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_MISSING = object()


@dataclass(frozen=True)
class LeafDelta:
    path: str
    kind: str  # "added" | "removed" | "changed"
    before: Any
    after: Any
    # Display paths are deliberately unchanged. Authorization uses these
    # original keys so a literal dot never manufactures another nesting level.
    tokens: tuple[str, ...] = ()


def leaf_changes(
    current: Mapping[str, Any],
    new: Mapping[str, Any],
    ignore_top: tuple[str, ...] = (),
) -> tuple[LeafDelta, ...]:
    """Every LEAF that differs between two mappings, WITH its raw before/after.
    Same traversal/semantics as changed_leaf_paths (null==absent, descended
    add/removed subtrees, atomic lists); sorted by path for determinism."""
    out: list[LeafDelta] = []
    _walk(dict(current), dict(new), (), out, ignore_top)
    return tuple(sorted(out, key=lambda d: (d.path, d.tokens)))


def changed_leaf_paths(
    current: Mapping[str, Any],
    new: Mapping[str, Any],
    ignore_top: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Dot-paths of every leaf that differs — now derived from leaf_changes so the
    field gate and the config diff share ONE definition of 'what changed'."""
    return tuple(d.path for d in leaf_changes(current, new, ignore_top))


def _walk(
    cur: Any, new: Any, tokens: tuple[str, ...], out: list[LeafDelta], ignore_top: tuple[str, ...]
) -> None:
    if isinstance(cur, dict) and isinstance(new, dict):
        for key in sorted(set(cur) | set(new)):
            if not tokens and key in ignore_top:
                continue
            sub = (*tokens, key)
            path = ".".join(sub)
            cv, nv = cur.get(key, _MISSING), new.get(key, _MISSING)
            # null == absent (Mist PUT semantics, same canon as compile equivalence)
            if cv is _MISSING and nv is None or nv is _MISSING and cv is None:
                continue
            # An object can activate defaults or reset a domain even when it
            # contains no non-null scalar leaves. Retain structural presence.
            if cv is _MISSING and isinstance(nv, dict):
                start = len(out)
                _walk({}, nv, sub, out, ignore_top)
                if len(out) == start:
                    out.append(LeafDelta(path, "added", None, nv, sub))
                continue
            if nv is _MISSING and isinstance(cv, dict):
                start = len(out)
                _walk(cv, {}, sub, out, ignore_top)
                if len(out) == start:
                    out.append(LeafDelta(path, "removed", cv, None, sub))
                continue
            # descend into an added/removed SUBTREE so its leaves surface individually
            if cv is _MISSING:
                out.append(LeafDelta(path, "added", None, nv, sub))  # scalar/list added
            elif nv is _MISSING:
                out.append(LeafDelta(path, "removed", cv, None, sub))  # scalar/list removed
            else:
                _walk(cv, nv, sub, out, ignore_top)
        return
    if _normalized(cur) != _normalized(new):
        out.append(LeafDelta(".".join(tokens), "changed", cur, new, tokens))


def _normalized(value: Any) -> Any:
    """null==absent must hold DEEPLY: lists compare atomically, so None-valued
    dict keys inside list elements are stripped before comparison."""
    if isinstance(value, dict):
        return (
            "object",
            tuple((k, _normalized(v)) for k, v in sorted(value.items()) if v is not None),
        )
    if isinstance(value, list):
        return ("array", tuple(_normalized(v) for v in value))
    # Python equality conflates bool and int (True == 1), including inside
    # atomic lists. JSON types must survive the comparison and L0 validation.
    if isinstance(value, bool):
        return ("boolean", value)
    if isinstance(value, (int, float)):
        return ("number", value)
    return (type(value).__name__, value)


def _matches_segs(entry_segs: list[str], path_segs: list[str]) -> bool:
    """'*' matches EXACTLY ONE segment; '**' matches ONE OR MORE segments.
    '**' exists for dict keys that contain literal dots (BGP neighbors are keyed
    by IP, e.g. '10.0.0.2', expanding to multiple dot-path segments)."""
    ei = pi = 0
    while ei < len(entry_segs):
        e = entry_segs[ei]
        if e == "**":
            if pi >= len(path_segs):
                return False  # '**' requires at least one segment
            ei += 1
            for consume in range(1, len(path_segs) - pi + 1):
                if _matches_segs(entry_segs[ei:], path_segs[pi + consume :]):
                    return True
            return False
        if pi >= len(path_segs):
            return False
        if e != "*" and e != path_segs[pi]:
            return False
        ei += 1
        pi += 1
    return pi == len(path_segs)


def matches(path: str, entry: str) -> bool:
    """Match a concrete dot-path against an allowlist entry.

    Tokens: '*' = exactly one segment; '**' = one or more segments (for
    IP-address dict keys that expand to multiple dot-segments); trailing '.*'
    = whole subtree including the root key; bare string = exact match.

    Denied leaves stay denied because '*' never crosses nesting levels and
    '**' is used only at the BGP neighbor-IP position."""
    if entry.endswith(".*"):
        root = entry[:-2]
        return path == root or path.startswith(root + ".")
    return _matches_segs(entry.split("."), path.split("."))


def allowed(path: str, allowlist: tuple[str, ...]) -> bool:
    return any(matches(path, entry) for entry in allowlist)


def allowed_tokens(tokens: tuple[str, ...], allowlist: tuple[str, ...]) -> bool:
    """Authorize structural JSON keys, never the ambiguous display path.

    Both '*' and the legacy '**' spelling consume ONE original map key. IPs
    and dotted names are already single tokens; '**' must not consume subtrees.
    Only a trailing '.*' retains the explicit whole-subtree contract (vars).
    """
    for entry in allowlist:
        parts = tuple(entry.split("."))
        if parts[-1] == "*":
            root = parts[:-1]
            if len(tokens) >= len(root) and all(
                pattern in ("*", "**") or pattern == key
                for pattern, key in zip(root, tokens, strict=False)
            ):
                return True
        elif len(parts) == len(tokens) and all(
            pattern in ("*", "**") or pattern == key
            for pattern, key in zip(parts, tokens, strict=True)
        ):
            return True
    return False
