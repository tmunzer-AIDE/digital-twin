"""Site-level merge: networktemplate (base) + site_setting (wins), per-field policy.

The policy table is DATA. Default is REPLACE (site value replaces the field
wholesale) — conservative and Mist-like. DICT_MERGE fields merge per key with the
site winning per key. The Tier-1 OAS tests assert precedence for every schema leaf;
the Tier-2 live gate hardens this table against Mist's real derivation.
"""

from __future__ import annotations

import copy
from enum import StrEnum
from typing import Any

JsonObj = dict[str, Any]


class MergePolicy(StrEnum):
    REPLACE = "replace"
    DICT_MERGE = "dict_merge"

    @classmethod
    def for_field(cls, field: str) -> MergePolicy:
        return _POLICY.get(field, cls.REPLACE)


# Fields whose values are keyed collections merged per key (site wins per key).
# Everything else: REPLACE. Grow/adjust as the Tier-2 gate uncovers divergences.
_POLICY: dict[str, MergePolicy] = {
    "networks": MergePolicy.DICT_MERGE,
    "port_usages": MergePolicy.DICT_MERGE,
    "vars": MergePolicy.DICT_MERGE,
    "dhcpd_config": MergePolicy.DICT_MERGE,
    "switch_matching": MergePolicy.REPLACE,
}


def merge_site_effective(networktemplate: JsonObj | None, site_setting: JsonObj) -> JsonObj:
    """Full effective SITE config (all fields, including out-of-scope ones)."""
    out: JsonObj = copy.deepcopy(dict(networktemplate or {}))
    for field, site_value in site_setting.items():
        policy = MergePolicy.for_field(field)
        base_value = out.get(field)
        if (
            policy is MergePolicy.DICT_MERGE
            and isinstance(base_value, dict)
            and isinstance(site_value, dict)
        ):
            merged = dict(base_value)
            merged.update(copy.deepcopy(site_value))
            out[field] = merged
        else:
            out[field] = copy.deepcopy(site_value)
    return out
