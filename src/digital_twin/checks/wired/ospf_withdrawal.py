"""wired.l3.ospf_withdrawal — structural withdrawal of a switch's OSPF
participation for a routed segment (GS26/GS27).

The twin has no RIB, so this detects MODELED participation leaving OSPF, never
real reachability, and floors accordingly. Codes:
- .egress_lost: a device's last ACTIVE (adjacency-bearing) interface goes away
  (removed, ospf disabled, or active->passive flip that collapses it) -> the
  device loses modeled dynamic egress. ERROR/UNSAFE iff an affected routed
  segment has observed clients; else WARNING/REVIEW.
- .advertised_removed: a routed segment fully withdrawn from OSPF while its
  device keeps adjacency -> WARNING/REVIEW (prefix no longer distributed).
- .area_changed: a retained (device, vlan) whose area SET changed -> WARNING/REVIEW.
- .passive_flip: a retained (device, vlan, area) whose passive flag flipped,
  non-collapsing -> WARNING/REVIEW (transit role changed).
- .metric_changed: a retained (device, vlan, area) whose OSPF cost changed ->
  WARNING/REVIEW (path selection may shift).
- .participation_added: a (device, vlan) newly present in OSPF (wholly absent in
  baseline) -> WARNING/REVIEW (new advertisement / possible transit).

Comparison is by the semantic (device, vlan[, area, active]) tuple, NEVER by
OspfIntf.id, so rename/area-move is not a false withdrawal. l3_unmodeled is
gateway-only; the sole switch-side blindness is an unresolved network name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState, Status
from digital_twin.contracts import (
    Cause,
    Finding,
    FindingCategory,
    FindingSource,
    ObjectRef,
    Severity,
)
from digital_twin.ir import (
    Capability,
    Confidence,
    ConfidenceLevel,
    IRCapability,
    IRDiff,
    min_confidence,
)
from digital_twin.ir.model import IR

_HIGH = Confidence(level=ConfidenceLevel.HIGH)
_UNVERIFIED = Confidence(
    level=ConfidenceLevel.MEDIUM,
    reasons=(
        "OSPF reachability is not computed — a static default or redistribution "
        "the twin does not model may still cover this segment",
    ),
)
# egress_lost with NO (or unknown) observed clients: the adjacency loss is
# config-certain, but its impact is unconfirmed — REVIEW, not the HIGH the
# observed-client UNSAFE case carries.
_EGRESS_UNCONFIRMED = Confidence(
    level=ConfidenceLevel.MEDIUM,
    reasons=(
        "the OSPF adjacency loss is config-certain, but no observed client "
        "confirms impact on the affected segments",
    ),
)


@dataclass(frozen=True)
class _Row:
    passive: bool
    metric: int | None


@dataclass
class _Seg:
    by_area: dict[str, _Row] = field(default_factory=dict)
    # areas where 2+ network entries gave conflicting (passive, metric) — never last-win;
    # GS27 transit-mutation detection skips precise compare for these (emits a PARTIAL note).
    ambiguous_areas: set[str] = field(default_factory=set)

    @property
    def active(self) -> bool:
        return any(not r.passive for r in self.by_area.values())

    @property
    def areas(self) -> set[str]:
        return set(self.by_area)


@dataclass
class _Part:
    by_dev_vlan: dict[tuple[str, int], _Seg]
    active_by_dev: dict[str, set[int]]


def _participation(ir: IR) -> _Part:
    by_dev_vlan: dict[tuple[str, int], _Seg] = {}
    active_by_dev: dict[str, set[int]] = {}
    for o in ir.ospf_intfs:
        if o.vlan_id is None:
            continue  # unresolved rows handled separately
        seg = by_dev_vlan.setdefault((o.device_id, o.vlan_id), _Seg())
        row = _Row(passive=o.passive, metric=o.metric)
        if o.area in seg.by_area and seg.by_area[o.area] != row:
            seg.ambiguous_areas.add(o.area)  # differing (passive, metric) -> ambiguous
        else:
            seg.by_area[o.area] = row
        if not o.passive:
            active_by_dev.setdefault(o.device_id, set()).add(o.vlan_id)
    return _Part(by_dev_vlan, active_by_dev)


def _l3_vids(ir: IR) -> set[int]:
    return {i.vlan_id for i in ir.l3intfs if i.vlan_id is not None}


def _routed(ir: IR, vid: int, l3_vids: set[int]) -> bool:
    vlan = ir.vlans.get(vid)
    return vlan is not None and (vlan.subnet is not None or vid in l3_vids)


def _touches_vlan_subnet(diff: IRDiff) -> bool:
    """vlan add/remove, or a modified vlan whose changed fields touch the subnet —
    never name/collisions/dhcp_sources/etc."""
    if any(r.kind == "vlan" for r in (*diff.added, *diff.removed)):
        return True
    return any(
        m.ref.kind == "vlan" and ({"subnet", "subnet_unresolved"} & set(m.changed_fields))
        for m in diff.modified
    )


class OspfWithdrawalCheck:
    id = "wired.l3.ospf_withdrawal"
    title = "routed segment withdrawn from OSPF"
    domain = "wired.l3"
    default_severity = Severity.ERROR

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2, IRCapability.L3_EXITS})

    def applies_to(self, diff: IRDiff) -> bool:
        return diff.touches("ospf_intf") or _touches_vlan_subnet(diff)

    def run(self, ctx: CheckContext) -> CheckResult:
        base_ir, prop_ir = ctx.baseline.ir, ctx.proposed.ir
        base, prop = _participation(base_ir), _participation(prop_ir)
        base_l3, prop_l3 = _l3_vids(base_ir), _l3_vids(prop_ir)
        clients_known = (
            IRCapability.CLIENTS_ACTIVE in base_ir.capabilities
            and IRCapability.CLIENTS_ACTIVE in prop_ir.capabilities
        )
        findings: list[Finding] = []
        notes: list[str] = []
        # egress_lost subsumes the weaker codes per (device, vlan): only the
        # COLLAPSED device's own withdrawal/mutation of a segment is the same
        # event — an independent withdrawal or mutation of that vlan on ANOTHER
        # device is a distinct finding.
        egress_owned_pairs: set[tuple[str, int]] = set()

        # 1. device adjacency collapse -> .egress_lost
        collapsed = sorted(
            did
            for did, act in base.active_by_dev.items()
            if act and not prop.active_by_dev.get(did)
        )
        for did in collapsed:
            affected = sorted(
                {
                    vid
                    for (d, vid) in base.by_dev_vlan
                    if d == did and _routed(base_ir, vid, base_l3)
                }
            )
            if not affected:
                continue
            affected_set = set(affected)
            egress_owned_pairs.update((did, v) for v in affected)
            n_clients = (
                sum(1 for c in base_ir.clients if c.vlan in affected_set)
                if clients_known
                else 0
            )
            severity = (
                Severity.ERROR if (clients_known and n_clients) else Severity.WARNING
            )
            if not clients_known:
                notes.append(
                    f"device {did}: client data unavailable — the egress-loss blast "
                    "radius is unknown"
                )
            who = (
                f"{n_clients} observed client(s)"
                if clients_known
                else "an unknown number of clients"
            )
            findings.append(
                Finding(
                    source=FindingSource.CHECK,
                    category=FindingCategory.NETWORK,
                    code=f"{self.id}.egress_lost",
                    subject=ObjectRef("device", did),
                    severity=severity,
                    confidence=(_HIGH if severity is Severity.ERROR else _EGRESS_UNCONFIRMED),
                    message=(
                        f"switch {did} loses its last active OSPF adjacency — routed "
                        f"segments {affected} lose their modeled dynamic egress; {who} "
                        "on them are affected"
                    ),
                    affected_entities=tuple(str(v) for v in affected),
                    evidence={
                        "device": did,
                        "affected_vlans": affected,
                        "observed_clients": n_clients if clients_known else None,
                    },
                    caused_by=ctx.delta_index.causes(
                        "ospf_intf",
                        [oi.id for oi in base_ir.ospf_intfs
                         if oi.device_id == did and not oi.passive],
                    ),
                )
            )

        # 2. per-(device, vlan) full withdrawal -> .advertised_removed: a device
        # drops its OWN OSPF advertisement of a routed segment while keeping its
        # adjacency. Another device may still advertise the vlan, but that is
        # still a real, surfaceable change — never silently SAFE.
        for did, vid in sorted(set(base.by_dev_vlan) - set(prop.by_dev_vlan)):
            if (did, vid) in egress_owned_pairs or not _routed(base_ir, vid, base_l3):
                continue
            findings.append(
                Finding(
                    source=FindingSource.CHECK,
                    category=FindingCategory.NETWORK,
                    code=f"{self.id}.advertised_removed",
                    subject=ObjectRef("vlan", str(vid)),
                    severity=Severity.WARNING,
                    confidence=_UNVERIFIED,
                    message=(
                        f"switch {did} withdraws routed segment (vlan {vid}) from OSPF "
                        "— its prefix is no longer advertised there; external "
                        "reachability depends on redistribution the twin does not model"
                    ),
                    affected_entities=(str(vid),),
                    evidence={"device": did, "vlan": vid},
                    caused_by=ctx.delta_index.causes(
                        "ospf_intf",
                        [oi.id for oi in (*base_ir.ospf_intfs, *prop_ir.ospf_intfs)
                         if oi.device_id == did and oi.vlan_id == vid],
                    ),
                )
            )

        # 3. retained participation mutated -> precise per-area structural codes
        def _ospf_caused_by(did: str, vid: int) -> tuple[Cause, ...]:
            return ctx.delta_index.causes(
                "ospf_intf",
                [oi.id for oi in (*base_ir.ospf_intfs, *prop_ir.ospf_intfs)
                 if oi.device_id == did and oi.vlan_id == vid],
            )

        def _mutation(
            did: str, vid: int, code: str, message: str, extra: dict[str, Any]
        ) -> Finding:
            # the four GS27 structural codes share everything but code/message/evidence-extras
            return Finding(
                source=FindingSource.CHECK,
                category=FindingCategory.NETWORK,
                code=f"{self.id}.{code}",
                subject=ObjectRef("vlan", str(vid)),
                severity=Severity.WARNING,
                confidence=_UNVERIFIED,
                message=message,
                affected_entities=(str(vid),),
                evidence={"device": did, "vlan": vid, **extra},
                caused_by=_ospf_caused_by(did, vid),
            )

        for key in sorted(set(base.by_dev_vlan) & set(prop.by_dev_vlan)):
            did, vid = key
            if (did, vid) in egress_owned_pairs or not _routed(prop_ir, vid, prop_l3):
                continue
            b, p = base.by_dev_vlan[key], prop.by_dev_vlan[key]

            # 3a. area_changed: the area SET changed
            if b.areas != p.areas:
                findings.append(_mutation(
                    did, vid, "area_changed",
                    f"OSPF area set for vlan {vid} on {did} changed "
                    f"{sorted(b.areas)} → {sorted(p.areas)} — adjacency / LSA-scope may shift",
                    {"base_areas": sorted(b.areas), "proposed_areas": sorted(p.areas)},
                ))

            # 3b. per-area passive/metric comparison (only non-ambiguous retained areas)
            ambiguous = b.ambiguous_areas | p.ambiguous_areas
            for area in sorted(b.areas & p.areas):
                if area in ambiguous:
                    # 3c. emit PARTIAL note for ambiguous areas — skip precise compare
                    notes.append(
                        f"OSPF vlan {vid} on {did} area {area} is claimed by multiple "
                        "network entries with differing passive/metric — transit-change "
                        "detection skipped"
                    )
                    continue
                b_row, p_row = b.by_area[area], p.by_area[area]

                if b_row.passive != p_row.passive:
                    direction = "active→passive" if not b_row.passive else "passive→active"
                    findings.append(_mutation(
                        did, vid, "passive_flip",
                        f"OSPF vlan {vid} on {did} area {area} flipped {direction}"
                        " — transit role changed",
                        {"area": area},
                    ))

                if b_row.metric != p_row.metric:
                    findings.append(_mutation(
                        did, vid, "metric_changed",
                        f"OSPF cost for vlan {vid} on {did} area {area} changed "
                        f"{b_row.metric} → {p_row.metric} — path selection may shift "
                        "(no RIB computed)",
                        {"area": area, "base_metric": b_row.metric,
                         "proposed_metric": p_row.metric},
                    ))

        # 4. participation_added: (device, vlan) wholly new to OSPF in proposed
        for key in sorted(set(prop.by_dev_vlan) - set(base.by_dev_vlan)):
            did, vid = key
            if not _routed(prop_ir, vid, prop_l3):
                continue
            findings.append(_mutation(
                did, vid, "participation_added",
                f"vlan {vid} on {did} is newly added to OSPF — new advertisement / "
                "possible transit; review intended scope",
                {},
            ))

        # 5. unresolved rows touched by the delta -> PARTIAL abstain (never silent)
        touched_ids = {
            r.id
            for r in (*ctx.diff.added, *ctx.diff.removed, *(m.ref for m in ctx.diff.modified))
            if r.kind == "ospf_intf"
        }
        seen: set[str] = set()
        for o in (*base_ir.ospf_intfs, *prop_ir.ospf_intfs):
            if o.unresolved and o.id in touched_ids and o.id not in seen:
                seen.add(o.id)
                notes.append(
                    f"ospf interface {o.id}: network name {o.network_name!r} does not "
                    "resolve to a vlan — withdrawal impact cannot be verified"
                )

        worst = Status.PASS
        for f in findings:
            this = Status.FAIL if f.severity is Severity.ERROR else Status.WARN
            if this is Status.FAIL or worst is Status.PASS:
                worst = this
        return CheckResult(
            check_id=self.id,
            status=worst,
            findings=tuple(findings),
            coverage=Coverage(
                state=CoverageState.PARTIAL if notes else CoverageState.COMPLETE,
                notes=tuple(notes),
            ),
            confidence=(
                min_confidence(*(f.confidence for f in findings)) if findings else _HIGH
            ),
            reasoning="compared per-(device,vlan) OSPF participation, baseline vs proposed",
        )
