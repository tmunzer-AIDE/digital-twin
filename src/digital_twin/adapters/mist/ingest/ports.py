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
_USAGE_OVERRIDE_ATTRS = ("mode", "port_network", "networks", "all_networks", "voip_network")
# port_config_overwrite only carries usage-attribute tweaks (schema-confirmed);
# port_network is the VLAN-relevant one (disabled/speed/poe are not VLAN state).
_OVERWRITE_ATTRS = ("port_network",)


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


def resolve_port_bases(eff: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """member -> merged port_config attrs: port_config <- local_port_config (per
    member). The usage assignment + flags (e.g. dynamic_usage) BEFORE the named
    port_usages profile is applied. A port present only in local_port_config is
    included (a local override can stand alone)."""
    base = _expand_map(eff.get("port_config") or {})
    for member, attrs in _expand_map(eff.get("local_port_config") or {}).items():
        base[member] = {**base.get(member, {}), **attrs}  # local override wins per member
    return base


def resolve_effective_ports(
    eff: dict[str, Any],
) -> Iterator[tuple[str, dict[str, Any], str | None]]:
    """Yield (member, effective_usage, usage_name) for every configured port.

    Layers port_config + local_port_config + port_config_overwrite onto the named
    port_usages profile (see module docstring).
    """
    usages: dict[str, Any] = eff.get("port_usages") or {}
    overwrite = _expand_map(eff.get("port_config_overwrite") or {})
    for member, attrs in resolve_port_bases(eff).items():
        usage_name = attrs.get("usage")
        effective = dict(usages.get(str(usage_name)) or {})
        for key in _USAGE_OVERRIDE_ATTRS:
            if key in attrs:
                effective[key] = attrs[key]
        for key in _OVERWRITE_ATTRS:
            ow = overwrite.get(member, {})
            if key in ow:
                effective[key] = ow[key]
        yield member, effective, (str(usage_name) if usage_name is not None else None)


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
