"""Warn about management dependencies; never infer a cloud outage from intent."""

from __future__ import annotations

from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState, Status
from digital_twin.checks.wired.config_lint import touched_ids
from digital_twin.checks.wired.static_route_reachability import changed_l3_devices
from digital_twin.contracts import Finding, FindingCategory, FindingSource, ObjectRef, Severity
from digital_twin.ir import IR, Capability, Confidence, ConfidenceLevel, IRCapability, IRDiff

_HIGH = Confidence(level=ConfidenceLevel.HIGH)
_FORWARDING_FIELDS = frozenset({"disabled", "mode", "native_vlan", "tagged_vlans"})


def _defaults(ir: IR, did: str) -> set[str]:
    return {
        r.destination
        for r in ir.static_routes
        if r.device_id == did
        and r.vrf == "default"
        and r.destination in {"0.0.0.0/0", "::/0"}
        and not r.discard
        and not r.unresolved
    }


class ControlPlaneReachabilityCheck:
    id = "wired.l3.control_plane_reachability"
    title = "Control-plane routing dependencies changed"
    domain = "wired.l3"
    default_severity = Severity.WARNING

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.L3_EXITS})

    def applies_to(self, diff: IRDiff) -> bool:
        return (
            diff.touches("static_route")
            or diff.touches("l3intf")
            or diff.touches("vrf_instance")
            or any(r.kind == "port" for r in (*diff.added, *diff.removed))
            or any(
                m.ref.kind == "port" and _FORWARDING_FIELDS.intersection(m.changed_fields)
                for m in diff.modified
            )
        )

    def run(self, ctx: CheckContext) -> CheckResult:
        route_ids = touched_ids(ctx.diff, "static_route")
        routes = (*ctx.baseline.ir.static_routes, *ctx.proposed.ir.static_routes)
        devices = {r.device_id for r in routes if r.id in route_ids}
        devices |= changed_l3_devices(ctx) & {r.device_id for r in routes}
        vrf_ids = touched_ids(ctx.diff, "vrf_instance")
        vrfs = (*ctx.baseline.ir.vrf_instances, *ctx.proposed.ir.vrf_instances)
        devices |= {v.device_id for v in vrfs if v.id in vrf_ids}
        ports = {
            m.ref.id
            for m in ctx.diff.modified
            if m.ref.kind == "port" and _FORWARDING_FIELDS.intersection(m.changed_fields)
        } | {r.id for r in (*ctx.diff.added, *ctx.diff.removed) if r.kind == "port"}
        device_ports: dict[str, set[str]] = {}
        for ir in (ctx.baseline.ir, ctx.proposed.ir):
            for pid in sorted(ports):
                port = ir.ports.get(pid)
                if port is not None:
                    device_ports.setdefault(port.device_id, set()).add(pid)
        devices |= device_ports.keys() & {r.device_id for r in routes}
        findings = []
        for did in sorted(devices):
            lost = sorted(_defaults(ctx.baseline.ir, did) - _defaults(ctx.proposed.ir, did))
            code = "default_route_lost" if lost else "path_dependency_changed"
            detail = (
                "configured default route removed or unresolved"
                if lost
                else "routing or interface configuration changed"
            )
            findings.append(
                Finding(
                    source=FindingSource.CHECK,
                    category=FindingCategory.NETWORK,
                    code=f"{self.id}.{code}",
                    severity=Severity.WARNING,
                    confidence=_HIGH,
                    message=f"{did}: {detail}; verify management and authentication connectivity",
                    subject=ObjectRef("device", did),
                    affected_entities=(did,),
                    evidence={
                        "ports": sorted(device_ports.get(did, ())),
                        "lost_configured_defaults": lost,
                        "services_to_verify": [
                            "Mist cloud",
                            "DNS",
                            "NTP",
                            "RADIUS",
                            "TACACS",
                            "syslog",
                        ],
                    },
                    caused_by=tuple(
                        dict.fromkeys(
                            (
                                *ctx.delta_index.causes("port", sorted(device_ports.get(did, ()))),
                                *ctx.delta_index.causes(
                                    "static_route",
                                    sorted(r.id for r in routes if r.device_id == did),
                                ),
                                *ctx.delta_index.causes(
                                    "vrf_instance",
                                    sorted(v.id for v in vrfs if v.device_id == did),
                                ),
                                *ctx.delta_index.causes(
                                    "l3intf",
                                    sorted(
                                        i.id
                                        for ir in (ctx.baseline.ir, ctx.proposed.ir)
                                        for i in ir.l3intfs
                                        if i.device_id == did
                                    ),
                                ),
                            )
                        )
                    ),
                )
            )
        return CheckResult(
            check_id=self.id,
            status=Status.WARN if findings else Status.PASS,
            findings=tuple(findings),
            confidence=_HIGH,
            coverage=Coverage(
                CoverageState.PARTIAL if findings else CoverageState.COMPLETE,
                ("management endpoints, source interfaces and live routes are unavailable",)
                if findings
                else (),
            ),
            reasoning="compared per-device IPv4/IPv6 defaults and configured routing dependencies",
        )
