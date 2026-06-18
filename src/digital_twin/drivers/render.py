"""Verdict -> JSON-able dict / human summary (shared by CLI and MCP)."""

from __future__ import annotations

import dataclasses
from enum import Enum
from typing import Any

from digital_twin.contracts import Finding
from digital_twin.verdict.org_verdict import OrgVerdict
from digital_twin.verdict.verdict import Verdict
from digital_twin.viz.markdown import to_markdown


def _cause_clause(f: Finding) -> str:
    if not f.caused_by:
        return ""
    parts = []
    for c in f.caused_by:
        who = f'"{c.ref.name}"' if c.ref.name else c.ref.id
        flds = f" [{', '.join(c.fields)}]" if c.fields else ""
        parts.append(f"{c.ref.kind} {who}{flds}")
    return f" (caused by {', '.join(parts)})"


def _finding_line(f: Finding, label: str = "finding") -> str:
    """One human line: severity, code, WHICH object (subject), WHICH attribute
    (evidence path when present), then the message."""
    where = ""
    if f.subject is not None:
        who = f'"{f.subject.name}"' if f.subject.name else f.subject.id
        where = f" on {f.subject.kind} {who}"
    path = f.evidence.get("path")
    at = f" at {path}" if path else ""
    return f"  {label} [{f.severity.value}] {f.code}{where}{at}: {f.message}{_cause_clause(f)}"


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


def render_diagrams_markdown(verdict: Verdict) -> str:
    """The paste-ready mermaid blob for the elicitation UI."""
    return to_markdown(verdict.diagrams)


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
        lines.append(_finding_line(f))
    for d in verdict.diagrams:
        lines.append(f"  diagram: {d.title}")
    if verdict.state_meta:
        lines.append(
            f"  state: {verdict.state_meta.host} @ {verdict.state_meta.state_acquired_at}"
            f" (age {verdict.state_meta.age_seconds}s)"
        )
    if verdict.trace_ref:
        lines.append(f"  trace: {verdict.trace_ref}")
    return "\n".join(lines)


def org_verdict_to_dict(ov: OrgVerdict) -> dict[str, Any]:
    """Serialize an OrgVerdict to a JSON-able dict."""
    return {
        "decision": ov.decision.value,
        "decision_reasons": list(ov.decision_reasons),
        "changes": [
            {"object_type": c.ref.kind, "object_id": c.ref.id, "name": c.ref.name,
             "action": c.action}
            for c in ov.changes
        ],
        "driving_sites": list(ov.driving_sites),
        "site_failures": dict(ov.site_failures),
        "template_findings": [_plain(f) for f in ov.template_findings],
        "org_rejections": [
            {"stage": r.stage, "reasons": list(r.reasons)} for r in ov.org_rejections
        ],
        "per_site": {sid: verdict_to_dict(v) for sid, v in ov.per_site.items()},
    }


def render_org_human(ov: OrgVerdict) -> str:
    """Render an OrgVerdict as readable plain text."""
    changed = ", ".join(f"{c.action} {c.ref.kind} {c.ref.id}" for c in ov.changes) or "(none)"
    lines = [
        f"org decision: {ov.decision.name}  changes: {changed}",
    ]
    lines += [f"  reason: {r}" for r in ov.decision_reasons[:10]]
    for f in ov.template_findings:
        lines.append(_finding_line(f, "template-finding"))
    if ov.per_site:
        lines.append("  per-site:")
        for sid, v in sorted(ov.per_site.items()):
            top_reason = v.decision_reasons[0] if v.decision_reasons else "ok"
            freshness = ""
            if v.state_meta:
                freshness = f"  age={v.state_meta.age_seconds}s"
            lines.append(f"    {sid}  {v.decision.name}  {top_reason}{freshness}")
    if ov.site_failures:
        failure_parts = ", ".join(f"{sid}({err})" for sid, err in ov.site_failures.items())
        lines.append(f"  site failures: {failure_parts}")
    return "\n".join(lines)
