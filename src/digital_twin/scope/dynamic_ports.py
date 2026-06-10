"""Dynamic-port honesty: a usage/network redefinition with dynamic ports in play
cannot be verified -> WARNING finding (-> REVIEW), never silent SAFE.

Mist dynamic port profiles assign a port's RUNTIME usage from match rules when a
device (e.g. an AP) connects; `port_config` entries carry `dynamic_usage` as the
config-declared marker. The twin models such ports at their STATIC usage (M1
limit, see adapters/mist/compile/switch.py) and no fetched stat exposes the
applied runtime usage — so when an op REDEFINES a `port_usages` entry or a
`networks` vlan whose blast radius includes dynamic ports, the impact on those
ports is unknowable from config. Found in real use (2026-06-10): AP uplinks ran
usage 'ap' via dynamic profiles; redefining 'ap' trunk->access blackholed the
APs' WLANs while the twin reported SAFE.

Pure policy over raw objects (like the field gate). The finding is WARNING +
OPERATIONAL + HIGH: the network MAY break but the twin cannot conclude it
(never UNSAFE from a blind spot), and it must never stay SAFE.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from digital_twin.contracts import Finding, FindingCategory, FindingSource, Severity
from digital_twin.ir import Confidence, ConfidenceLevel

_VLAN_DEFINING_ROOTS = ("port_usages", "networks")


def _without_nulls(obj: Any) -> Any:
    """null == absent (project canon: Mist GETs return null for unset optional
    fields) — a payload omitting a null-valued key is NOT a redefinition."""
    if isinstance(obj, Mapping):
        return {k: _without_nulls(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_without_nulls(v) for v in obj]
    return obj


def _dynamic_ports(device: Mapping[str, Any]) -> list[str]:
    port_config = device.get("port_config")
    if not isinstance(port_config, Mapping):
        return []
    return sorted(
        str(key) for key, attrs in port_config.items()
        if isinstance(attrs, Mapping) and attrs.get("dynamic_usage")
    )


def dynamic_profile_findings(
    object_type: str,
    current: Mapping[str, Any],
    effective: Mapping[str, Any],
    devices: Iterable[Mapping[str, Any]],
) -> tuple[Finding, ...]:
    """One WARNING finding when the op redefines vlan-defining roots and dynamic
    ports are in the blast radius. For a device op the radius is the device
    itself; for a site_setting op it is every switch in the site (`devices` =
    the rolling raw device list)."""
    changed = tuple(
        root
        for root in _VLAN_DEFINING_ROOTS
        if _without_nulls(current.get(root)) != _without_nulls(effective.get(root))
    )
    if not changed:
        return ()
    if object_type == "device":
        targets: list[Mapping[str, Any]] = [effective]
    elif object_type == "site_setting":
        targets = [d for d in devices if d.get("type") == "switch"]
    else:
        return ()
    affected: dict[str, list[str]] = {}
    for dev in targets:
        ports = _dynamic_ports(dev)
        if ports:
            affected[str(dev.get("mac", "?"))] = ports
    if not affected:
        return ()
    return (
        Finding(
            source=FindingSource.ADAPTER,
            category=FindingCategory.OPERATIONAL,
            code="scope.dynamic_ports.unverifiable",
            severity=Severity.WARNING,
            confidence=Confidence(level=ConfidenceLevel.HIGH),
            message=(
                f"{' and '.join(changed)} redefined while dynamic port profiles are in "
                "use — the runtime usage of dynamically-profiled ports is not modeled, "
                "so the impact on them (and the devices attached, e.g. APs) cannot be "
                "verified"
            ),
            evidence={"changed_roots": list(changed), "dynamic_ports": affected},
        ),
    )
