"""The M1 allowlist DATA (spec: 'Supported delta types — the honest-decision boundary').

Default-deny everywhere, LEAF-TIGHTENED (spec wording): only the exact leaves the
IR actually models are in scope — networks carry 7 OAS leaves but the IR models
only vlan_id; port_usages carry 42 but the IR consumes only the four VLAN-
semantics attributes. Allowing a whole subtree would let an unmodeled change
(networks.*.isolation) simulate as falsely "in scope". Entry syntax is
scope.paths.matches: '*' = exactly one key segment, '**' = one or more segments
(only for dict keys that contain literal dots, e.g. BGP neighbor IPs), trailing
'.*' = whole subtree, bare = exact leaf.
"""

from __future__ import annotations

SUPPORTED_OBJECT_TYPES: tuple[str, ...] = ("site_setting", "device", "wlan")

# Org-level object types simulated by fan-out (NOT single-site). networktemplate
# carries the SAME modeled config layer as a site_setting, so its raw field gate
# reuses the site_setting leaf tuple EXACTLY (switch_matching stays out -> UNKNOWN).
ORG_OBJECT_TYPES: tuple[str, ...] = ("networktemplate", "gatewaytemplate", "sitetemplate")

# Org-level NAC rules (GS34). SEPARATE from SUPPORTED_OBJECT_TYPES (the site
# whitelist, whose gate branch requires a site_id) and from ORG_OBJECT_TYPES
# (which drives the per-site fan-out). Routed by its own gate branch + driver
# predicate to simulate_org_nac.
NAC_OBJECT_TYPES: tuple[str, ...] = ("nacrule",)

_NAC_MATCH_DIMS: tuple[str, ...] = (
    "auth_type", "port_types", "nactags", "site_ids", "sitegroup_ids",
    "family", "mfg", "model", "os_type", "vendor",
)

# Wired-auth attrs (SP3) — modeled by wired.auth.access_change (policy-floor).
# OAS-present on port_usages + local_port_config only, so routing them through
# _MODELED_USAGE_ATTRS puts port_usages.* in scope for site/device/networktemplate
# and local_port_config.* in scope for device only — and nothing on
# port_config/port_config_overwrite (auth is absent from those maps).
_AUTH_ATTRS: tuple[str, ...] = (
    "port_auth", "enable_mac_auth", "mac_auth_only", "mac_auth_preferred",
    "mac_auth_protocol", "allow_multiple_supplicants", "dynamic_vlan_networks",
    "server_fail_network", "server_reject_network", "guest_network",
    "bypass_auth_when_server_down", "bypass_auth_when_server_down_for_unknown_client",
    "persist_mac", "reauth_interval",
)
# What the IR consumes from a port usage: VLAN semantics (ingest.ports.usage_vlans)
# + `poe_disabled` (ingest populates Port.poe; the poe.disconnect check reasons
# about cutting power to a powered device).
_MODELED_USAGE_ATTRS: tuple[str, ...] = (
    "mode",
    "port_network",
    "networks",
    "all_networks",
    "voip_network",
    "poe_disabled",
    "mtu",
    "mac_limit",
    "allow_dhcpd",
    "speed",
    "duplex",
    "disable_autoneg",
    *_AUTH_ATTRS,
)
# Dynamic-profile machinery the runtime-usage resolver consumes
# (ingest.dynamic_usage): `rules` evaluated against observed LLDP (lists diff
# atomically, so it is a single leaf) and `reset_default_when` (down-port
# semantics). These live on port_usages ONLY — inline port_config rules are
# not a thing the resolver honors.
_DYNAMIC_PROFILE_ATTRS: tuple[str, ...] = ("rules", "reset_default_when")
# STP config the IR models (Port.stp_edge / Port.bpdu_filter; the
# wired.stp checks). On port_usages; inline ONLY stp_edge on
# local_port_config (schema) — hence not in _MODELED_USAGE_ATTRS.
_STP_USAGE_ATTRS: tuple[str, ...] = ("stp_edge", "stp_disable")

# vlan_id = the L2 fact; subnet/gateway = ROUTED intent (Vlan.subnet feeds the
# wired.l3.gateway_gap check).
_NETWORK_LEAVES: tuple[str, ...] = (
    "networks.*.vlan_id",
    "networks.*.subnet",
    "networks.*.gateway",
)
# Switch IRB facts the IR ingests (other_ip_configs -> L3Intf): existence +
# addressing. Other leaves (evpn flags, dhcp relay knobs) stay denied.
_IRB_LEAVES: tuple[str, ...] = (
    "other_ip_configs.*.type",
    "other_ip_configs.*.ip",
    "other_ip_configs.*.netmask",
)
# Site-level DHCP path facts (Vlan.dhcp_sources, the wired.dhcp.path check)
# plus the scope range/gateway facts that feed IR.dhcp_scopes (the
# wired.dhcp.scope_lint check): type decides serve/relay/none; servers decide
# whether a relay goes anywhere; ip_start/ip_end/gateway bound the scope.
# SITE_SETTING ONLY — device-level switch dhcpd_config is unmodeled (the
# compiler does not carry it; allowlisting it would be a false-SAFE shape).
_DHCP_LEAVES: tuple[str, ...] = (
    "dhcpd_config.*.type",
    "dhcpd_config.*.servers",
    "dhcpd_config.*.ip_start",
    "dhcpd_config.*.ip_end",
    "dhcpd_config.*.gateway",
)
# BGP peering the IR models AND acts on (GS28 wired.l3.bgp_adjacency): per-neighbor
# AS + admin-state (the break signals), session local_as + type. EVERYTHING else
# (auth_key=secret, networks=advertised prefixes [no v1 check], timers, policies,
# bfd, multihop) stays DENIED -> UNKNOWN: allowlisting a leaf no check reasons
# about is a false-SAFE.
_BGP_LEAVES: tuple[str, ...] = (
    "bgp_config.*.local_as",
    "bgp_config.*.type",
    "bgp_config.*.neighbors.**.neighbor_as",
    "bgp_config.*.neighbors.**.disabled",
)
# Gateway BGP adds the transport selector `via` (lan|tunnel|vpn|wan); switches are
# implicitly LAN and have no via.
_BGP_GATEWAY_LEAVES: tuple[str, ...] = (*_BGP_LEAVES, "bgp_config.*.via")

# Gateway modeled effective leaves: exactly what _gateway_ports_and_l3 + gateway
# dhcp consume AND act on. NOT port_config.*.usage (inert -> Port.profile), NOT
# networks (gateway namespace is org_networks, not the device's own networks).
_GATEWAY_PORT_LEAVES: tuple[str, ...] = (
    "port_config.*.networks",
    "port_config.*.port_network",
    "port_config.*.disabled",
)
_GATEWAY_L3_LEAVES: tuple[str, ...] = ("ip_configs.*.ip",)
_GATEWAY_DHCP_LEAVES: tuple[str, ...] = (
    "dhcpd_config.*.type",
    "dhcpd_config.*.servers",
    "dhcpd_config.*.ip_start",
    "dhcpd_config.*.ip_end",
    "dhcpd_config.*.gateway",
)
_GATEWAY_LEAVES: tuple[str, ...] = (
    *_GATEWAY_PORT_LEAVES, *_GATEWAY_L3_LEAVES, *_GATEWAY_DHCP_LEAVES, *_BGP_GATEWAY_LEAVES,
)

# Snooping intent (Device.dhcp_snooping, the wired.dhcp.snooping check):
# the toggle, the all-networks switch, and the per-network list — atomically.
_SNOOPING_LEAVES: tuple[str, ...] = (
    "dhcp_snooping.enabled",
    "dhcp_snooping.all_networks",
    "dhcp_snooping.networks",
)
# OSPF participation the IR models AND acts on (GS26 wired.l3.ospf_withdrawal):
# the master enable (disable = full collapse) + the per-network passive flag
# (active vs adjacency-bearing). EVERYTHING else (metric, area type, auth,
# timers, interface_type) stays DENIED -> UNKNOWN: GS27 owns those mutations,
# and allowlisting a leaf no check reasons about would be a false-SAFE.
_OSPF_LEAVES: tuple[str, ...] = (
    "ospf_config.enabled",
    "ospf_areas.*.networks.*.passive",
    "ospf_areas.*.networks.*.metric",
)
_USAGE_LEAVES: tuple[str, ...] = tuple(
    f"port_usages.*.{a}"
    for a in (*_MODELED_USAGE_ATTRS, *_DYNAMIC_PROFILE_ATTRS, *_STP_USAGE_ATTRS)
)
# Device/site-level STP: bridge priority feeds Device.stp_priority (root
# election, wired.stp.root_change).
_STP_CONFIG_LEAVES: tuple[str, ...] = ("stp_config.bridge_priority",)
# Inline attrs the resolver honors (ingest.ports resolve_effective_ports), per map.
# Narrowed to match the refreshed (closed, additionalProperties:false) device_switch
# OAS, which documents these usage-override attrs on DIFFERENT inline maps:
#   - port_config: usage + dynamic_usage + port_network/networks/poe_disabled/mtu
#     (NOT mode/all_networks/allow_dhcpd — those live on local_port_config/port_usages)
#   - local_port_config: the full usage-override set + stp_edge, but NOT dynamic_usage
#     (dynamic_usage is a port_config-only runtime-profile pointer)
# port_config_overwrite is honored for port_network + poe_disabled ONLY.
_PORT_CONFIG_ATTRS: tuple[str, ...] = (
    "usage", "dynamic_usage", "port_network", "networks", "poe_disabled", "mtu",
    "speed", "duplex", "disable_autoneg",
)
_PORT_CONFIG_LEAVES: tuple[str, ...] = tuple(f"port_config.*.{a}" for a in _PORT_CONFIG_ATTRS)
_LOCAL_PORT_CONFIG_LEAVES: tuple[str, ...] = tuple(
    f"local_port_config.*.{a}" for a in ("usage", "stp_edge", "disabled", *_MODELED_USAGE_ATTRS)
)
_OVERWRITE_LEAVES: tuple[str, ...] = (
    "port_config_overwrite.*.port_network",
    "port_config_overwrite.*.poe_disabled",
    "port_config_overwrite.*.disabled",
    "port_config_overwrite.*.speed",
    "port_config_overwrite.*.duplex",
    "port_config_overwrite.*.mac_limit",
)
_DEVICE_PORT_LEAVES: tuple[str, ...] = (
    *_PORT_CONFIG_LEAVES,
    *_LOCAL_PORT_CONFIG_LEAVES,
    *_OVERWRITE_LEAVES,
)

# Raw changed-path allowlist per object_type (post-fetch field gate).
# vars.* is a whole subtree ONLY because the post-compile derived gate catches
# its ripple into out-of-scope effective fields.
RAW_ALLOWLIST: dict[str, tuple[str, ...]] = {
    "site_setting": (
        *_NETWORK_LEAVES,
        *_USAGE_LEAVES,
        *_STP_CONFIG_LEAVES,
        *_DHCP_LEAVES,
        *_SNOOPING_LEAVES,
        *_OSPF_LEAVES,
        *_BGP_LEAVES,
        "vars.*",
    ),
    "device": (
        *_NETWORK_LEAVES,
        *_USAGE_LEAVES,
        *_DEVICE_PORT_LEAVES,
        *_STP_CONFIG_LEAVES,
        *_IRB_LEAVES,
        *_SNOOPING_LEAVES,
        *_OSPF_LEAVES,
        *_BGP_LEAVES,
        "name",
        "notes",
    ),
}

# Modeled WLAN leaves (exactly what _mint_wlan consumes). ap_ids/wxtag_ids are
# atomic list leaves (NOT ap_ids.* — the path flattener treats lists atomically).
_WLAN_LEAVES: tuple[str, ...] = (
    "ssid", "enabled", "auth.type", "isolation", "l2_isolation",
    "apply_to", "ap_ids", "wxtag_ids",
)
RAW_ALLOWLIST["wlan"] = _WLAN_LEAVES

# nacrule leaves — exact, leaf-tightened (no matching.* subtree). List values are
# atomic leaves (the path flattener treats lists atomically, as with ap_ids).
# id/org_id/created_time/modified_time are dropped by IGNORED_RAW_FIELDS.
RAW_ALLOWLIST["nacrule"] = (
    "name", "order", "enabled", "action", "apply_tags",
    *(f"matching.{d}" for d in _NAC_MATCH_DIMS),
    *(f"not_matching.{d}" for d in _NAC_MATCH_DIMS),
)

RAW_ALLOWLIST["networktemplate"] = RAW_ALLOWLIST["site_setting"]
# vars.* is allowlisted (like site_setting/networktemplate) so a gatewaytemplate
# vars edit passes the RAW field gate and the derived gate evaluates its ripple.
RAW_ALLOWLIST["gatewaytemplate"] = (*_GATEWAY_LEAVES, "vars.*")
# sitetemplate sits in BOTH stacks -> union of switch/site leaves + gateway leaves.
# Verified against the committed sitetemplate OAS in a later task (narrow only if
# the schema proves a leaf cannot appear).
RAW_ALLOWLIST["sitetemplate"] = (*RAW_ALLOWLIST["site_setting"], *_GATEWAY_LEAVES, "vars.*")

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
    *_STP_CONFIG_LEAVES,
    *_IRB_LEAVES,
    *_DHCP_LEAVES,
    *_SNOOPING_LEAVES,
    *_OSPF_LEAVES,
    *_BGP_LEAVES,
    "vars.*",
)

# Gateway effective allowlist (role-keyed derived gate): the gateway modeled leaves
# + vars.* (the vars root survives _resolve; the derived gate catches its ripple,
# so the vars.* leaf itself must be allowed).
GATEWAY_EFFECTIVE_ALLOWLIST: tuple[str, ...] = (*_GATEWAY_LEAVES, "vars.*")

# Modeled leaves a device-profile (higher precedence, unmodeled layer) can
# override, per role. EXACTLY the leaves the IR consumes for that role (so the
# gate cannot disagree with ingest): gateway = the modeled gateway leaves;
# switch = the modeled switch leaves.
DEVICE_PROFILE_OVERRIDABLE_LEAVES_BY_ROLE: dict[str, tuple[str, ...]] = {
    "gateway": (*_GATEWAY_LEAVES,),
    # The FULL modeled switch surface (= EFFECTIVE_ALLOWLIST minus vars.*). The
    # device-profile is an UNMODELED layer that wins over the template/site layers,
    # so it could override ANY modeled leaf — under-listing one (stp_config /
    # dhcp_snooping / ospf / other_ip_configs, which device profiles DO carry per the
    # device_switch OAS) is a false-SAFE: a below-profile edit to it on a profiled
    # switch would resolve SAFE/REVIEW instead of UNKNOWN. Fail-safe = list every
    # modeled leaf (over-tainting to UNKNOWN is acceptable; false-SAFE is not).
    "switch": (
        *_NETWORK_LEAVES, *_USAGE_LEAVES, *_DEVICE_PORT_LEAVES, *_STP_CONFIG_LEAVES,
        *_IRB_LEAVES, *_DHCP_LEAVES, *_SNOOPING_LEAVES, *_OSPF_LEAVES, *_BGP_LEAVES,
    ),
}
