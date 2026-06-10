"""Typed payloads carried on graph edges/nodes.

networkx stores attributes as ``Any`` dicts; we store a single typed object under the
``data`` key so consumers (analysis, checks) get attribute access and type-checking
instead of stringly-typed lookups, and the attribute schema lives in ONE place.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from digital_twin.ir.confidence import Confidence


@dataclass
class L2Edge:
    """The payload on an L2-graph edge (one logical link)."""

    vlans: set[int]
    kind: str  # LinkKind value: "physical" | "lag" | "mclag"
    bundle_id: str | None
    link_ids: list[str]
    member_ports: list[str]
    confidence: Confidence

    def copy(self) -> L2Edge:
        return L2Edge(
            vlans=set(self.vlans),
            kind=self.kind,
            bundle_id=self.bundle_id,
            link_ids=list(self.link_ids),
            member_ports=list(self.member_ports),
            confidence=self.confidence,
        )


@dataclass
class VlanNode:
    """The payload on a per-VLAN-graph node (a participating device).

    Membership has two bases (spec): access_ports = configuration-based
    (switched side); wireless_clients = OBSERVATION-based (an AP contributes
    membership only via its currently-observed wireless clients' vlans).
    """

    access_ports: list[str] = field(default_factory=list)
    exits: list[str] = field(default_factory=list)
    wireless_clients: list[str] = field(default_factory=list)  # observed client macs

    @property
    def is_member(self) -> bool:
        return bool(self.access_ports or self.wireless_clients)

    @property
    def is_exit(self) -> bool:
        return bool(self.exits)
