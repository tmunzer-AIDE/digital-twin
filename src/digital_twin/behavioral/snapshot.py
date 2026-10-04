"""Immutable source snapshots and optimistic, offline configuration batches."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

CONTRACT_VERSION = "1.0"


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True, order=True)
class ObjectKey:
    org_id: str
    kind: str
    object_id: str
    site_id: str = ""  # empty means organization scope; never an implicit current site

    def __post_init__(self) -> None:
        if not all(
            isinstance(v, str)
            for v in (
                self.org_id,
                self.kind,
                self.object_id,
                self.site_id,
            )
        ) or not all((self.org_id, self.kind, self.object_id)):
            raise ValueError("object keys require nonempty organization, kind and identity")


@dataclass(frozen=True)
class Record:
    key: ObjectKey
    body_json: str
    source: str
    schema_version: str
    platform: str = ""
    release: str = ""

    def __post_init__(self) -> None:
        if (
            not isinstance(self.key, ObjectKey)
            or not isinstance(self.body_json, str)
            or any(
                not isinstance(v, str)
                for v in (self.source, self.schema_version, self.platform, self.release)
            )
        ):
            raise ValueError("record identities, JSON and provenance must have immutable types")
        body = json.loads(self.body_json)
        if not isinstance(body, dict) or not self.source or not self.schema_version:
            raise ValueError("a configuration record must contain an object")
        object.__setattr__(self, "body_json", canonical(body))

    @classmethod
    def create(
        cls,
        key: ObjectKey,
        body: Mapping[str, Any],
        *,
        source: str = "mist",
        schema_version: str = "2609.1.0",
        platform: str = "",
        release: str = "",
    ) -> Record:
        return cls(key, canonical(dict(body)), source, schema_version, platform, release)

    @property
    def revision(self) -> str:
        return digest(self.body_json)

    def body(self) -> dict[str, Any]:
        # Returning a fresh tree prevents callers changing a captured snapshot.
        result: dict[str, Any] = json.loads(self.body_json)
        return result


@dataclass(frozen=True)
class InputWindow:
    name: str
    started_at: datetime
    completed_at: datetime
    complete: bool
    failure: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name or not isinstance(self.failure, str):
            raise ValueError("input acquisition window requires a name")
        if not isinstance(self.started_at, datetime) or not isinstance(self.completed_at, datetime):
            raise ValueError("input timestamps must be datetime values")
        if self.started_at.tzinfo is None or self.completed_at.tzinfo is None:
            raise ValueError("input timestamps must be timezone-aware")
        if self.completed_at < self.started_at:
            raise ValueError("input acquisition interval is inverted")
        if type(self.complete) is not bool:
            raise ValueError("input completeness must be a boolean")
        if self.complete and self.failure:
            raise ValueError("a failed input cannot be complete")


@dataclass(frozen=True)
class Snapshot:
    org_id: str
    records: tuple[Record, ...]
    inputs: tuple[InputWindow, ...] = ()
    contract_version: str = CONTRACT_VERSION

    def __post_init__(self) -> None:
        if (
            not isinstance(self.org_id, str)
            or not self.org_id
            or self.contract_version != CONTRACT_VERSION
        ):
            raise ValueError("unsupported snapshot contract or empty organization")
        if any(not isinstance(r, Record) for r in self.records) or any(
            not isinstance(w, InputWindow) for w in self.inputs
        ):
            raise ValueError("snapshot requires immutable records and input windows")
        if any(r.key.org_id != self.org_id for r in self.records):
            raise ValueError("snapshot contains a different organization")
        if len({r.key for r in self.records}) != len(self.records):
            raise ValueError("duplicate snapshot object")
        if len({i.name for i in self.inputs}) != len(self.inputs):
            raise ValueError("duplicate input window")
        object.__setattr__(self, "records", tuple(sorted(self.records, key=lambda r: r.key)))
        object.__setattr__(self, "inputs", tuple(self.inputs))

    def get(self, key: ObjectKey) -> Record:
        for record in self.records:
            if record.key == key:
                return record
        raise KeyError(key)

    @property
    def revision(self) -> str:
        return digest(
            canonical(
                {
                    "version": self.contract_version,
                    "org": self.org_id,
                    "records": [
                        [
                            r.key.kind,
                            r.key.object_id,
                            r.key.site_id,
                            r.revision,
                            r.source,
                            r.schema_version,
                            r.platform,
                            r.release,
                        ]
                        for r in self.records
                    ],
                    "inputs": [
                        [
                            i.name,
                            i.started_at.isoformat(),
                            i.completed_at.isoformat(),
                            i.complete,
                            i.failure,
                        ]
                        for i in sorted(self.inputs, key=lambda i: i.name)
                    ],
                }
            )
        )


class Action(StrEnum):
    UPDATE_ROOTS = "update_roots"
    CREATE = "create"
    DELETE = "delete"


@dataclass(frozen=True)
class Operation:
    key: ObjectKey
    action: Action
    expected_revision: str | None
    payload_json: str = "{}"
    created_record: Record | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.key, ObjectKey)
            or not isinstance(self.payload_json, str)
            or (self.expected_revision is not None and not isinstance(self.expected_revision, str))
        ):
            raise ValueError("operation key, revision and JSON require immutable types")
        payload = json.loads(self.payload_json)
        if not isinstance(payload, dict) or not isinstance(self.action, Action):
            raise ValueError("operation requires a supported action and object payload")
        if self.action is Action.DELETE and payload:
            raise ValueError("delete operations cannot contain a payload")
        if self.action is Action.CREATE and self.expected_revision is not None:
            raise ValueError("create expects absence, not an existing revision")
        if self.action is not Action.CREATE and not self.expected_revision:
            raise ValueError("update/delete require the rolling pre-operation revision")
        if self.action is Action.CREATE:
            if (
                self.created_record is None
                or self.created_record.key != self.key
                or self.created_record.body_json != canonical(payload)
            ):
                raise ValueError("create requires a matching record with explicit provenance")
        elif self.created_record is not None:
            raise ValueError("only create operations may supply record provenance")
        object.__setattr__(self, "payload_json", canonical(payload))

    @classmethod
    def update(cls, record: Record, payload: Mapping[str, Any]) -> Operation:
        return cls(record.key, Action.UPDATE_ROOTS, record.revision, canonical(dict(payload)))

    @classmethod
    def create(cls, record: Record) -> Operation:
        return cls(record.key, Action.CREATE, None, record.body_json, record)


@dataclass(frozen=True)
class Batch:
    snapshot_revision: str
    operations: tuple[Operation, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.snapshot_revision, str)
            or not self.snapshot_revision
            or any(not isinstance(op, Operation) for op in self.operations)
        ):
            raise ValueError("batch requires a snapshot revision and immutable operations")
        object.__setattr__(self, "operations", tuple(self.operations))

    def apply(self, baseline: Snapshot) -> Snapshot:
        if baseline.revision != self.snapshot_revision:
            raise ValueError("batch snapshot revision does not match")
        records = {r.key: r for r in baseline.records}
        for op in self.operations:
            if op.key.org_id != baseline.org_id:
                raise ValueError("batch operation belongs to a different organization")
            current = records.get(op.key)
            if op.action is Action.CREATE:
                if current is not None:
                    raise ValueError("create target already exists")
                assert op.created_record is not None
                records[op.key] = op.created_record
                continue
            if current is None or current.revision != op.expected_revision:
                raise ValueError("operation target is absent or its revision does not match")
            if op.action is Action.DELETE:
                del records[op.key]
                continue
            body = current.body()
            payload: dict[str, Any] = json.loads(op.payload_json)
            for key, value in payload.items():
                if key.startswith("-"):
                    root = key[1:]
                    if not root or root in payload or value != "":
                        raise ValueError("invalid or conflicting Mist root-deletion marker")
                    body.pop(root, None)
                else:
                    body[key] = value  # root replacement, never a nested merge
            records[op.key] = Record(
                op.key,
                canonical(body),
                current.source,
                current.schema_version,
                current.platform,
                current.release,
            )
        return Snapshot(baseline.org_id, tuple(records.values()), baseline.inputs)
