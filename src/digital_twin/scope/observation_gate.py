"""Baseline identity binding and bounded invalidation of name-dependent telemetry."""

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from digital_twin.adapters.mist.compile.switch import compile_device
from digital_twin.adapters.mist.ingest.identity import ambiguous_lldp_names, unique_name_index
from digital_twin.adapters.mist.ingest.ports import resolve_port_bases
from digital_twin.contracts import Rejection
from digital_twin.ir import device_id
from digital_twin.providers.base import RawSiteState


def proposed_observations(
    baseline: RawSiteState, proposed: RawSiteState,
    baseline_effective: Mapping[str, dict[str, Any]],
) -> tuple[RawSiteState, tuple[Rejection, ...]]:
    """Retain historical LLDP identity; expire dynamic outcomes after a peer rename.

The physical attachments remain observations. A proposed name does not rewrite
an observed system name or prove how a peer will advertise itself after rollout.
"""
    gaps: list[Rejection] = []
    ambiguous = ambiguous_lldp_names(baseline)
    if ambiguous:
        gaps.append(Rejection(stage="observation_gate", reasons=tuple(
            f"ambiguous LLDP system name {name!r} identifies multiple managed devices"
            for name in sorted(ambiguous)
        )))
    previous = {device_id(str(d["mac"])): d for d in baseline.devices if d.get("mac")}
    renamed = {device_id(str(d["mac"])) for d in proposed.devices if d.get("mac")
               and device_id(str(d["mac"])) in previous
               and d.get("name") != previous[device_id(str(d["mac"]))].get("name")}
    _, proposed_ambiguous = unique_name_index(proposed.devices)
    for did in sorted(renamed):
        name = next(d.get("name") for d in proposed.devices
                    if d.get("mac") and device_id(str(d["mac"])) == did)
        if name in proposed_ambiguous:
            gaps.append(Rejection(stage="observation_gate", reasons=(
                f"device {did} rename creates an ambiguous managed device name {name!r}",
            )))
    by_name, _ = unique_name_index(baseline.devices)
    proposed_effective = {
        device_id(str(d["mac"])): compile_device(
            dict(proposed.networktemplate) if proposed.networktemplate else None,
            dict(proposed.setting), dict(d),
            sitetemplate=dict(proposed.sitetemplate) if proposed.sitetemplate else None,
        ) for d in proposed.devices if renamed and d.get("type") == "switch" and d.get("mac")
    }
    effective_sides = (baseline_effective, proposed_effective)
    port_sides = tuple({did: resolve_port_bases(eff) for did, eff in side.items()}
                       for side in effective_sides)
    rows: list[Mapping[str, Any]] = []
    for row in proposed.port_stats:
        neighbor = (device_id(str(row["neighbor_mac"])) if row.get("neighbor_mac")
                    else by_name.get(str(row.get("neighbor_system_name"))))
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
        if neighbor in renamed and depends_on_name:
            rows.append({**row, "_twin_dynamic_observation_stale": True})
            gaps.append(Rejection(stage="observation_gate", reasons=(
                f"device {neighbor} rename invalidates observed LLDP dynamic usage "
                f"on {reporter}:{member}; the proposed runtime profile is unverified",
            )))
        else:
            rows.append(row)
    observed = baseline.observation_devices
    return replace(proposed, port_stats=tuple(rows), observation_devices=(
        observed if observed is not None else baseline.devices
    )), tuple(gaps)
