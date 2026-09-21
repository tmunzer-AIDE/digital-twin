"""routing.static_route_reachability — structural static-route hazards."""

from __future__ import annotations

import ipaddress

from digital_twin.checks.base import (
    CheckContext,
    CheckResult,
    Coverage,
    CoverageState,
    status_from_findings,
)
from digital_twin.contracts import Finding, FindingCategory, FindingSource, ObjectRef, Severity
from digital_twin.ir import IR, Capability, Confidence, ConfidenceLevel, IRDiff, StaticRoute

_HIGH = Confidence(level=ConfidenceLevel.HIGH)


def _network(value: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    try:
        return ipaddress.ip_network(value, strict=False)
    except ValueError:
        return None


def _reachable(ir: IR, route: StaticRoute) -> bool:
    for hop in route.next_hops:
        ip = ipaddress.ip_address(hop)
        for intf in ir.l3intfs:
            net = _network(intf.subnet or "")
            if (
                intf.device_id == route.device_id
                and net
                and ip.version == net.version
                and ip in net
            ):
                return True
    return False


def _recursive_loop(route: StaticRoute, routes: tuple[StaticRoute, ...]) -> bool:
    destination = _network(route.destination)
    if destination is None:
        return False
    for hop_text in route.next_hops:
        hop = ipaddress.ip_address(hop_text)
        for candidate in routes:
            candidate_net = _network(candidate.destination)
            if (
                candidate.id == route.id
                or candidate.device_id != route.device_id
                or candidate.vrf != route.vrf
                or candidate_net is None
                or hop.version != candidate_net.version
                or hop not in candidate_net
            ):
                continue
            for return_hop_text in candidate.next_hops:
                return_hop = ipaddress.ip_address(return_hop_text)
                if return_hop.version == destination.version and return_hop in destination:
                    return True
    return False


class StaticRouteReachabilityCheck:
    id = "routing.static_route_reachability"
    title = "Static route reachability"
    domain = "routing.static"
    default_severity = Severity.WARNING

    def requires(self) -> frozenset[Capability]:
        return frozenset()

    def applies_to(self, diff: IRDiff) -> bool:
        return diff.touches("static_route")

    def run(self, ctx: CheckContext) -> CheckResult:
        base = {r.id: r for r in ctx.baseline.ir.static_routes}
        prop = {r.id: r for r in ctx.proposed.ir.static_routes}
        findings: list[Finding] = []
        touched = {
            ref.id
            for ref in (*ctx.diff.added, *ctx.diff.removed, *(m.ref for m in ctx.diff.modified))
            if ref.kind == "static_route"
        }
        for rid in sorted(touched):
            before, after = base.get(rid), prop.get(rid)
            route = after or before
            assert route is not None
            suffix: str | None = None
            message = ""
            if after is None:
                assert before is not None
                alternatives = [
                    r
                    for r in prop.values()
                    if r.device_id == before.device_id
                    and r.vrf == before.vrf
                    and r.destination == before.destination
                ]
                if not alternatives:
                    suffix = "sole_route_removed"
                    message = f"sole modeled static route to {before.destination} is removed"
            elif after.unresolved:
                suffix = "unresolved"
                message = (
                    f"static route {after.destination} has an unreadable destination or next hop"
                )
            elif after.discard:
                net = _network(after.destination)
                covering = [
                    r.destination
                    for r in base.values()
                    if not r.discard
                    and r.device_id == after.device_id
                    and r.vrf == after.vrf
                    and (other := _network(r.destination)) is not None
                    and net is not None
                    and net.version == other.version
                    and int(other.network_address) <= int(net.network_address)
                    and int(net.broadcast_address) <= int(other.broadcast_address)
                    and net != other
                ]
                if covering:
                    suffix = "more_specific_blackhole"
                    message = f"discard route {after.destination} blackholes a more-specific prefix"
            elif _recursive_loop(after, ctx.proposed.ir.static_routes):
                suffix = "recursive_loop"
                message = f"static route {after.destination} participates in a recursive loop"
            elif not _reachable(ctx.proposed.ir, after):
                suffix = "next_hop_unreachable"
                message = (
                    f"static route {after.destination} has no next hop on a modeled "
                    "connected subnet"
                )
            if suffix is None:
                continue
            findings.append(
                Finding(
                    source=FindingSource.CHECK,
                    category=FindingCategory.NETWORK,
                    code=f"{self.id}.{suffix}",
                    severity=Severity.WARNING,
                    confidence=_HIGH,
                    message=message,
                    subject=ObjectRef("device", route.device_id),
                    affected_entities=(route.destination,),
                    evidence={
                        "device": route.device_id,
                        "destination": route.destination,
                        "next_hops": list(route.next_hops),
                        "vrf": route.vrf,
                    },
                    caused_by=ctx.delta_index.causes("static_route", [rid]),
                )
            )
        return CheckResult(
            check_id=self.id,
            status=status_from_findings(findings),
            findings=tuple(findings),
            coverage=Coverage(
                CoverageState.PARTIAL if findings else CoverageState.COMPLETE,
                ("live RIB and recursive-resolution telemetry are unavailable",)
                if findings
                else (),
            ),
            confidence=_HIGH,
            reasoning="checked changed static routes against modeled connected subnets",
        )
