"""The M1 allowlist DATA (spec: 'Supported delta types — the honest-decision boundary').

Default-deny everywhere: anything not listed here is out of scope -> UNKNOWN.
`X.*` entries allow a whole named subtree (and the key itself, e.g. removing the
whole subtree); bare entries allow exactly that leaf.
"""

from __future__ import annotations

SUPPORTED_OBJECT_TYPES: tuple[str, ...] = ("site_setting", "device")

# Raw changed-path allowlist per object_type (post-fetch field gate).
# vars.* is allowed ONLY because the post-compile derived gate catches ripple.
RAW_ALLOWLIST: dict[str, tuple[str, ...]] = {
    "site_setting": ("networks.*", "port_usages.*", "vars.*"),
    "device": ("port_config.*", "networks.*", "port_usages.*", "name", "notes"),
}

# Server-managed fields excluded from the raw diff: a PUT payload never carries
# them, and their absence is not a user change.
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
)

# Effective-config fields the IR actually consumes (post-compile derived gate):
# any OTHER effective field differing between baseline and proposed -> UNKNOWN.
# vars is listed because it is the allowed input; its RIPPLE into any
# out-of-scope field (e.g. dhcpd_config) still trips the gate on that field.
EFFECTIVE_ALLOWLIST: tuple[str, ...] = (
    "networks",
    "port_usages",
    "vars",
    "port_config",
    "local_port_config",
    "port_config_overwrite",
)
