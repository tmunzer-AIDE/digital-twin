"""wired.poe.disconnect — cutting PoE to a port that powers a device.

Found in real use (2026-06-10, repeatedly): `poe_disabled` on a switch port that
feeds an AP kills the AP and every client on it, yet nothing reasoned about it.
Power loss needs no VLAN/exit analysis — a port that delivered power and now has
`poe_disabled` disconnects whatever it powered.

A port is POWERING a device when, in the baseline, either:
- an LLDP-confirmed AP uplinks to it (config/topology; confidence = the link's),
  OR
- it is OBSERVED drawing power (stats `poe_on`; HIGH — direct evidence).
Cutting that (baseline PoE deliverable -> proposed `poe_disabled`) is a
disconnect: ERROR/NETWORK -> UNSAFE at the evidence's confidence. A port
OBSERVED not drawing, or one already disabled in the baseline, is not a
finding.

Honesty rails (review round, 2026-06-10):
- `poe_draw is None` means the powered state is UNKNOWABLE (no/blind
  telemetry) — cutting PoE then can never silently PASS: WARNING
  `.unverified` at MEDIUM (a camera/phone could be on the port).
- `base.poe is None` means the baseline INTENT is blind (unresolved usage) —
  an AP link alone then caps at WARNING/MEDIUM, never ERROR/HIGH; only direct
  observed draw restores HIGH.
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
    Link,
    min_confidence,
)
from digital_twin.ir.entities import DeviceRole
from digital_twin.ir.indexes import clients_by_ap
from digital_twin.ir.model import IR

_HIGH = Confidence(level=ConfidenceLevel.HIGH)
_UNOBSERVED = Confidence(
    level=ConfidenceLevel.MEDIUM,
    reasons=("the port's powered state is unobserved — no PoE telemetry for it",),
)
_BLIND_INTENT = Confidence(
    level=ConfidenceLevel.MEDIUM,
    reasons=("baseline PoE intent unknown — the port's usage did not resolve",),
)


def _ap_uplink_ports(ir: IR) -> dict[str, tuple[str, Link]]:
    """switch_port_id -> (ap_device_id, the uplink link) for every AP uplink."""
    out: dict[str, tuple[str, Link]] = {}
    for link in ir.links:
        pa, pb = ir.port(link.a_port), ir.port(link.b_port)
        a_ap = ir.devices[pa.device_id].role is DeviceRole.AP
        b_ap = ir.devices[pb.device_id].role is DeviceRole.AP
        if a_ap == b_ap:
            continue  # not an AP-uplink (both or neither are APs)
        ap_port, sw_port = (pa, pb) if a_ap else (pb, pa)
        out[sw_port.id] = (ap_port.device_id, link)
    return out


class PoeDisconnectCheck:
    id = "wired.poe.disconnect"
    title = "PoE loss disconnects a powered device"
    domain = "wired.poe"
    default_severity = Severity.ERROR

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2})

    def applies_to(self, diff: IRDiff) -> bool:
        return diff.touches("port")

    def run(self, ctx: CheckContext) -> CheckResult:
        base_ir, prop_ir = ctx.baseline.ir, ctx.proposed.ir
        ap_ports = _ap_uplink_ports(base_ir)
        clients_per_ap = clients_by_ap(base_ir)
        findings: list[Finding] = []
        for pid, base_port in base_ir.ports.items():
            prop_port = prop_ir.ports.get(pid)
            if prop_port is None or base_port.poe is False or prop_port.poe is not False:
                continue  # not "was deliverable -> now disabled"
            ap = ap_ports.get(pid)
            observed = base_port.poe_draw  # True/False observed; None = unknowable
            if observed is True:
                confidence, kind = _HIGH, "power_loss"  # direct evidence
            elif ap is not None:
                link_conf = ap[1].meta.confidence
                if base_port.poe is True:
                    confidence, kind = link_conf, "power_loss"
                else:  # AP present but baseline intent blind -> cap at MEDIUM
                    confidence, kind = min_confidence(link_conf, _BLIND_INTENT), "power_loss"
            elif observed is None and base_port.poe is True:
                confidence, kind = _UNOBSERVED, "unverified"
            else:
                # observed NOT drawing (and no AP), or blind-on-blind (intent
                # AND telemetry unknown — the usage blindness is gated elsewhere)
                continue
            high = confidence.level is ConfidenceLevel.HIGH
            ap_id = ap[0] if ap else None
            n_clients = len(clients_per_ap.get(ap_id, [])) if ap_id else 0
            if kind == "unverified":
                who = "anything drawing power on it (powered state unverifiable)"
            elif ap_id:
                who = f"AP {ap_id} ({n_clients} observed wireless client(s))"
            else:
                who = "the device drawing power on it"
            findings.append(
                Finding(
                    source=FindingSource.CHECK,
                    category=FindingCategory.NETWORK,
                    code=f"wired.poe.disconnect.{kind}",
                    severity=Severity.ERROR if high else Severity.WARNING,
                    confidence=confidence,
                    message=(
                        f"port {pid} stops delivering PoE — {who} loses power and "
                        "disconnects"
                    ),
                    affected_entities=(ap_id,) if ap_id else (pid,),
                    evidence={
                        "port": pid,
                        "powered_ap": ap_id,
                        "observed_power_draw": observed,
                        "affected_wireless_clients": n_clients,
                    },
                )
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
            coverage=Coverage(state=CoverageState.COMPLETE),
            confidence=min_confidence(*(f.confidence for f in findings)) if findings else _HIGH,
            reasoning="compared per-port PoE delivery to powered devices baseline vs proposed",
        )
