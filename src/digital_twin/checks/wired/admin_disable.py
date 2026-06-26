"""wired.port.admin_disable — administratively disabling a switch port.

Disabling a port (inline `disabled` on local_port_config / port_config_overwrite,
or a `usage: "disabled"` reassignment) takes the link down: Port.disabled
forwards NOTHING and the L2 graph drops its edge, so a disabled trunk/uplink
strands downstream segments (wired.l2.blackhole). This check surfaces the ACTION
and weights it by blast radius — an AP uplink, a modeled inter-switch/gateway
link, a port with active wired clients, or a trunk that is an uplink or unknown
floor REVIEW (or UNSAFE at HIGH confidence); a bare edge port or a trunk with
is_uplink=False and no modeled peer/AP/client is INFO context.

ERROR is emitted ONLY when the port<->peer tie is HIGH-confidence: decide()
floors UNSAFE on any network ERROR before consulting confidence, so a
MEDIUM/one-sided LLDP tie caps at WARNING even when wireless clients are observed
on the AP (observed clients raise the consequence, not the tie). Complementary to
wired.l2.blackhole (action vs consequence); both may fire on one delta.
"""

from __future__ import annotations

from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState, Status
from digital_twin.checks.wired.poe_disconnect import _ap_uplink_ports
from digital_twin.contracts import Finding, FindingCategory, FindingSource, ObjectRef, Severity
from digital_twin.ir import (
    Capability,
    Confidence,
    ConfidenceLevel,
    IRCapability,
    IRDiff,
    Link,
    min_confidence,
)
from digital_twin.ir.entities import Client, DeviceRole, Port, PortMode
from digital_twin.ir.indexes import clients_by_ap, clients_by_port
from digital_twin.ir.model import IR

_HIGH = Confidence(level=ConfidenceLevel.HIGH)
# (severity, confidence, finding code, message, headline subject)
_Verdict = tuple[Severity, Confidence, str, str, ObjectRef]


def _nonap_peer_links(ir: IR) -> dict[str, Link]:
    """switch-port id -> the baseline link to a managed NON-AP peer (inter-switch
    / gateway uplink). Carries the LINK so classification can use its confidence —
    a one-sided LLDP peer is weaker evidence than a two-sided one."""
    out: dict[str, Link] = {}
    for lk in ir.links:
        pa, pb = ir.ports.get(lk.a_port), ir.ports.get(lk.b_port)
        if pa is None or pb is None:
            continue
        a_ap = ir.devices[pa.device_id].role is DeviceRole.AP
        b_ap = ir.devices[pb.device_id].role is DeviceRole.AP
        if not a_ap and not b_ap:
            out[pa.id] = lk
            out[pb.id] = lk
    return out


class AdminDisableCheck:
    id = "wired.port.admin_disable"
    title = "Administratively disabling a switch port"
    domain = "wired.port"
    default_severity = Severity.WARNING

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2})

    def applies_to(self, diff: IRDiff) -> bool:
        return diff.touches("port")

    def run(self, ctx: CheckContext) -> CheckResult:
        base_ir, prop_ir = ctx.baseline.ir, ctx.proposed.ir
        ap_ports = _ap_uplink_ports(base_ir)
        nonap_peers = _nonap_peer_links(base_ir)
        wired = clients_by_port(base_ir)
        ap_clients = clients_by_ap(base_ir)
        findings: list[Finding] = []
        for pid, prop_port in prop_ir.ports.items():
            if not prop_port.disabled:
                continue
            base_port = base_ir.ports.get(pid)
            if base_port is not None and base_port.disabled:
                continue  # already disabled -> not the delta
            findings.append(
                self._finding(ctx, pid, base_port, ap_ports, nonap_peers, wired, ap_clients)
            )
        worst = Status.PASS
        for f in findings:
            if f.severity is Severity.ERROR:
                worst = Status.FAIL
            elif f.severity is Severity.WARNING and worst is Status.PASS:
                worst = Status.WARN
        return CheckResult(
            check_id=self.id,
            status=worst,
            findings=tuple(findings),
            coverage=Coverage(state=CoverageState.COMPLETE),
            confidence=min_confidence(*(f.confidence for f in findings)) if findings else _HIGH,
            reasoning="compared per-port admin-disable state baseline vs proposed",
        )

    def _finding(
        self,
        ctx: CheckContext,
        pid: str,
        base_port: Port | None,
        ap_ports: dict[str, tuple[str, Link]],
        nonap_peers: dict[str, Link],
        wired: dict[str, list[Client]],
        ap_clients: dict[str, list[Client]],
    ) -> Finding:
        severity, confidence, code, message, subject = self._classify(
            pid, base_port, ap_ports, nonap_peers, wired, ap_clients
        )
        return Finding(
            source=FindingSource.CHECK,
            category=FindingCategory.NETWORK,
            code=code,
            severity=severity,
            confidence=confidence,
            message=message,
            affected_entities=(subject.id,),
            subject=subject,
            evidence={"port": pid, "disabled": True},
            caused_by=tuple(c for c in (ctx.delta_index.cause("port", pid),) if c is not None),
        )

    def _classify(
        self,
        pid: str,
        base_port: Port | None,
        ap_ports: dict[str, tuple[str, Link]],
        nonap_peers: dict[str, Link],
        wired: dict[str, list[Client]],
        ap_clients: dict[str, list[Client]],
    ) -> _Verdict:
        port_ref = ObjectRef("port", pid)
        if base_port is None:
            return (
                Severity.INFO, _HIGH, "wired.port.admin_disable.unattributable",
                f"port {pid} administratively disabled — no baseline state, blast radius unknown",
                port_ref,
            )
        ap = ap_ports.get(pid)
        if ap is not None:
            ap_id, lk = ap
            conf = lk.meta.confidence
            high = conf.level is ConfidenceLevel.HIGH
            n = len(ap_clients.get(ap_id, []))
            return (
                Severity.ERROR if high else Severity.WARNING, conf,
                "wired.port.admin_disable.impact",
                f"port {pid} administratively disabled — AP {ap_id} ({n} observed "
                "wireless client(s)) loses its uplink",
                ObjectRef("device", ap_id),
            )
        n_wired = len(wired.get(pid, []))
        if n_wired:
            return (
                Severity.WARNING, _HIGH, "wired.port.admin_disable.impact",
                f"port {pid} administratively disabled — {n_wired} active wired client(s) "
                "disconnect",
                port_ref,
            )
        peer_lk = nonap_peers.get(pid)
        if peer_lk is not None:
            # a modeled inter-switch / gateway link: confidence is the LINK's
            # (a one-sided LLDP peer is weaker than a two-sided one)
            return (
                Severity.WARNING, peer_lk.meta.confidence, "wired.port.admin_disable.impact",
                f"port {pid} administratively disabled — an inter-switch / gateway link goes down",
                port_ref,
            )
        if base_port.mode is PortMode.TRUNK:
            if base_port.is_uplink is False:
                # POSITIVE evidence it is not an uplink and has no modeled peer/AP/
                # client -> a configured-but-unconnected trunk, no impact (INFO).
                return (
                    Severity.INFO, _HIGH, "wired.port.admin_disable.edge",
                    f"port {pid} administratively disabled — trunk port with no modeled "
                    "uplink or downstream, no impact",
                    port_ref,
                )
            # is_uplink True (faces the core) OR None (unknown) -> conservative WARNING
            return (
                Severity.WARNING, _HIGH, "wired.port.admin_disable.impact",
                f"port {pid} administratively disabled — a trunk link goes down",
                port_ref,
            )
        return (
            Severity.INFO, _HIGH, "wired.port.admin_disable.edge",
            f"port {pid} administratively disabled — edge port, no downstream impact modeled",
            port_ref,
        )
