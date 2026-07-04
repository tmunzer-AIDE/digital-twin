"""wired.l2.loop — a cycle is NOT a loop by itself (spec table):

all cycle ports STP-running -> protected redundancy (PASS); any port STP
DISABLED -> FAIL (ERROR, network, HIGH); STP UNKNOWN on any port -> WARN with
LOW confidence (floors the decision to REVIEW).

ATTRIBUTION is per the spec's CONDITION, not the cycle's mere existence: the
attributable condition is "cycle + STP disabled/unknown". A delta that disables
STP on an ALREADY-EXISTING cycle introduces that condition and must FAIL — only
when the condition is no worse than in the baseline is it pre-existing context
(INFO). Ranks: protected(0) < unknown(1) < disabled(2); attributable when the
proposed rank exceeds the baseline rank (or the cycle itself is new).
requires() is wired.l2 only — STP_STATE absence degrades to the UNKNOWN row,
which is exactly the honest answer (not INSUFFICIENT_DATA).

self_loop (Spec-3): a SEPARATE pass over BASELINE ports carrying an OBSERVED
physical self-loop (Port.self_loop_peer — LLDP: the chassis sees ITSELF on
another port; diff-ignored, so it is read as a fact, never as a delta trigger
itself). The trigger is a `stp_disable` delta: Port.bpdu_filter flips
False->True on the port or its claimed peer — the ONLY config mapping of that
leaf (Port.stp_enabled is OBSERVED telemetry, never used as a trigger). A
contained physical mis-wire becomes an active broadcast-storm risk the moment
STP protection is removed from either end. Tiers: triggered + reciprocal
(both rows name each other) -> ERROR/HIGH; triggered + one-sided claim ->
WARNING/MEDIUM (evidence-tier gate — never ERROR on unconfirmed evidence);
the pair otherwise touched by the delta (any port diff on either end, e.g. a
stp_edge flip) -> INFO context; untouched -> silent. One finding per PAIR.

AGGREGATION (review P1): this check's `worst`/`confidences` roll-up is
CUSTOM and, for the cycle pass, appends every finding's confidence
unconditionally. The self_loop pass therefore contributes to BOTH `worst`
and `confidences` ONLY for WARNING-or-worse self-loop findings; an INFO
self-loop finding is excluded from both (mirrors status_from_findings' INFO
exclusion) — otherwise an INFO context at sub-HIGH confidence would floor
the whole check's result confidence and, via decision.py's confidence rule,
cause a REVIEW that context alone should never trigger.
"""

from __future__ import annotations

from digital_twin.analysis.context import AnalysisContext
from digital_twin.analysis.cycles import Cycle
from digital_twin.analysis.delta_cause import causes_for_loop
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


class L2LoopCheck:
    id = "wired.l2.loop"
    title = "L2 loop risk (cycle without STP protection)"
    domain = "wired.l2"
    default_severity = Severity.ERROR

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2})

    def applies_to(self, diff: IRDiff) -> bool:
        return any(diff.touches(k) for k in ("link", "port", "vlan", "device"))

    def run(self, ctx: CheckContext) -> CheckResult:
        findings: list[Finding] = []
        worst = Status.PASS
        confidences: list[Confidence] = []
        vlan_ids = sorted(set(ctx.baseline.ir.vlans) | set(ctx.proposed.ir.vlans))
        for vid in vlan_ids:
            baseline_rank = {c.nodes: self._rank(ctx.baseline, c) for c in ctx.baseline.cycles(vid)}
            for cycle in ctx.proposed.cycles(vid):
                previous = baseline_rank.get(cycle.nodes)
                attributable = previous is None or self._rank(ctx.proposed, cycle) > previous
                finding, status = self._judge(ctx, vid, cycle, attributable)
                if finding:
                    findings.append(finding)
                    confidences.append(finding.confidence)
                worst = _worse(worst, status)

        for finding, status in self._self_loop_findings(ctx):
            findings.append(finding)
            # review P1: an INFO self-loop finding is context only — it is
            # excluded from BOTH the worst-status ranking and the confidence
            # roll-up (mirrors status_from_findings' INFO exclusion), so a
            # sub-HIGH INFO confidence can never floor this check's result
            # confidence into a decision-layer REVIEW.
            if finding.severity is not Severity.INFO:
                confidences.append(finding.confidence)
                worst = _worse(worst, status)

        confidence = (
            min_confidence(*confidences) if confidences else Confidence(level=ConfidenceLevel.HIGH)
        )
        return CheckResult(
            check_id=self.id,
            status=worst,
            findings=tuple(findings),
            coverage=Coverage(state=CoverageState.COMPLETE),
            confidence=confidence,
            reasoning=f"examined {len(vlan_ids)} vlan graphs for cycles",
        )

    def _self_loop_findings(self, ctx: CheckContext) -> list[tuple[Finding, Status]]:
        """Pass over BASELINE ports carrying an OBSERVED physical self-loop
        (Port.self_loop_peer). One finding per PAIR (not per end) — pairs are
        visited once via a `seen` set keyed by the frozenset of the two ids
        (a lone one-sided claim with an unmodeled/absent peer still yields a
        single-port pair)."""
        base_ports = ctx.baseline.ir.ports
        out: list[tuple[Finding, Status]] = []
        seen: set[frozenset[str]] = set()
        for pid, port in sorted(base_ports.items()):
            if port.self_loop_peer is None:
                continue
            peer_id = port.self_loop_peer
            pair_key = frozenset({pid, peer_id})
            if pair_key in seen:
                continue
            seen.add(pair_key)

            peer_base = base_ports.get(peer_id)
            # Peer corroboration means the peer names THIS port back — not
            # merely that the peer has *some* reciprocal claim (which may
            # describe an unrelated pair, e.g. peer_base.self_loop_peer
            # pointing at a third port). Reading peer_base.self_loop_reciprocal
            # here would leak that unrelated pair's reciprocity into this one.
            reciprocal = port.self_loop_reciprocal or (
                peer_base is not None and peer_base.self_loop_peer == pid
            )

            bpdu_a = self._bpdu_filter_flip(ctx, pid)
            bpdu_b = self._bpdu_filter_flip(ctx, peer_id)
            triggered = bpdu_a or bpdu_b

            pair_ids = tuple(sorted({pid, peer_id}))
            caused_by = ctx.delta_index.causes("port", pair_ids)

            if triggered:
                severity = Severity.ERROR if reciprocal else Severity.WARNING
                confidence = (
                    Confidence(level=ConfidenceLevel.HIGH)
                    if reciprocal
                    else Confidence(
                        level=ConfidenceLevel.MEDIUM,
                        reasons=("the self-loop claim is one-sided — not corroborated "
                                 "by the peer port",),
                    )
                )
                status = Status.FAIL if reciprocal else Status.WARN
                reason = (
                    "reciprocal observed self-loop (both ports name each other) — "
                    "STP protection disabled by this change"
                    if reciprocal
                    else "one-sided observed self-loop claim — STP protection disabled "
                         "by this change, unconfirmed by the peer"
                )
                message = (
                    f"physical self-loop observed on {pid} ↔ {peer_id}; STP "
                    f"protection disabled by this change — broadcast-storm risk"
                )
                out.append((
                    self._self_loop_finding(
                        severity=severity, confidence=confidence, message=message,
                        pair_ids=pair_ids, ctx=ctx, reason=reason, caused_by=caused_by,
                    ),
                    status,
                ))
                continue

            pair_touched = self._pair_touched(ctx, pid, peer_id)
            if pair_touched:
                message = (
                    f"physical self-loop observed on {pid} ↔ {peer_id}; unrelated "
                    f"change on this pair this delta — context only, STP protection "
                    f"unaffected"
                )
                out.append((
                    self._self_loop_finding(
                        severity=Severity.INFO,
                        confidence=Confidence(level=ConfidenceLevel.HIGH),
                        message=message, pair_ids=pair_ids, ctx=ctx,
                        reason="observed self-loop pair touched by this delta, but STP "
                               "protection (bpdu_filter) unaffected — context only",
                        caused_by=caused_by,
                    ),
                    Status.PASS,
                ))
            # untouched: nothing — silent (per spec, no finding at all)
        return out

    @staticmethod
    def _bpdu_filter_flip(ctx: CheckContext, pid: str) -> bool:
        """True iff Port.bpdu_filter went False->True on `pid` (the ONLY
        config mapping of the `stp_disable` leaf — stp_enabled is OBSERVED
        telemetry, never the trigger)."""
        old = ctx.baseline.ir.ports.get(pid)
        new = ctx.proposed.ir.ports.get(pid)
        if old is None or new is None:
            return False
        return old.bpdu_filter is False and new.bpdu_filter is True

    @staticmethod
    def _pair_touched(ctx: CheckContext, pid: str, peer_id: str) -> bool:
        """True iff EITHER end of the pair has any port diff at all (add/
        remove/modify) — used only after the bpdu_filter trigger has already
        been ruled out, so this never re-classifies a triggered pair."""
        di = ctx.delta_index
        return di.in_delta("port", pid) or di.in_delta("port", peer_id)

    def _self_loop_finding(
        self,
        *,
        severity: Severity,
        confidence: Confidence,
        message: str,
        pair_ids: tuple[str, ...],
        ctx: CheckContext,
        reason: str,
        caused_by: tuple[Cause, ...],
    ) -> Finding:
        observed_states: dict[str, dict[str, str]] = {}
        for pid in pair_ids:
            port = ctx.proposed.ir.ports.get(pid) or ctx.baseline.ir.ports.get(pid)
            if port is None:
                continue
            state: dict[str, str] = {}
            if port.stp_state is not None:
                state["state"] = port.stp_state
            if port.stp_role is not None:
                state["role"] = port.stp_role
            if state:
                observed_states[pid] = state
        evidence: dict[str, object] = {
            "ports": list(pair_ids),
            "severity_reason": reason,
        }
        if observed_states:
            evidence["observed_states"] = observed_states
        return Finding(
            source=FindingSource.CHECK,
            category=FindingCategory.NETWORK,
            code="wired.l2.loop.self_loop",
            severity=severity,
            confidence=confidence,
            message=message,
            affected_entities=pair_ids,
            subject=ObjectRef("port", pair_ids[0]),
            evidence=evidence,
            caused_by=caused_by,
        )

    @staticmethod
    def _rank(side: AnalysisContext, cycle: Cycle) -> int:
        """Condition severity of a cycle in ONE IR: protected(0)<unknown(1)<disabled(2)."""
        states = [side.ir.port(p).stp_enabled for p in cycle.member_ports]
        if any(s is False for s in states):
            return 2
        if any(s is None for s in states):
            return 1
        return 0

    def _judge(
        self, ctx: CheckContext, vid: int, cycle: Cycle, attributable: bool
    ) -> tuple[Finding | None, Status]:
        ports = [ctx.proposed.ir.port(p) for p in cycle.member_ports]
        disabled = [p.id for p in ports if p.stp_enabled is False]
        unknown = [p.id for p in ports if p.stp_enabled is None]
        if not attributable:  # condition no worse than baseline: context, not caused
            return (
                self._finding(
                    code="wired.l2.loop.preexisting",
                    severity=Severity.INFO,
                    confidence=cycle.confidence,
                    message=(
                        f"pre-existing cycle on vlan {vid} (condition unchanged by delta — context)"
                    ),
                    cycle=cycle,
                    vid=vid,
                    caused_by=(),  # pre-existing: not attributed to the delta
                ),
                Status.PASS,
            )
        caused_by = causes_for_loop(ctx, cycle)
        if disabled:
            return (
                self._finding(
                    code="wired.l2.loop.unprotected",
                    severity=Severity.ERROR,
                    confidence=min_confidence(
                        cycle.confidence, Confidence(level=ConfidenceLevel.HIGH)
                    ),
                    message=(
                        f"new cycle on vlan {vid} with STP DISABLED on "
                        f"{len(disabled)} port(s) — unprotected redundant path"
                    ),
                    cycle=cycle,
                    vid=vid,
                    extra={"stp_disabled_ports": disabled},
                    caused_by=caused_by,
                ),
                Status.FAIL,
            )
        if unknown:
            return (
                self._finding(
                    code="wired.l2.loop.unverified",
                    severity=Severity.WARNING,
                    confidence=Confidence(
                        level=ConfidenceLevel.LOW,
                        reasons=tuple(f"STP state unknown on {p}" for p in unknown[:5]),
                    ),
                    message=f"new cycle on vlan {vid}; STP state unverified — potential loop",
                    cycle=cycle,
                    vid=vid,
                    extra={"stp_unknown_ports": unknown},
                    caused_by=caused_by,
                ),
                Status.WARN,
            )
        return (
            self._finding(
                code="wired.l2.loop.protected",
                severity=Severity.INFO,
                confidence=min_confidence(cycle.confidence, Confidence(level=ConfidenceLevel.HIGH)),
                message=f"new cycle on vlan {vid} fully STP-protected (redundancy, not a loop)",
                cycle=cycle,
                vid=vid,
                caused_by=caused_by,
            ),
            Status.PASS,
        )

    def _finding(
        self,
        *,
        code: str,
        severity: Severity,
        confidence: Confidence,
        message: str,
        cycle: Cycle,
        vid: int,
        extra: dict[str, object] | None = None,
        caused_by: tuple[Cause, ...] = (),
    ) -> Finding:
        return Finding(
            source=FindingSource.CHECK,
            category=FindingCategory.NETWORK,
            code=code,
            severity=severity,
            confidence=confidence,
            message=message,
            affected_entities=cycle.member_ports,
            subject=ObjectRef("vlan", str(vid)),
            evidence={
                "vlan": vid,
                "cycle_nodes": list(cycle.nodes),
                "link_ids": list(cycle.link_ids),
                **(extra or {}),
            },
            caused_by=caused_by,
        )


_ORDER = [Status.PASS, Status.WARN, Status.FAIL]


def _worse(a: Status, b: Status) -> Status:
    return a if _ORDER.index(a) >= _ORDER.index(b) else b
