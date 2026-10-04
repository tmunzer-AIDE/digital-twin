"""WLAN attribute policy: the explicit boundary between SAFE and usage-gated edits.

Every writable attribute in the committed Mist WLAN schema belongs to exactly
one of three groups:

* ``ALWAYS_SAFE`` — operationally inert or performance-policy changes;
* ``BAND`` — evaluated by the band-specific transition policy; or
* ``USAGE_GATED`` — client-affecting changes which require REVIEW when the WLAN
  had sessions during the preceding seven days.

Server-owned/output-only attributes are deliberately absent. Shared server-
metadata handling ignores identity/audit roots, while the raw field gate rejects
the remaining generated outputs rather than approving values the WLAN API does
not own.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from digital_twin.scope.paths import changed_leaf_paths, matches

# Band steering is an explicit product-policy SAFE operation.  The notification
# and SLE toggles do not alter forwarding or client admission.  Performance
# controls may change service quality, but are explicitly outside the approval
# risk policy: they can be set or updated without review.
WLAN_ALWAYS_SAFE_PATHS: tuple[str, ...] = (
    "band_steer",
    "band_steer_force_band5",
    "disable_v1_roam_notify",
    "disable_v2_roam_notify",
    "sle_excluded",
    "app_limit.*",
    "app_qos.*",
    "qos.*",
    "client_limit_down",
    "client_limit_down_enabled",
    "client_limit_up",
    "client_limit_up_enabled",
    "wlan_limit_down",
    "wlan_limit_down_enabled",
    "wlan_limit_up",
    "wlan_limit_up_enabled",
)

WLAN_BAND_PATHS: tuple[str, ...] = ("band", "bands")


# A trailing .* owns the complete documented object subtree.  Lists are atomic
# in changed_leaf_paths(), so list-valued fields are listed by their root name.
WLAN_USAGE_GATED_PATHS: tuple[str, ...] = (
    # Availability and AP scope.
    "enabled",
    "ssid",
    "apply_to",
    "ap_ids",
    "wxtag_ids",
    "schedule.*",
    # Authentication, authorization, accounting, and external identity systems.
    "acct_immediate_update",
    "acct_interim_interval",
    "acct_servers",
    "airwatch.*",
    "auth.*",
    "auth_server_selection",
    "auth_servers",
    "auth_servers_nas_id",
    "auth_servers_nas_ip",
    "auth_servers_retries",
    "auth_servers_timeout",
    "cisco_cwa.*",
    "coa_servers",
    "disable_message_authenticator_check",
    "dynamic_psk.*",
    "enable_local_keycaching",
    "fast_dot1x_timers",
    "mist_nac.*",
    "radsec.*",
    "use_eapol_v1",
    # RF protocol, discovery, roaming, and client compatibility.
    "disable_11ax",
    "disable_11be",
    "disable_ht_vht_rates",
    "disable_uapsd",
    "disable_wmm",
    "dtim",
    "hide_ssid",
    "hostname_ie",
    "legacy_overds",
    "limit_probe_response",
    "max_idletime",
    "max_num_clients",
    "rateset.*",
    "reconnect_clients_when_roaming_mxcluster",
    "roam_mode",
    # Client forwarding and security behavior.
    "allow_ipv6_ndp",
    "allow_mdns",
    "allow_ssdp",
    "arp_filter",
    "block_blacklist_clients",
    "bonjour.*",
    "disable_when_gateway_unreachable",
    "disable_when_mxtunnel_down",
    "dns_server_rewrite.*",
    "enable_wireless_bridging",
    "enable_wireless_bridging_dhcp_tracking",
    "inject_dhcp_option_82.*",
    "isolation",
    "l2_isolation",
    "limit_bcast",
    "no_static_dns",
    "no_static_ip",
    # VLAN assignment and forwarding path.
    "dynamic_vlan.*",
    "interface",
    "mxtunnel_id",
    "mxtunnel_ids",
    "mxtunnel_name",
    "vlan_enabled",
    "vlan_id",
    "vlan_ids",
    "vlan_pooling",
    "wxtunnel_id",
    "wxtunnel_remote_id",
    # Guest admission and Passpoint behavior.  Auto-generated portal outputs
    # (portal_api_secret, portal_sso_url, portal_template_url, thumbnail) are
    # intentionally not writable here.
    "hotspot20.*",
    "portal.*",
    "portal_allowed_hostnames",
    "portal_allowed_subnets",
    "portal_denied_hostnames",
    "portal_image",
)

WLAN_POLICY_ALLOWLIST: tuple[str, ...] = (
    *WLAN_ALWAYS_SAFE_PATHS,
    *WLAN_BAND_PATHS,
    *WLAN_USAGE_GATED_PATHS,
)


@dataclass(frozen=True)
class WlanPolicyDelta:
    """Changed WLAN leaves partitioned by their owning policy."""

    always_safe: tuple[str, ...]
    bands: tuple[str, ...]
    usage_gated: tuple[str, ...]


def _owned(path: str, patterns: tuple[str, ...]) -> bool:
    return any(matches(path, pattern) for pattern in patterns)


def classify_wlan_delta(
    current: Mapping[str, Any], proposed: Mapping[str, Any]
) -> WlanPolicyDelta:
    changed = changed_leaf_paths(current, proposed)
    return WlanPolicyDelta(
        always_safe=tuple(path for path in changed if _owned(path, WLAN_ALWAYS_SAFE_PATHS)),
        bands=tuple(path for path in changed if _owned(path, WLAN_BAND_PATHS)),
        usage_gated=tuple(path for path in changed if _owned(path, WLAN_USAGE_GATED_PATHS)),
    )


def secure_to_open(current: Mapping[str, Any], proposed: Mapping[str, Any]) -> bool:
    current_auth = current.get("auth")
    proposed_auth = proposed.get("auth")
    before = current_auth.get("type") if isinstance(current_auth, Mapping) else None
    after = proposed_auth.get("type") if isinstance(proposed_auth, Mapping) else None
    return before not in (None, "open") and after == "open"


def usage_gated_paths_for_update(
    current: Mapping[str, Any], proposed: Mapping[str, Any]
) -> tuple[str, ...]:
    """Return client-affecting leaves not already owned by a stronger rule.

    Secured-to-open authentication transitions have their own security-downgrade
    assessment.  Removing their ``auth`` leaves here avoids a duplicate telemetry
    query/finding, while a mixed update still gates every other risky leaf.
    """
    paths = classify_wlan_delta(current, proposed).usage_gated
    if secure_to_open(current, proposed):
        paths = tuple(path for path in paths if not matches(path, "auth.*"))
    return paths
