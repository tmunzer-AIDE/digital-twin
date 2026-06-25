"""wired.l1.link_param_mismatch — incompatible speed/duplex/autoneg across a link.

A duplex/autoneg mismatch is invisible to reachability (the link carries every
VLAN, pings work) yet silently wrecks throughput — same class as the MTU check.
Per evaluable boundary (BoundaryView, ap_transparent=False — L1 exists on every
Ethernet link), classify each end as forced / autonegotiating / unknown-peer and
compare:

- both forced, different speed -> ERROR (.speed_conflict) — link won't establish
- both forced, same speed, different duplex -> ERROR (.duplex_conflict)
- one forced / one autonegotiating -> WARNING (.autoneg_mismatch) — auto side
  falls to half-duplex; 1g+ may not link
- one forced / one unknown-peer (no config facts) -> WARNING (.unverified)
- otherwise -> silent

Observed negotiated state (port_stats) enriches the PRE-EXISTING branch only —
it can confirm a live symptom or show the hardware negotiated around a predicted
mismatch, but it can never prove a post-change (introduced) outcome. An introduced
mismatch's severity/confidence come from config + link provenance alone.
"""

from __future__ import annotations

from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState, Status
from digital_twin.contracts import Finding, FindingCategory, FindingSource, ObjectRef, Severity
from digital_twin.ir import (
    Capability,
    Confidence,
    ConfidenceLevel,
    IRCapability,
    IRDiff,
    min_confidence,
)
from digital_twin.ir.entities import Port

from .link_boundary import BoundaryView, config_stated

_HIGH = Confidence(level=ConfidenceLevel.HIGH)
_UNVERIFIED = Confidence(
    level=ConfidenceLevel.MEDIUM,
    reasons=("peer port has no config facts — an L1 mismatch cannot be ruled out",),
)


def _forced(p: Port) -> bool:
    return p.autoneg_disabled and p.speed is not None and p.duplex is not None


def _classify(pa: Port, pb: Port) -> tuple[str, Severity, str] | None:
    """(code-suffix, base severity, message) for a config L1 incompatibility, else None."""
    fa, fb = _forced(pa), _forced(pb)
    if fa and fb:
        if pa.speed != pb.speed:
            return ("speed_conflict", Severity.ERROR,
                    f"forced speeds differ ({pa.speed} vs {pb.speed}) — link will not establish")
        if pa.duplex != pb.duplex:
            return ("duplex_conflict", Severity.ERROR,
                    f"forced duplex differs ({pa.duplex} vs {pb.duplex}) at {pa.speed}")
        return None
    if fa != fb:
        fp, other = (pa, pb) if fa else (pb, pa)
        hard = f"{fp.id} hard-set {fp.speed}/{fp.duplex} (autoneg off)"
        if config_stated(other):
            return ("autoneg_mismatch", Severity.WARNING,
                    f"{hard} but peer {other.id} autonegotiates — duplex-mismatch risk")
        return ("unverified", Severity.WARNING,
                f"{hard} but peer {other.id} has no config facts — mismatch cannot be ruled out")
    return None


def _l1(p: Port) -> tuple[str | None, str | None, bool]:
    return p.speed, p.duplex, p.autoneg_disabled


def _l1_sig(p: Port) -> tuple[str | None, str | None, bool, bool]:
    """Parity signature = the L1 tuple PLUS the endpoint class (config_stated).
    The L1 tuple alone is identical for a config-stated autonegotiating port and a
    no-config unknown peer ((None, None, False) both) — but they classify
    differently (.autoneg_mismatch vs .unverified), so config_stated MUST be part
    of the signature or a peer becoming config-stated would be wrongly demoted."""
    return (*_l1(p), config_stated(p))


def _same_l1(base_pair: tuple[Port, Port] | None, pa: Port, pb: Port) -> bool:
    """The same config L1 AND endpoint class on both ends already lived on the
    baseline boundary (matched by port id) — so the mismatch is pre-existing, not
    delta-caused."""
    if base_pair is None:
        return False
    by_id = {p.id: p for p in base_pair}
    ba, bb = by_id.get(pa.id), by_id.get(pb.id)
    return (
        ba is not None
        and bb is not None
        and _l1_sig(ba) == _l1_sig(pa)
        and _l1_sig(bb) == _l1_sig(pb)
    )


def _clean_negotiation(base_pair: tuple[Port, Port]) -> bool:
    """Both baseline ends observed full-duplex at the same known speed — the
    hardware negotiated a working link despite the config-predicted mismatch."""
    a, b = base_pair
    return (
        a.observed_duplex == "full" and b.observed_duplex == "full"
        and a.observed_speed is not None and a.observed_speed == b.observed_speed
    )


def _observed_half(base_pair: tuple[Port, Port]) -> bool:
    return any(p.observed_duplex == "half" for p in base_pair)


class L1ParamMismatchCheck:
    id = "wired.l1.link_param_mismatch"
    title = "Speed/duplex/autoneg mismatch across a link"
    domain = "wired.l1"
    default_severity = Severity.ERROR

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2})

    def applies_to(self, diff: IRDiff) -> bool:
        return any(diff.touches(k) for k in ("link", "port", "device"))

    def run(self, ctx: CheckContext) -> CheckResult:
        prop_view = BoundaryView(ctx.proposed.ir, ap_transparent=False)
        base_view = BoundaryView(ctx.baseline.ir, ap_transparent=False)
        findings: list[Finding] = []
        for lnk in ctx.proposed.ir.links:
            pair = prop_view.pair(lnk)
            if pair is None:
                continue
            pa, pb = pair
            verdict = _classify(pa, pb)
            if verdict is None:
                continue
            code, base_sev, message = verdict
            base_pair = base_view.pair(lnk)
            preexisting = _same_l1(base_pair, pa, pb)
            if preexisting:
                assert base_pair is not None  # _same_l1 is False when None
                if code == "unverified":
                    continue  # baseline-parity suppression (stale no-facts uncertainty)
                if _clean_negotiation(base_pair):
                    continue  # hardware negotiated around it, unchanged by the delta
                severity, code_out = Severity.INFO, "preexisting"
                live = "; a peer is observed half-duplex" if _observed_half(base_pair) else ""
                message = (
                    f"link {pa.id} <-> {pb.id}: pre-existing L1 mismatch, unchanged by the "
                    f"delta (context{live})"
                )
                confidence = min_confidence(
                    pa.meta.confidence, pb.meta.confidence, lnk.meta.confidence
                )
            else:
                confidence = min_confidence(
                    pa.meta.confidence, pb.meta.confidence, lnk.meta.confidence
                )
                if code == "unverified":
                    confidence = min_confidence(confidence, _UNVERIFIED)
                high = confidence.level is ConfidenceLevel.HIGH
                severity = base_sev if (base_sev is Severity.ERROR and high) else Severity.WARNING
                code_out = code
            findings.append(
                Finding(
                    source=FindingSource.CHECK,
                    category=FindingCategory.NETWORK,
                    code=f"{self.id}.{code_out}",
                    severity=severity,
                    confidence=confidence,
                    message=f"link {pa.id} <-> {pb.id}: {message}" if code_out != "preexisting"
                    else message,
                    affected_entities=(pa.id, pb.id),
                    subject=ObjectRef("link", lnk.id),
                    evidence={
                        "link": lnk.id,
                        "a_port": pa.id,
                        "b_port": pb.id,
                        "a_l1": list(_l1(pa)),
                        "b_l1": list(_l1(pb)),
                    },
                    caused_by=tuple(
                        c for c in (
                            ctx.delta_index.cause("port", lnk.a_port),
                            ctx.delta_index.cause("port", lnk.b_port),
                            ctx.delta_index.cause("link", lnk.id),
                        ) if c is not None
                    ) if severity is not Severity.INFO else (),
                )
            )
        worst = Status.PASS
        conclusions = [f for f in findings if f.severity is not Severity.INFO]
        for f in conclusions:
            this = Status.FAIL if f.severity is Severity.ERROR else Status.WARN
            if this is Status.FAIL or worst is Status.PASS:
                worst = this
        return CheckResult(
            check_id=self.id,
            status=worst,
            findings=tuple(findings),
            coverage=Coverage(state=CoverageState.COMPLETE),
            confidence=(
                min_confidence(*(f.confidence for f in conclusions)) if conclusions else _HIGH
            ),
            reasoning="compared L1 speed/duplex/autoneg across both ends of every link",
        )
