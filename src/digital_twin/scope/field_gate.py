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
from digital_twin.scope.allowlist import RAW_ALLOWLIST, ignored_raw_fields
from digital_twin.scope.atomic_lists import atomic_list_issues
from digital_twin.scope.paths import LeafDelta, allowed_tokens, leaf_changes

_STAGE = "field_gate"


def changed_paths(
    current: Mapping[str, Any], payload: Mapping[str, Any], *, object_type: str = "device"
) -> tuple[str, ...]:
    """Dot-paths of every leaf that differs (additions, edits, removals)."""
    return tuple(d.path for d in leaf_changes(
        current, payload, ignore_top=ignored_raw_fields(object_type)
    ))


def screen_op_split(
    object_type: str,
    current: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    enforce_wlan_site_ownership: bool = True,
) -> tuple[Rejection | None, tuple[Rejection, ...]]:
    """Split the screen into (hard, gaps).

    HARD = object-level "cannot simulate this object at all" (wrong device role,
    inherited WLAN) -> the engine short-circuits to UNKNOWN, no checks run.
    GAPS = field-level "cannot model these specific leaves" (out-of-scope raw
    leaves, no_local_overwrite ripple). These are COVERAGE gaps: the simulation
    still runs on the in-scope projection, the decision floors at UNKNOWN (never
    SAFE — same invariant as the derived gate), but a confidently-modeled UNSAFE
    from the rest of the change still wins (decision precedence: UNSAFE > coverage
    UNKNOWN). The IR never carries the out-of-scope leaf, so it cannot leak in."""
    if object_type == "device" and current.get("type") != "switch":
        return Rejection(
            stage=_STAGE,
            reasons=(
                f"device type {current.get('type')!r} is not modeled in M1 "
                "(switch config only — AP/gateway devices are out of scope)",
            ),
        ), ()
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
        ), ()
    allowlist = RAW_ALLOWLIST.get(object_type, ())
    changes = leaf_changes(current, payload, ignore_top=ignored_raw_fields(object_type))
    reasons = [
        _offense_reason(delta)
        for delta in changes
        if not allowed_tokens(delta.tokens, allowlist)
        and not _known_empty_nac_match(object_type, delta.path, current, payload)
    ]
    for delta in changes:
        if allowed_tokens(delta.tokens, allowlist):
            for value in (delta.before, delta.after):
                reasons.extend(
                    f"unsupported atomic-list content: {'.'.join(path)} "
                    "(the allowed parent does not authorize arbitrary children)"
                    for path in atomic_list_issues(delta.tokens, value)
                )
    if object_type == "device":
        # no_local_overwrite is in scope, but flipping it activates/deactivates the
        # member's local_port_config entry wholesale — including UNMODELED local
        # leaves the raw diff doesn't surface (the flag changed, not the leaves) and
        # the derived gate can't see (the resolver never projects them). Re-screen
        # those leaves here so a flip over an unmodeled local leaf -> coverage gap.
        reasons.extend(_local_overwrite_ripple(changes, current, payload, allowlist))
    gaps = (Rejection(stage=_STAGE, reasons=tuple(dict.fromkeys(reasons))),) if reasons else ()
    return None, gaps


def _known_empty_nac_match(
    object_type: str, path: str, current: Mapping[str, Any], proposed: Mapping[str, Any]
) -> bool:
    """An explicit empty NAC match is a supported unconstrained rule. Only
    exactly empty objects qualify; unknown children and null-only trees do not."""
    if object_type != "nacrule" or path not in ("matching", "not_matching"):
        return False
    before, after = current.get(path), proposed.get(path)
    return (before is None or before == {}) and (after is None or after == {})


def screen_op(
    object_type: str,
    current: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    enforce_wlan_site_ownership: bool = True,
) -> Rejection | None:
    """Back-compat combined view: any hard rejection OR field gap collapses to a
    single Rejection (callers that don't separate coverage from hard treat both as
    UNKNOWN). The single-object engine path uses screen_op_split to keep field gaps
    as coverage gaps; org/NAC paths stay conservative via this combined form."""
    hard, gaps = screen_op_split(
        object_type, current, payload,
        enforce_wlan_site_ownership=enforce_wlan_site_ownership,
    )
    if hard is not None:
        return hard
    if gaps:
        return Rejection(stage=_STAGE, reasons=tuple(r for g in gaps for r in g.reasons))
    return None


def _local_overwrite_ripple(
    changes: tuple[LeafDelta, ...],
    current: Mapping[str, Any],
    payload: Mapping[str, Any],
    allowlist: tuple[str, ...],
) -> list[str]:
    """Members whose no_local_overwrite flipped AND whose effective local_port_config
    entry carries an out-of-scope leaf — the flip silently activates/deactivates it."""
    if not any(
        len(d.tokens) == 3 and d.tokens[0] == "port_config"
        and d.tokens[2] == "no_local_overwrite" for d in changes
    ):
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
        for delta in leaf_changes({}, entry):
            if not allowed_tokens(("local_port_config", member, *delta.tokens), allowlist):
                out.append(
                    f"out-of-scope local leaf gated by a no_local_overwrite flip on {member}: "
                    f"local_port_config.{member}.{delta.path} (not in the M1 allowlist)"
                )
    return out


def _offense_reason(delta: LeafDelta) -> str:
    """Distinguish deletions from edits: with Mist update semantics (omitted
    roots persist), an absent path in the proposed object means it was deleted
    — via a '-attribute' marker at root, or by a sent root that drops it."""
    if delta.kind == "removed":
        return f"out-of-scope raw path deleted: {delta.path} (not in the M1 allowlist)"
    return f"out-of-scope raw path changed: {delta.path} (not in the M1 allowlist)"
