"""Static-route change warnings without simulating a live routing table."""

from __future__ import annotations

import ipaddress

from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState, Status
from digital_twin.checks.wired.config_lint import touched_ids
from digital_twin.contracts import Finding, FindingCategory, FindingSource, ObjectRef, Severity
from digital_twin.ir import IR, Capability, Confidence, ConfidenceLevel, IRCapability, IRDiff

_HIGH = Confidence(level=ConfidenceLevel.HIGH)


def changed_l3_devices(ctx: CheckContext) -> set[str]:
    ids = touched_ids(ctx.diff, "l3intf")
    return {
        i.device_id for ir in (ctx.baseline.ir, ctx.proposed.ir) for i in ir.l3intfs if i.id in ids
    }


def _connected(ir: IR, did: str, hop: str) -> bool:
    ip = ipaddress.ip_address(hop)
    for intf in ir.l3intfs:
        if intf.device_id != did:
            continue
        subnet = intf.subnet
        if not subnet and intf.addressing == "static" and intf.ip and intf.netmask:
            subnet = f"{intf.ip}/{intf.netmask}"
        if not subnet:
            continue
        try:
            net = ipaddress.ip_network(subnet, strict=False)
        except ValueError:
            continue
        if ip.version == net.version and ip in net:
            return True
    return False


class StaticRouteReachabilityCheck:
    id = "wired.l3.static_route_reachability"
    title = "Static route forwarding requires verification"
    domain = "wired.l3"
    default_severity = Severity.WARNING

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.L3_EXITS})

    def applies_to(self, diff: IRDiff) -> bool:
        return diff.touches("static_route") or diff.touches("l3intf")

    def run(self, ctx: CheckContext) -> CheckResult:
        touched = touched_ids(ctx.diff, "static_route")
        l3_devices = changed_l3_devices(ctx)
        base = {r.id: r for r in ctx.baseline.ir.static_routes}
        prop = {r.id: r for r in ctx.proposed.ir.static_routes}
        findings = []
        for rid in sorted(base.keys() | prop.keys()):
            route = prop.get(rid, base.get(rid))
            assert route is not None
            if rid not in touched and route.device_id not in l3_devices:
                continue
            if rid not in prop:
                code, detail = "removed", "configured static route removed"
            elif route.unresolved:
                code, detail = (
                    "unresolved",
                    "destination or next-hop configuration cannot be resolved",
                )
            elif route.discard:
                code, detail = "discard", "configured discard route may drop matching traffic"
            elif not all(_connected(ctx.proposed.ir, route.device_id, h) for h in route.next_hops):
                code, detail = (
                    "recursive_resolution",
                    "next hops require resolution beyond modeled connected subnets",
                )
            else:
                code, detail = (
                    "forwarding_unverified",
                    "static-route forwarding requires live verification",
                )
            l3_ids = sorted(
                i.id
                for ir in (ctx.baseline.ir, ctx.proposed.ir)
                for i in ir.l3intfs
                if i.device_id == route.device_id
            )
            findings.append(
                Finding(
                    source=FindingSource.CHECK,
                    category=FindingCategory.NETWORK,
                    code=f"{self.id}.{code}",
                    severity=Severity.WARNING,
                    confidence=_HIGH,
                    message=f"{route.device_id} {route.destination}: {detail}",
                    subject=ObjectRef("device", route.device_id),
                    affected_entities=(route.device_id,),
                    evidence={
                        "route": rid,
                        "destination": route.destination,
                        "next_hops": list(route.next_hops),
                        "discard": route.discard,
                    },
                    caused_by=tuple(
                        dict.fromkeys(
                            (
                                *ctx.delta_index.causes("static_route", [rid]),
                                *ctx.delta_index.causes("l3intf", l3_ids),
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
                ("live RIB/FIB, recursive resolution and competing routes are not modeled",)
                if findings
                else (),
            ),
            reasoning="compared configured routes and local L3 dependencies; forwarding unverified",
        )
