"""Verdict -> JSON-able dict / human summary (shared by CLI and MCP)."""

from __future__ import annotations

import dataclasses
from enum import Enum
from typing import Any

from digital_twin.verdict.verdict import Verdict


def _plain(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _plain(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_plain(v) for v in obj]
    return obj


def verdict_to_dict(verdict: Verdict) -> dict[str, Any]:
    out: dict[str, Any] = _plain(verdict)
    return out


def render_human(verdict: Verdict) -> str:
    lines = [
        f"decision: {verdict.decision.name}",
        f"severity: {verdict.overall_severity.name if verdict.overall_severity else '-'}",
    ]
    lines += [f"  reason: {r}" for r in verdict.decision_reasons[:10]]
    for res in verdict.check_results:
        lines.append(
            f"  check {res.check_id}: {res.status.value} (coverage={res.coverage.state.value})"
        )
    for f in verdict.findings[:20]:
        lines.append(f"  finding [{f.severity.value}] {f.code}: {f.message}")
    if verdict.state_meta:
        lines.append(
            f"  state: {verdict.state_meta.host} @ {verdict.state_meta.state_acquired_at}"
            f" (age {verdict.state_meta.age_seconds}s)"
        )
    if verdict.trace_ref:
        lines.append(f"  trace: {verdict.trace_ref}")
    return "\n".join(lines)
