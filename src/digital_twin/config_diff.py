"""Assemble a redacted before→after ObjectConfigDiff for one changed object.
Pure: diffs raw leaves (scope.paths.leaf_changes), redacts each value for display
(redaction.redact_leaf), never read back into the verdict/decision."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from digital_twin.contracts import FieldChange, ObjectConfigDiff
from digital_twin.redaction import redact_leaf
from digital_twin.scope.allowlist import IGNORED_RAW_FIELDS
from digital_twin.scope.paths import leaf_changes


def object_config_diff(
    *,
    object_type: str,
    object_id: str,
    name: str | None,
    action: str,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
) -> ObjectConfigDiff:
    deltas = leaf_changes(before or {}, after or {}, ignore_top=IGNORED_RAW_FIELDS)
    changes = tuple(
        FieldChange(
            path=d.path,
            kind=d.kind,
            before=redact_leaf(d.path, d.before),  # FULL path → any-segment STRIP (P1)
            after=redact_leaf(d.path, d.after),
        )
        for d in deltas
    )
    return ObjectConfigDiff(object_type, object_id, name, action, changes)
