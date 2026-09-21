"""Ordered orchestration for heterogeneous configuration ChangePlans."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from digital_twin.adapters.mist.adapter import MistAdapter
from digital_twin.checks.registry import CheckRegistry
from digital_twin.checks.wired import ALL_WIRED_CHECKS
from digital_twin.contracts import ChangeOp, ChangePlan, Rejection
from digital_twin.drivers.render import (
    org_nac_verdict_to_dict,
    org_verdict_to_dict,
    verdict_to_dict,
)
from digital_twin.engine.pipeline import (
    _simulate_site_state,
    simulate,
    simulate_org_nac,
    simulate_org_template,
)
from digital_twin.engine.run_context import RunContext
from digital_twin.providers.base import OrgScope, SiteScope, StateProvider
from digital_twin.providers.proposed import ProposedStateProvider
from digital_twin.scope.allowlist import (
    CONFIG_POLICY_OBJECT_TYPES,
    INERT_ORG_CREATE_OBJECT_TYPES,
    NAC_OBJECT_TYPES,
    ORG_OBJECT_TYPES,
    SUPPORTED_OBJECT_TYPES,
)
from digital_twin.scope.envelope import parse_change_plan
from digital_twin.verdict.state_meta import build_state_meta

_DECISION_PRECEDENCE = {"safe": 0, "review": 1, "unknown": 2, "unsafe": 3}
_CHECK_DOMAINS = {check.id: check.domain for check in ALL_WIRED_CHECKS}
_COVERAGE_STATES = ("complete", "partial", "insufficient", "not_applicable")


def _canonical_kind(value: object) -> str:
    kind = str(value or "").lower().removeprefix("org_").removeprefix("site_")
    return {
        "wlans": "wlan",
        "networks": "network",
        "devices": "device",
    }.get(kind, kind)


def _document_findings(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    findings = [
        *document.get("findings", []),
        *document.get("adapter_findings", []),
        *document.get("template_findings", []),
    ]
    for site_verdict in document.get("per_site", {}).values():
        findings.extend(site_verdict.get("findings", []))
        findings.extend(site_verdict.get("adapter_findings", []))
    return [item for item in findings if isinstance(item, dict)]


def _finding_decision(finding: Mapping[str, Any]) -> str:
    severity = str(finding.get("severity") or "").lower()
    category = str(finding.get("category") or "").lower()
    if category == "network" and severity in {"error", "critical"}:
        return "unsafe"
    if severity == "warning" or (
        category == "operational" and severity in {"error", "critical"}
    ):
        return "review"
    return "safe"


def _finding_matches_op(finding: Mapping[str, Any], op: ChangeOp) -> bool:
    refs: list[Mapping[str, Any]] = []
    subject = finding.get("subject")
    if isinstance(subject, Mapping):
        refs.append(subject)
    for cause in finding.get("caused_by", []):
        if isinstance(cause, Mapping) and isinstance(cause.get("ref"), Mapping):
            refs.append(cause["ref"])

    op_kind = _canonical_kind(op.object_type)
    for ref in refs:
        ref_id = str(ref.get("id") or "")
        ref_kind = _canonical_kind(ref.get("kind"))
        if ref_id and ref_id == op.object_id and (
            not ref_kind or ref_kind == op_kind
        ):
            return True
    return False


def _change_assessments(
    plan: ChangePlan, segment_documents: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Explain a batch per operation without pretending the batch is non-atomic.

    Segment validation supplies each item's intrinsic decision. The authoritative
    original-to-final pass can then raise an individual item when a finding names
    that item's subject/cause. Findings that cannot be attributed to one raw API
    operation remain explicit batch interactions instead of tainting every item.
    """
    ordered_ops = sorted(plan.ops, key=lambda item: item.order)
    by_order: dict[int, dict[str, Any]] = {
        op.order: {
            "order": op.order,
            "scope": _natural_scope(op, plan),
            "action": op.action,
            "object_type": op.object_type,
            "object_id": op.object_id,
            "decision": "safe",
            "decision_reasons": [
                "No item-specific finding requires review; applicable validation passed."
            ],
            "findings": [],
        }
        for op in ordered_ops
    }

    def raise_assessment(
        assessment: dict[str, Any],
        decision: str,
        reasons: list[str],
        findings: list[dict[str, Any]],
    ) -> None:
        old_rank = _DECISION_PRECEDENCE.get(str(assessment["decision"]), 2)
        new_rank = _DECISION_PRECEDENCE.get(decision, 2)
        if new_rank < old_rank:
            return
        if new_rank > old_rank:
            assessment["decision"] = decision
            assessment["decision_reasons"] = []
            assessment["findings"] = []
        elif reasons and assessment["decision_reasons"] == [
            "No item-specific finding requires review; applicable validation passed."
        ]:
            assessment["decision_reasons"] = []
        for reason in reasons:
            if reason and reason not in assessment["decision_reasons"]:
                assessment["decision_reasons"].append(reason)
        for finding in findings:
            compact = {
                key: finding[key]
                for key in ("code", "severity", "message")
                if finding.get(key) is not None
            }
            if compact and compact not in assessment["findings"]:
                assessment["findings"].append(compact)

    batch_segment: dict[str, Any] | None = None
    for segment in segment_documents:
        if segment["route"] == "batch":
            batch_segment = segment
            continue
        orders = [int(order) for order in segment["orders"]]
        document = segment["verdict"]
        decision = str(document.get("decision", "unknown"))
        reasons = [str(reason) for reason in document.get("decision_reasons", [])]
        findings = _document_findings(document)
        ops = [op for op in ordered_ops if op.order in orders]

        if len(ops) == 1:
            raise_assessment(by_order[ops[0].order], decision, reasons, findings)
            continue

        matched_any = False
        for op in ops:
            matched = [finding for finding in findings if _finding_matches_op(finding, op)]
            if not matched:
                continue
            matched_any = True
            matched_decision = max(
                (_finding_decision(finding) for finding in matched),
                key=lambda item: _DECISION_PRECEDENCE.get(item, 2),
            )
            matched_reasons = [
                f"{finding.get('code', 'finding')}: {finding.get('message', '')}".rstrip()
                for finding in matched
                if _finding_decision(finding) == matched_decision
            ]
            raise_assessment(
                by_order[op.order], matched_decision, matched_reasons, matched
            )

        # A hard failure can abort the whole grouped evaluation. If a non-SAFE
        # result has no object attribution, be conservative and label the group.
        if decision == "unknown" or (decision != "safe" and not matched_any):
            for op in ops:
                raise_assessment(by_order[op.order], decision, reasons, findings)

    interaction: dict[str, Any] | None = None
    if batch_segment is not None:
        document = batch_segment["verdict"]
        batch_findings = _document_findings(document)
        attributed: set[int] = set()
        for finding in batch_findings:
            finding_decision = _finding_decision(finding)
            if finding_decision == "safe":
                continue
            for op in ordered_ops:
                if not _finding_matches_op(finding, op):
                    continue
                attributed.add(op.order)
                reason = (
                    f"{finding.get('code', 'finding')}: "
                    f"{finding.get('message', '')}"
                ).rstrip()
                raise_assessment(
                    by_order[op.order], finding_decision, [reason], [finding]
                )
        interaction = {
            "decision": str(document.get("decision", "unknown")),
            "decision_reasons": [
                str(reason) for reason in document.get("decision_reasons", [])
            ],
            "finding_codes": list(dict.fromkeys(
                str(finding.get("code"))
                for finding in batch_findings
                if finding.get("code") is not None
            )),
            "attributed_change_orders": sorted(attributed),
        }

    return [by_order[op.order] for op in ordered_ops], interaction


def _natural_scope(op: ChangeOp, plan: ChangePlan) -> str:
    if op.scope is not None:
        return op.scope
    if op.object_type in NAC_OBJECT_TYPES:
        return "org"
    if op.object_type in ORG_OBJECT_TYPES:
        # WLAN exists at both scopes; legacy plans rely on plan-level site_id.
        if op.object_type == "wlan" and plan.scope.site_id:
            return "site"
        return "org"
    if op.object_type in CONFIG_POLICY_OBJECT_TYPES:
        return "site" if op.object_type.startswith("site_") else "org"
    return "site" if plan.scope.site_id else "org"


def _route(op: ChangeOp, plan: ChangePlan) -> str:
    if op.object_type == "wlantemplate" and op.action == "delete":
        return "org"
    if op.object_type in CONFIG_POLICY_OBJECT_TYPES or (
        op.action == "create" and op.object_type in INERT_ORG_CREATE_OBJECT_TYPES
    ):
        return "policy"
    if op.object_type in NAC_OBJECT_TYPES:
        return "nac"
    if op.object_type in ORG_OBJECT_TYPES and _natural_scope(op, plan) == "org":
        return "org"
    if op.object_type in SUPPORTED_OBJECT_TYPES and _natural_scope(op, plan) == "site":
        return "site"
    if (
        op.action == "update"
        and set(op.payload) == {"name"}
        and isinstance(op.payload.get("name"), str)
        and bool(str(op.payload["name"]).strip())
    ):
        return "name"
    return "fallback"


def needs_composite_evaluation(plan_data: Mapping[str, Any]) -> bool:
    plan = parse_change_plan(plan_data)
    if isinstance(plan, Rejection):
        return False
    routes = [_route(op, plan) for op in sorted(plan.ops, key=lambda item: item.order)]
    return (
        len(plan.ops) > 1
        or len(set(routes)) > 1
        or any(
            op.action == "create" and op.object_type in INERT_ORG_CREATE_OBJECT_TYPES
            for op in plan.ops
        )
        or any(
            op.object_type in CONFIG_POLICY_OBJECT_TYPES
            and op.object_type in ORG_OBJECT_TYPES
            for op in plan.ops
        )
        or any(
        _natural_scope(op, plan) == "org" and plan.scope.site_id
        for op in plan.ops
        )
    )


def _segments(plan: ChangePlan) -> list[tuple[str, tuple[ChangeOp, ...]]]:
    segments: list[tuple[str, list[ChangeOp]]] = []
    for op in sorted(plan.ops, key=lambda item: item.order):
        route = _route(op, plan)
        if not segments or segments[-1][0] != route:
            segments.append((route, [op]))
        else:
            segments[-1][1].append(op)
    return [(route, tuple(ops)) for route, ops in segments]


def _subplan(plan: ChangePlan, route: str, ops: tuple[ChangeOp, ...]) -> dict[str, Any]:
    include_site = route in {"site", "fallback"} or (
        route in {"policy", "name"}
        and any(_natural_scope(op, plan) == "site" for op in ops)
    )
    scope: dict[str, str] = {"org_id": plan.scope.org_id}
    if include_site and plan.scope.site_id:
        scope["site_id"] = plan.scope.site_id
    raw_ops = [
        {
            "action": op.action,
            "order": op.order,
            "scope": op.scope,
            "object_type": op.object_type,
            "object_id": op.object_id,
            "payload": dict(op.payload),
        }
        for op in ops
    ]
    return {
        "source": plan.source,
        "scope": scope,
        "ops": raw_ops,
        **({"intent": plan.intent} if plan.intent is not None else {}),
    }


def _rollup_check_coverage(checks: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Rebuild the normal Verdict coverage contract after composite de-duplication."""
    counts: dict[str, dict[str, int]] = {}
    for check in checks:
        check_id = str(check.get("check_id") or "unknown")
        coverage = check.get("coverage")
        state = coverage.get("state") if isinstance(coverage, Mapping) else None
        if state not in _COVERAGE_STATES:
            continue
        domain = _CHECK_DOMAINS.get(check_id, check_id.rsplit(".", 1)[0])
        bucket = counts.setdefault(domain, dict.fromkeys(_COVERAGE_STATES, 0))
        bucket[state] += 1
    return counts


def _summarize_confidence(findings: list[dict[str, Any]]) -> dict[str, Any]:
    """Mirror the standard finding-confidence summary for a flattened verdict."""
    counts = {"high": 0, "medium": 0, "low": 0}
    reasons: list[str] = []
    level_names = {3: "high", 2: "medium", 1: "low", "high": "high",
                   "medium": "medium", "low": "low"}
    for finding in findings:
        confidence = finding.get("confidence")
        if not isinstance(confidence, Mapping):
            continue
        level = confidence.get("level")
        normalized = level.lower() if isinstance(level, str) else level
        name = level_names.get(normalized)
        if name is None:
            continue
        counts[name] += 1
        if name != "high":
            for reason in confidence.get("reasons", []):
                reasons.append(str(reason))
    return {**counts, "reasons": reasons}


def _flatten_state_meta(segment_documents: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Expose the oldest/least-fresh state and union partial-fetch evidence."""
    metadata: list[Mapping[str, Any]] = []
    for item in segment_documents:
        document = item["verdict"]
        direct = document.get("state_meta")
        if isinstance(direct, Mapping):
            metadata.append(direct)
        for site_verdict in document.get("per_site", {}).values():
            site_meta = site_verdict.get("state_meta")
            if isinstance(site_meta, Mapping):
                metadata.append(site_meta)
    if not metadata:
        return None

    def age(meta: Mapping[str, Any]) -> int:
        value = meta.get("age_seconds")
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    oldest = max(metadata, key=age)
    hosts: list[str] = []
    fetched: list[str] = []
    failures: list[Any] = []
    for meta in metadata:
        host = meta.get("host")
        if isinstance(host, str) and host not in hosts:
            hosts.append(host)
        for name in meta.get("fetched", []):
            value = str(name)
            if value not in fetched:
                fetched.append(value)
        for failure in meta.get("fetch_failures", []):
            if failure not in failures:
                failures.append(failure)

    return {
        "state_acquired_at": oldest.get("state_acquired_at"),
        "host": ", ".join(hosts),
        "age_seconds": max(age(meta) for meta in metadata),
        "fetched": fetched,
        "fetch_failures": failures,
    }


def _flatten_document(
    *, plan: ChangePlan, segment_documents: list[dict[str, Any]]
) -> dict[str, Any]:
    decisions = [str(item["verdict"].get("decision", "unknown")) for item in segment_documents]
    decision = max(decisions, key=lambda item: _DECISION_PRECEDENCE.get(item, 2))
    reasons: list[str] = []
    findings: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    config_diffs: list[dict[str, Any]] = []
    diagrams: list[dict[str, Any]] = []

    batch_check_ids = {
        str(check.get("check_id"))
        for item in segment_documents
        if item["route"] == "batch"
        for site_verdict in item["verdict"].get("per_site", {}).values()
        for check in site_verdict.get("check_results", [])
    }

    def superseded_finding(finding: Mapping[str, Any]) -> bool:
        if finding.get("source") != "check":
            return False
        code = str(finding.get("code") or "")
        return any(code == check_id or code.startswith(f"{check_id}.")
                   for check_id in batch_check_ids)

    for item in segment_documents:
        verdict = item["verdict"]
        is_batch = item["route"] == "batch"
        label = f"segment {item['index']} ({item['route']}, orders {item['orders']})"
        reasons.extend(
            f"{label}: {reason}" for reason in verdict.get("decision_reasons", [])
        )
        findings.extend(
            finding for finding in verdict.get("findings", [])
            if is_batch or not superseded_finding(finding)
        )
        findings.extend(verdict.get("adapter_findings", []))
        findings.extend(verdict.get("template_findings", []))
        checks.extend(
            check for check in verdict.get("check_results", [])
            if is_batch or str(check.get("check_id")) not in batch_check_ids
        )
        config_diffs.extend(verdict.get("config_diffs", []))
        diagrams.extend(verdict.get("diagrams", []))
        for site_id, site_verdict in verdict.get("per_site", {}).items():
            findings.extend(
                finding for finding in site_verdict.get("findings", [])
                if is_batch or not superseded_finding(finding)
            )
            checks.extend(
                check for check in site_verdict.get("check_results", [])
                if is_batch or str(check.get("check_id")) not in batch_check_ids
            )
            config_diffs.extend(site_verdict.get("config_diffs", []))
            diagrams.extend(site_verdict.get("diagrams", []))
            for reason in site_verdict.get("decision_reasons", []):
                reasons.append(f"{label}, site {site_id}: {reason}")

    severity_order = {"info": 0, "warning": 1, "error": 2, "critical": 3}
    severities = [str(f.get("severity", "")).lower() for f in findings]
    overall = max(severities, key=lambda value: severity_order.get(value, -1), default=None)
    change_assessments, batch_interaction = _change_assessments(
        plan, segment_documents
    )
    return {
        "decision": decision,
        "decision_reasons": reasons,
        "overall_severity": overall,
        "findings": findings,
        "check_results": checks,
        "coverage": _rollup_check_coverage(checks),
        "confidence_summary": _summarize_confidence(findings),
        "state_meta": _flatten_state_meta(segment_documents),
        "config_diffs": config_diffs,
        "diagrams": diagrams,
        "changes": [
            {
                "order": op.order,
                "scope": _natural_scope(op, plan),
                "action": op.action,
                "object_type": op.object_type,
                "object_id": op.object_id,
            }
            for op in sorted(plan.ops, key=lambda item: item.order)
        ],
        "change_assessments": change_assessments,
        "batch_interaction": batch_interaction,
        "composite": True,
        "segments": segment_documents,
    }


def _batch_validation_document(provider: ProposedStateProvider) -> dict[str, Any] | None:
    """Evaluate the whole batch as original state -> fully composed final state."""
    per_site: dict[str, dict[str, Any]] = {}
    adapter = MistAdapter()
    registry = CheckRegistry(ALL_WIRED_CHECKS)
    for site_id, (baseline, proposed) in sorted(provider.cumulative_site_states().items()):
        verdict = _simulate_site_state(
            baseline,
            proposed,
            adapter=adapter,
            registry=registry,
            run=RunContext(),
            state_meta=build_state_meta(baseline.meta, now=datetime.now(UTC)),
        )
        per_site[site_id] = verdict_to_dict(verdict)
    if not per_site:
        return None

    decision = max(
        (str(verdict.get("decision", "unknown")) for verdict in per_site.values()),
        key=lambda item: _DECISION_PRECEDENCE.get(item, 2),
    )
    reasons = [
        f"site {site_id}: {reason}"
        for site_id, verdict in per_site.items()
        for reason in verdict.get("decision_reasons", [])
    ]
    return {
        "decision": decision,
        "decision_reasons": reasons,
        "per_site": per_site,
        "findings": [],
        "check_results": [],
        "config_diffs": [],
        "diagrams": [],
    }


def simulate_composite(
    plan_data: Mapping[str, Any],
    *,
    provider: StateProvider,
    run: RunContext | None = None,
    l0_full_object: bool = False,
) -> dict[str, Any]:
    """Evaluate every ordered segment and return one flattened batch verdict."""
    parsed = parse_change_plan(plan_data)
    if isinstance(parsed, Rejection):
        return {
            "decision": "unknown",
            "decision_reasons": [
                f"UNSUPPORTED [{parsed.stage}]: {reason}" for reason in parsed.reasons
            ],
            "findings": [],
            "check_results": [],
            "config_diffs": [],
            "changes": [],
            "change_assessments": [],
            "batch_interaction": None,
            "composite": True,
            "segments": [],
        }

    proposed = ProposedStateProvider(provider)
    segment_documents: list[dict[str, Any]] = []
    for index, (route, ops) in enumerate(_segments(parsed), start=1):
        data = _subplan(parsed, route, ops)
        segment_run = RunContext()
        if route == "org":
            document = org_verdict_to_dict(simulate_org_template(
                data, provider=proposed, run=segment_run,
                # The final original->composed pass below is authoritative for
                # topology checks. Segment evaluation still owns schema, field,
                # fetch, compile, and apply failures.
                registry=CheckRegistry([]),
                l0_full_object=l0_full_object,
            ))
        elif route == "nac":
            document = org_nac_verdict_to_dict(simulate_org_nac(
                data, provider=proposed, run=segment_run,
                l0_full_object=l0_full_object,
            ))
        else:
            document = verdict_to_dict(simulate(
                data, provider=proposed, run=segment_run,
                registry=(CheckRegistry([]) if route == "site" else None),
                l0_full_object=l0_full_object,
            ))
        segment_documents.append({
            "index": index,
            "route": route,
            "orders": [op.order for op in ops],
            "verdict": document,
        })

        if route == "policy":
            proposed.apply_policy_ops(OrgScope(parsed.scope.org_id), ops)
        elif route == "org":
            proposed.apply_org_ops(OrgScope(parsed.scope.org_id), ops)
        elif route == "site" and parsed.scope.site_id:
            proposed.apply_site_ops(
                SiteScope(parsed.scope.org_id, parsed.scope.site_id), ops
            )

    batch_document = _batch_validation_document(proposed)
    if batch_document is not None:
        segment_documents.append({
            "index": len(segment_documents) + 1,
            "route": "batch",
            "orders": [op.order for op in sorted(parsed.ops, key=lambda item: item.order)],
            "verdict": batch_document,
        })

    return _flatten_document(plan=parsed, segment_documents=segment_documents)
