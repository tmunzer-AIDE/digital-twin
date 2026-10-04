"""Explicit safety policy for non-topological Mist configuration objects."""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from digital_twin.adapters.mist.adapter import MistAdapter
from digital_twin.adapters.mist.apply.objects import effective_update
from digital_twin.analysis.context import AnalysisContext
from digital_twin.analysis.delta_cause import delta_index
from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState, Status
from digital_twin.checks.registry import CheckRegistry
from digital_twin.checks.wired import ALL_WIRED_CHECKS
from digital_twin.config_diff import object_config_diff
from digital_twin.contracts import (
    Cause,
    ChangeOp,
    Finding,
    FindingCategory,
    FindingSource,
    ObjectConfigDiff,
    ObjectRef,
    Rejection,
    Severity,
)
from digital_twin.engine.run_context import RunContext
from digital_twin.ir import Confidence, ConfidenceLevel, IRDiff, diff_ir
from digital_twin.providers.base import (
    FetchError,
    ObjectRelationshipContext,
    OrgNetworksContext,
    OrgScope,
    OrgSiteGroupContext,
    OrgWlanTemplateContext,
    PskUsageContext,
    RawSiteState,
    SiteScope,
    StateProvider,
)
from digital_twin.scope.allowlist import (
    CONFIG_POLICY_OBJECT_TYPES,
    INERT_ORG_CREATE_OBJECT_TYPES,
)
from digital_twin.scope.envelope import parse_change_plan
from digital_twin.verdict.decision import DecisionInputs
from digital_twin.verdict.verdict import Verdict, assemble

_EMPTY_DIFF = IRDiff((), (), ())
_HIGH = Confidence(level=ConfidenceLevel.HIGH)
_ACTIONS = frozenset({"create", "update", "delete"})
_CREATE_SAFE_RELATIONSHIP_TYPES = frozenset(
    {
        "org_avprofiles",
        "org_deviceprofiles",
        "org_idpprofiles",
        "org_aamwprofiles",
        "org_nactags",
        "org_rftemplates",
        "org_services",
        "org_servicepolicies",
        "org_sites",
        "org_vpns",
        "org_wxtags",
        "site_wxtags",
    }
)
_RELATIONSHIP_TYPES = frozenset(
    {
        "org_avprofiles",
        "org_deviceprofiles",
        "org_idpprofiles",
        "org_aamwprofiles",
        "org_nactags",
        "org_rftemplates",
        "org_services",
        "org_servicepolicies",
        "org_vpns",
    }
)
_ALWAYS_REVIEW_TYPES = frozenset({"org_wxrules", "site_wxrules"})


@dataclass(frozen=True)
class _Assessment:
    check_id: str
    reason: str
    review: bool = False
    coverage_complete: bool = True
    evidence: Mapping[str, Any] | None = None
    severity: Severity = Severity.WARNING
    category: FindingCategory = FindingCategory.NETWORK
    affected_entities: tuple[str, ...] = ()


def _warning(assessment: _Assessment, op: ChangeOp) -> Finding:
    ref = ObjectRef(op.object_type, op.object_id)
    return Finding(
        source=FindingSource.CHECK,
        category=assessment.category,
        code=assessment.check_id,
        severity=assessment.severity,
        confidence=_HIGH,
        message=assessment.reason,
        affected_entities=assessment.affected_entities or (op.object_id,),
        subject=ref,
        evidence=assessment.evidence or {},
        caused_by=(
            Cause(
                ref=ref,
                fields=(tuple(sorted(op.payload)) if op.action != "delete" else ("deleted",)),
            ),
        ),
    )


def _result(assessment: _Assessment, op: ChangeOp) -> CheckResult:
    finding = (_warning(assessment, op),) if assessment.review else ()
    return CheckResult(
        check_id=assessment.check_id,
        status=(
            Status.FAIL
            if assessment.review and assessment.severity in (Severity.ERROR, Severity.CRITICAL)
            else Status.WARN
            if assessment.review
            else Status.PASS
        ),
        findings=finding,
        coverage=Coverage(
            CoverageState.COMPLETE if assessment.coverage_complete else CoverageState.PARTIAL,
            (assessment.reason,),
        ),
        confidence=_HIGH,
        reasoning=assessment.reason,
    )


def _static_assessment(op: ChangeOp) -> _Assessment | None:
    if op.object_type == "wlantemplate":
        if op.action == "create":
            name = op.payload.get("name")
            if not isinstance(name, str) or not name.strip():
                return _Assessment(
                    "config.wlantemplate.invalid",
                    "new WLAN templates require a non-empty name",
                    review=True,
                    severity=Severity.ERROR,
                    category=FindingCategory.OPERATIONAL,
                )
            return _Assessment(
                "config.wlantemplate.create",
                "new unassigned WLAN template metadata is valid and inert",
            )
        assignment_roots = {"site_ids", "sitegroup_ids"} & set(op.payload)
        if assignment_roots:
            return None
        return _Assessment(
            "config.wlantemplate.update",
            "WLAN template metadata update does not change site assignments",
        )
    if op.object_type in INERT_ORG_CREATE_OBJECT_TYPES and op.action == "create":
        return _Assessment(
            f"config.{op.object_type}.create",
            f"new unassigned {op.object_type} configuration is valid and inert",
        )
    if op.object_type == "org_info":
        return _Assessment("config.org_info", "organization information changes are safe")
    if op.object_type == "org_alarmtemplates":
        return _Assessment("config.alarmtemplate", "alarm-template changes are safe")
    if op.object_type == "org_settings":
        return _Assessment(
            "config.org_settings.review",
            "organization settings can change network-wide behavior and require review",
            review=True,
        )
    if op.object_type == "org_sitegroups" and op.action == "create":
        return _Assessment("config.sitegroup", "new empty site groups are safe")
    if op.object_type == "org_sitegroups" and op.action == "update":
        return _Assessment(
            "config.sitegroup.membership.review",
            "site-group changes can alter template or policy targeting and require review",
            review=True,
        )
    if op.object_type in {"org_psks", "site_psks"} and op.action == "create":
        return _Assessment("config.psk.recent_usage", "new PSKs have no prior client usage")
    if op.object_type in {"org_webhooks", "site_webhooks"}:
        return _Assessment(
            "config.webhook",
            "webhook changes do not alter network connectivity or forwarding",
        )
    if op.object_type in _CREATE_SAFE_RELATIONSHIP_TYPES:
        family = op.object_type.removeprefix("org_").removeprefix("site_")
        if op.action == "create":
            return _Assessment(
                f"config.{family}.create",
                f"new unreferenced {family} objects are safe to create",
            )
        if op.object_type in {"org_sites", "org_wxtags", "site_wxtags"}:
            return _Assessment(
                f"config.{family}.review",
                f"{family} {op.action} can change device or policy targeting and requires review",
                review=True,
            )
    if op.object_type in _ALWAYS_REVIEW_TYPES:
        family = op.object_type.removeprefix("org_").removeprefix("site_")
        return _Assessment(
            f"config.{family}.review",
            f"{family} changes can immediately alter user-experience policy and require review",
            review=True,
        )
    return None


def _string_ids(value: Any) -> tuple[str, ...] | None:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        return None
    return tuple(dict.fromkeys(value))


def _wlan_scope_overlap(a: Mapping[str, Any], b: Mapping[str, Any]) -> str:
    """Return yes/no/unknown for whether two vendor WLAN rows share an AP."""
    a_scope = a.get("apply_to")
    b_scope = b.get("apply_to")
    if a_scope in (None, "wxtags") or b_scope in (None, "wxtags"):
        return "unknown"
    if a_scope == "site" and b_scope == "site":
        return "yes"
    if a_scope == "site" and b_scope == "aps":
        return "yes" if b.get("ap_ids") else "no"
    if b_scope == "site" and a_scope == "aps":
        return "yes" if a.get("ap_ids") else "no"
    if a_scope == "aps" and b_scope == "aps":
        a_ids = {str(ap_id) for ap_id in (a.get("ap_ids") or ())}
        b_ids = {str(ap_id) for ap_id in (b.get("ap_ids") or ())}
        return "yes" if a_ids & b_ids else "no"
    return "unknown"


def _wlantemplate_site_impact(
    state: RawSiteState, template_id: str, rows: tuple[Mapping[str, Any], ...]
) -> tuple[list[dict[str, Any]], list[str]]:
    proposed_rows = tuple(
        row for row in state.wlans if str(row.get("template_id") or "") != template_id
    ) + tuple({**dict(row), "template_id": template_id, "for_site": False} for row in rows)
    adapter = MistAdapter()
    baseline = adapter.ingest(state)
    proposed = adapter.ingest(replace(state, wlans=proposed_rows))
    if baseline.ir is None or proposed.ir is None:
        return [], ["site configuration could not be compiled"]
    diff = diff_ir(baseline.ir, proposed.ir)
    results = CheckRegistry(ALL_WIRED_CHECKS).run_all(
        CheckContext(
            baseline=AnalysisContext(baseline.ir),
            proposed=AnalysisContext(proposed.ir),
            diff=diff,
            delta_index=delta_index(diff),
        )
    )
    findings = [
        {
            "code": finding.code,
            "severity": finding.severity.value,
            "message": finding.message,
            "evidence": dict(finding.evidence),
        }
        for result in results
        for finding in result.findings
        if finding.severity in {Severity.WARNING, Severity.ERROR, Severity.CRITICAL}
    ]
    coverage = [
        note
        for result in results
        if result.coverage.state in {CoverageState.PARTIAL, CoverageState.INSUFFICIENT}
        for note in (result.coverage.notes or (result.reasoning,))
    ]
    return findings, coverage


def _wlantemplate_assignment(op: ChangeOp, provider: StateProvider, org_id: str) -> _Assessment:
    context = provider.resolve_org_wlan_template(OrgScope(org_id), op.object_id)
    if isinstance(context, FetchError):
        return _Assessment(
            "config.wlantemplate.assignment",
            "WLAN template or its WLAN definitions could not be fetched; "
            "assignment requires review",
            review=True,
            coverage_complete=False,
            evidence={"fetch_failures": [f.error for f in context.failures]},
        )
    assert isinstance(context, OrgWlanTemplateContext)
    if not context.template_wlans_complete:
        return _Assessment(
            "config.wlantemplate.assignment",
            "WLAN definitions for the template are incomplete; assignment requires review",
            review=True,
            coverage_complete=False,
        )

    site_ids = _string_ids(op.payload.get("site_ids", context.template.get("site_ids", [])))
    sitegroup_ids = _string_ids(
        op.payload.get("sitegroup_ids", context.template.get("sitegroup_ids", []))
    )
    if site_ids is None or sitegroup_ids is None:
        return _Assessment(
            "config.wlantemplate.assignment.invalid",
            "site_ids and sitegroup_ids must be lists of non-empty identifiers",
            review=True,
            severity=Severity.ERROR,
            category=FindingCategory.OPERATIONAL,
        )
    targets = set(site_ids)
    group_evidence: dict[str, list[str]] = {}
    for group_id in sitegroup_ids:
        group = provider.resolve_org_sitegroup(OrgScope(org_id), group_id)
        if isinstance(group, FetchError):
            return _Assessment(
                "config.wlantemplate.assignment",
                f"site group {group_id} could not be resolved; assignment requires review",
                review=True,
                coverage_complete=False,
                evidence={"fetch_failures": [f.error for f in group.failures]},
            )
        assigned = list(group.assigned_site_ids)
        group_evidence[group_id] = assigned
        targets.update(assigned)

    previous_targets = set(context.derived_rows_by_site)
    affected_sites = targets | previous_targets
    if not affected_sites:
        return _Assessment(
            "config.wlantemplate.assignment",
            "WLAN template assignment has no current or proposed target sites",
            evidence={"target_site_ids": sorted(targets)},
        )
    states = provider.fetch_sites(OrgScope(org_id), site_ids=sorted(affected_sites))
    missing = sorted(affected_sites - set(states))
    failures: list[str] = []
    conflicts: list[dict[str, str]] = []
    unverifiable: list[dict[str, str]] = []
    impact_findings: list[dict[str, Any]] = []
    impact_coverage: list[str] = []
    for site_id in sorted(affected_sites):
        state = states.get(site_id)
        if not isinstance(state, RawSiteState):
            if isinstance(state, FetchError):
                failures.extend(f"{site_id}: {f.error}" for f in state.failures)
            elif site_id in missing:
                failures.append(f"{site_id}: no site state returned")
            continue
        desired_rows = context.template_wlans if site_id in targets else ()
        for proposed in desired_rows:
            if proposed.get("enabled") is False:
                continue
            ssid = proposed.get("ssid")
            if not isinstance(ssid, str) or not ssid:
                continue
            proposed_id = str(proposed.get("id") or "")
            for current in state.wlans:
                if current.get("enabled") is False:
                    continue
                if str(current.get("id") or "") == proposed_id:
                    continue
                if str(current.get("ssid") or "").casefold() == ssid.casefold():
                    detail = {
                        "site_id": site_id,
                        "ssid": ssid,
                        "template_wlan_id": proposed_id,
                        "existing_wlan_id": str(current.get("id") or ""),
                    }
                    overlap = _wlan_scope_overlap(proposed, current)
                    if overlap == "yes":
                        conflicts.append(detail)
                    elif overlap == "unknown":
                        unverifiable.append(detail)
        site_findings, site_coverage = _wlantemplate_site_impact(state, op.object_id, desired_rows)
        impact_findings.extend({**finding, "site_id": site_id} for finding in site_findings)
        impact_coverage.extend(f"{site_id}: {note}" for note in site_coverage)
    evidence: dict[str, Any] = {
        "target_site_ids": sorted(targets),
        "previous_target_site_ids": sorted(previous_targets),
        "sitegroups": group_evidence,
        "template_wlan_ids": [str(row.get("id") or "") for row in context.template_wlans],
    }
    if conflicts:
        evidence["conflicts"] = conflicts
        return _Assessment(
            "config.wlantemplate.assignment.conflict",
            "WLAN template assignment would create duplicate SSIDs on target sites",
            review=True,
            severity=Severity.ERROR,
            evidence=evidence,
        )
    impact_errors = [
        finding
        for finding in impact_findings
        if finding["severity"] in {Severity.ERROR.value, Severity.CRITICAL.value}
    ]
    if impact_errors:
        evidence["impact_findings"] = impact_errors
        return _Assessment(
            "config.wlantemplate.assignment.impact",
            "WLAN template assignment introduces a connectivity error on target sites",
            review=True,
            severity=Severity.ERROR,
            evidence=evidence,
        )
    if failures:
        evidence["fetch_failures"] = failures
        return _Assessment(
            "config.wlantemplate.assignment",
            "no SSID conflict was observed, but some target sites could not be evaluated",
            review=True,
            coverage_complete=False,
            evidence=evidence,
        )
    if unverifiable:
        evidence["unverifiable_conflicts"] = unverifiable
        return _Assessment(
            "config.wlantemplate.assignment.unverified",
            "duplicate SSIDs use WxLAN or unknown AP scopes, so overlap cannot be verified",
            review=True,
            coverage_complete=False,
            evidence=evidence,
        )
    if impact_findings or impact_coverage:
        evidence["impact_findings"] = impact_findings
        evidence["coverage_gaps"] = impact_coverage
        return _Assessment(
            "config.wlantemplate.assignment.impact",
            "WLAN template assignment has warnings or incomplete connectivity coverage",
            review=True,
            coverage_complete=not impact_coverage,
            evidence=evidence,
        )
    return _Assessment(
        "config.wlantemplate.assignment",
        "WLAN template assignment has no duplicate-SSID or connectivity conflicts "
        "on affected sites",
        evidence=evidence,
    )


def _relationship_change(
    op: ChangeOp,
    provider: StateProvider,
    org_id: str,
    *,
    resolved: ObjectRelationshipContext | FetchError | None = None,
) -> _Assessment:
    family = op.object_type.removeprefix("org_")
    context = resolved
    if context is None:
        resolver = getattr(provider, "resolve_object_relationships", None)
        if resolver is None:
            return _Assessment(
                f"config.{family}.relationships",
                f"{family} relationships cannot be resolved by this state provider; "
                "the change requires review",
                review=True,
                coverage_complete=False,
                evidence={"relationship_validation": "provider unsupported"},
            )
        context = resolver(OrgScope(org_id), op.object_type, op.object_id)
    if isinstance(context, FetchError):
        return _Assessment(
            f"config.{family}.relationships",
            f"{family} relationships could not be resolved; the change requires review",
            review=True,
            coverage_complete=False,
            evidence={"fetch_failures": [failure.error for failure in context.failures]},
        )
    assert isinstance(context, ObjectRelationshipContext)
    references = [
        {
            "source_type": ref.source_type,
            "source_id": ref.source_id,
            "source_name": ref.source_name,
            "site_id": ref.site_id,
            "path": ref.path,
        }
        for ref in context.references
    ]
    evidence: dict[str, Any] = {
        "references": references,
        "checked_sources": list(context.checked_sources),
    }
    if context.failures:
        evidence["fetch_failures"] = [failure.error for failure in context.failures]

    if context.references:
        if op.object_type == "org_nactags":
            return _Assessment(
                "config.nactag.referenced",
                f"NAC tag is referenced by {len(context.references)} rule path(s); "
                f"the {op.action} requires review",
                review=True,
                coverage_complete=not context.failures,
                evidence=evidence,
            )
        device_kind = str(context.target.get("type") or "")
        kind_text = (
            f" {device_kind}" if op.object_type == "org_deviceprofiles" and device_kind else ""
        )
        return _Assessment(
            (
                "config.referenced_update_impact"
                if op.action == "update"
                else f"config.{family}.referenced"
            ),
            f"{family}{kind_text} is referenced by {len(context.references)} configuration "
            f"path(s); "
            + (
                "deletion would break those references"
                if op.action == "delete"
                else "the update can affect active network behavior and requires review"
            ),
            review=True,
            coverage_complete=False if op.action == "update" else not context.failures,
            evidence={
                **evidence,
                "affected_dependents": sorted(
                    {f"{ref.source_type}:{ref.source_id}" for ref in context.references}
                ),
                "impact_simulation": (
                    "dependent expansion complete; effective dependent recompilation unavailable"
                    if op.action == "update"
                    else "deletion reference integrity"
                ),
                **({"profile_type": device_kind} if device_kind else {}),
            },
            severity=Severity.ERROR if op.action == "delete" else Severity.WARNING,
            affected_entities=tuple(sorted({ref.source_id for ref in context.references})),
        )
    if context.failures:
        return _Assessment(
            f"config.{family}.relationships",
            f"no {family} references were found, but relationship discovery was incomplete; "
            "the change requires review",
            review=True,
            coverage_complete=False,
            evidence=evidence,
        )
    return _Assessment(
        f"config.{family}.unused",
        f"{family} is not referenced by any checked configuration; the {op.action} is safe",
        evidence=evidence,
    )


def _relationship_context(
    op: ChangeOp, provider: StateProvider, org_id: str
) -> ObjectRelationshipContext | FetchError | None:
    resolver = getattr(provider, "resolve_object_relationships", None)
    return None if resolver is None else resolver(OrgScope(org_id), op.object_type, op.object_id)


def _rf_coverage_change(op: ChangeOp, provider: StateProvider, org_id: str) -> _Assessment:
    context = _relationship_context(op, provider, org_id)
    if context is None or isinstance(context, FetchError):
        return _relationship_change(op, provider, org_id, resolved=context)
    proposed = effective_update(context.target, op.payload)
    before = context.target.get("radio_config") or context.target.get("radios") or {}
    after = proposed.get("radio_config") or proposed.get("radios") or {}
    sensitive: list[str] = []
    harmful: list[str] = []
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        for band in sorted(set(before) | set(after)):
            old_value = before.get(band)
            new_value = after.get(band)
            old: Mapping[str, Any] = old_value if isinstance(old_value, Mapping) else {}
            new: Mapping[str, Any] = new_value if isinstance(new_value, Mapping) else {}
            for key in (
                "disabled",
                "bandwidth",
                "channel_width",
                "power",
                "tx_power",
                "min_basic_rate",
            ):
                if old.get(key) == new.get(key):
                    continue
                path = f"{band}.{key}"
                sensitive.append(path)
                if key == "disabled" and new.get(key) is True:
                    harmful.append(path)
                elif key in {"power", "tx_power"}:
                    try:
                        new_number = float(str(new.get(key)))
                        old_number = float(str(old.get(key)))
                        if new_number < old_number:
                            harmful.append(path)
                    except TypeError, ValueError:
                        harmful.append(path)
                elif key == "min_basic_rate":
                    try:
                        new_number = float(str(new.get(key)))
                        old_number = float(str(old.get(key)))
                        if new_number > old_number:
                            harmful.append(path)
                    except TypeError, ValueError:
                        harmful.append(path)
                else:
                    harmful.append(path)
    if not sensitive:
        return _relationship_change(op, provider, org_id, resolved=context)
    evidence = {
        "changed_rf_fields": sensitive,
        "coverage_risk_fields": harmful,
        "references": [
            {
                "source_type": ref.source_type,
                "source_id": ref.source_id,
                "site_id": ref.site_id,
                "path": ref.path,
            }
            for ref in context.references
        ],
    }
    if not context.references:
        return _Assessment(
            "wireless.rf_coverage_regression",
            "RF parameters changed on an unreferenced template; no AP coverage is affected",
            evidence=evidence,
        )
    return _Assessment(
        "wireless.rf_coverage_regression",
        "an assigned RF template changes coverage-sensitive radio parameters; AP placement "
        "and client radio capabilities require review",
        review=True,
        coverage_complete=False,
        evidence=evidence,
        affected_entities=tuple(sorted({ref.source_id for ref in context.references})),
    )


def _policy_rules(value: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    for key in ("policies", "rules", "service_policies"):
        rows = value.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, Mapping)]
    return []


def _rule_match(rule: Mapping[str, Any]) -> tuple[str, str, tuple[str, ...]]:
    def first(*names: str) -> str:
        for name in names:
            value = rule.get(name)
            if value not in (None, "", []):
                return str(value).casefold()
        return "any"

    services = rule.get("service_ids", rule.get("services", ()))
    service_tuple = (
        (services,)
        if isinstance(services, str)
        else tuple(sorted(str(item) for item in services))
        if isinstance(services, list)
        else ()
    )
    return (
        first("src", "source", "src_network", "source_network"),
        first("dst", "destination", "dst_network", "destination_network"),
        service_tuple,
    )


def _policy_hazards(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    hazards: list[dict[str, Any]] = []
    seen: dict[tuple[str, str, tuple[str, ...]], tuple[str, str]] = {}
    broad_rule: str | None = None
    for index, rule in enumerate(rows):
        match = _rule_match(rule)
        action = str(rule.get("action") or rule.get("then") or "").casefold()
        identity = str(rule.get("id") or rule.get("name") or f"{match}:{action}")
        if broad_rule is not None:
            hazards.append({
                "index": index, "kind": "shadowed_by_broad_permit",
                "rule": identity, "shadower": broad_rule,
            })
        is_broad = action in {"allow", "permit", "accept"} and match[:2] == ("any", "any")
        if is_broad:
            hazards.append({"index": index, "kind": "broad_permit", "rule": identity})
            broad_rule = identity
        previous = seen.get(match)
        if previous is not None and previous[0] != action:
            hazards.append({
                "index": index, "kind": "conflicting_action",
                "rule": identity, "conflicts_with": previous[1],
            })
        seen.setdefault(match, (action, identity))
    return hazards


def _hazard_signature(hazard: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(hazard.get("kind") or ""),
        str(hazard.get("rule") or ""),
        str(hazard.get("shadower") or ""),
        str(hazard.get("conflicts_with") or ""),
    )


def _service_policy_semantics(op: ChangeOp, provider: StateProvider, org_id: str) -> _Assessment:
    context = _relationship_context(op, provider, org_id)
    if context is None or isinstance(context, FetchError):
        return _relationship_change(op, provider, org_id, resolved=context)
    proposed = effective_update(context.target, op.payload)
    before, after = _policy_rules(context.target), _policy_rules(proposed)
    baseline_hazards = _policy_hazards(before)
    baseline_signatures = {_hazard_signature(hazard) for hazard in baseline_hazards}
    hazards = [
        hazard for hazard in _policy_hazards(after)
        if _hazard_signature(hazard) not in baseline_signatures
    ]
    before_order = [str(r.get("id") or r.get("name") or i) for i, r in enumerate(before)]
    after_order = [str(r.get("id") or r.get("name") or i) for i, r in enumerate(after)]
    if set(before_order) == set(after_order) and before_order != after_order:
        hazards.append({"kind": "order_changed", "before": before_order, "after": after_order})
    evidence = {
        "semantic_hazards": hazards,
        "baseline_semantic_hazards": baseline_hazards,
        "baseline_rule_order": before_order,
        "proposed_rule_order": after_order,
        "affected_dependents": sorted({ref.source_id for ref in context.references}),
    }
    if hazards:
        unsafe = any(h["kind"] in {"broad_permit", "conflicting_action"} for h in hazards)
        return _Assessment(
            "security.service_policy_semantics",
            "service-policy semantics introduce broad access, conflicting/shadowed rules, "
            "or an effective order change",
            review=True,
            coverage_complete=False,
            evidence=evidence,
            severity=Severity.ERROR if unsafe else Severity.WARNING,
            category=FindingCategory.NETWORK,
            affected_entities=tuple(sorted({ref.source_id for ref in context.references})),
        )
    if context.references:
        return _Assessment(
            "security.service_policy_semantics",
            "referenced service-policy rules changed; named service expansion and effective "
            "gateway compilation require review",
            review=True,
            coverage_complete=False,
            evidence=evidence,
            category=FindingCategory.NETWORK,
            affected_entities=tuple(sorted({ref.source_id for ref in context.references})),
        )
    return _Assessment(
        "security.service_policy_semantics",
        "service policy is unreferenced and introduces no structural semantic hazard",
        evidence=evidence,
        category=FindingCategory.NETWORK,
    )


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
            f"PSK was used during the last {context.window_days} days; "
            + (
                "deleting it would disconnect recently authenticated clients"
                if op.action == "delete"
                else "the change requires review"
            ),
            review=True,
            coverage_complete=not context.failures,
            evidence=evidence,
            severity=Severity.ERROR if op.action == "delete" else Severity.WARNING,
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


def _network_create(
    op: ChangeOp,
    existing: list[Mapping[str, Any]],
) -> _Assessment:
    if op.action != "create":
        return _Assessment(
            "config.org_network.review",
            "only new org gateway networks have a deterministic safety rule; "
            "updates and deletes require review",
            review=True,
        )

    name = op.payload.get("name")
    vlan_raw = op.payload.get("vlan_id")
    subnet_raw = op.payload.get("subnet")
    errors: list[str] = []
    if not isinstance(name, str) or not name.strip():
        errors.append("name must be a non-empty string")
    network_name = name.strip() if isinstance(name, str) else ""
    try:
        vlan_id = int(str(vlan_raw))
        if not 1 <= vlan_id <= 4094:
            errors.append("vlan_id must be between 1 and 4094")
    except TypeError, ValueError:
        errors.append("vlan_id must be an integer between 1 and 4094")

    proposed_subnet: ipaddress.IPv4Network | ipaddress.IPv6Network | None = None
    if subnet_raw not in (None, ""):
        if not isinstance(subnet_raw, str):
            errors.append("subnet must be a CIDR string")
        else:
            try:
                proposed_subnet = ipaddress.ip_network(subnet_raw, strict=False)
            except ValueError:
                errors.append("subnet must be a valid IPv4 or IPv6 CIDR")
    if errors:
        return _Assessment(
            "config.org_network.invalid",
            "; ".join(errors),
            review=True,
            severity=Severity.ERROR,
            category=FindingCategory.OPERATIONAL,
            evidence={"validation_errors": errors},
        )

    duplicate_names = [
        str(row.get("name"))
        for row in existing
        if str(row.get("name") or "").casefold() == network_name.casefold()
    ]
    overlapping: list[str] = []
    if proposed_subnet is not None:
        for row in existing:
            raw = row.get("subnet")
            if not isinstance(raw, str) or not raw:
                continue
            try:
                current = ipaddress.ip_network(raw, strict=False)
            except ValueError:
                continue
            if current.version == proposed_subnet.version and current.overlaps(proposed_subnet):
                overlapping.append(str(current))
    if duplicate_names or overlapping:
        details: list[str] = []
        if duplicate_names:
            details.append(f"network name '{network_name}' already exists")
        if overlapping:
            details.append(
                f"subnet {proposed_subnet} overlaps existing subnet(s) "
                f"{', '.join(sorted(set(overlapping)))}"
            )
        return _Assessment(
            "config.org_network.conflict",
            "; ".join(details),
            review=True,
            severity=Severity.ERROR,
            evidence={
                "name": network_name,
                "subnet": str(proposed_subnet) if proposed_subnet else None,
                "conflicting_subnets": sorted(set(overlapping)),
            },
        )
    return _Assessment(
        "config.org_network.create",
        "new gateway network configuration is valid and its subnet is unique",
        evidence={
            "name": network_name,
            "vlan_id": vlan_id,
            "subnet": str(proposed_subnet) if proposed_subnet else None,
        },
    )


def _network_change(
    op: ChangeOp,
    existing: list[Mapping[str, Any]],
    provider: StateProvider,
    org_id: str,
) -> _Assessment:
    if op.action == "create":
        return _network_create(op, existing)
    resolver = getattr(provider, "resolve_object_relationships", None)
    if resolver is None:
        return _relationship_change(op, provider, org_id)
    context = resolver(OrgScope(org_id), op.object_type, op.object_id)
    if isinstance(context, FetchError):
        return _relationship_change(op, provider, org_id, resolved=context)
    if op.action == "update":
        others = [row for row in existing if str(row.get("id") or "") != op.object_id]
        proposed = effective_update(context.target, op.payload)
        validation = _network_create(replace(op, action="create", payload=proposed), others)
        if validation.review:
            return validation
    return _relationship_change(op, provider, org_id, resolved=context)


def _payload_reference_ids(value: Any) -> set[str]:
    """Collect explicit ``*_id``/``*_ids`` references from vendor-shaped JSON."""
    found: set[str] = set()
    if not isinstance(value, Mapping):
        return found
    for key, child in value.items():
        name = str(key).lower()
        if name.endswith("_id") and isinstance(child, str) and child:
            found.add(child)
        elif name.endswith("_ids") and isinstance(child, list):
            found.update(item for item in child if isinstance(item, str) and item)
        if isinstance(child, Mapping):
            found.update(_payload_reference_ids(child))
        elif isinstance(child, list):
            for item in child:
                found.update(_payload_reference_ids(item))
    return found


def _contains_value(value: Any, candidates: set[str]) -> bool:
    if isinstance(value, str):
        return value in candidates
    if isinstance(value, Mapping):
        return any(_contains_value(child, candidates) for child in value.values())
    if isinstance(value, list):
        return any(_contains_value(child, candidates) for child in value)
    return False


def _reference_repaired(
    ref: Any,
    *,
    target_values: set[str],
    source_ops: Mapping[tuple[str, str], ChangeOp],
) -> bool:
    source = source_ops.get((ref.source_type, ref.source_id))
    if source is None:
        return False
    if source.action == "delete":
        return True
    if source.action != "update":
        return False
    path = ref.path.removeprefix("$.")
    root = path.split(".", 1)[0].split("[", 1)[0]
    if root not in source.payload:
        return False  # omitted root persists under Mist update semantics
    return not _contains_value(source.payload[root], target_values)


def _batch_integrity(
    ops: tuple[ChangeOp, ...], provider: StateProvider, org_id: str
) -> tuple[list[tuple[ChangeOp, _Assessment]], dict[tuple[str, str], _Assessment]]:
    """Evaluate final-plan reference integrity and unique-mutation invariants."""
    findings: list[tuple[ChangeOp, _Assessment]] = []
    overrides: dict[tuple[str, str], _Assessment] = {}
    deleted = {op.object_id: op for op in ops if op.action == "delete"}
    for op in ops:
        if op.action == "delete":
            continue
        dangling_ids = sorted(_payload_reference_ids(op.payload) & set(deleted))
        if dangling_ids:
            findings.append(
                (
                    op,
                    _Assessment(
                        "config.batch_integrity.dangling_reference",
                        f"{op.object_type} {op.object_id} references object(s) deleted by the "
                        "same plan",
                        review=True,
                        severity=Severity.ERROR,
                        evidence={"dangling_target_ids": dangling_ids},
                        affected_entities=tuple(dangling_ids),
                    ),
                )
            )

    resolver = getattr(provider, "resolve_object_relationships", None)
    if resolver is None:
        return findings, overrides
    source_ops = {(op.object_type, op.object_id): op for op in ops}
    for op in ops:
        if op.action != "delete" or op.object_type not in _RELATIONSHIP_TYPES:
            continue
        context = resolver(OrgScope(org_id), op.object_type, op.object_id)
        if isinstance(context, FetchError):
            continue  # normal relationship assessment reports the coverage gap
        target_values = {op.object_id}
        name = context.target.get("name")
        if isinstance(name, str) and name:
            target_values.add(name)
        dangling = [
            ref
            for ref in context.references
            if not _reference_repaired(ref, target_values=target_values, source_ops=source_ops)
        ]
        key = (op.object_type, op.object_id)
        if dangling:
            # NAC tags classify/match rules but deleting a referenced tag does not
            # deterministically mean deny; preserve the established REVIEW posture.
            dangling_severity = (
                Severity.WARNING if op.object_type == "org_nactags" else Severity.ERROR
            )
            overrides[key] = _Assessment(
                "config.batch_integrity.dangling_reference",
                f"deleting {op.object_type} {op.object_id} leaves "
                f"{len(dangling)} unresolved dependent reference(s)",
                review=True,
                severity=dangling_severity,
                coverage_complete=not context.failures,
                evidence={
                    "references": [
                        {
                            "source_type": ref.source_type,
                            "source_id": ref.source_id,
                            "site_id": ref.site_id,
                            "path": ref.path,
                        }
                        for ref in dangling
                    ],
                    "fetch_failures": [failure.error for failure in context.failures],
                },
                affected_entities=tuple(sorted({ref.source_id for ref in dangling})),
            )
        elif context.references:
            overrides[key] = _Assessment(
                "config.batch_integrity.references_resolved",
                f"all {len(context.references)} dependent reference(s) are removed "
                "or deleted by the final plan",
                review=bool(context.failures),
                coverage_complete=not context.failures,
                evidence={
                    "resolved_dependents": sorted(
                        {f"{ref.source_type}:{ref.source_id}" for ref in context.references}
                    ),
                    "fetch_failures": [failure.error for failure in context.failures],
                },
            )
    return findings, overrides


def simulate_configuration_policy(
    plan_data: Mapping[str, Any], *, provider: StateProvider, run: RunContext
) -> Verdict | None:
    """Evaluate explicit policy objects, or return ``None`` for other plans."""
    raw_ops = plan_data.get("ops")
    if isinstance(raw_ops, list) and raw_ops and all(isinstance(op, Mapping) for op in raw_ops):
        raw_types = {str(op.get("object_type") or "") for op in raw_ops}
        if raw_types and raw_types <= set(CONFIG_POLICY_OBJECT_TYPES):
            targets = [
                (str(op.get("object_type") or ""), str(op.get("object_id") or "")) for op in raw_ops
            ]
            duplicates = sorted({target for target in targets if targets.count(target) > 1})
            if duplicates:
                finding = Finding(
                    source=FindingSource.CHECK,
                    category=FindingCategory.OPERATIONAL,
                    code="config.batch_integrity.duplicate_mutation",
                    severity=Severity.ERROR,
                    confidence=_HIGH,
                    message="one configuration object has multiple final mutations",
                    affected_entities=tuple(object_id for _, object_id in duplicates),
                    evidence={
                        "duplicate_targets": [
                            {"object_type": kind, "object_id": object_id}
                            for kind, object_id in duplicates
                        ]
                    },
                )
                result = CheckResult(
                    check_id="config.batch_integrity",
                    status=Status.WARN,
                    findings=(finding,),
                    coverage=Coverage(CoverageState.COMPLETE),
                    confidence=_HIGH,
                    reasoning="duplicate mutations make earlier full replacements dead",
                )
                return assemble(
                    inputs=DecisionInputs(
                        rejections=(
                            Rejection(
                                stage="config.batch_integrity",
                                reasons=(
                                    "one object must have one final mutation; duplicate targets "
                                    "make earlier full replacements dead",
                                ),
                            ),
                        ),
                        l0_fatal=False,
                        baseline_unavailable=False,
                        check_results=(result,),
                    ),
                    ir_diff=_EMPTY_DIFF,
                    trace_ref=run.run_id,
                )

    plan = parse_change_plan(plan_data)
    if isinstance(plan, Rejection) or not plan.ops:
        return None
    if not all(
        op.object_type in CONFIG_POLICY_OBJECT_TYPES
        or (op.object_type in INERT_ORG_CREATE_OBJECT_TYPES and op.action == "create")
        for op in plan.ops
    ):
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

        network_context: OrgNetworksContext | FetchError | None = None
        proposed_networks: list[Mapping[str, Any]] = []
        if any(op.object_type == "org_networks" for op in plan.ops):
            network_context = provider.resolve_org_networks(OrgScope(plan.scope.org_id))
            if isinstance(network_context, OrgNetworksContext):
                proposed_networks = list(network_context.networks)

        adapter_findings: list[Finding] = []
        for op in plan.ops:
            if op.object_type not in INERT_ORG_CREATE_OBJECT_TYPES:
                continue
            l0 = MistAdapter().validate(op, scope_roots=None)
            adapter_findings.extend(l0.findings)
            if l0.fatal:
                return assemble(
                    inputs=DecisionInputs(
                        rejections=(),
                        l0_fatal=True,
                        baseline_unavailable=False,
                        check_results=(),
                        adapter_findings=tuple(adapter_findings),
                    ),
                    ir_diff=_EMPTY_DIFF,
                    trace_ref=run.run_id,
                )

        batch_findings, batch_overrides = _batch_integrity(plan.ops, provider, plan.scope.org_id)
        results: list[CheckResult] = [_result(assessment, op) for op, assessment in batch_findings]
        config_diffs: list[ObjectConfigDiff] = []
        for op in sorted(plan.ops, key=lambda item: item.order):
            assessment = batch_overrides.get((op.object_type, op.object_id))
            if assessment is None:
                assessment = _static_assessment(op)
            if (
                assessment is None
                and op.object_type == "wlantemplate"
                and {"site_ids", "sitegroup_ids"} & set(op.payload)
            ):
                assessment = _wlantemplate_assignment(op, provider, plan.scope.org_id)
            if assessment is None and op.object_type == "org_sitegroups":
                assessment = _sitegroup_delete(op, provider, plan.scope.org_id)
            if assessment is None and op.object_type in {"org_psks", "site_psks"}:
                assessment = _psk_change(op, provider, plan.scope.org_id, plan.scope.site_id)
            if assessment is None and op.object_type == "org_rftemplates" and op.action == "update":
                assessment = _rf_coverage_change(op, provider, plan.scope.org_id)
            if (
                assessment is None
                and op.object_type == "org_servicepolicies"
                and op.action == "update"
            ):
                assessment = _service_policy_semantics(op, provider, plan.scope.org_id)
            if assessment is None and op.object_type in _RELATIONSHIP_TYPES:
                assessment = _relationship_change(op, provider, plan.scope.org_id)
            if assessment is None and op.object_type == "org_networks":
                current_network = next(
                    (row for row in proposed_networks if str(row.get("id") or "") == op.object_id),
                    None,
                )
                if isinstance(network_context, FetchError):
                    assessment = _Assessment(
                        "config.org_network.unverified",
                        "existing gateway networks could not be fetched; subnet uniqueness "
                        "cannot be verified",
                        review=True,
                        coverage_complete=False,
                        evidence={"fetch_failures": [f.error for f in network_context.failures]},
                    )
                else:
                    assessment = _network_change(op, proposed_networks, provider, plan.scope.org_id)
                    if op.action == "create":
                        created_network = {**dict(op.payload), "id": op.object_id}
                        config_diffs.append(
                            object_config_diff(
                                object_type=op.object_type,
                                object_id=op.object_id,
                                name=created_network.get("name"),
                                action=op.action,
                                before=None,
                                after=created_network,
                            )
                        )
                        proposed_networks.append(created_network)
                    elif op.action == "update":
                        if current_network is not None:
                            updated_network = effective_update(current_network, op.payload)
                            config_diffs.append(
                                object_config_diff(
                                    object_type=op.object_type,
                                    object_id=op.object_id,
                                    name=(
                                        updated_network.get("name") or current_network.get("name")
                                    ),
                                    action=op.action,
                                    before=current_network,
                                    after=updated_network,
                                )
                            )
                        proposed_networks = [
                            (
                                effective_update(row, op.payload)
                                if str(row.get("id") or "") == op.object_id
                                else row
                            )
                            for row in proposed_networks
                        ]
                    elif op.action == "delete":
                        if current_network is not None:
                            config_diffs.append(
                                object_config_diff(
                                    object_type=op.object_type,
                                    object_id=op.object_id,
                                    name=current_network.get("name"),
                                    action=op.action,
                                    before=current_network,
                                    after=None,
                                )
                            )
                        proposed_networks = [
                            row
                            for row in proposed_networks
                            if str(row.get("id") or "") != op.object_id
                        ]
            assert assessment is not None
            results.append(_result(assessment, op))

        verdict = assemble(
            inputs=DecisionInputs(
                rejections=(),
                l0_fatal=False,
                baseline_unavailable=False,
                check_results=tuple(results),
                adapter_findings=tuple(adapter_findings),
            ),
            ir_diff=_EMPTY_DIFF,
            trace_ref=run.run_id,
        )
        return replace(verdict, config_diffs=tuple(config_diffs))
