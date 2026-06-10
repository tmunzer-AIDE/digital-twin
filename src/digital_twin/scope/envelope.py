"""Envelope (shape) validation: dict in -> ChangePlan value or Rejection.

SHAPE only — vendor-neutral structural rules incl. the two static multi-op
constraints from the spec's Delta semantics (unique `order`; one op per
(object_type, object_id), because a full-object-replacement plan with two ops
on one object makes the earlier op dead — an authoring error). M1 *policy*
(which types/actions are supported) lives in object_gate, not here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from digital_twin.contracts import ChangeOp, ChangePlan, ChangeScope, Rejection

_STAGE = "envelope"


def parse_change_plan(data: Mapping[str, Any]) -> ChangePlan | Rejection:
    reasons: list[str] = []

    source = data.get("source")
    if not isinstance(source, str) or not source:
        reasons.append("source must be a non-empty string")

    scope_raw = data.get("scope")
    org_id, site_id = "", None
    if (
        not isinstance(scope_raw, Mapping)
        or not isinstance(scope_raw.get("org_id"), str)
        or not scope_raw.get("org_id")
    ):
        reasons.append("scope.org_id must be a non-empty string")
    else:
        org_id = str(scope_raw["org_id"])
        sid = scope_raw.get("site_id")
        if sid is not None and (not isinstance(sid, str) or not sid):
            reasons.append("scope.site_id must be a non-empty string when present")
        else:
            site_id = sid

    intent = data.get("intent")
    if intent is not None and not isinstance(intent, str):
        reasons.append("intent must be a string when present")

    ops_raw = data.get("ops")
    ops: list[ChangeOp] = []
    if not isinstance(ops_raw, list) or not ops_raw:
        reasons.append("ops must be a non-empty list")
    else:
        for i, op in enumerate(ops_raw):
            parsed = _parse_op(op, i, reasons)
            if parsed is not None:
                ops.append(parsed)

    if isinstance(ops_raw, list) and len(ops) == len(ops_raw):
        # cross-op checks only when every op parsed (else reasons already explain)
        orders = [op.order for op in ops]
        if len(set(orders)) != len(orders):
            reasons.append("op order values must be unique (duplicate order)")
        targets = [(op.object_type, op.object_id) for op in ops]
        if len(set(targets)) != len(targets):
            reasons.append(
                "two ops target the same object (full replacement makes the earlier op dead)"
            )

    if reasons:
        return Rejection(stage=_STAGE, reasons=tuple(reasons))
    return ChangePlan(
        source=str(source),
        scope=ChangeScope(org_id=org_id, site_id=site_id),
        ops=tuple(ops),
        intent=intent,
    )


def _parse_op(op: Any, index: int, reasons: list[str]) -> ChangeOp | None:
    if not isinstance(op, Mapping):
        reasons.append(f"ops[{index}] must be an object")
        return None
    action, order = op.get("action"), op.get("order")
    object_type, object_id, payload = (
        op.get("object_type"),
        op.get("object_id"),
        op.get("payload"),
    )
    if (
        isinstance(action, str)
        and action
        and isinstance(order, int)
        and not isinstance(order, bool)
        and isinstance(object_type, str)
        and object_type
        and isinstance(object_id, str)
        and object_id
        and isinstance(payload, Mapping)
    ):
        return ChangeOp(
            action=action,
            order=order,
            object_type=object_type,
            object_id=object_id,
            payload=dict(payload),
        )
    problems = [
        name
        for name, ok in (
            ("action", isinstance(action, str) and bool(action)),
            ("order (int)", isinstance(order, int) and not isinstance(order, bool)),
            ("object_type", isinstance(object_type, str) and bool(object_type)),
            ("object_id", isinstance(object_id, str) and bool(object_id)),
            ("payload (object)", isinstance(payload, Mapping)),
        )
        if not ok
    ]
    reasons.append(f"ops[{index}] invalid fields: {', '.join(problems)}")
    return None
