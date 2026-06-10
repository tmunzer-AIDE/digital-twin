"""Post-compile derived-impact gate: diff the FULL effective configs.

The IR is a projection of in-scope fields only, so an out-of-scope effective
change (e.g. a vars edit rippling into dhcpd_config) NEVER enters the IR and
IRDiff cannot see it. This gate diffs the compiler's full effective output
(baseline vs proposed) — site effective AND each device effective — and rejects
if any field OUTSIDE the effective allowlist differs. Both sides come from the
identical compiler code path, so plain equality is sound (no normalization
needed — same code, same shapes).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from digital_twin.contracts import Rejection
from digital_twin.scope.allowlist import EFFECTIVE_ALLOWLIST

_STAGE = "derived_gate"


def changed_effective_fields(
    baseline: Mapping[str, Any], proposed: Mapping[str, Any]
) -> tuple[str, ...]:
    keys = set(baseline) | set(proposed)
    return tuple(sorted(k for k in keys if baseline.get(k) != proposed.get(k)))


def check_derived(
    baseline: Mapping[str, Any], proposed: Mapping[str, Any], *, artifact: str = "site"
) -> Rejection | None:
    offending = [
        field
        for field in changed_effective_fields(baseline, proposed)
        if field not in EFFECTIVE_ALLOWLIST
    ]
    if offending:
        return Rejection(
            stage=_STAGE,
            reasons=tuple(
                f"{field}: out-of-scope EFFECTIVE field differs in {artifact} config "
                "(change ripples beyond the M1 model)"
                for field in offending
            ),
        )
    return None
