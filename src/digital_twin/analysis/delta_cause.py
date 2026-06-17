"""Pure delta-attribution helper. DeltaIndex is the cached diff lookup ONLY:
given an entity (kind,id), is it in the delta and with which changed IR fields?
It does NO graph analysis. The Family-2 mapping functions (added in later tasks)
take a CheckContext + the affected component/cycle/vid and consult this index."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from digital_twin.contracts import Cause, ObjectRef
from digital_twin.ir.diff import IRDiff


@dataclass(frozen=True)
class DeltaIndex:
    _fields: dict[tuple[str, str], tuple[str, ...]]   # (kind,id) -> changed fields
    _addremove: frozenset[tuple[str, str]]            # added or removed (no field set)

    def in_delta(self, kind: str, oid: str) -> bool:
        key = (kind, oid)
        return key in self._fields or key in self._addremove

    def cause(self, kind: str, oid: str) -> Cause | None:
        """Cause for an entity IFF it is in the delta; else None (honesty rule)."""
        key = (kind, oid)
        if key in self._fields:
            return Cause(ref=ObjectRef(kind, oid), fields=self._fields[key])
        if key in self._addremove:
            return Cause(ref=ObjectRef(kind, oid), fields=())
        return None

    def causes(self, kind: str, oids: Iterable[object]) -> tuple[Cause, ...]:
        """Map an iterable of ids of one kind to the subset that is in the delta."""
        out = []
        for oid in oids:
            c = self.cause(kind, str(oid))
            if c is not None:
                out.append(c)
        return tuple(out)


def delta_index(diff: IRDiff) -> DeltaIndex:
    fields = {(m.ref.kind, m.ref.id): m.changed_fields for m in diff.modified}
    addremove = frozenset((r.kind, r.id) for r in (*diff.added, *diff.removed))
    return DeltaIndex(_fields=fields, _addremove=addremove)
