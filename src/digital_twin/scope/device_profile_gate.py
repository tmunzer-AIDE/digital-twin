from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from digital_twin.contracts import Rejection
from digital_twin.ir.entities import device_id
from digital_twin.scope.allowlist import DEVICE_PROFILE_OVERRIDABLE_LEAVES_BY_ROLE
from digital_twin.scope.paths import allowed, changed_leaf_paths

JsonObj = dict[str, Any]


@dataclass(frozen=True)
class DeviceProfileGap:
    rejection: Rejection
    device_id: str
    paths: tuple[str, ...]


def device_profile_gap(
    devices: Sequence[Mapping[str, Any]],
    baseline_eff: Mapping[str, JsonObj],
    proposed_eff: Mapping[str, JsonObj],
) -> DeviceProfileGap | None:
    """Per-site coverage gate for the unmodeled device-profile layer. Taints (->
    UNKNOWN) iff a modeled switch/gateway device carries a `deviceprofile_id` AND its
    OWN effective config — restricted to that role's overridable leaves — DIFFERS
    between baseline and the (below-profile) proposed. `*_eff` are device_id-keyed
    effective maps (switch device_effective union gateway_effective). Reads
    `deviceprofile_id` from the RAW device dict (the IR Device entity drops it).
    AP devices, unprofiled devices, and devices whose modeled surface is unchanged
    never taint."""
    for dev in devices:
        role = str((dev or {}).get("type") or "")
        patterns = DEVICE_PROFILE_OVERRIDABLE_LEAVES_BY_ROLE.get(role)
        if patterns is None:                      # ap / unknown role -> not modeled
            continue
        if not (dev or {}).get("deviceprofile_id") or not (dev or {}).get("mac"):
            continue
        did = device_id(str(dev["mac"]))
        changed = changed_leaf_paths(baseline_eff.get(did) or {}, proposed_eff.get(did) or {})
        paths = tuple(path for path in changed if allowed(path, patterns))
        if paths:
            return DeviceProfileGap(
                rejection=Rejection(
                    stage="device_profile_gate",
                    reasons=(
                        f"device {did} has a deviceprofile_id and the edit changes "
                        "overridable leaf path(s) "
                        f"{', '.join(paths)} on its effective config; the unmodeled "
                        "device-profile layer could override the outcome",
                    ),
                ),
                device_id=did,
                paths=paths,
            )
    return None


def device_profile_rejection(
    devices: Sequence[Mapping[str, Any]],
    baseline_eff: Mapping[str, JsonObj],
    proposed_eff: Mapping[str, JsonObj],
) -> Rejection | None:
    gap = device_profile_gap(devices, baseline_eff, proposed_eff)
    return gap.rejection if gap is not None else None
