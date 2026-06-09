"""Switch effective-config compilation (M1 chain).

merge_only:     networktemplate + site_setting (site wins), {{vars}} UNRESOLVED.
                This is the Tier-2 gate artifact — getSiteSettingDerived does NOT
                resolve vars (confirmed), so the oracle comparison uses this.
compile_site:   merge_only + {{vars}} resolved — the site-level live artifact
                (VLAN ids etc. usable by ingest).
compile_device: device config layered on the UNRESOLVED merge (per-key, device
                wins), THEN {{vars}} resolved once — device-level config can
                reference site vars; devices have no vars of their own.
                NO ORACLE exists for this layer (derived is site-level) —
                covered by unit tests only.
"""

from __future__ import annotations

import copy
from typing import Any

from .merge import merge_site_effective
from .vars import resolve_vars

JsonObj = dict[str, Any]

_DEVICE_DICT_MERGE_FIELDS = ("networks", "port_usages")
_DEVICE_OWN_FIELDS = ("port_config", "ip_config", "other_ip_configs")


def merge_only(networktemplate: JsonObj | None, site_setting: JsonObj) -> JsonObj:
    """Site-level merge with {{vars}} left intact (the oracle-comparison artifact)."""
    return merge_site_effective(networktemplate, site_setting)


def _resolve(effective: JsonObj) -> JsonObj:
    variables = effective.get("vars") or {}
    if not variables:
        return effective
    resolved: JsonObj = resolve_vars(effective, {str(k): str(v) for k, v in variables.items()})
    return resolved


def compile_site(networktemplate: JsonObj | None, site_setting: JsonObj) -> JsonObj:
    return _resolve(merge_only(networktemplate, site_setting))


def compile_device(
    networktemplate: JsonObj | None, site_setting: JsonObj, device: JsonObj
) -> JsonObj:
    """Per-device effective: unresolved site merge + device overlay, then vars once."""
    out = merge_only(networktemplate, site_setting)
    for field in _DEVICE_DICT_MERGE_FIELDS:
        dev_val = device.get(field)
        if isinstance(dev_val, dict):
            merged = dict(out.get(field) or {})
            merged.update(copy.deepcopy(dev_val))
            out[field] = merged
    for field in _DEVICE_OWN_FIELDS:
        if field in device:
            out[field] = copy.deepcopy(device[field])
    return _resolve(out)
