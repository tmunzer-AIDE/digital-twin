"""Verdict -> JSON-able dict / human summary (shared by CLI and MCP)."""

from __future__ import annotations

import dataclasses
from enum import Enum
from typing import Any

from digital_twin.verdict.org_verdict import OrgVerdict
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


def org_verdict_to_dict(ov: OrgVerdict) -> dict[str, Any]:
    """Serialize an OrgVerdict to a JSON-able dict."""
    return {
        "decision": ov.decision.value,
        "decision_reasons": list(ov.decision_reasons),
        "template_id": ov.template_id,
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
    lines = [
        f"org decision: {ov.decision.name}  template: {ov.template_id}",
    ]
    lines += [f"  reason: {r}" for r in ov.decision_reasons[:10]]
    for f in ov.template_findings:
        lines.append(f"  template-finding [{f.severity.value}] {f.code}: {f.message}")
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
