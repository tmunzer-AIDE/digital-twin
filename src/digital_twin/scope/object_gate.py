"""Pre-fetch policy gate: M1 object_type whitelist, action, source, single-site.

Runs before any state fetch (no state needed). Everything outside the M1
boundary is rejected LOUDLY with per-op reasons — never silently passed.
The post-fetch device-ROLE check (switch-only) cannot run here; it lives with
the field gate where the fetched device is available.
"""

from __future__ import annotations

from digital_twin.contracts import ChangePlan, Rejection
from digital_twin.scope.allowlist import NAC_OBJECT_TYPES, ORG_OBJECT_TYPES, SUPPORTED_OBJECT_TYPES

_STAGE = "object_gate"
_M1_SOURCE = "mist"
_M1_ACTION = "update"
_M1_SITE_DELETE_OBJECT_TYPES = ("wlan",)
_NAC_ACTIONS = ("create", "update", "delete")
_ORG_ACTIONS = ("update", "delete")


def check_objects(plan: ChangePlan) -> Rejection | None:
    reasons: list[str] = []
    if plan.source != _M1_SOURCE:
        reasons.append(f"unsupported source {plan.source!r} (M1 supports only 'mist')")
    ops = plan.ops
    # NAC mode: every op is a NAC type and there is no site_id.
    # Evaluated before is_org so NAC plans never fall into the site branch.
    is_nac = (
        bool(ops)
        and all(op.object_type in NAC_OBJECT_TYPES for op in ops)
        and not plan.scope.site_id
    )
    if is_nac:
        for op in ops:
            if op.action not in _NAC_ACTIONS:
                reasons.append(
                    f"ops[order={op.order}]: unsupported action {op.action!r} "
                    "(nac ops support 'create' | 'update' | 'delete')"
                )
            if op.action == "delete" and op.payload:
                reasons.append(
                    f"ops[order={op.order}]: delete payload must be empty "
                    "(a delete has no proposed object)"
                )
        return Rejection(stage=_STAGE, reasons=tuple(reasons)) if reasons else None
    # ORG mode ONLY when EVERY op is an ORG_OBJECT_TYPE AND there is no site_id.
    # Anything else (incl. an org type WITH a site_id, or a mix) falls into the
    # SITE branch, which preserves the existing per-op diagnostics verbatim.
    # ORG_OBJECT_TYPES drives this check; keep it in sync with allowlist.py.
    is_org = (
        bool(ops)
        and all(op.object_type in ORG_OBJECT_TYPES for op in ops)
        and not plan.scope.site_id
    )
    if is_org:
        for op in ops:
            if op.action not in _ORG_ACTIONS:
                reasons.append(
                    f"ops[order={op.order}]: unsupported action {op.action!r} "
                    "(org ops support 'update' | 'delete')"
                )
            if op.action == "delete" and op.payload:
                reasons.append(
                    f"ops[order={op.order}]: delete payload must be empty "
                    "(a delete has no proposed object)"
                )
        # duplicate (object_type, object_id) is NOT checked here — the envelope
        # (parse_change_plan) already rejects "two ops target the same object".
    else:  # SITE mode + everything else
        for op in ops:
            if op.action == "delete" and op.object_type in _M1_SITE_DELETE_OBJECT_TYPES:
                if op.payload:
                    reasons.append(
                        f"ops[order={op.order}]: delete payload must be empty "
                        "(a delete has no proposed object)"
                    )
            elif op.action != _M1_ACTION:
                reasons.append(
                    f"ops[order={op.order}]: unsupported action {op.action!r} "
                    "(M1 supports only 'update' plus site-local 'wlan' delete)"
                )
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
