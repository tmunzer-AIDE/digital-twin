"""Port-config helpers: member expansion and usage->VLAN resolution.

Mist port_config keys may be single ports, comma lists, or trailing ranges
("ge-0/0/0-23"). Usages resolve to (native_vlan, tagged_vlans) via the effective
networks map (name -> vlan_id).
"""

from __future__ import annotations

import re
from typing import Any

_RANGE = re.compile(r"^(?P<prefix>.*?/)(?P<start>\d+)-(?P<end>\d+)$")


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
