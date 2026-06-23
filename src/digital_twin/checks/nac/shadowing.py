"""NAC rule shadowing — the conservative provable-superset core (GS34 spec §6)."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from digital_twin.ir import NacRule


def is_provable(r: NacRule) -> bool:
    """Eligible for a shadowing proof: cleanly parsed, ordered, and constraining ONLY
    on {auth_types, port_types, match_tags}. One chokepoint — no dimension forgotten."""
    return (
        r.opaque_digest is None
        and r.order is not None
        and not r.not_matching
        and not (r.site_ids or r.sitegroup_ids or r.family or r.mfg
                 or r.model or r.os_type or r.vendor)
    )


def covers_choice(a: frozenset[str], b: frozenset[str]) -> bool:
    """auth_types / port_types — ∅ = any; A covers B iff A is unconstrained or B ⊆ A."""
    if not a:
        return True
    if not b:
        return False
    return b <= a


def covers_tags(a: frozenset[str], b: frozenset[str]) -> bool:
    """match_tags — CONSERVATIVE: A has no tag filter OR identical sets. NOT `a <= b`
    (that assumes tags AND, which is unconfirmed; a strict subset would false-positive)."""
    return (not a) or (a == b)


def A_covers_B(a: NacRule, b: NacRule) -> bool:  # noqa: N802 — matches spec name
    return (
        covers_choice(a.auth_types, b.auth_types)
        and covers_choice(a.port_types, b.port_types)
        and covers_tags(a.match_tags, b.match_tags)
    )


class ShadowStatus(StrEnum):
    TRUE = "true"
    FALSE = "false"
    INDETERMINATE = "indeterminate"


def shadow_status(a_id: str, b_id: str, state: Mapping[str, NacRule]) -> ShadowStatus:
    """Does A shadow B in `state`? The single definition attribution derives from.
    Ordering matters: disabled/order/absence short-circuit to FALSE even when the cover
    test is unevaluable."""
    a, b = state.get(a_id), state.get(b_id)
    if a is None or b is None:
        return ShadowStatus.FALSE          # absent (e.g. newly created)
    if not a.enabled or not b.enabled:
        return ShadowStatus.FALSE          # disabled never participates
    if a.order is not None and b.order is not None and a.order >= b.order:
        return ShadowStatus.FALSE          # A not strictly earlier than B
    if not is_provable(a) or not is_provable(b):
        return ShadowStatus.INDETERMINATE  # opaque / orderless / unmodeled criteria
    return ShadowStatus.TRUE if A_covers_B(a, b) else ShadowStatus.FALSE
