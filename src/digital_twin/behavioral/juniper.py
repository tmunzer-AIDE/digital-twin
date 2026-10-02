"""Bounded EX and SRX packet primitives, independent of cloud rendering and I/O.

These model supplied native semantics. They are not Mist-to-native translators,
hardware emulators, or complete platform feature-support declarations.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from .program import Copy, Match, Node, Outcome, Rule, Trace
from .snapshot import ObjectKey
from .space import Domain, Space


def ex_ingress(
    node_id: str,
    *,
    mode: str,
    native_vlan: int | None,
    vlans: tuple[int, ...],
    destination: str,
    els: bool,
    access_accepts_tagged: bool | None = None,
    sources: tuple[ObjectKey, ...] = (),
) -> Node:
    """Admission/tag classification only; MAC learning and STP require other nodes."""
    if mode not in ("access", "trunk") or type(els) is not bool:
        raise ValueError("EX ingress requires a known mode and ELS profile")
    if access_accepts_tagged is not None and type(access_accepts_tagged) is not bool:
        raise ValueError("access tagging profile must be boolean or unknown")
    if any(
        type(v) is not int or not 1 <= v <= 4094
        for v in (*vlans, *((native_vlan,) if native_vlan is not None else ()))
    ):
        raise ValueError("invalid VLAN identifier")
    rules: list[Rule] = []
    rules.append(
        Rule(
            matches=(Match("tagged", Domain.value(True)), Match("vlan", Domain.value(0))),
            opaque=True,
            reason="priority-tag classification is outside this EX subset",
        )
    )
    if els and mode == "trunk":
        rules.extend(
            Rule(
                matches=(
                    Match("tagged", Domain.value(False)),
                    Match("control", Domain.value(control)),
                ),
                destinations=(destination,),
                reason="ELS untagged control admission",
            )
            for control in ("lldp", "lacp")
        )
    if native_vlan is not None:
        rules.append(
            Rule(
                matches=(Match("tagged", Domain.value(False)),),
                writes=(("vlan", Domain.value(native_vlan)),),
                destinations=()
                if els and mode == "trunk" and native_vlan not in vlans
                else (destination,),
                opaque=els and mode == "trunk" and native_vlan not in vlans,
                reason="native VLAN membership is missing"
                if els and mode == "trunk" and native_vlan not in vlans
                else "native VLAN classification",
            )
        )
    if mode == "trunk":
        rules.extend(
            Rule(
                matches=(Match("tagged", Domain.value(True)), Match("vlan", Domain.value(vlan))),
                destinations=(destination,),
                reason="tagged VLAN admission",
            )
            for vlan in sorted(set(vlans))
        )
    elif access_accepts_tagged is None:
        rules.append(
            Rule(
                matches=(Match("tagged", Domain.value(True)),),
                opaque=True,
                reason="tagged access admission needs a platform/release profile",
            )
        )
    elif access_accepts_tagged:
        rules.extend(
            Rule(
                matches=(Match("tagged", Domain.value(True)), Match("vlan", Domain.value(vlan))),
                destinations=(destination,),
                reason="explicit tagged access profile",
            )
            for vlan in sorted(set(vlans))
        )
    return Node(node_id, tuple(rules), sources)


def ex_egress(
    node_id: str,
    *,
    native_vlan: int | None,
    tagged_vlans: tuple[int, ...],
    destination: str,
    sources: tuple[ObjectKey, ...] = (),
) -> Node:
    if any(
        type(v) is not int or not 1 <= v <= 4094
        for v in (*tagged_vlans, *((native_vlan,) if native_vlan is not None else ()))
    ):
        raise ValueError("invalid egress VLAN identifier")
    rules: list[Rule] = []
    if native_vlan is not None:
        rules.append(
            Rule(
                matches=(Match("vlan", Domain.value(native_vlan)),),
                writes=(("tagged", Domain.value(False)),),
                destinations=(destination,),
            )
        )
    rules.extend(
        Rule(
            matches=(Match("vlan", Domain.value(vlan)),),
            writes=(("tagged", Domain.value(True)),),
            destinations=(destination,),
        )
        for vlan in sorted(set(tagged_vlans) - {native_vlan})
    )
    return Node(node_id, tuple(rules), sources)


def lacp_eligibility(local: str, peer: str, *, force_up: bool = False) -> bool:
    if local not in ("active", "passive") or peer not in ("active", "passive"):
        raise ValueError("LACP mode must be known; static aggregation is a separate model")
    if type(force_up) is not bool:
        raise ValueError("force-up must be boolean")
    return force_up or "active" in (local, peer)


@dataclass(frozen=True)
class Route:
    prefix: str
    vrf: str
    interface: str
    zone: str
    preference: int = 0
    resolved: bool | None = True

    def __post_init__(self) -> None:
        network = ipaddress.ip_network(self.prefix, strict=False)
        object.__setattr__(self, "prefix", str(network))
        if type(self.preference) is not int or not all((self.vrf, self.interface, self.zone)):
            raise ValueError("route needs a typed preference and explicit routing context")
        if self.resolved is not None and type(self.resolved) is not bool:
            raise ValueError("next-hop resolution must be known or explicitly absent")


def route_lookup(
    node_id: str,
    routes: tuple[Route, ...],
    destination: str,
    *,
    sources: tuple[ObjectKey, ...] = (),
) -> Node:
    """Supplied RIB -> longest-prefix transfer; no OSPF/BGP route-generation claim."""
    groups: dict[tuple[str, str], list[Route]] = {}
    for route in routes:
        if route.resolved is not False:
            groups.setdefault((route.vrf, route.prefix), []).append(route)
    rules: list[Rule] = []
    for (vrf, prefix), candidates in sorted(
        groups.items(),
        key=lambda entry: (
            -ipaddress.ip_network(entry[0][1]).prefixlen,
            entry[0],
        ),
    ):
        preference = min(r.preference for r in candidates)
        selected = [r for r in candidates if r.preference == preference]
        matches = (Match("vrf", Domain.value(vrf)), Match("dst", Domain.ip(prefix)))
        if any(r.resolved is None for r in selected):
            rules.append(Rule(matches=matches, opaque=True, reason="unknown next-hop resolution"))
            continue
        if len({(r.interface, r.zone) for r in selected}) != 1:
            rules.append(Rule(matches=matches, opaque=True, reason="ECMP selection is unmodeled"))
            continue
        route = selected[0]
        rules.append(
            Rule(
                matches=matches,
                writes=(
                    ("dst_interface", Domain.value(route.interface)),
                    ("dst_zone", Domain.value(route.zone)),
                ),
                destinations=(destination,),
            )
        )
    return Node(node_id, tuple(rules), sources)


@dataclass(frozen=True)
class NatSet:
    name: str
    source: Match
    rules: tuple[Rule, ...]
    destination: Match | None = None

    def __post_init__(self) -> None:
        if not self.name or self.source.field not in ("src_interface", "src_zone", "vrf"):
            raise ValueError("NAT rule-set source must select interface, zone or routing instance")
        if self.destination and self.destination.field not in (
            "dst_interface",
            "dst_zone",
            "dst_vrf",
        ):
            raise ValueError("unsupported NAT destination selector")
        for selector in (self.source, *((self.destination,) if self.destination else ())):
            if selector.domain.kind != "string" or not selector.domain.singleton:
                raise ValueError("this NAT subset requires literal context selectors")
        if any(rule.destinations or rule.outcome is not None or rule.opaque for rule in self.rules):
            raise ValueError("NAT entries contain only match and rewrite operations")
        object.__setattr__(self, "rules", tuple(self.rules))


def _nat_nodes(
    prefix: str,
    name: str,
    sets: tuple[NatSet, ...],
    destination: str,
    *,
    marker: str,
    sources: tuple[ObjectKey, ...],
) -> tuple[Node, ...]:
    source_rank = {"src_interface": 0, "src_zone": 1, "vrf": 2}
    dest_rank = {"dst_interface": 0, "dst_zone": 1, "dst_vrf": 2}
    ordered = sorted(
        sets,
        key=lambda s: (
            dest_rank[s.destination.field] * 3 + source_rank[s.source.field]
            if s.destination
            else source_rank[s.source.field],
            s.name,
        ),
    )
    selectors: list[Rule] = []
    nodes: list[Node] = []
    seen: set[tuple[Match, Match | None]] = set()
    for rule_set in ordered:
        if (rule_set.source, rule_set.destination) in seen:
            raise ValueError("ambiguous NAT rule-set selectors")
        seen.add((rule_set.source, rule_set.destination))
        node_id = f"{prefix}:{name}:{rule_set.name}"
        selectors.append(
            Rule(
                matches=(
                    rule_set.source,
                    *((rule_set.destination,) if rule_set.destination else ()),
                ),
                destinations=(node_id,),
            )
        )
        entries = tuple(
            Rule(
                matches=r.matches,
                writes=(*r.writes, (marker, Domain.value(True))),
                destinations=(destination,),
                reason=r.reason,
            )
            for r in rule_set.rules
        )
        # Selecting a more-specific set does not permit fallback to another set.
        nodes.append(Node(node_id, (*entries, Rule(destinations=(destination,))), sources))
    selectors.append(Rule(destinations=(destination,)))
    return (Node(f"{prefix}:{name}", tuple(selectors), sources), *nodes)


def srx_flow(
    prefix: str,
    *,
    routes: tuple[Route, ...],
    policies: tuple[Rule, ...],
    destination: str,
    static: tuple[NatSet, ...] = (),
    dnat: tuple[NatSet, ...] = (),
    reverse_static: tuple[NatSet, ...] = (),
    snat: tuple[NatSet, ...] = (),
    sources: tuple[ObjectKey, ...] = (),
    unsupported_features: tuple[str, ...] = (),
) -> tuple[Node, ...]:
    """L3/L4 first-packet flow subset; simple literal rewrites, not NAT pool allocation."""
    if any(s.destination is not None for s in (*static, *dnat)):
        raise ValueError("static/DNAT cannot select a destination context before routing")
    nodes = [
        Node(
            prefix,
            (
                Rule(opaque=True, reason=", ".join(unsupported_features))
                if unsupported_features
                else Rule(
                    writes=(
                        ("original_src", Copy("src")),
                        ("original_dst", Copy("dst")),
                        ("original_src_port", Copy("src_port")),
                        ("original_dst_port", Copy("dst_port")),
                        ("static_matched", Domain.value(False)),
                        ("reverse_static_matched", Domain.value(False)),
                    ),
                    destinations=(f"{prefix}:static",),
                ),
            ),
            sources,
        )
    ]
    nodes.extend(
        _nat_nodes(
            prefix,
            "static",
            static,
            f"{prefix}:dnat_gate",
            marker="static_matched",
            sources=sources,
        )
    )
    nodes.append(
        Node(
            f"{prefix}:dnat_gate",
            (
                Rule(
                    matches=(Match("static_matched", Domain.value(True)),),
                    destinations=(f"{prefix}:route",),
                ),
                Rule(destinations=(f"{prefix}:dnat",)),
            ),
            sources,
        )
    )
    nodes.extend(
        _nat_nodes(prefix, "dnat", dnat, f"{prefix}:route", marker="dnat_matched", sources=sources)
    )
    nodes.append(route_lookup(f"{prefix}:route", routes, f"{prefix}:policy", sources=sources))
    if any(p.destinations for p in policies):
        raise ValueError("SRX policies use explicit delivered/dropped decisions")
    nodes.append(
        Node(
            f"{prefix}:policy",
            tuple(
                Rule(
                    p.matches,
                    p.writes,
                    (f"{prefix}:reverse_static",),
                    reason=p.reason,
                    opaque=p.opaque,
                )
                if p.outcome is Outcome.DELIVERED
                else p
                for p in policies
            ),
            sources,
        )
    )
    nodes.extend(
        _nat_nodes(
            prefix,
            "reverse_static",
            reverse_static,
            f"{prefix}:snat_gate",
            marker="reverse_static_matched",
            sources=sources,
        )
    )
    nodes.append(
        Node(
            f"{prefix}:snat_gate",
            (
                Rule(
                    matches=(Match("reverse_static_matched", Domain.value(True)),),
                    destinations=(destination,),
                ),
                Rule(destinations=(f"{prefix}:snat",)),
            ),
            sources,
        )
    )
    nodes.extend(
        _nat_nodes(prefix, "snat", snat, destination, marker="snat_matched", sources=sources)
    )
    return tuple(nodes)


@dataclass(frozen=True)
class SrxSession:
    original: Space
    translated: Space

    def __post_init__(self) -> None:
        for space in (self.original, self.translated):
            for field in ("src", "dst", "src_port", "dst_port", "protocol", "vrf"):
                domain = space.get(field)
                if domain is None:
                    raise ValueError("session association needs its five-tuple and routing context")
                numeric = domain.kind in ("integer", "ipv4", "ipv6")
                singleton = (
                    (
                        len(domain.intervals) == 1
                        and domain.intervals[0][0] == domain.intervals[0][1]
                    )
                    if numeric
                    else len(domain.symbols) == 1 and not domain.excluded
                )
                if not singleton:
                    raise ValueError("session association requires an exact five-tuple/context")

    @classmethod
    def establish(cls, original: Space, trace: Trace) -> SrxSession:
        if trace.outcome is not Outcome.DELIVERED or trace.uncertainties:
            raise ValueError("a session needs a definite successful first-packet trace")
        return cls(original, trace.space)

    def response_node(self, node_id: str, destination: str) -> Node:
        matches = []
        writes: list[tuple[str, Domain | Copy]] = []
        for response, forward in (
            ("src", "dst"),
            ("dst", "src"),
            ("src_port", "dst_port"),
            ("dst_port", "src_port"),
            ("protocol", "protocol"),
            ("vrf", "vrf"),
        ):
            translated = self.translated.get(forward)
            original = self.original.get(forward)
            assert translated is not None and original is not None
            matches.append(Match(response, translated))
            writes.append((response, original))
        return Node(
            node_id,
            (
                Rule(
                    tuple(matches),
                    tuple(writes),
                    (destination,),
                    reason="associated SRX return translation",
                ),
            ),
        )
