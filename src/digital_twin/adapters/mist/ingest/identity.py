"""Only unique managed device names may resolve an observed LLDP identity."""

from collections.abc import Iterable, Mapping
from typing import Any

from digital_twin.ir import device_id
from digital_twin.providers.base import RawSiteState


def unique_name_index(
    devices: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, str], frozenset[str]]:
    candidates: dict[str, set[str]] = {}
    for device in devices:
        if device.get("name") and device.get("mac"):
            candidates.setdefault(str(device["name"]), set()).add(device_id(str(device["mac"])))
    return (
        {name: next(iter(ids)) for name, ids in candidates.items() if len(ids) == 1},
        frozenset(name for name, ids in candidates.items() if len(ids) > 1),
    )


def ambiguous_lldp_names(raw: RawSiteState) -> frozenset[str]:
    observed = raw.observation_devices if raw.observation_devices is not None else raw.devices
    _, ambiguous = unique_name_index(observed)
    used = {str(r.get("neighbor_system_name")) for r in raw.port_stats if not r.get("neighbor_mac")}
    switches = tuple(d for d in observed if d.get("type") == "switch")
    _, ambiguous_switches = unique_name_index(switches)
    switch_ids = {device_id(str(d["mac"])) for d in switches if d.get("mac")}
    for stat in raw.device_stats:
        lldp = stat.get("lldp_stat") or {}
        if stat.get("type") == "ap" and isinstance(lldp, Mapping):
            chassis = device_id(str(lldp["chassis_id"])) if lldp.get("chassis_id") else None
            if chassis not in switch_ids and str(lldp.get("system_name")) in ambiguous_switches:
                used.add(str(lldp["system_name"]))
    return ambiguous & used
