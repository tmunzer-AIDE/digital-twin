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
- .advertised_prefix_changed: a retained OSPF (device, vlan) — active OR passive —
  whose canonical Vlan.subnet changed -> WARNING/REVIEW (connected prefix shifted).
  An unresolved/absent subnet on either side -> structural prefix-coverage NOTE
  (PARTIAL -> REVIEW), telemetry-independent.
- .peer_unreachable: telemetry escalation backstop — a confirmed peer break with no
  matching adjacency-affecting structural owner -> ERROR/UNSAFE (GS27 Task 9).
An UNEVALUABLE live peer (interface still active OSPF but subnet now unresolved) is an
unknown, not a break -> a PARTIAL coverage note (-> REVIEW floor), never UNSAFE, the
same as the structural prefix-coverage note (telemetry-independent).

Comparison is by the semantic (device, vlan[, area, active]) tuple, NEVER by
OspfIntf.id, so rename/area-move is not a false withdrawal. l3_unmodeled is
gateway-only; the sole switch-side blindness is an unresolved network name.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Any

from digital_twin.analysis.ospf_reachability import (
    blind_peers,
    broken_peers,
    covering_dev_vlan,
    unevaluable_peers,
)
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

_Net = ipaddress.IPv4Network | ipaddress.IPv6Network


def _net(subnet: str | None) -> _Net | None:
    """Canonical network from a subnet string (GS31-style). Returns None on absent/invalid."""
    if not subnet:
        return None
    try:
        return ipaddress.ip_network(subnet, strict=False)
    except ValueError:
        return None

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


def _subnet_touched_vids(diff: IRDiff) -> set[int]:
    """vlan ids whose subnet/subnet_unresolved changed, or that were added/removed."""
    out: set[int] = set()
    for r in (*diff.added, *diff.removed):
        if r.kind == "vlan":
            try:
                out.add(int(r.id))
            except ValueError:
                pass
    for m in diff.modified:
        if m.ref.kind == "vlan" and ({"subnet", "subnet_unresolved"} & set(m.changed_fields)):
            try:
                out.add(int(m.ref.id))
            except ValueError:
                pass
    return out


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

        # 5. advertised_prefix_changed: retained OSPF (device, vlan) — active OR passive —
        # whose Vlan.subnet was delta-touched. Distinct source from the four ospf_intf-diff
        # codes: joins participation set with Vlan.subnet diff. Never double-emits with
        # egress_owned_pairs (collapsed device's withdrawal subsumes this).
        touched_vids = _subnet_touched_vids(ctx.diff)
        retained_pairs = set(base.by_dev_vlan) & set(prop.by_dev_vlan)
        for key in sorted(retained_pairs):
            did, vid = key
            if (did, vid) in egress_owned_pairs or vid not in touched_vids:
                continue
            bnet = _net(base_ir.vlans[vid].subnet if vid in base_ir.vlans else None)
            pnet = _net(prop_ir.vlans[vid].subnet if vid in prop_ir.vlans else None)
            if bnet is not None and pnet is not None and bnet != pnet:
                findings.append(Finding(
                    source=FindingSource.CHECK,
                    category=FindingCategory.NETWORK,
                    code=f"{self.id}.advertised_prefix_changed",
                    subject=ObjectRef("vlan", str(vid)),
                    severity=Severity.WARNING,
                    confidence=_UNVERIFIED,
                    message=(
                        f"OSPF-participating vlan {vid} on {did} changed its connected "
                        f"prefix {bnet} → {pnet} — the advertised subnet shifted; "
                        "reachability impact unverifiable without RIB"
                    ),
                    affected_entities=(str(vid),),
                    evidence={
                        "device": did,
                        "vlan": vid,
                        "base_prefix": str(bnet),
                        "proposed_prefix": str(pnet),
                    },
                    caused_by=ctx.delta_index.causes("vlan", [str(vid)]),
                ))
            elif bnet is None or pnet is None:
                notes.append(
                    f"OSPF-participating vlan {vid} on {did} advertised prefix could not "
                    "be compared (unresolved/absent) — prefix-change impact unverifiable"
                )

        # 6. unresolved rows touched by the delta -> PARTIAL abstain (never silent)
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

        # 7. Telemetry escalation (GS27 Task 9) — ESCALATE-ONLY post-pass.
        # telemetry_known: both base and prop have OSPF_TELEMETRY -> parsed rows usable.
        # has_unparsed: some rows were dropped -> the parsed-only slice still escalates.
        telemetry_known = (
            IRCapability.OSPF_TELEMETRY in base_ir.capabilities
            and IRCapability.OSPF_TELEMETRY in prop_ir.capabilities
        )
        has_unparsed = (
            base_ir.ospf_telemetry_unparsed_count > 0
            or prop_ir.ospf_telemetry_unparsed_count > 0
        )

        # Adjacency-affecting codes: only these justify escalating a structural finding
        # to ERROR/UNSAFE when a confirmed peer break is attributed to them.
        # metric_changed and participation_added are NOT adjacency-affecting.
        _ADJACENCY_AFFECTING = frozenset({
            f"{self.id}.egress_lost",
            f"{self.id}.advertised_removed",
            f"{self.id}.passive_flip",
            f"{self.id}.area_changed",
            f"{self.id}.advertised_prefix_changed",
        })

        if telemetry_known:
            broken = broken_peers(base_ir, prop_ir)
            # For each broken peer, find its owning structural finding and escalate it.
            # Keep track of broken (did, vid) pairs that have been escalated to avoid
            # a double .peer_unreachable backstop for the same pair.
            escalated_pairs: set[tuple[str, int]] = set()
            escalated_findings: set[int] = set()  # indices into `findings`

            for n in broken:
                cv = covering_dev_vlan(n, base_ir)
                if cv is None:
                    continue  # shouldn't happen: broken_peers are covered-in-base
                did, vid = cv
                # Find the owning finding: an adjacency-affecting finding whose evidence
                # names this (device, vlan) pair. Owner-matching relies on the
                # structural findings (sections 1-5) carrying evidence device+vlan (or
                # egress_lost's affected_vlans); a finding missing those keys falls
                # through to the .peer_unreachable backstop — which is safe by design.
                owner_idx: int | None = None
                for i, f in enumerate(findings):
                    if f.code not in _ADJACENCY_AFFECTING:
                        continue
                    ev = f.evidence or {}
                    if ev.get("device") == did and ev.get("vlan") == vid:
                        owner_idx = i
                        break
                    # egress_lost uses affected_vlans (a list) and device key
                    if f.code == f"{self.id}.egress_lost":
                        if ev.get("device") == did and vid in (ev.get("affected_vlans") or []):
                            owner_idx = i
                            break

                if owner_idx is not None:
                    if owner_idx in escalated_findings:
                        # Already escalated to ERROR by an earlier broken peer — no double-wrap.
                        escalated_pairs.add((did, vid))
                        continue
                    # Escalate the owning REVIEW finding to ERROR/HIGH, naming the peer.
                    old = findings[owner_idx]
                    escalated_findings.add(owner_idx)
                    escalated_pairs.add((did, vid))
                    findings[owner_idx] = Finding(
                        source=old.source,
                        category=old.category,
                        code=old.code,
                        subject=old.subject,
                        severity=Severity.ERROR,
                        confidence=_HIGH,
                        message=(
                            f"{old.message} | OSPF peer {n.peer_ip} on {did} "
                            "confirmed unreachable in proposed config"
                        ),
                        affected_entities=old.affected_entities,
                        evidence={**(old.evidence or {}), "broken_peer": n.peer_ip},
                        caused_by=old.caused_by,
                    )
                else:
                    # Defensive backstop: confirmed break with no owning finding.
                    if (did, vid) not in escalated_pairs:
                        escalated_pairs.add((did, vid))
                        findings.append(Finding(
                            source=FindingSource.CHECK,
                            category=FindingCategory.NETWORK,
                            code=f"{self.id}.peer_unreachable",
                            subject=ObjectRef("device", did),
                            severity=Severity.ERROR,
                            confidence=_HIGH,
                            message=(
                                f"OSPF peer {n.peer_ip} on {did} is confirmed unreachable "
                                "in the proposed config — no structural finding covers this "
                                "break (attribution gap backstop)"
                            ),
                            affected_entities=(str(vid),),
                            evidence={"device": did, "vlan": vid, "peer_ip": n.peer_ip},
                            caused_by=(),
                        ))

            # Unevaluable peers -> PARTIAL coverage NOTE (-> REVIEW floor), NOT a break.
            # A live established peer whose proposed coverage cannot be evaluated (subnet
            # unresolved but interface still active OSPF) is unknown, not confirmed-broken
            # — so it is a note, never UNSAFE. This is the SAME condition as Task 7's
            # structural prefix-coverage note, so it stays a note regardless of telemetry:
            # observing a live peer there must not change the verdict STATUS of an unknown
            # (telemetry escalates confirmed breaks, not unknowns — spec §4 matrix).
            for n in unevaluable_peers(base_ir, prop_ir):
                notes.append(
                    f"OSPF peer {n.peer_ip} on {n.device_id}: proposed coverage unevaluable "
                    "(the advertising prefix is unresolved) — adjacency impact not confirmed"
                )

        # 8. Relevance-scoped notes.

        # Compute OSPF-relevant context for note gating: a structural finding exists, OR
        # a delta-touched subnet belongs to an ACTIVE OSPF (device, vlan) in base or prop.
        ospf_relevant = bool(findings) or any(
            any(oi.vlan_id == vid and not oi.passive
                for oi in (*base_ir.ospf_intfs, *prop_ir.ospf_intfs))
            for vid in touched_vids
        )

        # 8a. Telemetry-blind note: when no telemetry OR has unparsed rows, and OSPF-relevant.
        if (not telemetry_known or has_unparsed) and ospf_relevant:
            notes.append(
                "OSPF neighbor telemetry unavailable/partial — peer-break detection is "
                "blind for the changed OSPF segment(s)"
            )

        # 8b. Baseline-uncovered (blind) peer note: only note when the peer's device is
        # delta-touched (has a structural OSPF finding on it, OR an ospf_intf diff ref,
        # OR a _subnet_touched_vids vlan whose OSPF participation is on that device).
        if telemetry_known:
            # Compute delta-touched device IDs.
            ospf_intf_touched_devices: set[str] = {
                r.id.split(":")[0]
                for r in (*ctx.diff.added, *ctx.diff.removed, *(m.ref for m in ctx.diff.modified))
                if r.kind == "ospf_intf"
            }
            subnet_touched_devices: set[str] = {
                oi.device_id
                for vid in touched_vids
                for oi in (*base_ir.ospf_intfs, *prop_ir.ospf_intfs)
                if oi.vlan_id == vid
            }
            finding_devices: set[str] = {
                f.evidence.get("device", "")
                for f in findings
                if f.evidence and "device" in f.evidence
            }
            touched_devices = ospf_intf_touched_devices | subnet_touched_devices | finding_devices

            for n in blind_peers(base_ir):
                if n.device_id in touched_devices:
                    notes.append(
                        f"OSPF peer {n.peer_ip} on {n.device_id}: peer could not be "
                        "placed in the baseline coverage model (no matching active OSPF "
                        "subnet) — adjacency state is unverifiable for this peer"
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
