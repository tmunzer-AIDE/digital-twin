"""SSR service FIB construction for explicit route updates and static service classes.

Dynamic AppID, generated cloud policy, peer signaling and session migration are
outside this bounded primitive. Broader-route expansion is explicitly opaque.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Literal

from .juniper import Route
from .program import Match, Node, Outcome, Rule
from .snapshot import ObjectKey
from .space import Domain


def _subnet(prefix: str, parent: str) -> bool:
    child, containing = ipaddress.ip_network(prefix), ipaddress.ip_network(parent)
    return (
        child.version == containing.version
        and int(containing.network_address) <= int(child.network_address)
        and int(child.broadcast_address) <= int(containing.broadcast_address)
    )


def tenant_includes(parent: str, child: str) -> bool:
    if not parent or not child:
        raise ValueError("tenant identities must be nonempty")
    return child == parent or child.endswith("." + parent)


@dataclass(frozen=True)
class Transport:
    protocol: str
    ports: Domain | None = None

    def __post_init__(self) -> None:
        if self.protocol not in ("any", "tcp", "udp", "icmp"):
            raise ValueError("unsupported SSR transport protocol")
        if self.ports is not None and (
            self.protocol not in ("tcp", "udp")
            or self.ports.kind != "integer"
            or self.ports.empty
            or self.ports.intervals[0][0] < 0
            or self.ports.intervals[-1][1] > 65535
        ):
            raise ValueError("transport ports require a valid TCP/UDP range")

    def overlaps(self, other: Transport) -> bool:
        if self.protocol != "any" and other.protocol != "any" and self.protocol != other.protocol:
            return False
        return (
            self.ports is None or other.ports is None or not self.ports.intersect(other.ports).empty
        )

    def matches(self) -> tuple[Match, ...]:
        return (
            *((Match("protocol", Domain.value(self.protocol)),) if self.protocol != "any" else ()),
            *((Match("dst_port", self.ports),) if self.ports is not None else ()),
        )


@dataclass(frozen=True)
class Service:
    name: str
    prefix: str
    tenants: tuple[str, ...]
    transports: tuple[Transport, ...] = (Transport("any"),)

    def __post_init__(self) -> None:
        if not self.name or not self.tenants or not self.transports:
            raise ValueError("service requires a name, authorization and transports")
        object.__setattr__(self, "prefix", str(ipaddress.ip_network(self.prefix, strict=False)))
        object.__setattr__(self, "tenants", tuple(self.tenants))
        object.__setattr__(self, "transports", tuple(self.transports))


@dataclass(frozen=True)
class FibEntry:
    tenant: str
    service: Service
    prefix: str
    transport: Transport
    next_hop: str | None
    uncertain: bool = False


def build_fib(
    services: tuple[Service, ...],
    routes: tuple[Route, ...],
    *,
    tenants: tuple[str, ...],
    vrfs: tuple[tuple[str, str], ...] = (),
    mode: Literal["best-match-only", "any-match"] = "best-match-only",
) -> tuple[FibEntry, ...]:
    if mode not in ("best-match-only", "any-match"):
        raise ValueError("SSR fib-service-match mode is unknown")
    if len({s.name for s in services}) != len(services):
        raise ValueError("service names must be unique within this reference subset")
    entries: list[FibEntry] = []
    for tenant in tenants:
        authorized = [s for s in services if any(tenant_includes(t, tenant) for t in s.tenants)]
        mapped = [vrf for owner, vrf in vrfs if tenant_includes(owner, tenant)]
        if len(set(mapped)) > 1:
            raise ValueError("ambiguous tenant VRF mapping")
        vrf = mapped[0] if mapped else "default"
        for service in authorized:
            entries.extend(
                FibEntry(tenant, service, service.prefix, transport, None)
                for transport in service.transports
            )
        for route in routes:
            if route.vrf != vrf or route.resolved is False:
                continue
            network = ipaddress.ip_network(route.prefix)
            candidates = [
                s
                for s in authorized
                if (
                    ipaddress.ip_network(s.prefix).version == network.version
                    and _subnet(route.prefix, s.prefix)
                )
            ]
            # Expanding a route broader than a configured service needs the
            # additional native construction rules; don't approximate those.
            broader = [
                s
                for s in authorized
                if (
                    ipaddress.ip_network(s.prefix).version == network.version
                    and _subnet(s.prefix, route.prefix)
                    and s.prefix != route.prefix
                )
            ]
            for service in broader:
                entries.extend(
                    FibEntry(tenant, service, service.prefix, transport, None, True)
                    for transport in service.transports
                )
            if not candidates:
                continue
            candidates.sort(key=lambda s: (-ipaddress.ip_network(s.prefix).prefixlen, s.name))
            if mode == "best-match-only":
                best = ipaddress.ip_network(candidates[0].prefix).prefixlen
                candidates = [
                    s for s in candidates if ipaddress.ip_network(s.prefix).prefixlen == best
                ]
            installed: list[Service] = []
            for service in candidates:
                if any(
                    a.overlaps(b)
                    for prior in installed
                    for a in prior.transports
                    for b in service.transports
                ):
                    # A collision excludes all transports of the losing service.
                    continue
                installed.append(service)
                entries.extend(
                    FibEntry(
                        tenant,
                        service,
                        route.prefix,
                        transport,
                        route.interface,
                        route.resolved is None,
                    )
                    for transport in service.transports
                )
    # Multiple route candidates require native RIB selection before this stage.
    seen: dict[tuple[str, str, str, Transport], FibEntry] = {}
    for entry in entries:
        key = (entry.tenant, entry.service.name, entry.prefix, entry.transport)
        prior = seen.get(key)
        if prior is not None and (prior.uncertain or entry.uncertain):
            seen[key] = FibEntry(
                entry.tenant, entry.service, entry.prefix, entry.transport, None, True
            )
        elif prior is not None and prior.next_hop != entry.next_hop and prior.next_hop is not None:
            seen[key] = FibEntry(
                entry.tenant, entry.service, entry.prefix, entry.transport, None, True
            )
        elif prior is None or entry.next_hop is not None or entry.uncertain:
            seen[key] = entry
    return tuple(
        sorted(
            seen.values(),
            key=lambda e: (
                e.tenant,
                -ipaddress.ip_network(e.prefix).prefixlen,
                not e.uncertain,
                e.service.name,
                e.transport.protocol,
            ),
        )
    )


def fib_node(
    node_id: str,
    entries: tuple[FibEntry, ...],
    destination: str,
    *,
    sources: tuple[ObjectKey, ...] = (),
) -> Node:
    return Node(
        node_id,
        tuple(
            Rule(
                matches=(
                    Match("tenant", Domain.value(entry.tenant)),
                    Match("dst", Domain.ip(entry.prefix)),
                    *entry.transport.matches(),
                ),
                writes=(
                    ("service", Domain.value(entry.service.name)),
                    *((("next_hop", Domain.value(entry.next_hop)),) if entry.next_hop else ()),
                ),
                destinations=(destination,)
                if entry.next_hop is not None and not entry.uncertain
                else (),
                outcome=Outcome.DROPPED if entry.next_hop is None and not entry.uncertain else None,
                opaque=entry.uncertain,
                reason="SSR service entry has no next hop"
                if entry.next_hop is None
                else "SSR service forwarding entry",
            )
            for entry in entries
        ),
        sources,
    )


@dataclass(frozen=True)
class Path:
    name: str
    preference: int
    connected: bool
    meets_sla: bool | None

    def __post_init__(self) -> None:
        if not self.name or type(self.preference) is not int or type(self.connected) is not bool:
            raise ValueError("path requires identity, typed preference and connectivity")
        if self.meets_sla is not None and type(self.meets_sla) is not bool:
            raise ValueError("SLA status must be boolean or explicitly unknown")


def eligible_paths(paths: tuple[Path, ...], *, best_effort: bool) -> tuple[str, ...] | None:
    """New-session eligibility; None means health knowledge cannot settle selection."""
    if type(best_effort) is not bool:
        raise ValueError("best-effort policy must be established explicitly")
    connected = [p for p in paths if p.connected]
    if any(p.meets_sla is None for p in connected):
        return None
    healthy = [p for p in connected if p.meets_sla is True]
    candidates = healthy or (connected if best_effort else [])
    if not candidates:
        return ()
    preference = min(p.preference for p in candidates)
    return tuple(sorted(p.name for p in candidates if p.preference == preference))
