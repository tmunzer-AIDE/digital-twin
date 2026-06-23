"""Raw configuration diff (before→after) for a simulated change — additive,
non-load-bearing evidence on every verdict. Values here are the REDACTED display
values (masked at assembly in config_diff.object_config_diff); this module is
pure types and reads nothing back into the decision."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FieldChange:
    """One changed configuration LEAF. `path` is the dot-path within the object
    (e.g. "enabled", "matching.nactags", "networks.corp.vlan_id"); `before`/`after`
    are REDACTED display values — `before` is None for kind="added", `after` is
    None for kind="removed"."""

    path: str
    kind: str  # "added" | "removed" | "changed"
    before: Any | None
    after: Any | None


@dataclass(frozen=True)
class ObjectConfigDiff:
    """The raw config delta for ONE object an op touches. `object_id`/`name` are
    raw (the verdict-wide ObjectRef convention); only `changes[*].before/after`
    are redacted."""

    object_type: str
    object_id: str
    name: str | None
    action: str  # "create" | "update" | "delete"
    changes: tuple[FieldChange, ...]
