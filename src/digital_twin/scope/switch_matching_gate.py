"""A rename can select uncompiled template settings; no IR delta is not proof."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from digital_twin.adapters.mist.compile.switch_matching import (
    select_switch_matching_rule,
    supported_match_key,
)
from digital_twin.contracts import Rejection
from digital_twin.ir import device_id
from digital_twin.providers.base import RawSiteState
from digital_twin.scope.paths import leaf_changes


@dataclass(frozen=True)
class SwitchMatchingGap:
    rejection: Rejection
    device_id: str
    paths: tuple[str, ...]


def _uncompiled(rule: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        k: v for k, v in (rule or {}).items()
        if k not in ("port_config", "name") and not k.startswith("match_")
    }


def switch_matching_gaps(
    baseline_raw: RawSiteState,
    proposed_raw: RawSiteState,
    baseline_eff: Mapping[str, Any],
    proposed_eff: Mapping[str, Any],
) -> tuple[SwitchMatchingGap, ...]:
    """Compare selected rules' omitted settings as well as their compiled ports.

    This is a coverage screen, not an implementation of rule IP/STP/mirroring
    semantics. Unknown matching grammars cannot prove a rename non-interfering.
    """
    before = {device_id(str(d["mac"])): d for d in baseline_raw.devices if d.get("mac")}
    gaps = []
    for dev in proposed_raw.devices:
        if dev.get("type") != "switch" or not dev.get("mac"):
            continue
        did = device_id(str(dev["mac"]))
        old = before.get(did, {})
        bsm = baseline_eff.get("switch_matching") or {}
        psm = proposed_eff.get("switch_matching") or {}
        paths = tuple(
            f"switch_matching.selected.{d.path}"
            for d in leaf_changes(
                _uncompiled(select_switch_matching_rule(bsm, dict(old))),
                _uncompiled(select_switch_matching_rule(psm, dict(dev))),
            )
        )
        selector_changed = any(old.get(k) != dev.get(k) for k in ("name", "model", "role"))
        unknown_match = selector_changed and any(
            sm.get("enable") and any(
                k.startswith("match_") and not supported_match_key(k)
                for rule in sm.get("rules") or [] for k in rule
            ) for sm in (bsm, psm)
        )
        if paths or unknown_match:
            reasons = tuple(
                f"{p} changes on device {did}: selected rule setting is not compiled"
                for p in paths
            )
            if unknown_match:
                reasons += (f"device {did}: selector change crosses an unsupported match grammar",)
            gaps.append(SwitchMatchingGap(
                Rejection(stage="switch_matching_gate", reasons=reasons), did, paths,
            ))
    return tuple(gaps)
