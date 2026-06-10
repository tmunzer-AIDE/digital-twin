"""Post-fetch raw pre-screen: which raw paths does this op actually change?

Diffs payload vs the CURRENT raw object (per spec, the rolling pre-op state —
the engine passes the right one) and matches every changed path against the
raw allowlist. Full-object-replacement semantics: a field present in current
but absent from payload counts as CHANGED (removed). Server-managed metadata
(IGNORED_RAW_FIELDS) is excluded — a payload never carries it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from digital_twin.contracts import Rejection
from digital_twin.scope.allowlist import IGNORED_RAW_FIELDS, RAW_ALLOWLIST

_STAGE = "field_gate"


def changed_paths(current: Mapping[str, Any], payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Dot-paths of every leaf that differs (additions, edits, removals)."""
    out: list[str] = []
    _walk(dict(current), dict(payload), "", out, ignore_top=IGNORED_RAW_FIELDS)
    return tuple(sorted(out))


def _walk(
    cur: Any, new: Any, path: str, out: list[str], ignore_top: tuple[str, ...] = ()
) -> None:
    if isinstance(cur, dict) and isinstance(new, dict):
        for key in sorted(set(cur) | set(new)):
            if not path and key in ignore_top:
                continue
            sub = f"{path}.{key}" if path else key
            if key not in cur or key not in new:
                out.append(sub)  # added or removed = changed (PUT replaces the object)
            else:
                _walk(cur[key], new[key], sub, out)
        return
    if cur != new:
        out.append(path)


def _allowed(path: str, allowlist: tuple[str, ...]) -> bool:
    for entry in allowlist:
        if entry.endswith(".*"):
            root = entry[:-2]
            if path == root or path.startswith(root + "."):
                return True
        elif path == entry:
            return True
    return False


def screen_op(
    object_type: str, current: Mapping[str, Any], payload: Mapping[str, Any]
) -> Rejection | None:
    allowlist = RAW_ALLOWLIST.get(object_type, ())
    offending = [p for p in changed_paths(current, payload) if not _allowed(p, allowlist)]
    if offending:
        return Rejection(
            stage=_STAGE,
            reasons=tuple(
                f"out-of-scope raw path changed: {p} (not in the M1 allowlist)" for p in offending
            ),
        )
    return None
