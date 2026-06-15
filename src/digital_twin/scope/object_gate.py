"""Pre-fetch policy gate: M1 object_type whitelist, action, source, single-site.

Runs before any state fetch (no state needed). Everything outside the M1
boundary is rejected LOUDLY with per-op reasons — never silently passed.
The post-fetch device-ROLE check (switch-only) cannot run here; it lives with
the field gate where the fetched device is available.
"""

from __future__ import annotations

from digital_twin.contracts import ChangePlan, Rejection
from digital_twin.scope.allowlist import SUPPORTED_OBJECT_TYPES

_STAGE = "object_gate"
_M1_SOURCE = "mist"
_M1_ACTION = "update"


def check_objects(plan: ChangePlan) -> Rejection | None:
    reasons: list[str] = []
    if plan.source != _M1_SOURCE:
        reasons.append(f"unsupported source {plan.source!r} (M1 supports only 'mist')")
    ops = plan.ops
    # ORG mode ONLY when EVERY op is networktemplate AND there is no site_id.
    # Anything else (incl. networktemplate WITH a site_id, or a mix) falls into
    # the SITE branch, which preserves the existing per-op diagnostics verbatim.
    # NB: this keys on the literal "networktemplate"; a new ORG type must be added
    # BOTH here and to ORG_OBJECT_TYPES in allowlist.py (used by the CLI for mode).
    is_org = (
        bool(ops)
        and all(op.object_type == "networktemplate" for op in ops)
        and not plan.scope.site_id
    )
    for op in ops:
        if op.action != _M1_ACTION:
            reasons.append(
                f"ops[order={op.order}]: unsupported action {op.action!r} "
                "(M1 supports only 'update')"
            )
    if is_org:
        if len({op.object_id for op in ops}) > 1:
            reasons.append("one template per plan in M1 (multiple networktemplate ids)")
    else:  # SITE mode + everything else — UNCHANGED from today
        if not plan.scope.site_id:
            reasons.append("scope.site_id is required (M1 simulates exactly one site)")
        for op in ops:
            if op.object_type not in SUPPORTED_OBJECT_TYPES:
                reasons.append(
                    f"ops[order={op.order}]: unsupported object_type {op.object_type!r} "
                    "(templates/org objects fan out beyond one site; not modeled in M1)"
                )
            elif (
                op.object_type == "site_setting"
                and plan.scope.site_id
                and op.object_id != plan.scope.site_id
            ):
                reasons.append(
                    f"ops[order={op.order}]: site_setting object_id {op.object_id!r} "
                    f"!= scope.site_id {plan.scope.site_id!r} (cross-site fan-out)"
                )
    return Rejection(stage=_STAGE, reasons=tuple(reasons)) if reasons else None
