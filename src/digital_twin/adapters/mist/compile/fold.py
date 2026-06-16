"""Ordered layer fold over the vendor stack (base -> winner), per-field policy.

The single merge primitive for every device family: switch and gateway pass
their own PolicyTable so a field can merge differently per family without a
refactor. REPLACE (default) replaces the field wholesale; DICT_MERGE merges a
keyed collection per key (later layer wins per key) so a higher layer that sets
one key does not wipe the others.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any

JsonObj = dict[str, Any]


class MergePolicy(StrEnum):
    REPLACE = "replace"
    DICT_MERGE = "dict_merge"


PolicyTable = Mapping[str, MergePolicy]


def fold_layers(layers: Sequence[JsonObj | None], policy: PolicyTable) -> JsonObj:
    out: JsonObj = {}
    for layer in layers:
        if layer is None:
            continue
        for field, value in layer.items():
            base = out.get(field)
            if (
                policy.get(field, MergePolicy.REPLACE) is MergePolicy.DICT_MERGE
                and isinstance(base, dict)
                and isinstance(value, dict)
            ):
                merged = dict(base)
                merged.update(copy.deepcopy(value))
                out[field] = merged
            else:
                out[field] = copy.deepcopy(value)
    return out
