"""wired.l2.blackhole — a member component with no path to the VLAN exit.

Per VLAN (spec contract):
- exit resolved by analysis/exits (IRB HIGH > boundary uplink edge-confidence >
  NONE). NONE while members exist -> INSUFFICIENT_DATA for that vlan.
- ATTRIBUTION is condition-based: a stranded member component is attributed to
  the delta when it lost an exit it had in IR (`exit_lost`) OR when its members
  are newly introduced (`new_member_stranded` — e.g. the delta adds the first
  access port on an isolated switch). It is pre-existing INFO context ONLY when
  the same stranded-member condition already existed in the baseline.
- FAIL only at HIGH exit confidence; MEDIUM/LOW downgrades to WARN.
- The exit's confidence bounds the conclusion ONLY for vlans whose member
  reachability relies on it — a transit-only vlan (no members) never consults
  its exit, so a LOW uplink there cannot taint the check.
- Switched membership is configuration-based (access ports — empty ports count).
  AP/wireless membership is observation-based; when client data is absent the
  coverage is PARTIAL (noted), never silently complete.
"""

from __future__ import annotations

from digital_twin.analysis.exits import ExitKind
from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState, Status
from digital_twin.contracts import Finding, FindingCategory, FindingSource, Severity
from digital_twin.ir import (
    Capability,
    Confidence,
    ConfidenceLevel,
    IRCapability,
    IRDiff,
    min_confidence,
)
from digital_twin.ir.entities import DeviceRole


class L2BlackholeCheck:
    id = "wired.l2.blackhole"
    title = "VLAN segment loses its exit"
    domain = "wired.l2"
    default_severity = Severity.ERROR

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2, IRCapability.L3_EXITS})

    def applies_to(self, diff: IRDiff) -> bool:
        return any(diff.touches(k) for k in ("link", "port", "vlan", "l3intf", "device"))

    def run(self, ctx: CheckContext) -> CheckResult:
        findings: list[Finding] = []
        statuses: list[Status] = []
        confidences: list[Confidence] = []
        notes: list[str] = []
        if IRCapability.CLIENTS_ACTIVE not in ctx.proposed.capabilities:
            notes.append(
                "AP/wireless VLAN membership is observation-based and client data "
                "is absent — wireless membership not evaluated"
            )
        wireless_in_play = False
        for vid in sorted(set(ctx.baseline.ir.vlans) | set(ctx.proposed.ir.vlans)):
            statuses.append(self._check_vlan(ctx, vid, findings, confidences))
            # observation-based coverage matters only for conclusions that RELIED
            # on it: the delta touched this vlan AND wireless members are in play
            wireless_in_play = wireless_in_play or (
                _vlan_changed(ctx, vid)
                and any(c.wireless_members for c in ctx.proposed.vlan_components(vid))
            )
            notes.extend(self._ap_blind_spots(ctx, vid))
        if wireless_in_play:
            # spec: AP membership is observation-based — not-yet-connected clients
            # are a known coverage gap, so this conclusion can never be "complete"
            notes.append(
                "AP VLAN membership is observation-based (currently-connected "
                "clients only) — coverage partial by construction"
            )
        status = _aggregate(statuses)
        coverage_state = CoverageState.PARTIAL if notes else CoverageState.COMPLETE
        if status is Status.INSUFFICIENT_DATA:
            coverage_state = CoverageState.INSUFFICIENT
        if confidences:
            confidence: Confidence | None = min_confidence(*confidences)
        elif status is Status.PASS:
            # vacuous pass (no exits consulted, nothing stranded) is still a
            # deterministic structural conclusion — HIGH, not "absent"
            confidence = Confidence(level=ConfidenceLevel.HIGH)
        else:
            confidence = None
        return CheckResult(
            check_id=self.id,
            status=status,
            findings=tuple(findings),
            coverage=Coverage(state=coverage_state, notes=tuple(notes)),
            confidence=confidence,
            reasoning="compared member-component exit reachability per vlan",
        )

    def _ap_blind_spots(self, ctx: CheckContext, vid: int) -> list[str]:
        """APs that the DELTA cut off this vlan, with ZERO observed clients: the
        wireless impact is UNKNOWABLE (future clients), not absent — a coverage
        blind spot (spec: AP-side VLAN coverage is observation-based -> partial
        -> REVIEW). Delta-conditioned: an AP that did not reach the vlan in the
        BASELINE either is pre-existing context, not a blind spot (else every
        delta on a site with exit-less AP vlans would flood REVIEW). APs with
        observed clients are handled by the member path instead."""
        baseline_reached: set[str] = set()
        for comp in ctx.baseline.vlan_components(vid):
            if comp.reaches_exit:
                baseline_reached |= comp.nodes
        baseline_aps = {
            n
            for n in baseline_reached
            if (dev := ctx.baseline.ir.devices.get(n)) is not None and dev.role is DeviceRole.AP
        }
        if not baseline_aps:
            return []
        proposed_reached: set[str] = set()
        observed_ap_nodes: set[str] = set()
        for comp in ctx.proposed.vlan_components(vid):
            if comp.reaches_exit:
                proposed_reached |= comp.nodes
            if comp.wireless_members:
                observed_ap_nodes |= comp.nodes
        return [
            f"vlan {vid}: AP {n} no longer reaches the vlan and has no observed "
            "clients — wireless impact unknowable (observation-based coverage)"
            for n in sorted(baseline_aps)
            if n not in proposed_reached and n not in observed_ap_nodes
        ]

    def _check_vlan(
        self,
        ctx: CheckContext,
        vid: int,
        findings: list[Finding],
        confidences: list[Confidence],
    ) -> Status:
        proposed_exit = ctx.proposed.exit_for(vid)
        components = ctx.proposed.vlan_components(vid)
        if any(c.has_members for c in components) and proposed_exit.confidence is not None:
            # the exit bounds the conclusion's confidence ONLY when member
            # reachability actually relies on it (a LOW exit = a LOW "still
            # reachable"); a transit-only vlan never consulted its exit, so its
            # confidence must not taint the check
            confidences.append(proposed_exit.confidence)
        stranded = [c for c in components if c.has_members and not c.reaches_exit]
        if not stranded:
            return Status.PASS
        if proposed_exit.kind is ExitKind.NONE:
            if not _vlan_changed(ctx, vid):
                # the delta did not touch this vlan: a pre-existing strand with an
                # unlocatable exit is CONTEXT (spec: pre-existing = not caused),
                # else every cosmetic delta on such a site would floor to REVIEW
                findings.append(
                    self._finding(
                        code="wired.l2.blackhole.preexisting_unlocatable",
                        severity=Severity.INFO,
                        category=FindingCategory.NETWORK,
                        confidence=Confidence(level=ConfidenceLevel.HIGH),
                        message=(
                            f"vlan {vid}: pre-existing member strand with unlocatable "
                            "exit, unchanged by the delta (context)"
                        ),
                        vid=vid,
                        nodes=sorted(n for c in stranded for n in c.nodes),
                    )
                )
                return Status.PASS
            findings.append(
                self._finding(
                    code="wired.l2.blackhole.exit_unlocatable",
                    severity=Severity.WARNING,
                    category=FindingCategory.OPERATIONAL,
                    confidence=Confidence(
                        level=ConfidenceLevel.LOW,
                        reasons=(f"no IRB and no boundary uplink found for vlan {vid}",),
                    ),
                    message=f"vlan {vid} has members but its exit cannot be located",
                    vid=vid,
                    nodes=sorted(n for c in stranded for n in c.nodes),
                )
            )
            return Status.INSUFFICIENT_DATA
        baseline_components = ctx.baseline.vlan_components(vid)
        baseline_reaching = {
            frozenset(c.nodes) for c in baseline_components if c.has_members and c.reaches_exit
        }
        baseline_reaching_ports = frozenset(
            p for c in baseline_components if c.reaches_exit for p in c.member_ports
        )
        baseline_stranded_ports = frozenset(
            p for c in baseline_components if not c.reaches_exit for p in c.member_ports
        )
        baseline_reaching_wireless = frozenset(
            m for c in baseline_components if c.reaches_exit for m in c.wireless_members
        )
        baseline_stranded_wireless = frozenset(
            m for c in baseline_components if not c.reaches_exit for m in c.wireless_members
        )
        exit_conf = proposed_exit.confidence
        assert exit_conf is not None  # kind != NONE guarantees it (appended above)
        worst = Status.PASS
        for comp in stranded:
            lost_exit = (
                bool(comp.member_ports & baseline_reaching_ports)
                or bool(comp.wireless_members & baseline_reaching_wireless)
                or any(comp.nodes & prev for prev in baseline_reaching)
            )
            # attribution is per MEMBER (port or observed wireless client): the
            # condition is pre-existing ONLY if every member of this stranded
            # component was ALREADY a stranded member in the baseline. A new
            # access port — or a client observed on a newly stranded AP — on an
            # already-blackholed node is still a newly blackholed member.
            new_ports = sorted(comp.member_ports - baseline_stranded_ports)
            new_wireless = sorted(comp.wireless_members - baseline_stranded_wireless)
            preexisting = not lost_exit and not new_ports and not new_wireless
            if preexisting:
                findings.append(
                    self._finding(
                        code="wired.l2.blackhole.preexisting",
                        severity=Severity.INFO,
                        category=FindingCategory.NETWORK,
                        confidence=exit_conf,
                        message=(
                            f"vlan {vid}: component already had no exit path before the "
                            "delta (context)"
                        ),
                        vid=vid,
                        nodes=sorted(comp.nodes),
                    )
                )
                continue
            high = exit_conf.level is ConfidenceLevel.HIGH
            code = (
                "wired.l2.blackhole.exit_lost"
                if lost_exit
                else "wired.l2.blackhole.new_member_stranded"
            )
            message = (
                f"vlan {vid}: member segment loses its path to the {proposed_exit.kind} exit"
                if lost_exit
                else (
                    f"vlan {vid}: newly configured member port(s) have no path to the "
                    f"{proposed_exit.kind} exit"
                )
            )
            findings.append(
                self._finding(
                    code=code,
                    severity=Severity.ERROR if high else Severity.WARNING,
                    category=FindingCategory.NETWORK,
                    confidence=exit_conf,
                    message=message,
                    vid=vid,
                    nodes=sorted(comp.nodes),
                    new_member_ports=new_ports,
                )
            )
            worst = _aggregate([worst, Status.FAIL if high else Status.WARN])
        return worst

    def _finding(
        self,
        *,
        code: str,
        severity: Severity,
        category: FindingCategory,
        confidence: Confidence,
        message: str,
        vid: int,
        nodes: list[str],
        new_member_ports: list[str] | None = None,
    ) -> Finding:
        evidence: dict[str, object] = {"vlan": vid, "component_nodes": nodes}
        if new_member_ports:
            evidence["new_member_ports"] = new_member_ports
        return Finding(
            source=FindingSource.CHECK,
            category=category,
            code=code,
            severity=severity,
            confidence=confidence,
            message=message,
            affected_entities=tuple(nodes),
            evidence=evidence,
        )


def _vlan_changed(ctx: CheckContext, vid: int) -> bool:
    """Did the delta touch this vlan's structure? Components capture nodes,
    member ports, wireless members and exit reach — equality means unchanged."""
    return ctx.baseline.vlan_components(vid) != ctx.proposed.vlan_components(vid)


_ORDER = [Status.PASS, Status.INSUFFICIENT_DATA, Status.WARN, Status.FAIL]


def _aggregate(statuses: list[Status]) -> Status:
    return max(statuses, key=_ORDER.index) if statuses else Status.PASS
