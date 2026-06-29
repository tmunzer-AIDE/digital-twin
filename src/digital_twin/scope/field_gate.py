"""Post-fetch raw pre-screen: which raw LEAVES does this op actually change?

Two checks, both needing the fetched object:
1. DEVICE ROLE — M1 models switch config only; an op targeting an AP/gateway
   device is rejected here (the spec's post-fetch role check: the role is only
   known once the device is fetched).
2. CHANGED PATHS — diffs payload vs the CURRENT raw object (the rolling pre-op
   state; the engine passes the right one) and matches every changed LEAF
   against the leaf-tightened raw allowlist. Full-object-replacement semantics:
   a field present in current but absent from payload counts as CHANGED
   (removed); added/removed subtrees gate leaf-by-leaf. Server-managed metadata
   (IGNORED_RAW_FIELDS) is excluded — a payload never carries it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from digital_twin.adapters.mist.ingest.ports import expand_port_map
from digital_twin.adapters.mist.ingest.wlan import wlan_is_inherited
from digital_twin.contracts import Rejection
from digital_twin.scope.allowlist import IGNORED_RAW_FIELDS, RAW_ALLOWLIST
from digital_twin.scope.paths import allowed, changed_leaf_paths

_STAGE = "field_gate"


def changed_paths(current: Mapping[str, Any], payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Dot-paths of every leaf that differs (additions, edits, removals)."""
    return changed_leaf_paths(current, payload, ignore_top=IGNORED_RAW_FIELDS)


def screen_op(
    object_type: str,
    current: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    enforce_wlan_site_ownership: bool = True,
) -> Rejection | None:
    if object_type == "device" and current.get("type") != "switch":
        return Rejection(
            stage=_STAGE,
            reasons=(
                f"device type {current.get('type')!r} is not modeled in M1 "
                "(switch config only — AP/gateway devices are out of scope)",
            ),
        )
    if (
        object_type == "wlan"
        and enforce_wlan_site_ownership
        and wlan_is_inherited(current)
    ):
        return Rejection(
            stage=_STAGE,
            reasons=(
                f"WLAN {current.get('id')!r} is inherited from an org wlantemplate "
                "(not a site-writable object) — simulate the change at the org/template level",
            ),
        )
    allowlist = RAW_ALLOWLIST.get(object_type, ())
    changed = changed_paths(current, payload)
    reasons = [_offense_reason(p, current, payload) for p in changed if not allowed(p, allowlist)]
    if object_type == "device":
        # no_local_overwrite is in scope, but flipping it activates/deactivates the
        # member's local_port_config entry wholesale — including UNMODELED local
        # leaves the raw diff doesn't surface (the flag changed, not the leaves) and
        # the derived gate can't see (the resolver never projects them). Re-screen
        # those leaves here so a flip over an unmodeled local leaf -> UNKNOWN.
        reasons.extend(_local_overwrite_ripple(changed, current, payload, allowlist))
    if reasons:
        return Rejection(stage=_STAGE, reasons=tuple(reasons))
    return None


def _local_overwrite_ripple(
    changed: tuple[str, ...],
    current: Mapping[str, Any],
    payload: Mapping[str, Any],
    allowlist: tuple[str, ...],
) -> list[str]:
    """Members whose no_local_overwrite flipped AND whose effective local_port_config
    entry carries an out-of-scope leaf — the flip silently activates/deactivates it."""
    if not any(".no_local_overwrite" in p for p in changed):
        return []
    cur_pc = expand_port_map(current.get("port_config") or {})
    new_pc = expand_port_map(payload.get("port_config") or {})
    cur_local = expand_port_map(current.get("local_port_config") or {})
    new_local = expand_port_map(payload.get("local_port_config") or {})
    out: list[str] = []
    for member in cur_pc.keys() | new_pc.keys():
        # default true (OAS): local discarded unless explicitly allowed
        cur_flag = (cur_pc.get(member) or {}).get("no_local_overwrite", True)
        new_flag = (new_pc.get(member) or {}).get("no_local_overwrite", True)
        if cur_flag == new_flag:
            continue
        entry = new_local.get(member, cur_local.get(member)) or {}
        for leaf in entry:
            if not allowed(f"local_port_config.{member}.{leaf}", allowlist):
                out.append(
                    f"out-of-scope local leaf gated by a no_local_overwrite flip on {member}: "
                    f"local_port_config.{member}.{leaf} (not in the M1 allowlist)"
                )
    return out


def _offense_reason(path: str, current: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
    """Distinguish deletions from edits: with Mist update semantics (omitted
    roots persist), an absent path in the proposed object means it was deleted
    — via a '-attribute' marker at root, or by a sent root that drops it."""
    if _present(current, path) and not _present(payload, path):
        return f"out-of-scope raw path deleted: {path} (not in the M1 allowlist)"
    return f"out-of-scope raw path changed: {path} (not in the M1 allowlist)"


def _present(obj: Mapping[str, Any], path: str) -> bool:
    node: Any = obj
    for segment in path.split("."):
        if not isinstance(node, Mapping) or segment not in node:
            return False
        node = node[segment]
    return node is not None  # null == absent (the established canon)
