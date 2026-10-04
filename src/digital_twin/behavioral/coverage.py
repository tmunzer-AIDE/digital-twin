"""Exact source-path receipts; unconsumed structure never establishes coverage."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from .snapshot import ObjectKey, Record, Snapshot


def paths(value: Any, prefix: tuple[str, ...] = ()) -> Iterator[tuple[str, ...]]:
    if isinstance(value, dict) and value:
        for key, child in value.items():
            yield from paths(child, (*prefix, key))
    elif isinstance(value, list) and value:
        for index, child in enumerate(value):
            yield from paths(child, (*prefix, str(index)))
    else:
        # Empty containers and explicit null are proof-bearing structural facts.
        yield prefix


class Support(StrEnum):
    MODELED = "modeled"
    NON_INTERFERING = "non_interfering"
    OPAQUE = "opaque"


@dataclass(frozen=True)
class Receipt:
    key: ObjectKey
    source_revision: str
    source: str
    path: tuple[str, ...]
    support: Support
    model: str
    model_version: str
    schema_version: str
    platform: str
    release: str
    evidence: tuple[str, ...]
    dependencies: tuple[ObjectKey, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.support, Support):
            raise ValueError("receipt support must be a recognized Support value")
        object.__setattr__(self, "path", tuple(self.path))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "dependencies", tuple(self.dependencies))

    @classmethod
    def bind(
        cls,
        record: Record,
        path: tuple[str, ...],
        *,
        model: str,
        evidence: tuple[str, ...],
        support: Support = Support.MODELED,
        dependencies: tuple[ObjectKey, ...] = (),
        reason: str = "",
    ) -> Receipt:
        return cls(
            record.key,
            record.revision,
            record.source,
            path,
            support,
            model,
            "1.0",
            record.schema_version,
            record.platform,
            record.release,
            evidence,
            dependencies,
            reason,
        )


@dataclass(frozen=True)
class Gap:
    code: str
    key: ObjectKey | None
    path: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class Coverage:
    receipts: tuple[Receipt, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "receipts", tuple(self.receipts))

    def dependency_closure(self, keys: tuple[ObjectKey, ...]) -> tuple[ObjectKey, ...]:
        """Conservative source closure for explanation, not a coverage verdict."""
        by_key: dict[ObjectKey, set[ObjectKey]] = {}
        for receipt in self.receipts:
            by_key.setdefault(receipt.key, set()).update(receipt.dependencies)
        visited: set[ObjectKey] = set()
        pending = list(keys)
        while pending:
            key = pending.pop()
            if key not in visited:
                visited.add(key)
                pending.extend(by_key.get(key, ()))
        return tuple(sorted(visited))

    def inspect(
        self,
        snapshot: Snapshot,
        keys: tuple[ObjectKey, ...],
        *,
        at: datetime | None = None,
        max_age: timedelta | None = None,
    ) -> tuple[Gap, ...]:
        if (at is None) != (max_age is None):
            raise ValueError("freshness requires both at and max_age")
        if at is not None and max_age is not None and (at.tzinfo is None or max_age < timedelta(0)):
            raise ValueError("freshness requires an aware clock and nonnegative max_age")
        gaps: list[Gap] = []
        requested = set(keys)
        pending = list(keys)
        visited: set[ObjectKey] = set()
        by_path: dict[tuple[ObjectKey, tuple[str, ...]], list[Receipt]] = {}
        for receipt in self.receipts:
            by_path.setdefault((receipt.key, receipt.path), []).append(receipt)
        records = {r.key: r for r in snapshot.records}
        while pending:
            key = pending.pop()
            if key in visited:
                continue
            visited.add(key)
            record = records.get(key)
            if record is None:
                gaps.append(Gap("missing_object", key, (), "dependency is not in the snapshot"))
                continue
            for path in paths(record.body()):
                candidates = by_path.get((key, path), [])
                if len(candidates) != 1:
                    gaps.append(
                        Gap(
                            "unconsumed" if not candidates else "ambiguous_receipt",
                            key,
                            path,
                            "an exact, unique compiler receipt is required",
                        )
                    )
                    continue
                receipt = candidates[0]
                if (
                    receipt.source_revision != record.revision
                    or receipt.source != record.source
                    or receipt.schema_version != record.schema_version
                    or receipt.platform != record.platform
                    or receipt.release != record.release
                    or not receipt.model
                    or not receipt.model_version
                    or not receipt.evidence
                ):
                    gaps.append(
                        Gap("invalid_receipt", key, path, "receipt binding/evidence differs")
                    )
                    continue
                if receipt.support is Support.OPAQUE:
                    gaps.append(Gap("opaque", key, path, receipt.reason or "unsupported semantics"))
                elif receipt.support is Support.NON_INTERFERING and not receipt.reason:
                    gaps.append(
                        Gap("invalid_receipt", key, path, "non-interference needs a reason")
                    )
                elif receipt.support not in (Support.MODELED, Support.NON_INTERFERING):
                    gaps.append(Gap("invalid_receipt", key, path, "unrecognized support status"))
                pending.extend(receipt.dependencies)
        # Snapshot-level failures are not converted to an authoritative empty graph.
        if requested:
            for window in snapshot.inputs:
                if not window.complete:
                    gaps.append(Gap("incomplete_input", None, (window.name,), window.failure))
                if at is not None and max_age is not None:
                    if at.tzinfo is None or max_age < timedelta(0):
                        raise ValueError(
                            "freshness requires an aware clock and nonnegative max_age"
                        )
                    if at < window.completed_at or at - window.completed_at > max_age:
                        gaps.append(
                            Gap("stale_input", None, (window.name,), "input window is invalid")
                        )
        return tuple(
            sorted(
                gaps,
                key=lambda g: (
                    g.key.org_id if g.key else "",
                    g.key.kind if g.key else "",
                    g.key.site_id if g.key else "",
                    g.key.object_id if g.key else "",
                    g.path,
                    g.code,
                ),
            )
        )
