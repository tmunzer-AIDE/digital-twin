"""Pure resolver: site WLANs -> the VLAN set each AP must receive on its uplink.

Grounded in the live `site_wlan` schema. A WLAN contributes a per-AP VLAN
requirement IFF it is enabled, vlan-tagged (`vlan_enabled`), and LOCALLY BRIDGED
(`interface` all/eth0..3 — tunnelled WLANs don't ride the wired uplink). The
required vlans are the integer `vlan_id`, the `vlan_ids` pool, and the static
candidate pool inside `dynamic_vlan` (802.1x still trunks every candidate vlan).

What can't be resolved statically — wxtag-scoped membership, a template/variable
vlan — is reported per-AP as UNRESOLVED, so the check notes a coverage gap
(REVIEW) instead of silently passing. Default-deny: never invent a requirement.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from digital_twin.ir import device_id

_LOCAL_INTERFACES = frozenset({"all", "eth0", "eth1", "eth2", "eth3"})


def _as_vlan(v: Any) -> int | None:
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return None


def _wlan_vlans(wlan: Mapping[str, Any]) -> tuple[frozenset[int], bool]:
    """(resolved vlan ids, fully_resolved?). fully_resolved is False when a vlan
    source existed but could not be parsed to an int (e.g. a `{{var}}` template)."""
    vlans: set[int] = set()
    ok = True
    if wlan.get("vlan_id") is not None:
        vid = _as_vlan(wlan["vlan_id"])
        ok = ok and vid is not None
        if vid is not None:
            vlans.add(vid)
    for raw in wlan.get("vlan_ids") or []:
        vid = _as_vlan(raw)
        ok = ok and vid is not None
        if vid is not None:
            vlans.add(vid)
    dv = wlan.get("dynamic_vlan")
    if isinstance(dv, Mapping) and dv:
        default = _as_vlan(dv.get("default_vlan_id"))
        if default is not None:
            vlans.add(default)
        pool = dv.get("vlans")
        if isinstance(pool, Mapping):
            for key in pool:
                vid = _as_vlan(key)
                ok = ok and vid is not None
                if vid is not None:
                    vlans.add(vid)
    return frozenset(vlans), ok


def ap_required_vlans(
    wlans: Iterable[Mapping[str, Any]],
    ap_devices: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, frozenset[int]], dict[str, list[str]]]:
    """(ap_device_id -> required vlan ids, ap_device_id -> unresolved reasons).

    Keys are IR device ids (normalised MACs). `ap_devices` are the raw AP device
    dicts (carry the Mist `id` used by `apply_to`==`aps` and the `mac`).
    """
    aps = [d for d in ap_devices if d.get("mac")]
    all_ids = [device_id(str(d["mac"])) for d in aps]
    by_mist_id = {str(d["id"]): device_id(str(d["mac"])) for d in aps if d.get("id")}

    resolved: dict[str, set[int]] = {}
    unresolved: dict[str, list[str]] = {}

    def flag(ap: str, reason: str) -> None:
        unresolved.setdefault(ap, []).append(reason)

    for wlan in wlans:
        if not wlan.get("enabled") or not wlan.get("vlan_enabled"):
            continue
        if str(wlan.get("interface", "all")) not in _LOCAL_INTERFACES:
            continue
        apply_to = str(wlan.get("apply_to", ""))
        ssid = str(wlan.get("ssid", "?"))
        if apply_to == "site":
            targets: list[str] = list(all_ids)
        elif apply_to == "aps":
            targets = [by_mist_id[i] for i in (wlan.get("ap_ids") or []) if i in by_mist_id]
        elif apply_to == "wxtags":
            # membership is a wxtag group match we don't model: every AP MIGHT be
            # in scope, so mark them all unresolved (conservative + honest).
            for ap in all_ids:
                flag(ap, f"WLAN '{ssid}' is scoped by wxtag — AP membership not modelled")
            continue
        else:
            continue  # unknown apply_to -> no basis to require anything

        vlans, ok = _wlan_vlans(wlan)
        if not ok:
            reason = (
                f"WLAN '{ssid}' vlan is a template/variable — not resolved"
                if not vlans
                else f"WLAN '{ssid}' has a partly-unresolvable vlan source"
            )
            for ap in targets:
                flag(ap, reason)
        for ap in targets:
            if vlans:
                resolved.setdefault(ap, set()).update(vlans)

    return {ap: frozenset(v) for ap, v in resolved.items()}, unresolved
