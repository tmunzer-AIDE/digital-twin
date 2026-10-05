"""apply_plan: ordered rolling full-object replacement (spec: Delta semantics).

Ops apply in strictly increasing `order` against a ROLLING raw state — op N sees
the state already modified by earlier ops. Static constraints (unique order, one
op per object) were checked by the envelope gate; they are re-checked here
cheaply (defense in depth — apply must be safe even if a future caller skips the
gates). Unknown target -> Rejection (errors are values).
"""

from __future__ import annotations

from collections.abc import Sequence

from digital_twin.contracts import ChangeOp, Rejection
from digital_twin.providers.base import RawSiteState

from .objects import create_object, delete_object, get_object, replace_object

_STAGE = "apply"


def apply_plan(raw: RawSiteState, ops: Sequence[ChangeOp]) -> RawSiteState | Rejection:
    orders = [op.order for op in ops]
    if len(set(orders)) != len(orders):
        return Rejection(stage=_STAGE, reasons=("duplicate op order values",))
    targets = [(op.object_type, op.object_id) for op in ops]
    if len(set(targets)) != len(targets):
        return Rejection(stage=_STAGE, reasons=("two ops target the same object",))

    state = raw
    for op in sorted(ops, key=lambda o: o.order):
        current = get_object(state, op.object_type, op.object_id)
        if op.action == "create":
            if current is not None:
                return Rejection(
                    stage=_STAGE,
                    reasons=(
                        f"ops[order={op.order}]: {op.object_type} with id "
                        f"{op.object_id!r} already exists",
                    ),
                )
            try:
                state = create_object(state, op.object_type, op.object_id, op.payload)
            except ValueError as e:
                return Rejection(stage=_STAGE, reasons=(f"ops[order={op.order}]: {e}",))
            continue
        if current is None:
            return Rejection(
                stage=_STAGE,
                reasons=(
                    f"ops[order={op.order}]: no {op.object_type} with id "
                    f"{op.object_id!r} in fetched state",
                ),
            )
        if op.action == "delete":
            try:
                state = delete_object(state, op.object_type, op.object_id)
            except ValueError as e:
                return Rejection(stage=_STAGE, reasons=(f"ops[order={op.order}]: {e}",))
        else:
            state = replace_object(state, op.object_type, op.object_id, op.payload)
    return state
