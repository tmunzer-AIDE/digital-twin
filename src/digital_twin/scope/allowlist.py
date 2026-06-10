"""The M1 allowlist DATA (spec: 'Supported delta types — the honest-decision boundary').

Default-deny everywhere, LEAF-TIGHTENED (spec wording): only the exact leaves the
IR actually models are in scope — networks carry 7 OAS leaves but the IR models
only vlan_id; port_usages carry 42 but the IR consumes only the four VLAN-
semantics attributes. Allowing a whole subtree would let an unmodeled change
(networks.*.isolation, port_usages.*.allow_dhcpd) simulate as falsely "in
scope". Entry syntax is scope.paths.matches: '*' = one key segment, trailing
'.*' = whole subtree, bare = exact leaf.
"""

from __future__ import annotations

SUPPORTED_OBJECT_TYPES: tuple[str, ...] = ("site_setting", "device")

# What the IR consumes from a port usage (ingest.ports.usage_vlans).
_MODELED_USAGE_ATTRS: tuple[str, ...] = ("mode", "port_network", "networks", "all_networks")

_NETWORK_LEAVES: tuple[str, ...] = ("networks.*.vlan_id",)
_USAGE_LEAVES: tuple[str, ...] = tuple(f"port_usages.*.{a}" for a in _MODELED_USAGE_ATTRS)
# Inline attrs the resolver honors (ingest.ports resolve_effective_ports), per map:
# port_config and local_port_config take usage + the usage-override attrs;
# port_config_overwrite is honored for port_network ONLY (_OVERWRITE_ATTRS).
_PORT_CONFIG_LEAVES: tuple[str, ...] = tuple(
    f"port_config.*.{a}" for a in ("usage", *_MODELED_USAGE_ATTRS)
)
_LOCAL_PORT_CONFIG_LEAVES: tuple[str, ...] = tuple(
    f"local_port_config.*.{a}" for a in ("usage", *_MODELED_USAGE_ATTRS)
)
_OVERWRITE_LEAVES: tuple[str, ...] = ("port_config_overwrite.*.port_network",)
_DEVICE_PORT_LEAVES: tuple[str, ...] = (
    *_PORT_CONFIG_LEAVES,
    *_LOCAL_PORT_CONFIG_LEAVES,
    *_OVERWRITE_LEAVES,
)

# Raw changed-path allowlist per object_type (post-fetch field gate).
# vars.* is a whole subtree ONLY because the post-compile derived gate catches
# its ripple into out-of-scope effective fields.
RAW_ALLOWLIST: dict[str, tuple[str, ...]] = {
    "site_setting": (*_NETWORK_LEAVES, *_USAGE_LEAVES, "vars.*"),
    "device": (*_NETWORK_LEAVES, *_USAGE_LEAVES, *_DEVICE_PORT_LEAVES, "name", "notes"),
}

# Server-managed fields excluded from the raw diff: a PUT payload never carries
# them, and their absence is not a user change. Two groups: identity/audit
# metadata, and GET-only device STATUS fields (live state, not config intent).
IGNORED_RAW_FIELDS: tuple[str, ...] = (
    "id",
    "org_id",
    "site_id",
    "created_time",
    "modified_time",
    "mac",
    "serial",
    "model",
    "type",
    # device status (GET-only):
    "adopted",
    "connected",
    "hw_rev",
    "heightSet",
    "mist_configured",
    "magic",
    "sku",
    "image1_url",
    "simplifiedName",
)

# Effective-config LEAVES the IR consumes (post-compile derived gate): any other
# effective leaf differing between baseline and proposed -> UNKNOWN. vars is the
# allowed input; its ripple into any out-of-scope leaf still trips the gate.
EFFECTIVE_ALLOWLIST: tuple[str, ...] = (
    *_NETWORK_LEAVES,
    *_USAGE_LEAVES,
    *_DEVICE_PORT_LEAVES,
    "vars.*",
)
