"""IRDiff: the vendor-neutral change set between two IR snapshots.

Checks read this (never the raw vendor payload). Entities are compared by stable id;
the per-fact `meta` is excluded — a confidence change is not a config change.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, fields
from typing import Any

from .model import IR

# Provenance/confidence wrappers are not config changes; the underlying facts
# (stp_enabled/stp_state/...) ARE compared, so a real STP change is still detected.
_IGNORED_FIELDS = {"meta", "stp_meta"}

# Entity kinds the diff walks. Adding a domain (WAN/NAC/routing) = append ONE line
# here (every entity exposes a stable `.id`); the diff then extends automatically.
_ENTITY_KINDS: list[tuple[str, Callable[[IR], Iterable[Any]]]] = [
    ("device", lambda ir: ir.devices.values()),
    ("port", lambda ir: ir.ports.values()),
    ("link", lambda ir: ir.links),
    ("vlan", lambda ir: ir.vlans.values()),
    ("l3intf", lambda ir: ir.l3intfs),
    ("client", lambda ir: ir.clients),
]


@dataclass(frozen=True)
class EntityRef:
    kind: str
    id: str


@dataclass(frozen=True)
class Modified:
    ref: EntityRef
    changed_fields: tuple[str, ...]


@dataclass(frozen=True)
class IRDiff:
    added: tuple[EntityRef, ...]
    removed: tuple[EntityRef, ...]
    modified: tuple[Modified, ...]

    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.modified)

    def touches(self, kind: str) -> bool:
        refs: Iterable[EntityRef] = (
            *self.added,
            *self.removed,
            *(m.ref for m in self.modified),
        )
        return any(r.kind == kind for r in refs)


def _index(ir: IR) -> dict[tuple[str, str], Any]:
    out: dict[tuple[str, str], Any] = {}
    for kind, extract in _ENTITY_KINDS:
        for entity in extract(ir):
            out[(kind, entity.id)] = entity
    return out


def _changed_fields(a: Any, b: Any) -> tuple[str, ...]:
    changed: list[str] = []
    for f in fields(a):
        if f.name in _IGNORED_FIELDS:
            continue
        if getattr(a, f.name) != getattr(b, f.name):
            changed.append(f.name)
    return tuple(sorted(changed))  # field-order independent -> stable fixtures


def diff_ir(baseline: IR, proposed: IR) -> IRDiff:
    """Diff two IR snapshots. Output is sorted by (kind, id) so verdicts and
    replay fixtures are deterministic across runs."""
    base = _index(baseline)
    prop = _index(proposed)
    added = [EntityRef(*k) for k in sorted(prop.keys() - base.keys())]
    removed = [EntityRef(*k) for k in sorted(base.keys() - prop.keys())]
    modified: list[Modified] = []
    for key in sorted(base.keys() & prop.keys()):
        changed = _changed_fields(base[key], prop[key])
        if changed:
            modified.append(Modified(EntityRef(*key), changed))
    return IRDiff(tuple(added), tuple(removed), tuple(modified))
