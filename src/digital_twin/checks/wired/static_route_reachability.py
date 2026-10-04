"""Static-route change warnings without simulating a live routing table."""

from __future__ import annotations

import ipaddress

from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState, Status
from digital_twin.checks.wired.config_lint import touched_ids
from digital_twin.contracts import Finding, FindingCategory, FindingSource, ObjectRef, Severity
from digital_twin.ir import (
    IR,
    Capability,
    Confidence,
    ConfidenceLevel,
    IRCapability,
    IRDiff,
    StaticRoute,
)

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


def _network(value: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    try:
        return ipaddress.ip_network(value, strict=False)
    except ValueError:
        return None


def _covering_blackhole(route: StaticRoute, baseline: IR) -> bool:
    """A discard route inside a less-specific forwarding route of the same table."""
    net = _network(route.destination)
    if net is None:
        return False
    for other in baseline.static_routes:
        wider = _network(other.destination)
        if (
            not other.discard
            and other.device_id == route.device_id
            and other.vrf == route.vrf
            and wider is not None
            and wider.version == net.version
            and wider != net
            and net.subnet_of(wider)  # type: ignore[arg-type]
        ):
            return True
    return False


def _recursive_loop(route: StaticRoute, routes: tuple[StaticRoute, ...]) -> bool:
    """A next hop resolved by a route whose own next hop falls back inside this one."""
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
                or candidate.unresolved
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
                sole = not any(
                    r.device_id == route.device_id
                    and r.vrf == route.vrf
                    and r.destination == route.destination
                    for r in prop.values()
                )
                code, detail = (
                    ("sole_route_removed", "the only configured route to this prefix is removed")
                    if sole
                    else ("removed", "configured static route removed")
                )
            elif route.unresolved:
                code, detail = (
                    "unresolved",
                    "destination or next-hop configuration cannot be resolved",
                )
            elif route.discard:
                code, detail = (
                    (
                        "more_specific_blackhole",
                        "discard route blackholes part of a wider forwarding route",
                    )
                    if _covering_blackhole(route, ctx.baseline.ir)
                    else ("discard", "configured discard route may drop matching traffic")
                )
            elif _recursive_loop(route, ctx.proposed.ir.static_routes):
                code, detail = (
                    "recursive_loop",
                    "next hops resolve through a route that points back into this prefix",
                )
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
                    message=f"{route.device_id} {route.destination} ({route.vrf}): {detail}",
                    subject=ObjectRef("device", route.device_id),
                    affected_entities=(route.device_id,),
                    evidence={
                        "route": rid,
                        "destination": route.destination,
                        "next_hops": list(route.next_hops),
                        "discard": route.discard,
                        "vrf": route.vrf,
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
