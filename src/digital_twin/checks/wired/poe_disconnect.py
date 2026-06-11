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
disconnect: ERROR/NETWORK -> UNSAFE at the evidence's confidence. An unpowered
port, or one already disabled in the baseline, is not a finding.
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
            powered_by_ap = ap is not None
            observed = base_port.poe_draw
            if not (powered_by_ap or observed):
                continue  # cutting PoE on an unpowered port harms nothing
            confidence = _HIGH if observed else ap[1].meta.confidence  # type: ignore[index]
            high = confidence.level is ConfidenceLevel.HIGH
            ap_id = ap[0] if ap else None
            n_clients = len(clients_per_ap.get(ap_id, [])) if ap_id else 0
            who = (
                f"AP {ap_id} ({n_clients} observed wireless client(s))"
                if ap_id
                else "the device drawing power on it"
            )
            findings.append(
                Finding(
                    source=FindingSource.CHECK,
                    category=FindingCategory.NETWORK,
                    code="wired.poe.disconnect.power_loss",
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
