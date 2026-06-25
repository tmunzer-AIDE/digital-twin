"""Port-config helpers: member expansion, per-port override layering, usage->VLAN.

Mist port_config keys may be single ports, comma lists, or trailing ranges
("ge-0/0/0-23"). A port's EFFECTIVE usage is layered (lowest precedence first):

    port_usages[usage]                     the named profile
      <- inline attrs on port_config        (e.g. port_network override)
      <- local_port_config                  local override; may REASSIGN usage
      <- port_config_overwrite              tweak usage attrs (e.g. port_network)
                                            without minting a new port_usage

Overrides are applied per MEMBER, not per config key, so a single-port override
inside a range key ("ge-0/0/5" within "ge-0/0/0-10") resolves correctly. The
effective usage then resolves to (native_vlan, tagged_vlans) via the networks map.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

_RANGE = re.compile(r"^(?P<prefix>.*?/)(?P<start>\d+)-(?P<end>\d+)$")

# Inline attrs on port_config/local_port_config that override the named usage.
_USAGE_OVERRIDE_ATTRS = (
    "mode",
    "port_network",
    "networks",
    "all_networks",
    "voip_network",
    "poe_disabled",
    "mtu",
    "allow_dhcpd",
    "stp_edge",  # schema: inline on local_port_config only (gate enforces)
)
# port_config_overwrite only carries usage-attribute tweaks (schema-confirmed);
# port_network is the VLAN-relevant one, poe_disabled feeds Port.poe (the
# poe.disconnect check). disabled/speed et al. remain unmodeled -> out of scope.
_OVERWRITE_ATTRS = ("port_network", "poe_disabled", "disabled")

# local_port_config may additionally carry the admin-down boolean (OAS).
_LOCAL_ATTRS = (*_USAGE_OVERRIDE_ATTRS, "disabled")

# Mist SYSTEM-DEFINED port usages: referenced by port_config but defined in NO
# config object (template/site/device — not even getSiteSettingDerived exposes
# them; verified live 2026-06-10). Semantics per Mist docs — hence consumers
# mark system-resolved ports INFERRED (MEDIUM), never config-HIGH. Honored
# unless `no_system_defined_port_usages` or listed in
# `disabled_system_defined_port_usages` (template-root flags).
_SYSTEM_USAGES: dict[str, dict[str, Any]] = {
    "ap": {"mode": "trunk", "all_networks": True, "port_network": "default"},
    "uplink": {"mode": "trunk", "all_networks": True},
    "default": {"mode": "access", "port_network": "default"},
    "disabled": {"disabled": True},  # admin-down: genuinely carries nothing
}


def _system_usage(eff: dict[str, Any], name: str) -> dict[str, Any] | None:
    if eff.get("no_system_defined_port_usages"):
        return None
    if name in (eff.get("disabled_system_defined_port_usages") or ()):
        return None
    return _SYSTEM_USAGES.get(name)


def usage_definition(eff: dict[str, Any], name: str) -> tuple[dict[str, Any], str]:
    """(usage attrs, resolution) for a usage NAME: explicit map > Mist system
    defaults > unresolved (empty — carriage unknown, never silently empty)."""
    explicit = (eff.get("port_usages") or {}).get(name)
    if explicit is not None:
        return dict(explicit), "explicit"
    system = _system_usage(eff, name)
    if system is not None:
        return dict(system), "system"
    return {}, "unresolved"


def expand_port_members(key: str) -> list[str]:
    members: list[str] = []
    for part in key.split(","):
        part = part.strip()
        m = _RANGE.match(part)
        if m:
            prefix = m.group("prefix")
            for n in range(int(m.group("start")), int(m.group("end")) + 1):
                members.append(f"{prefix}{n}")
        else:
            members.append(part)
    return members


def _expand_map(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """{port-or-range key -> attrs} -> {member -> attrs} (later keys win per member)."""
    out: dict[str, dict[str, Any]] = {}
    for key, attrs in config.items():
        for member in expand_port_members(key):
            out[member] = {**out.get(member, {}), **(attrs or {})}
    return out


def _overridable(pc_member: dict[str, Any] | None) -> bool:
    """local_port_config applies to a member ONLY when there is no port_config
    entry to protect, or that entry explicitly allows it. `no_local_overwrite`
    defaults to true (OAS) -> local is DISCARDED by default."""
    if pc_member is None:
        return True
    return not pc_member.get("no_local_overwrite", True)


def resolve_port_bases(eff: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """member -> merged base attrs (usage, dynamic_usage, inline) with real Mist
    precedence: port_config <- local_port_config, per member, ONLY when locally
    overridable (`no_local_overwrite == false` or no port_config entry). The base
    the named port_usages profile is then applied to. port_config_overwrite is
    NOT merged here (it tweaks effective attrs in resolve_effective_ports and
    carries no usage/dynamic_usage). A port present only in local_port_config is
    included."""
    pc = _expand_map(eff.get("port_config") or {})
    out: dict[str, dict[str, Any]] = {m: dict(a) for m, a in pc.items()}
    for member, attrs in _expand_map(eff.get("local_port_config") or {}).items():
        if _overridable(pc.get(member)):
            out[member] = {**out.get(member, {}), **attrs}
    return out


def resolve_effective_ports(
    eff: dict[str, Any],
) -> Iterator[tuple[str, dict[str, Any], str | None, str]]:
    """Yield (member, effective_usage, usage_name, resolution) per configured port.

    Layers (lowest -> highest precedence): named port_usages profile <- inline
    port_config attrs <- port_config_overwrite attrs <- local_port_config attrs
    (highest, applied only when the member is locally overridable). The member
    set is the union of all three maps, so an overwrite-only or local-only port
    still yields a port (resolution "none" when no usage name resolves).
    `resolution` states where the usage came from: "explicit"/"system"/
    "unresolved" (see usage_definition) or "none" (no usage name).
    """
    pc = _expand_map(eff.get("port_config") or {})
    overwrite = _expand_map(eff.get("port_config_overwrite") or {})
    local = _expand_map(eff.get("local_port_config") or {})
    bases = resolve_port_bases(eff)
    for member in sorted(set(bases) | set(overwrite)):
        usage_name = (bases.get(member) or {}).get("usage")
        effective: dict[str, Any]
        if usage_name is None:
            effective, resolution = {}, "none"
        else:
            effective, resolution = usage_definition(eff, str(usage_name))
        for key in _USAGE_OVERRIDE_ATTRS:  # port_config inline
            if key in pc.get(member, {}):
                effective[key] = pc[member][key]
        for key in _OVERWRITE_ATTRS:  # port_config_overwrite
            if key in overwrite.get(member, {}):
                effective[key] = overwrite[member][key]
        if _overridable(pc.get(member)):  # local_port_config (highest, gated)
            for key in _LOCAL_ATTRS:
                if key in local.get(member, {}):
                    effective[key] = local[member][key]
        yield member, effective, (str(usage_name) if usage_name is not None else None), resolution


def usage_vlans(
    usage: dict[str, Any], networks: dict[str, Any]
) -> tuple[int | None, tuple[int, ...]]:
    """(native_vlan, tagged_vlans) for a port usage, resolved via `networks`.

    The native network is carried UNTAGGED, so it is always excluded from the
    tagged set (Plan 1's link_carried_vlans handles the native via the
    matching-natives path; double-listing it would carry it through the tagged
    path even on a native mismatch).
    """

    def vlan_of(name: str | None) -> int | None:
        if not name or name not in networks:
            return None
        vid = networks[name].get("vlan_id")
        return int(vid) if vid is not None else None

    native = vlan_of(usage.get("port_network"))
    if usage.get("mode") != "trunk":
        return native, ()
    names = list(networks) if usage.get("all_networks") else list(usage.get("networks") or [])
    tagged = tuple(sorted(v for v in (vlan_of(n) for n in names) if v is not None and v != native))
    return native, tagged
