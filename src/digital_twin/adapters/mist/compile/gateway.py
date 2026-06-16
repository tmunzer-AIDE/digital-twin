"""Gateway device effective config: fold the gateway stack, overlay the device
PER-KEY (NOT root-replace), then resolve {{vars}} LAST (matching compile_device).

GATEWAY_POLICY DICT_MERGEs the keyed maps port_config/ip_configs/dhcpd_config so a
higher layer (and the device overlay) that sets one port/scope does not wipe the
rest. The device overlay is just another fold layer with the SAME policy — so the
keyed maps merge per key and device-own roots REPLACE. (Do NOT use
`effective_update`: it is a root-level merge — `device["port_config"]` would
replace the inherited `port_config` wholesale, wiping template ports.) The proposed
edit's `{"-attr":""}` delete-markers are applied via apply_template BEFORE this
fold, so the device overlay needs no marker handling. Exact Mist layering for these
maps is Tier-2 live-verified (starting from DICT_MERGE).
"""

from __future__ import annotations

from typing import Any

from .fold import MergePolicy, PolicyTable, fold_layers
from .switch import _resolve  # vars substitution, resolve-last

JsonObj = dict[str, Any]

GATEWAY_POLICY: PolicyTable = {
    "networks": MergePolicy.DICT_MERGE,
    "vars": MergePolicy.DICT_MERGE,
    "dhcpd_config": MergePolicy.DICT_MERGE,
    "port_config": MergePolicy.DICT_MERGE,
    "ip_configs": MergePolicy.DICT_MERGE,
}

# switch/site-namespace keys a gateway does NOT inherit from the SITE-level layers
# (sitetemplate/site_setting): gateway networks = org_networks; gateway dhcpd_config
# comes from the gatewaytemplate + the gateway device. (Mirrors how `networks` is
# already excluded from the gateway namespace.)
_SITE_NAMESPACE_KEYS = ("networks", "dhcpd_config")


def _gateway_site_layer(layer: JsonObj | None) -> JsonObj | None:
    if layer is None:
        return None
    return {k: v for k, v in layer.items() if k not in _SITE_NAMESPACE_KEYS}


def compile_gateway_device(
    gatewaytemplate: JsonObj | None,
    sitetemplate: JsonObj | None,
    site_setting: JsonObj,
    device: JsonObj,
) -> JsonObj:
    site_effective = fold_layers(
        [gatewaytemplate, _gateway_site_layer(sitetemplate), _gateway_site_layer(site_setting)],
        GATEWAY_POLICY,
    )
    # device overlay = one more fold layer under the same policy: keyed maps merge
    # per key (device port adds, template ports survive), device-own roots replace.
    overlaid = fold_layers([site_effective, device], GATEWAY_POLICY)
    return _resolve(overlaid)
