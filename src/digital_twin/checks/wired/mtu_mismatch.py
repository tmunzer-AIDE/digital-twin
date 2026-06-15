"""wired.l2.mtu_mismatch — ends of a link disagreeing on the interface MTU.

Frames larger than the smaller end silently die on the wire (no ICMP at L2):
classic symptoms are working pings with hanging TLS/transfers. Reachability
analysis can't see it — the link still carries every vlan. This check names it.

Port.mtu is the EXPLICIT config value; None on a CONFIG port means the
platform default (a real statement whose numeric value is unmodeled), while
None on a non-config port (stat-ensured, unresolved usage, or an AP/LLDP end)
means UNKNOWN — split by link_boundary.config_stated. Boundary selection and
baseline parity live in link_boundary.BoundaryView, with ap_transparent=False:
AP transparency is a VLAN property, but MTU exists on every Ethernet link, so
AP uplinks ARE evaluated and their AP end is an unknown.

Attribution and honesty:
- both ends explicit and different, introduced or ALTERED by the delta ->
  ERROR at the claim's confidence (UNSAFE at HIGH); the same pair already
  ACTIVE on the same evaluable baseline boundary -> INFO context;
- one end explicit vs a CONFIG peer on the platform default -> almost
  certainly mismatched but the default's value is unmodeled: WARNING capped
  MEDIUM (.vs_default). Pre-existing identical state -> silent (too soft a
  claim to surface as context);
- one end explicit vs a NON-CONFIG peer (blind / AP end) -> unverifiable ->
  WARNING/MEDIUM (.unverified), suppressed only when the baseline had the SAME
  live link, same non-config end, same explicit mtu (uncertainty symmetry);
- both default / both equal -> silent; VC-internal links never fire.
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
_DEFAULT_ASSUMED = Confidence(
    level=ConfidenceLevel.MEDIUM,
    reasons=("peer runs the platform-default MTU, whose numeric value is not modeled",),
)
_UNVERIFIED = Confidence(
    level=ConfidenceLevel.MEDIUM,
    reasons=("peer port has no config facts — an MTU mismatch cannot be ruled out",),
)


class MtuMismatchCheck:
    id = "wired.l2.mtu_mismatch"
    title = "MTU mismatch across a link"
    domain = "wired.l2"
    default_severity = Severity.ERROR

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2})

    def applies_to(self, diff: IRDiff) -> bool:
        # every entity kind run() reads (same contract note as native_mismatch)
        return any(diff.touches(k) for k in ("link", "port", "device"))

    def run(self, ctx: CheckContext) -> CheckResult:
        # ap_transparent=False: AP transparency is a VLAN property — the AP
        # uplink is still an Ethernet link with an MTU on both ends; the AP
        # end is an UNKNOWN (no config facts), not a non-entity
        prop_view = BoundaryView(ctx.proposed.ir, ap_transparent=False)
        base_view = BoundaryView(ctx.baseline.ir, ap_transparent=False)
        findings: list[Finding] = []
        for lnk in ctx.proposed.ir.links:
            pair = prop_view.pair(lnk)
            if pair is None:
                continue
            pa, pb = pair
            ma, mb = pa.mtu, pb.mtu
            base_pair = base_view.pair(lnk)
            if ma is not None and mb is not None and ma != mb:
                preexisting = base_pair is not None and (
                    base_pair[0].mtu,
                    base_pair[1].mtu,
                ) == (ma, mb)
                confidence = min_confidence(
                    pa.meta.confidence, pb.meta.confidence, lnk.meta.confidence
                )
                high = confidence.level is ConfidenceLevel.HIGH
                if preexisting:
                    severity, code = Severity.INFO, "preexisting"
                    message = (
                        f"link {pa.id} <-> {pb.id}: pre-existing MTU mismatch "
                        f"({ma} vs {mb}), unchanged by the delta (context)"
                    )
                else:
                    severity = Severity.ERROR if high else Severity.WARNING
                    code = "introduced"
                    message = (
                        f"link {pa.id} <-> {pb.id}: MTU mismatch ({ma} vs {mb}) — frames "
                        f"larger than {min(ma, mb)} silently die on this link"
                    )
            elif (ma is None) != (mb is None):
                cfg, other = (pa, pb) if ma is not None else (pb, pa)
                explicit = cfg.mtu
                base_cfg, base_other = _ends(base_pair, cfg.id, other.id)
                if not config_stated(other):
                    if (
                        base_other is not None
                        and not config_stated(base_other)
                        and base_cfg is not None
                        and base_cfg.mtu == explicit
                    ):
                        continue  # same uncertainty was already live in the baseline
                    severity, code = Severity.WARNING, "unverified"
                    confidence = min_confidence(lnk.meta.confidence, _UNVERIFIED)
                    message = (
                        f"port {cfg.id} now runs MTU {explicit} but peer {other.id} has "
                        f"no config facts — an MTU mismatch cannot be ruled out"
                    )
                else:
                    if (
                        base_cfg is not None
                        and base_cfg.mtu == explicit
                        and base_other is not None
                        and base_other.mtu is None
                        and config_stated(base_other)
                    ):
                        continue  # same explicit-vs-default state already live in baseline
                    severity, code = Severity.WARNING, "vs_default"
                    confidence = min_confidence(
                        pa.meta.confidence,
                        pb.meta.confidence,
                        lnk.meta.confidence,
                        _DEFAULT_ASSUMED,
                    )
                    message = (
                        f"port {cfg.id} runs explicit MTU {explicit} but peer {other.id} "
                        f"runs the platform default — almost certainly mismatched"
                    )
                pa, pb, ma, mb = cfg, other, explicit, None
            else:
                continue  # both explicit-and-equal, or both default
            findings.append(
                Finding(
                    source=FindingSource.CHECK,
                    category=FindingCategory.NETWORK,
                    code=f"{self.id}.{code}",
                    severity=severity,
                    confidence=confidence,
                    message=message,
                    affected_entities=(pa.id, pb.id),
                    subject=ObjectRef("link", lnk.id),
                    evidence={
                        "link": lnk.id,
                        "a_port": pa.id,
                        "b_port": pb.id,
                        "a_mtu": ma,
                        "b_mtu": mb,
                    },
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
            reasoning="compared interface MTUs across both ends of every link, base vs proposed",
        )


def _ends(
    base_pair: tuple[Port, Port] | None, cfg_id: str, other_id: str
) -> tuple[Port | None, Port | None]:
    """The baseline ports matching (cfg, other) by port id — (None, None) when
    the baseline had no evaluable boundary there."""
    if base_pair is None:
        return None, None
    by_id = {p.id: p for p in base_pair}
    return by_id.get(cfg_id), by_id.get(other_id)
