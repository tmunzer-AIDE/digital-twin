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
)
from digital_twin.ir.entities import L3Intf
from digital_twin.ir.model import IR

_HIGH = Confidence(level=ConfidenceLevel.HIGH)
_UNMODELED = Confidence(
    level=ConfidenceLevel.MEDIUM,
    reasons=("the L3 interface may live on a device the model does not cover",),
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
        # interfaces — every "no modeled L3" conclusion is partial over it
        notes = tuple(
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
            coverage=Coverage(
                state=CoverageState.PARTIAL if notes else CoverageState.COMPLETE,
                notes=notes,
            ),
            confidence=(
                min_confidence(*(f.confidence for f in conclusions)) if conclusions else _HIGH
            ),
            reasoning="matched every routed network against the modeled L3 interfaces",
        )
