"""Site-level merge: <type>template (base) + sitetemplate + site_setting (wins).

Reimplemented on fold_layers. The 2-arg merge_site_effective(nt, ss) signature
is preserved (existing callers + the offline Tier-2 equivalence gate); the
optional sitetemplate layer folds between the template and site_setting.
"""

from __future__ import annotations

from typing import Any

from .fold import MergePolicy, PolicyTable, fold_layers

JsonObj = dict[str, Any]

# Keyed collections merged per key (later layer wins per key). Everything else
# REPLACE. GATEWAY_POLICY adds the gateway keyed maps (Phase 3).
SWITCH_POLICY: PolicyTable = {
    "networks": MergePolicy.DICT_MERGE,
    "port_usages": MergePolicy.DICT_MERGE,
    "vars": MergePolicy.DICT_MERGE,
    "dhcpd_config": MergePolicy.DICT_MERGE,
    "switch_matching": MergePolicy.REPLACE,
}


def merge_site_effective(
    networktemplate: JsonObj | None,
    site_setting: JsonObj,
    *,
    sitetemplate: JsonObj | None = None,
) -> JsonObj:
    """Full effective SITE config (all fields). nt (base) -> sitetemplate ->
    site_setting (wins)."""
    return fold_layers([networktemplate, sitetemplate, site_setting], SWITCH_POLICY)
