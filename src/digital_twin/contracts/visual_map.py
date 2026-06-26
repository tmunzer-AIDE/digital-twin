"""VisualMap: a presentation-only attribution layer keyed by (view, entity).

PURELY presentational — verdict/decision.py never reads it. Each entry records
how central an entity is to the change (`tier`) and how bad it is (`severity`),
the two axes kept independent. Keyed per rendered view (l2 | vlan:<id> |
l3_exits) so a finding scoped to one VLAN can never paint another VLAN's chart.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .finding import ObjectRef, Severity


class VisualTier(StrEnum):
    ORIGIN = "origin"  # the changed thing (from caused_by) — visually distinct
    AFFECTED = "affected"  # the blast radius that loses service


@dataclass(frozen=True)
class FindingRef:
    """A back-link to the exact finding instance, NOT a bare code: two findings
    can share a code (blackhole on vlan 10 and vlan 20), so `index` (position in
    Verdict.findings) is what disambiguates the instance for the UI."""

    index: int
    code: str
    subject: ObjectRef | None = None


@dataclass(frozen=True)
class VisualEntry:
    kind: str  # device | vlan | port | link | intf — structured, no string-parsing
    id: str  # raw entity id (may contain colons, e.g. s1:ge-0/0/1)
    tier: VisualTier
    severity: Severity  # worst severity touching this (view, entity)
    findings: tuple[FindingRef, ...]  # instances touching this (view, entity)


# view_id -> entity_key -> entry. entity_key == f"{kind}:{id}".
VisualMap = dict[str, dict[str, VisualEntry]]


def entity_key(kind: str, id: str) -> str:
    """`kind:id`. Consumers split on the FIRST colon only (id may contain more)."""
    return f"{kind}:{id}"
