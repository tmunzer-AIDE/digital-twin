"""Lean verdict for the org-NAC simulate path (GS34). No per-site shape."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from digital_twin.checks.base import CheckResult
from digital_twin.contracts import Finding, ObjectConfigDiff, Rejection
from digital_twin.ir import IRDiff
from digital_twin.verdict.decision import Decision


@dataclass(frozen=True)
class NacDelta:
    rule_id: str
    name: str | None
    kind: str                          # "added" | "removed" | "modified"
    changed_fields: tuple[str, ...]    # () for added/removed


@dataclass(frozen=True)
class OrgNacVerdict:
    decision: Decision
    decision_reasons: tuple[str, ...]
    changes: tuple[NacDelta, ...]
    check_results: tuple[CheckResult, ...]
    adapter_findings: tuple[Finding, ...]
    rejections: tuple[Rejection, ...]
    config_diffs: tuple[ObjectConfigDiff, ...] = ()  # raw before→after of the touched nacrules


def nac_changes(diff: IRDiff, baseline: Mapping[str, object],
                proposed: Mapping[str, object]) -> tuple[NacDelta, ...]:
    """Project the nacrule rows of a diff into NacDelta rows (for the verdict record)."""
    out: list[NacDelta] = []
    for e in diff.added:
        if e.kind == "nacrule":
            r = proposed.get(e.id)
            out.append(NacDelta(e.id, getattr(r, "name", None), "added", ()))
    for e in diff.removed:
        if e.kind == "nacrule":
            r = baseline.get(e.id)
            out.append(NacDelta(e.id, getattr(r, "name", None), "removed", ()))
    for m in diff.modified:
        if m.ref.kind == "nacrule":
            r = proposed.get(m.ref.id)
            out.append(NacDelta(m.ref.id, getattr(r, "name", None), "modified",
                                m.changed_fields))
    return tuple(out)
