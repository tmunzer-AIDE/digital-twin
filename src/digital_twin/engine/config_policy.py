"""Explicit safety policy for non-topological Mist configuration objects."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from digital_twin.checks.base import CheckResult, Coverage, CoverageState, Status
from digital_twin.contracts import (
    ChangeOp,
    Finding,
    FindingCategory,
    FindingSource,
    Rejection,
    Severity,
)
from digital_twin.engine.run_context import RunContext
from digital_twin.ir import Confidence, ConfidenceLevel, IRDiff
from digital_twin.providers.base import (
    FetchError,
    OrgScope,
    OrgSiteGroupContext,
    PskUsageContext,
    SiteScope,
    StateProvider,
)
from digital_twin.scope.allowlist import CONFIG_POLICY_OBJECT_TYPES
from digital_twin.scope.envelope import parse_change_plan
from digital_twin.verdict.decision import DecisionInputs
from digital_twin.verdict.verdict import Verdict, assemble

_EMPTY_DIFF = IRDiff((), (), ())
_HIGH = Confidence(level=ConfidenceLevel.HIGH)
_ACTIONS = frozenset({"create", "update", "delete"})


@dataclass(frozen=True)
class _Assessment:
    check_id: str
    reason: str
    review: bool = False
    coverage_complete: bool = True
    evidence: Mapping[str, Any] | None = None


def _warning(assessment: _Assessment, op: ChangeOp) -> Finding:
    return Finding(
        source=FindingSource.CHECK,
        category=FindingCategory.NETWORK,
        code=assessment.check_id,
        severity=Severity.WARNING,
        confidence=_HIGH,
        message=assessment.reason,
        affected_entities=(op.object_id,),
        evidence=assessment.evidence or {},
    )


def _result(assessment: _Assessment, op: ChangeOp) -> CheckResult:
    finding = (_warning(assessment, op),) if assessment.review else ()
    return CheckResult(
        check_id=assessment.check_id,
        status=Status.WARN if assessment.review else Status.PASS,
        findings=finding,
        coverage=Coverage(
            CoverageState.COMPLETE if assessment.coverage_complete else CoverageState.PARTIAL,
            (assessment.reason,),
        ),
        confidence=_HIGH,
        reasoning=assessment.reason,
    )


def _static_assessment(op: ChangeOp) -> _Assessment | None:
    if op.object_type == "org_info":
        return _Assessment("config.org_info", "organization information changes are safe")
    if op.object_type == "org_alarmtemplates":
        return _Assessment("config.alarmtemplate", "alarm-template changes are safe")
    if op.object_type == "org_sitegroups" and op.action != "delete":
        return _Assessment("config.sitegroup", "site-group create and update changes are safe")
    if op.object_type in {"org_psks", "site_psks"} and op.action == "create":
        return _Assessment("config.psk.recent_usage", "new PSKs have no prior client usage")
    if op.object_type in {"org_webhooks", "site_webhooks"}:
        return _Assessment(
            "config.webhook.review",
            "webhook changes require review because delivery impact is external to the twin",
            review=True,
        )
    return None


def _sitegroup_delete(op: ChangeOp, provider: StateProvider, org_id: str) -> _Assessment:
    context = provider.resolve_org_sitegroup(OrgScope(org_id), op.object_id)
    if isinstance(context, FetchError):
        return _Assessment(
            "config.sitegroup.delete",
            "site-group membership could not be verified; deletion requires review",
            review=True,
            coverage_complete=False,
            evidence={"fetch_failures": [f.error for f in context.failures]},
        )
    assert isinstance(context, OrgSiteGroupContext)
    if context.assigned_site_ids:
        return _Assessment(
            "config.sitegroup.delete",
            "site group has assigned sites; deletion requires review",
            review=True,
            evidence={"assigned_site_ids": list(context.assigned_site_ids)},
        )
    return _Assessment(
        "config.sitegroup.delete",
        "site group has no assigned sites; deletion is safe",
    )


def _psk_change(
    op: ChangeOp, provider: StateProvider, org_id: str, site_id: str | None
) -> _Assessment:
    scope: OrgScope | SiteScope
    if op.object_type == "site_psks":
        if not site_id:
            return _Assessment(
                "config.psk.recent_usage",
                "site PSK scope is missing; recent usage cannot be verified",
                review=True,
                coverage_complete=False,
            )
        scope = SiteScope(org_id, site_id)
    else:
        scope = OrgScope(org_id)

    context = provider.resolve_psk_usage(scope, op.object_id, window_days=7)
    if isinstance(context, FetchError):
        return _Assessment(
            "config.psk.recent_usage",
            "PSK usage could not be verified; the change requires review",
            review=True,
            coverage_complete=False,
            evidence={"fetch_failures": [f.error for f in context.failures], "window_days": 7},
        )
    assert isinstance(context, PskUsageContext)
    evidence: dict[str, Any] = {
        "window_days": context.window_days,
        "checked_site_ids": list(context.checked_site_ids),
        "active_site_ids": list(context.active_site_ids),
    }
    if context.failures:
        evidence["fetch_failures"] = [f.error for f in context.failures]
    if context.active_site_ids:
        return _Assessment(
            "config.psk.recent_usage",
            f"PSK was used during the last {context.window_days} days; the change requires review",
            review=True,
            coverage_complete=not context.failures,
            evidence=evidence,
        )
    if context.failures:
        return _Assessment(
            "config.psk.recent_usage",
            "PSK had no observed sessions, but telemetry was incomplete; "
            "the change requires review",
            review=True,
            coverage_complete=False,
            evidence=evidence,
        )
    return _Assessment(
        "config.psk.recent_usage",
        f"PSK had no client sessions during the last {context.window_days} days; "
        "the change is safe",
        evidence=evidence,
    )


def simulate_configuration_policy(
    plan_data: Mapping[str, Any], *, provider: StateProvider, run: RunContext
) -> Verdict | None:
    """Evaluate explicit policy objects, or return ``None`` for other plans."""
    plan = parse_change_plan(plan_data)
    if isinstance(plan, Rejection) or not plan.ops:
        return None
    if not all(op.object_type in CONFIG_POLICY_OBJECT_TYPES for op in plan.ops):
        return None

    trace = run.trace
    assert trace is not None
    with trace.stage("configuration_policy"):
        invalid: list[str] = []
        for op in plan.ops:
            if op.action not in _ACTIONS:
                invalid.append(
                    f"ops[order={op.order}]: unsupported action {op.action!r} "
                    "(configuration policy supports create | update | delete)"
                )
            if op.object_type == "org_info" and op.action != "update":
                invalid.append("org_info supports update only")
            if op.action == "delete" and op.payload:
                invalid.append(f"ops[order={op.order}]: delete payload must be empty")
        if invalid:
            return assemble(
                inputs=DecisionInputs(
                    rejections=(Rejection(stage="configuration_policy", reasons=tuple(invalid)),),
                    l0_fatal=False,
                    baseline_unavailable=False,
                    check_results=(),
                ),
                ir_diff=_EMPTY_DIFF,
                trace_ref=run.run_id,
            )

        results: list[CheckResult] = []
        for op in sorted(plan.ops, key=lambda item: item.order):
            assessment = _static_assessment(op)
            if assessment is None and op.object_type == "org_sitegroups":
                assessment = _sitegroup_delete(op, provider, plan.scope.org_id)
            if assessment is None and op.object_type in {"org_psks", "site_psks"}:
                assessment = _psk_change(
                    op, provider, plan.scope.org_id, plan.scope.site_id
                )
            assert assessment is not None
            results.append(_result(assessment, op))

        return assemble(
            inputs=DecisionInputs(
                rejections=(),
                l0_fatal=False,
                baseline_unavailable=False,
                check_results=tuple(results),
            ),
            ir_diff=_EMPTY_DIFF,
            trace_ref=run.run_id,
        )
