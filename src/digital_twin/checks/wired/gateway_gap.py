"""wired.l3.gateway_gap — a ROUTED network with no L3 interface (MVP: ROUTE-GW).

A network that declares a subnet (Vlan.subnet) states routed intent: something
must own its L3 interface — a switch IRB (other_ip_configs) or the gateway
(device ip_configs), both modeled in the IR. Unlike the blackhole check this
needs NO members: an empty-but-routed vlan losing its gateway is still a real
config break for everything that joins it later.

Honesty grades (the L3 model is not exhaustive — BGP-learned or
config-cmd-driven interfaces are invisible):
- the delta REMOVES the only MODELED L3 interface of a routed vlan -> we SAW
  the interface and saw it go: ERROR at the removed fact's confidence
  (UNSAFE at HIGH);
- routed intent NEWLY declared with no modeled interface anywhere -> it may
  live on an unmodeled box: WARNING capped MEDIUM (.unserved -> REVIEW);
- the same gap already in the baseline -> INFO context only.

.gateway_unowned (GS22-GW): when L3 interfaces EXIST on a vlan, the declared
default gateway must be OWNED by one of them — breaking a KNOWN baseline owner
is strong evidence (ERROR); an absence with no known owner is honest REVIEW
(WARNING/MEDIUM, the owner may live on an unmodeled box); unknown ownership
(unresolved intent, unparseable IPs) abstains with a coverage note, never a
violation.
"""

from __future__ import annotations

from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState, Status
from digital_twin.contracts import Finding, FindingCategory, FindingSource, Severity
from digital_twin.ir import (
    Capability,
    Confidence,
    ConfidenceLevel,
    IRCapability,
    IRDiff,
    min_confidence,
    same_ip,
)
from digital_twin.ir.entities import L3Intf
from digital_twin.ir.model import IR

_HIGH = Confidence(level=ConfidenceLevel.HIGH)
_UNMODELED = Confidence(
    level=ConfidenceLevel.MEDIUM,
    reasons=("the L3 interface may live on a device the model does not cover",),
)
_BLIND_GATEWAY = Confidence(
    level=ConfidenceLevel.MEDIUM,
    reasons=("an unmodeled gateway may hold the replacement L3 interface",),
)


def _l3_by_vlan(ir: IR) -> dict[int, list[L3Intf]]:
    out: dict[int, list[L3Intf]] = {}
    for intf in ir.l3intfs:
        if intf.vlan_id is not None:
            out.setdefault(intf.vlan_id, []).append(intf)
    return out


class GatewayGapCheck:
    id = "wired.l3.gateway_gap"
    title = "routed network without an L3 interface"
    domain = "wired.l3"
    default_severity = Severity.ERROR

    def requires(self) -> frozenset[Capability]:
        # the core predicate consumes ir.l3intfs: without L3_EXITS the check
        # must be INSUFFICIENT_DATA, never a conclusion over missing facts
        return frozenset({IRCapability.WIRED_L2, IRCapability.L3_EXITS})

    def applies_to(self, diff: IRDiff) -> bool:
        # routed intent lives on vlans; the interfaces are l3intf entities
        return any(diff.touches(k) for k in ("vlan", "l3intf"))

    def run(self, ctx: CheckContext) -> CheckResult:
        base_ir, prop_ir = ctx.baseline.ir, ctx.proposed.ir
        base_l3, prop_l3 = _l3_by_vlan(base_ir), _l3_by_vlan(prop_ir)
        # a gateway whose network namespace was not fetched has UNKNOWN L3
        # interfaces — it degrades NEGATIVE-existence conclusions only (every
        # finding this check emits is one: "no modeled L3 in proposed").
        # A routed vlan SERVED by a modeled interface is a positive fact the
        # blind gateway cannot taint.
        blind_notes = tuple(
            f"gateway {d.id}: network namespace unmodeled (org networks not "
            "fetched) — its L3 interfaces are invisible to this check"
            for d in sorted(prop_ir.devices.values(), key=lambda d: d.id)
            if d.l3_unmodeled
        )
        findings: list[Finding] = []
        for vid, vlan in sorted(prop_ir.vlans.items()):
            if vlan.subnet is None or prop_l3.get(vid):
                continue  # not routed, or served
            base_vlan = base_ir.vlans.get(vid)
            base_intfs = base_l3.get(vid, [])
            if base_intfs:
                severity, code = Severity.ERROR, "removed"
                confidence = min_confidence(*(i.meta.confidence for i in base_intfs))
                if blind_notes:
                    # the invisible replacement may live on the blind gateway:
                    # never a confident ERROR/HIGH -> caps to WARNING/MEDIUM
                    confidence = min_confidence(confidence, _BLIND_GATEWAY)
                message = (
                    f"routed network (vlan {vid}, subnet {vlan.subnet}) loses its only "
                    f"modeled L3 interface ({', '.join(i.id for i in base_intfs)}) — "
                    "nothing routes the subnet"
                )
            elif base_vlan is None or base_vlan.subnet is None:
                severity, code = Severity.WARNING, "unserved"
                confidence = _UNMODELED
                message = (
                    f"network (vlan {vid}) is declared routed (subnet {vlan.subnet}) but "
                    "no modeled device provides an L3 interface for it"
                )
            else:
                severity, code = Severity.INFO, "preexisting"
                confidence = _UNMODELED
                message = (
                    f"vlan {vid}: pre-existing routed network without a modeled L3 "
                    "interface, unchanged by the delta (context)"
                )
            high = confidence.level is ConfidenceLevel.HIGH
            if severity is Severity.ERROR and not high:
                severity = Severity.WARNING
            findings.append(
                Finding(
                    source=FindingSource.CHECK,
                    category=FindingCategory.NETWORK,
                    code=f"{self.id}.{code}",
                    severity=severity,
                    confidence=confidence,
                    message=message,
                    affected_entities=(str(vid),),
                    evidence={
                        "vlan": vid,
                        "subnet": vlan.subnet,
                        "baseline_l3_interfaces": [i.id for i in base_intfs],
                    },
                )
            )
        # --- .gateway_unowned: interfaces EXIST but none owns the declared
        # gateway (strict precedence: the no-interface cases belong to the
        # existence codes above; never double-fire)
        changed_vlan_ids = {
            r.id for r in (*ctx.diff.added, *ctx.diff.removed,
                           *(m.ref for m in ctx.diff.modified))
            if r.kind == "vlan"
        }
        l3_touched_vlans = {
            r.id.rsplit(":", 1)[-1]
            for r in (*ctx.diff.added, *ctx.diff.removed,
                      *(m.ref for m in ctx.diff.modified))
            if r.kind == "l3intf"
        }
        abstain_notes: list[str] = []
        for vid, vlan in sorted(prop_ir.vlans.items()):
            intfs = prop_l3.get(vid)
            if not intfs:
                continue  # existence codes own this case
            relevant = str(vid) in changed_vlan_ids or str(vid) in l3_touched_vlans
            if vlan.gateway_unresolved:
                if relevant:
                    abstain_notes.append(
                        f"vlan {vid}: declared default gateway is unreadable or "
                        "ambiguous — ownership cannot be verified"
                    )
                continue
            g = vlan.gateway
            if g is None:
                continue  # no declared intent
            verdicts = [same_ip(i.ip, g) for i in intfs]
            if any(v is True for v in verdicts):
                continue  # owned — a positive fact nothing can taint
            if any(v is None for v in verdicts):
                if relevant:
                    abstain_notes.append(
                        f"vlan {vid}: an L3 interface has an unknown/unparseable "
                        f"address — it may own the declared gateway {g}"
                    )
                continue
            # definitively unowned: parity + severity per the doctrine
            base_vlan = base_ir.vlans.get(vid)
            base_g = base_vlan.gateway if base_vlan is not None else None
            base_intfs = base_l3.get(vid, [])
            owners = [
                i for i in base_intfs
                if base_g is not None and same_ip(i.ip, base_g) is True
            ]
            if owners:
                # known owner broken (G moved, or the owner changed/left)
                severity, code = Severity.ERROR, "gateway_unowned"
                confidence = min_confidence(*(i.meta.confidence for i in owners))
                if blind_notes:
                    confidence = min_confidence(confidence, _BLIND_GATEWAY)
                message = (
                    f"vlan {vid}: declared default gateway {g} is owned by NO "
                    f"modeled L3 interface — the baseline owner "
                    f"({', '.join(i.id for i in owners)}) no longer matches"
                )
            elif (
                base_vlan is not None
                and not base_vlan.gateway_unresolved
                and base_g == g
                and base_intfs
                and all(same_ip(i.ip, base_g) is False for i in base_intfs)
            ):
                severity, code = Severity.INFO, "gateway_unowned"
                confidence = _UNMODELED
                message = (
                    f"vlan {vid}: pre-existing unowned declared gateway {g}, "
                    "unchanged by the delta (context)"
                )
            else:
                # never owned / newly declared / G changed between unowned
                # values: there was no KNOWN owner -> honest REVIEW, never
                # ERROR (the owner may live on an unmodeled box)
                severity, code = Severity.WARNING, "gateway_unowned"
                confidence = _UNMODELED
                message = (
                    f"vlan {vid}: declared default gateway {g} is owned by no "
                    "modeled L3 interface"
                )
            high = confidence.level is ConfidenceLevel.HIGH
            if severity is Severity.ERROR and not high:
                severity = Severity.WARNING
            findings.append(
                Finding(
                    source=FindingSource.CHECK,
                    category=FindingCategory.NETWORK,
                    code=f"{self.id}.{code}",
                    severity=severity,
                    confidence=confidence,
                    message=message,
                    affected_entities=(str(vid),),
                    evidence={"vlan": vid, "gateway": g,
                              "l3_interfaces": [i.id for i in intfs]},
                )
            )
        worst = Status.PASS
        conclusions = [f for f in findings if f.severity is not Severity.INFO]
        for f in conclusions:
            this = Status.FAIL if f.severity is Severity.ERROR else Status.WARN
            if this is Status.FAIL or worst is Status.PASS:
                worst = this
        # INFO .preexisting is context (excluded from verdict floors): only a
        # real CONCLUSION lets the blind-gateway notes degrade coverage;
        # abstain notes attach whenever generated (already delta-scoped)
        notes = (blind_notes if conclusions else ()) + tuple(abstain_notes)
        return CheckResult(
            check_id=self.id,
            status=worst,
            findings=tuple(findings),
            coverage=Coverage(
                state=CoverageState.PARTIAL if notes else CoverageState.COMPLETE,
                notes=notes,
            ),
            confidence=(
                min_confidence(*(f.confidence for f in conclusions)) if conclusions else _HIGH
            ),
            reasoning="matched every routed network against the modeled L3 interfaces",
        )
