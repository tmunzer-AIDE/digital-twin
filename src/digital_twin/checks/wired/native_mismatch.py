"""wired.l2.native_mismatch — a link whose ends disagree on the native VLAN.

Untagged frames leaving end A belong to A's native vlan and arrive inside B's
native vlan: a silent LEAK between the two vlans (plus a one-way blackhole for
whatever expected the untagged path). The L2 graph deliberately does NOT carry
a mismatched native (link_carried_vlans), so reachability checks see the lost
path — but the LEAK itself is invisible to them. This check names it.

Attribution and honesty:
- mismatch introduced or ALTERED by the delta -> ERROR at the claim's
  confidence (min over both port facts and the link's existence);
- the same mismatch already present in the baseline -> INFO context only;
- a native CHANGED against a vlan-blind peer (stat-ensured / unresolved usage:
  carriage unknown) -> the mismatch is unverifiable -> WARNING at MEDIUM,
  never silence;
- AP uplinks are vlan-transparent (the AP end has no native facts by
  construction) -> never a finding;
- an end with NO native (config: nothing untagged) cannot leak -> silent.
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
from digital_twin.ir.entities import DeviceRole, Port
from digital_twin.ir.indexes import node_for, vc_root_map
from digital_twin.ir.model import IR
from digital_twin.ir.provenance import Provenance

_HIGH = Confidence(level=ConfidenceLevel.HIGH)
_UNVERIFIED = Confidence(
    level=ConfidenceLevel.MEDIUM,
    reasons=("peer port has no vlan facts — a native mismatch cannot be ruled out",),
)


def _blind(port: Port) -> bool:
    """Vlan-blind: no vlan facts and NOT a config statement (stat-ensured or
    unresolved usage) — same notion as the L2 graph's assumed-carriage rule."""
    return (
        port.meta.provenance in (Provenance.OBSERVED, Provenance.INFERRED)
        and port.native_vlan is None
        and not port.tagged_vlans
    )


def _native(ir: IR, pid: str) -> int | None:
    port = ir.ports.get(pid)
    return port.native_vlan if port else None


class NativeVlanMismatchCheck:
    id = "wired.l2.native_mismatch"
    title = "native-VLAN mismatch across a link"
    domain = "wired.l2"
    default_severity = Severity.ERROR

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2})

    def applies_to(self, diff: IRDiff) -> bool:
        return diff.touches("port")

    def run(self, ctx: CheckContext) -> CheckResult:
        base_ir, prop_ir = ctx.baseline.ir, ctx.proposed.ir
        vc_root = vc_root_map(prop_ir)
        findings: list[Finding] = []
        for lnk in prop_ir.links:
            pa, pb = prop_ir.ports.get(lnk.a_port), prop_ir.ports.get(lnk.b_port)
            if pa is None or pb is None or pa.disabled or pb.disabled:
                continue
            if node_for(vc_root, pa.device_id) == node_for(vc_root, pb.device_id):
                continue  # VC-internal / self: chassis backplane, not an L2 boundary
            a_ap = prop_ir.devices[pa.device_id].role is DeviceRole.AP
            b_ap = prop_ir.devices[pb.device_id].role is DeviceRole.AP
            if a_ap != b_ap:
                continue  # AP uplink: vlan-transparent, the AP end has no native
            na, nb = pa.native_vlan, pb.native_vlan
            if na is not None and nb is not None and na != nb:
                base_pair = (_native(base_ir, pa.id), _native(base_ir, pb.id))
                preexisting = base_pair == (na, nb)
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
            elif _blind(pa) != _blind(pb):
                cfg, blind = (pb, pa) if _blind(pa) else (pa, pb)
                if cfg.native_vlan is None or _native(base_ir, cfg.id) == cfg.native_vlan:
                    continue  # no untagged side, or the delta didn't touch the native
                severity, code = Severity.WARNING, "unverified"
                confidence = min_confidence(lnk.meta.confidence, _UNVERIFIED)
                message = (
                    f"port {cfg.id} native VLAN changes to {cfg.native_vlan} but peer "
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
                    evidence={
                        "link": lnk.id,
                        "a_port": pa.id,
                        "b_port": pb.id,
                        "a_native": na,
                        "b_native": nb,
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
            reasoning="compared native VLANs across both ends of every link, baseline vs proposed",
        )
