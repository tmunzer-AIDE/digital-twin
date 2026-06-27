"""Post-compile derived-impact gate: diff the FULL effective configs, leaf-level.

The IR is a projection of in-scope fields only, so an out-of-scope effective
change (e.g. a vars edit rippling into dhcpd_config) NEVER enters the IR and
IRDiff cannot see it. This gate diffs the compiler's full effective output
(baseline vs proposed) — site effective AND each device effective — at LEAF
granularity and rejects if any leaf outside the effective allowlist differs.
Leaf-level matters for the same reason as the raw gate: an in-scope subtree
(networks) can carry out-of-scope leaves (isolation) that the IR does not
model. Both sides come from the identical compiler code path, so plain
equality is sound (no normalization needed — same code, same shapes).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from digital_twin.contracts import Rejection
from digital_twin.scope.allowlist import EFFECTIVE_ALLOWLIST
from digital_twin.scope.dhcp_screen import dhcp_row_rejection
from digital_twin.scope.paths import allowed, changed_leaf_paths

_STAGE = "derived_gate"


@dataclass(frozen=True)
class DerivedGap:
    rejection: Rejection
    paths: tuple[str, ...] = ()
    dhcp_row: str | None = None


def changed_effective_paths(
    baseline: Mapping[str, Any], proposed: Mapping[str, Any]
) -> tuple[str, ...]:
    return changed_leaf_paths(baseline, proposed)


def check_derived_gap(
    baseline: Mapping[str, Any],
    proposed: Mapping[str, Any],
    *,
    artifact: str = "site",
    allowlist: tuple[str, ...] = EFFECTIVE_ALLOWLIST,
) -> DerivedGap | None:
    offending = [
        path
        for path in changed_effective_paths(baseline, proposed)
        if not allowed(path, allowlist)
    ]
    if offending:
        return DerivedGap(
            rejection=Rejection(
                stage=_STAGE,
                reasons=tuple(
                    f"{path}: out-of-scope EFFECTIVE leaf differs in {artifact} config "
                    "(change ripples beyond the M1 model)"
                    for path in offending
                ),
            ),
            paths=tuple(offending),
        )
    b_dhcp = baseline.get("dhcpd_config") or {}
    p_dhcp = proposed.get("dhcpd_config") or {}
    for name in sorted(set(b_dhcp) | set(p_dhcp)):
        rej = dhcp_row_rejection(b_dhcp.get(name) or {}, p_dhcp.get(name) or {})
        if rej is not None:
            return DerivedGap(
                rejection=Rejection(
                    stage=rej.stage,
                    reasons=tuple(
                        f"dhcpd_config.{name} in {artifact}: {reason}"
                        for reason in rej.reasons
                    ),
                ),
                dhcp_row=name,
            )
    return None


def check_derived(
    baseline: Mapping[str, Any],
    proposed: Mapping[str, Any],
    *,
    artifact: str = "site",
    allowlist: tuple[str, ...] = EFFECTIVE_ALLOWLIST,
) -> Rejection | None:
    gap = check_derived_gap(
        baseline,
        proposed,
        artifact=artifact,
        allowlist=allowlist,
    )
    return gap.rejection if gap is not None else None
