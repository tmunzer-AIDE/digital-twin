"""wired.l2.native_mismatch — a link whose ends disagree on the native VLAN.

Untagged frames leaving end A belong to A's native vlan and arrive inside B's
native vlan: a silent LEAK between the two vlans (plus a one-way blackhole for
whatever expected the untagged path). The L2 graph deliberately does NOT carry
a mismatched native (link_carried_vlans), so reachability checks see the lost
path — but the LEAK itself is invisible to them. This check names it.

Boundary selection and baseline parity live in link_boundary.BoundaryView —
shared with the other link-walking checks. Attribution and honesty:
- mismatch introduced or ALTERED by the delta -> ERROR at the claim's
  confidence (min over both port facts and the link's existence);
- the same mismatch already ACTIVE in the baseline (same evaluable boundary,
  same pair) -> INFO context only; a disabled/absent/VC-internal/AP-transparent
  baseline link means the delta ACTIVATES the leak -> attributed;
- a native CHANGED against a vlan-blind peer (stat-ensured / unresolved usage:
  carriage unknown), a blind-peer link the delta ACTIVATES, or a peer that
  GOES blind after a verified match -> the mismatch is unverifiable -> WARNING
  at MEDIUM, never silence (pre-existing blind-peer uncertainty stays silent
  only when the baseline had the same live link, same blind end, same native);
- an end with NO native (config: nothing untagged) cannot leak -> silent.
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

from .link_boundary import BoundaryView, vlan_blind

_HIGH = Confidence(level=ConfidenceLevel.HIGH)
_UNVERIFIED = Confidence(
    level=ConfidenceLevel.MEDIUM,
    reasons=("peer port has no vlan facts — a native mismatch cannot be ruled out",),
)


class NativeVlanMismatchCheck:
    id = "wired.l2.native_mismatch"
    title = "native-VLAN mismatch across a link"
    domain = "wired.l2"
    default_severity = Severity.ERROR

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2})

    def applies_to(self, diff: IRDiff) -> bool:
        # the contract is two IRs, not pipeline invariants: a link-only delta
        # can activate a mismatch between unchanged ports, and a device-only
        # delta can change VC folding / AP role — every fact run() consumes
        return any(diff.touches(k) for k in ("link", "port", "device"))

    def run(self, ctx: CheckContext) -> CheckResult:
        prop_view = BoundaryView(ctx.proposed.ir)
        base_view = BoundaryView(ctx.baseline.ir)
        findings: list[Finding] = []
        for lnk in ctx.proposed.ir.links:
            pair = prop_view.pair(lnk)
            if pair is None:
                continue
            pa, pb = pair
            na, nb = pa.native_vlan, pb.native_vlan
            base_pair = base_view.pair(lnk)
            if na is not None and nb is not None and na != nb:
                preexisting = base_pair is not None and (
                    base_pair[0].native_vlan,
                    base_pair[1].native_vlan,
                ) == (na, nb)
                confidence = min_confidence(
                    pa.meta.confidence, pb.meta.confidence, lnk.meta.confidence
                )
                high = confidence.level is ConfidenceLevel.HIGH
                if preexisting:
                    severity, code = Severity.INFO, "preexisting"
                    message = (
                        f"link {pa.id} <-> {pb.id}: pre-existing native VLAN mismatch "
                        f"({na} vs {nb}), unchanged by the delta (context)"
                    )
                else:
                    severity = Severity.ERROR if high else Severity.WARNING
                    code = "introduced"
                    message = (
                        f"link {pa.id} <-> {pb.id}: native VLAN mismatch ({na} vs {nb}) — "
                        f"untagged traffic silently crosses between vlan {na} and vlan {nb}"
                    )
            elif vlan_blind(pa) != vlan_blind(pb):
                cfg, blind = (pb, pa) if vlan_blind(pa) else (pa, pb)
                if cfg.native_vlan is None:
                    continue  # nothing untagged on the configured side: no leak
                if base_pair is not None:
                    base_by_id = {p.id: p for p in base_pair}
                    bb, bc = base_by_id.get(blind.id), base_by_id.get(cfg.id)
                    if (
                        bb is not None
                        and vlan_blind(bb)
                        and bc is not None
                        and bc.native_vlan == cfg.native_vlan
                    ):
                        # suppress ONLY when the baseline already had this exact
                        # uncertainty: same live link, same end blind, same
                        # native. A peer that GOES blind (was known/matching) is
                        # a knowledge regression the delta caused — surface it.
                        continue
                severity, code = Severity.WARNING, "unverified"
                confidence = min_confidence(lnk.meta.confidence, _UNVERIFIED)
                message = (
                    f"port {cfg.id} now delivers native VLAN {cfg.native_vlan} but peer "
                    f"{blind.id} has no vlan facts — a native mismatch cannot be ruled out"
                )
                pa, pb, na, nb = cfg, blind, cfg.native_vlan, None
            else:
                continue
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
                        "a_native": na,
                        "b_native": nb,
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
            reasoning="compared native VLANs across both ends of every link, baseline vs proposed",
        )
