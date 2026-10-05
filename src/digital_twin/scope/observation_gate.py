"""Baseline identity binding and bounded invalidation of name-dependent telemetry."""

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from digital_twin.adapters.mist.ingest.identity import ambiguous_lldp_names, unique_name_index
from digital_twin.adapters.mist.ingest.ports import resolve_port_bases
from digital_twin.contracts import Rejection
from digital_twin.ir import device_id
from digital_twin.providers.base import RawSiteState


def _renamed_devices(baseline: RawSiteState, proposed: RawSiteState) -> dict[str, str]:
    previous = {device_id(str(d["mac"])): d for d in baseline.devices if d.get("mac")}
    return {
        device_id(str(d["mac"])): str(previous[device_id(str(d["mac"]))].get("name", ""))
        for d in proposed.devices if d.get("mac")
        and device_id(str(d["mac"])) in previous
        and d.get("name") != previous[device_id(str(d["mac"]))].get("name")
    }


def proposed_observations(baseline: RawSiteState, proposed: RawSiteState) -> RawSiteState:
    """Retain historical LLDP identity; expire dynamic outcomes after a peer rename.

The physical attachments remain observations. A proposed name does not rewrite
an observed system name or prove how a peer will advertise itself after rollout.
"""
    renamed = _renamed_devices(baseline, proposed)
    old_names = set(renamed.values()) - {""}
    rows: list[Mapping[str, Any]] = []
    for row in proposed.port_stats:
        # LLDP chassis IDs can be Virtual Chassis IDs rather than the Mist MAC.
        # An observed old name can invalidate a name rule without proving a
        # physical attachment to that managed device.
        neighbor = device_id(str(row["neighbor_mac"])) if row.get("neighbor_mac") else None
        stale = neighbor in renamed or row.get("neighbor_system_name") in old_names
        rows.append({**row, "_twin_dynamic_observation_stale": True} if stale else row)
    observed = baseline.observation_devices
    return replace(proposed, port_stats=tuple(rows), observation_devices=(
        observed if observed is not None else baseline.devices
    ))


def observation_gaps(
    baseline: RawSiteState, proposed: RawSiteState,
    baseline_effective: Mapping[str, dict[str, Any]],
    proposed_effective: Mapping[str, dict[str, Any]],
    *, requires_topology: bool,
) -> tuple[Rejection, ...]:
    """Report only relevant gaps, using the adapter's already compiled configs."""
    gaps: list[Rejection] = []
    ambiguous = ambiguous_lldp_names(baseline) if requires_topology else set()
    if ambiguous:
        gaps.append(Rejection(stage="observation_gate", reasons=tuple(
            f"ambiguous LLDP system name {name!r} identifies multiple managed devices"
            for name in sorted(ambiguous)
        )))
    renamed = _renamed_devices(baseline, proposed)
    if not renamed:
        return tuple(gaps)
    _, proposed_ambiguous = unique_name_index(proposed.devices)
    for did in sorted(renamed):
        name = next(d.get("name") for d in proposed.devices
                    if d.get("mac") and device_id(str(d["mac"])) == did)
        if name in proposed_ambiguous:
            gaps.append(Rejection(stage="observation_gate", reasons=(
                f"device {did} rename creates an ambiguous managed device name {name!r}",
            )))
    effective_sides = (baseline_effective, proposed_effective)
    port_sides = tuple({did: resolve_port_bases(eff) for did, eff in side.items()}
                       for side in effective_sides)
    for row in proposed.port_stats:
        if not row.get("_twin_dynamic_observation_stale"):
            continue
        reporter = device_id(str(row["mac"])) if row.get("mac") else ""
        member = str(row.get("port_id", ""))
        depends_on_name = False
        for side, ports in zip(effective_sides, port_sides, strict=True):
            profile = ports.get(reporter, {}).get(member, {}).get("dynamic_usage")
            spec = (side.get(reporter, {}).get("port_usages") or {}).get(profile) or {}
            rules = spec.get("rules") or []
            depends_on_name |= isinstance(rules, list) and any(
                isinstance(rule, Mapping) and rule.get("src") == "lldp_system_name"
                for rule in rules
            )
        if depends_on_name:
            gaps.append(Rejection(stage="observation_gate", reasons=(
                "peer rename invalidates observed LLDP dynamic usage "
                f"on {reporter}:{member}; the proposed runtime profile is unverified",
            )))
    return tuple(gaps)
