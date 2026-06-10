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
from typing import Any

from digital_twin.contracts import Rejection
from digital_twin.scope.allowlist import EFFECTIVE_ALLOWLIST
from digital_twin.scope.paths import allowed, changed_leaf_paths

_STAGE = "derived_gate"


def changed_effective_paths(
    baseline: Mapping[str, Any], proposed: Mapping[str, Any]
) -> tuple[str, ...]:
    return changed_leaf_paths(baseline, proposed)


def check_derived(
    baseline: Mapping[str, Any], proposed: Mapping[str, Any], *, artifact: str = "site"
) -> Rejection | None:
    offending = [
        path
        for path in changed_effective_paths(baseline, proposed)
        if not allowed(path, EFFECTIVE_ALLOWLIST)
    ]
    if offending:
        return Rejection(
            stage=_STAGE,
            reasons=tuple(
                f"{path}: out-of-scope EFFECTIVE leaf differs in {artifact} config "
                "(change ripples beyond the M1 model)"
                for path in offending
            ),
        )
    return None
