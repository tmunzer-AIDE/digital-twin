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

from .objects import get_object, replace_object

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
        if get_object(state, op.object_type, op.object_id) is None:
            return Rejection(
                stage=_STAGE,
                reasons=(
                    f"ops[order={op.order}]: no {op.object_type} with id "
                    f"{op.object_id!r} in fetched state",
                ),
            )
        state = replace_object(state, op.object_type, op.object_id, op.payload)
    return state
