"""Switch effective-config compilation (M1 chain).

merge_only:     networktemplate + site_setting (site wins), {{vars}} UNRESOLVED.
                This is the Tier-2 gate artifact — getSiteSettingDerived does NOT
                resolve vars (confirmed), so the oracle comparison uses this.
compile_site:   merge_only + {{vars}} resolved — the site-level live artifact
                (VLAN ids etc. usable by ingest).
compile_device: device config layered on the UNRESOLVED merge (per-key, device
                wins), THEN {{vars}} resolved once — device-level config can
                reference site vars; devices have no vars of their own.
                Template `switch_matching` rules supply the BASE port_config
                (see switch_matching.py); the device's own port_config overlays
                it per-port. NO site oracle exists for this layer (derived is
                site-level); the live gate's port-usage cross-check (compiled vs
                observed) validates the projection, alongside unit tests.

KNOWN LIMITS (M1): `dynamic_usage` ports keep their STATIC usage — their runtime
usage (driven by the connected device) is intentionally not modeled. Ports left
unassigned by every rule + the device fall to Mist's implicit `default` usage;
those are not synthesized here (no static port inventory, and they carry no VLAN).
"""

from __future__ import annotations

import copy
from typing import Any

from .merge import merge_site_effective
from .switch_matching import resolve_switch_matching
from .vars import resolve_vars

JsonObj = dict[str, Any]

# Device keyed-collection fields: device overlays the inherited map PER KEY (a
# device defining one port range must not wipe the rest). port_config and its
# overrides are keyed by port/range; local_port_config + port_config_overwrite
# layer on top at resolution time (see ingest.ports.resolve_effective_ports).
_DEVICE_DICT_MERGE_FIELDS = (
    "networks",
    "port_usages",
    "port_config",
    "local_port_config",
    "port_config_overwrite",
)
# Device whole-field overrides (not keyed port maps): device value wins wholesale.
_DEVICE_OWN_FIELDS = (
    "ip_config",
    "other_ip_configs",
    "stp_config",
    "dhcp_snooping",
    "ospf_config",
    "ospf_areas",
)


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
    """Per-device effective: unresolved site merge + device overlay, then vars once.

    switch_matching (template rules) provides the device's BASE port_config (first
    matching rule wins); the device's own port_config then overlays it per-port via
    the per-key DICT_MERGE below.
    """
    out = merge_only(networktemplate, site_setting)
    base_port_config = resolve_switch_matching(out.get("switch_matching"), device)
    if base_port_config:
        out["port_config"] = base_port_config
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
