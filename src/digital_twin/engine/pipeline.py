"""The 10-stage simulation pipeline — ORCHESTRATION ONLY (spec diagram).

 1 ScopeResolver.pre   envelope + object gate (pre-fetch)        -> UNKNOWN
 3 StateProvider       fetch raw                                  -> UNKNOWN on total failure
 2+4 per op, vs the ROLLING pre-op state:
     effective object   Mist root-level update semantics (present roots replace,
                        omitted roots persist, "-attr" markers delete; conflicts
                        rejected)                                  -> UNKNOWN
     Adapter.validate   L0 on the EFFECTIVE object (fatal -> stop) -> UNKNOWN
     field gate         changed leaves vs allowlist (incl. role)   -> UNKNOWN
 5 Adapter.ingest      baseline (effective + IR)                  -> UNKNOWN if not ok
 6 Adapter.apply       per-op update on the rolling state          -> UNKNOWN on bad target
 7 Adapter.ingest      proposed                                   -> UNKNOWN if not ok
 8 derived gate        full effective config, site + per device   -> coverage gap
 9 diff + checks       registry (gating order, isolation)
10 verdict             DecisionInputs -> decision + assembly

L0 runs inside the loop (not pre-fetch as originally specced) because Mist
update semantics are a root-level merge: `required`/conditional validation is
only meaningful against the EFFECTIVE object, which needs the fetched state.
Every failure is a VALUE produced by the owning module; this file only maps
them into DecisionInputs and stops at the right stage. No business logic.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from digital_twin.adapters.mist.adapter import IngestOutcome, MistAdapter
from digital_twin.adapters.mist.apply import get_object
from digital_twin.adapters.mist.apply.objects import effective_update, update_conflicts
from digital_twin.adapters.mist.ingest.dynamic_usage import unresolved_dynamic_findings
from digital_twin.adapters.mist.ingest.switch import (
    invalid_bridge_priority_findings,
    unresolved_dhcp_range_findings,
)
from digital_twin.analysis.context import AnalysisContext
from digital_twin.analysis.delta_cause import delta_index
from digital_twin.checks.base import (
    CheckContext,
    CheckResult,
    Coverage,
    CoverageState,
    Status,
)
from digital_twin.checks.registry import CheckRegistry
from digital_twin.checks.wired import ALL_WIRED_CHECKS
from digital_twin.contracts import (
    Cause,
    ChangePlan,
    Finding,
    FindingCategory,
    FindingSource,
    ObjectRef,
    Rejection,
    Severity,
)
from digital_twin.engine.config_policy import simulate_configuration_policy
from digital_twin.engine.effective_override import effective_override_result
from digital_twin.engine.name_change import (
    NameChangeAssessment,
    RenameRisks,
    assess_name_only_change,
    device_rename_risks,
)
from digital_twin.engine.org_overlay import OrgOverlay, affected_sites, apply_overlays
from digital_twin.engine.org_template import apply_template
from digital_twin.engine.run_context import RunContext
from digital_twin.ir import Confidence, ConfidenceLevel, IRDiff, NacRule, diff_ir
from digital_twin.providers.base import (
    FetchError,
    FetchFailure,
    NacRuleUsageContext,
    OrgScope,
    OrgTemplateContext,
    OrgWlanContext,
    OrgWlanTemplateContext,
    RawSiteState,
    SiteScope,
    StateMeta,
    StateProvider,
    WlanUsageContext,
)
from digital_twin.providers.fetch_limits import with_fetch_budget
from digital_twin.scope.allowlist import GATEWAY_EFFECTIVE_ALLOWLIST, ORG_OBJECT_TYPES
from digital_twin.scope.derived_gate import check_derived_gaps
from digital_twin.scope.device_profile_gate import device_profile_gaps
from digital_twin.scope.envelope import parse_change_plan
from digital_twin.scope.field_gate import changed_paths, screen_op, screen_op_split
from digital_twin.scope.object_gate import check_objects
from digital_twin.scope.wlan_policy import (
    classify_wlan_delta,
    usage_gated_paths_for_update,
)
from digital_twin.verdict.decision import Decision, DecisionInputs
from digital_twin.verdict.org_verdict import OrgChange, OrgVerdict, decide_org
from digital_twin.verdict.state_meta import StateMetaView, build_state_meta
from digital_twin.verdict.verdict import Verdict, assemble
from digital_twin.viz.mermaid import safe_build_diagrams
from digital_twin.viz.visual_map import safe_build_visual_map

_EMPTY_DIFF = IRDiff((), (), ())

# Gateway roots screened by the derived gate when the edit is NOT a
# gatewaytemplate op (i.e. for sitetemplate/site_setting edits the switch
# derived gate already owns `networks` via site_effective, so projecting it
# here would false-UNKNOWN those edits). For gatewaytemplate edits the FULL
# effective is screened (full=True) to catch networks changes owned by the
# gateway namespace that never appear in site_effective.
GATEWAY_SCREENED_ROOTS: tuple[str, ...] = (
    "port_config", "ip_configs", "dhcpd_config", "vars",
    # Newly recognized switch roots also survive the generic gateway fold.
    # Until gateway semantics are modeled, changes here must remain UNKNOWN.
    "radius_config", "mist_nac", "extra_routes", "extra_routes6",
)


@dataclass(frozen=True)
class WlanAssociations:
    """Current wireless association evidence for one WLAN (one site or merged).

    Session history is not proof of absence: a client that is still connected
    may have no session record yet. ``connected`` clients are proven use;
    ``unidentified`` clients (no WLAN id, no comparable SSID) and telemetry
    ``gaps`` are uncertainty, never evidence that nobody uses the WLAN.
    ``shared_ssid`` clients carry only an SSID that another enabled WLAN at the
    same site also serves, so they may or may not be on this WLAN; a surviving
    SSID proves neither admission nor forwarding, so they are uncertainty too.
    """

    connected: tuple[Mapping[str, Any], ...] = ()
    unidentified: tuple[Mapping[str, Any], ...] = ()
    gaps: tuple[str, ...] = ()
    shared_ssid: tuple[Mapping[str, Any], ...] = ()

    def on_band(self, band: str | None) -> WlanAssociations:
        return WlanAssociations(
            tuple(c for c in self.connected if _on_band(c, band)),
            tuple(c for c in self.unidentified if _on_band(c, band)),
            self.gaps,
            tuple(c for c in self.shared_ssid if _on_band(c, band)),
        )

    @property
    def unverified_reasons(self) -> tuple[str, ...]:
        reasons = list(self.gaps)
        if self.unidentified:
            reasons.append(
                f"{len(self.unidentified)} connected client(s) could not be associated "
                "with a WLAN"
            )
        if self.shared_ssid:
            reasons.append(
                f"{len(self.shared_ssid)} connected client(s) use an SSID that another "
                "WLAN at the same site also serves"
            )
        return tuple(reasons)

    @staticmethod
    def merge(parts: Iterable[WlanAssociations]) -> WlanAssociations:
        rows = tuple(parts)
        return WlanAssociations(
            tuple(c for p in rows for c in p.connected),
            tuple(c for p in rows for c in p.unidentified),
            tuple(g for p in rows for g in p.gaps),
            tuple(c for p in rows for c in p.shared_ssid),
        )


def _site_wlan_associations(
    raw: RawSiteState, wlan_id: str, wlan: Mapping[str, Any]
) -> WlanAssociations:
    """Classify the site's current wireless clients against one WLAN.

    A client matches by WLAN id; without one, its SSID identifies this WLAN
    only when no other enabled WLAN at the site serves the same SSID. A failed
    or absent client fetch is a gap, not an empty population.
    """
    site = raw.scope.site_id
    if "wireless_clients" not in raw.meta.fetched or any(
        failure.object == "wireless_clients" for failure in raw.meta.failures
    ):
        return WlanAssociations(gaps=(f"site {site}: current wireless clients unavailable",))
    raw_ssid = wlan.get("ssid")
    ssid = raw_ssid if isinstance(raw_ssid, str) and raw_ssid and "{{" not in raw_ssid else None
    shared = ssid is not None and any(
        isinstance(other, Mapping)
        and other.get("id") != wlan_id
        and other.get("enabled") is not False
        and other.get("ssid") == ssid
        for other in raw.wlans
    )
    connected: list[Mapping[str, Any]] = []
    unidentified: list[Mapping[str, Any]] = []
    shared_ssid: list[Mapping[str, Any]] = []
    for client in raw.wireless_clients:
        if not isinstance(client, Mapping):
            unidentified.append({})
            continue
        client_wlan = client.get("wlan_id")
        client_ssid = client.get("ssid")
        if client_wlan:
            if client_wlan == wlan_id:
                connected.append(client)
        elif ssid is None or not client_ssid:
            # Neither identity can rule this WLAN out.
            unidentified.append(client)
        elif client_ssid == ssid:
            (shared_ssid if shared else connected).append(client)
    return WlanAssociations(
        tuple(connected), tuple(unidentified), shared_ssid=tuple(shared_ssid)
    )


def _org_wlan_associations(
    raw_map: Mapping[str, RawSiteState | FetchError],
    wlan_id: str,
    rows_by_site: Mapping[str, Mapping[str, Any]],
) -> WlanAssociations:
    parts = []
    for sid, row in sorted(rows_by_site.items()):
        fetched = raw_map.get(sid)
        if isinstance(fetched, RawSiteState):
            parts.append(_site_wlan_associations(fetched, wlan_id, row))
        else:
            parts.append(WlanAssociations(gaps=(f"site {sid}: site state unavailable",)))
    return WlanAssociations.merge(parts)


def _on_band(client: Mapping[str, Any], band: str | None) -> bool:
    # Mist reports the client radio as "24" | "5" | "6"; an unknown band counts.
    observed = client.get("band")
    return band is None or observed is None or str(observed) == band.split("-", 1)[0]


def _resolve_wlan_usage(
    provider: StateProvider,
    scope: OrgScope | SiteScope,
    wlan_id: str,
    band: str | None = None,
) -> WlanUsageContext | FetchError:
    resolver = getattr(provider, "resolve_wlan_usage", None)
    if resolver is None:
        return FetchError(
            scope=scope,
            failures=(FetchFailure(
                object="wlan_sessions",
                error="provider does not expose historical WLAN usage",
            ),),
            acquired_at=datetime.now(UTC),
            host="provider",
        )
    try:
        context: WlanUsageContext | FetchError = (
            resolver(scope, wlan_id, window_days=7)
            if band is None
            else resolver(scope, wlan_id, window_days=7, band=band)
        )
    except TypeError as exc:
        # Older/custom providers may implement only whole-WLAN usage.  They
        # remain usable for generic changes, while an unsupported per-band
        # query fails closed to REVIEW rather than crashing the simulation.
        context = FetchError(
            scope=scope,
            failures=(FetchFailure(object="wlan_sessions", error=str(exc)),),
            acquired_at=datetime.now(UTC),
            host="provider",
        )
    return context


@dataclass(frozen=True)
class _UsageAssessment:
    used: bool  # proven: connected right now, or sessions inside the window
    unverified: tuple[str, ...]  # why absence of use is not proven
    reason: str
    evidence: dict[str, Any]


def _assess_wlan_usage(
    context: WlanUsageContext | FetchError,
    associations: WlanAssociations,
    *,
    band: str | None = None,
) -> _UsageAssessment:
    """Combine session history with current associations. Only complete, clean
    evidence on BOTH sides can establish that nobody uses the WLAN."""
    qualifier = f" on band {band}" if band is not None else ""
    observed = associations.on_band(band)
    unverified = list(observed.unverified_reasons)
    window_days = 7
    evidence: dict[str, Any] = {
        "window_days": window_days,
        "connected_clients": len(observed.connected),
        "unidentified_clients": len(observed.unidentified),
        "shared_ssid_clients": len(observed.shared_ssid),
    }
    if observed.gaps:
        evidence["association_gaps"] = list(observed.gaps)
    if band is not None:
        evidence["band"] = band
    active_site_ids: tuple[str, ...] = ()
    if isinstance(context, FetchError):
        unverified.insert(0, f"WLAN usage{qualifier} during the last 7 days could not be verified")
    else:
        window_days = context.window_days
        active_site_ids = context.active_site_ids
        evidence.update({
            "window_days": context.window_days,
            "checked_site_ids": list(context.checked_site_ids),
            "active_site_ids": list(context.active_site_ids),
        })
        if context.failures:
            unverified.insert(0, f"WLAN usage{qualifier} was only partially verified")
    if context.failures:
        evidence["fetch_failures"] = [failure.error for failure in context.failures]
    if observed.connected:
        reason = f"{len(observed.connected)} client(s) are connected to the WLAN{qualifier}"
    elif active_site_ids:
        reason = f"WLAN had client sessions{qualifier} during the last {window_days} days"
    elif unverified:
        reason = "; ".join(unverified)
    else:
        reason = (
            f"WLAN had no client sessions{qualifier} during the last {window_days} days "
            "and no connected client can be on it"
        )
    return _UsageAssessment(
        used=bool(observed.connected) or bool(active_site_ids),
        unverified=tuple(unverified),
        reason=reason,
        evidence=evidence,
    )


def _usage_gate_result(
    check_id: str,
    wlan_id: str,
    assessment: _UsageAssessment,
    *,
    used_code: str,
    unverified_code: str,
    consequence: str,
    extra_evidence: Mapping[str, Any] | None = None,
) -> CheckResult:
    """REVIEW on proven use or unverifiable use; PASS only on complete evidence."""
    coverage_state = (
        CoverageState.PARTIAL if assessment.unverified else CoverageState.COMPLETE
    )
    notes = (assessment.reason, *(r for r in assessment.unverified if r != assessment.reason))
    if not assessment.used and not assessment.unverified:
        return CheckResult(
            check_id=check_id,
            status=Status.PASS,
            findings=(),
            coverage=Coverage(CoverageState.COMPLETE, (assessment.reason,)),
            confidence=Confidence(level=ConfidenceLevel.HIGH),
            reasoning=assessment.reason,
        )
    finding = Finding(
        source=FindingSource.CHECK,
        category=FindingCategory.NETWORK,
        code=used_code if assessment.used else unverified_code,
        severity=Severity.WARNING,
        confidence=Confidence(level=ConfidenceLevel.HIGH),
        message=f"{assessment.reason}; {consequence}",
        affected_entities=(wlan_id,),
        evidence={**(extra_evidence or {}), **assessment.evidence},
    )
    return CheckResult(
        check_id=check_id,
        status=Status.WARN,
        findings=(finding,),
        coverage=Coverage(coverage_state, notes),
        confidence=Confidence(level=ConfidenceLevel.HIGH),
        reasoning=assessment.reason,
    )


def _wlan_delete_usage_result(
    provider: StateProvider,
    scope: OrgScope | SiteScope,
    wlan_id: str,
    *,
    associations: WlanAssociations,
) -> CheckResult:
    assessment = _assess_wlan_usage(
        _resolve_wlan_usage(provider, scope, wlan_id), associations
    )
    return _usage_gate_result(
        "wireless.wlan.recent_usage",
        wlan_id,
        assessment,
        used_code="wireless.wlan.recent_usage",
        unverified_code="wireless.wlan.recent_usage.unverified",
        consequence="deletion requires review",
    )


def _wlan_auth_transition_result(
    provider: StateProvider,
    scope: OrgScope | SiteScope,
    wlan_id: str,
    current: Mapping[str, Any],
    proposed: Mapping[str, Any],
    *,
    associations: WlanAssociations,
) -> CheckResult | None:
    """Assess the security downgrade from a secured WLAN to an open WLAN."""
    current_auth = current.get("auth")
    proposed_auth = proposed.get("auth")
    before = current_auth.get("type") if isinstance(current_auth, Mapping) else None
    after = proposed_auth.get("type") if isinstance(proposed_auth, Mapping) else None
    if before in (None, "open") or after != "open":
        return None

    assessment = _assess_wlan_usage(
        _resolve_wlan_usage(provider, scope, wlan_id), associations
    )
    evidence = {
        "before_auth_type": str(before),
        "after_auth_type": "open",
        **assessment.evidence,
    }
    if assessment.used:
        # Proven use makes the downgrade UNSAFE regardless of other gaps.
        reason = f"WLAN authentication changes from {before} to open: {assessment.reason}"
        code, severity, status = (
            "wireless.wlan.auth_transition.recent_usage", Severity.ERROR, Status.FAIL
        )
    elif assessment.unverified:
        reason = (
            f"WLAN authentication changes from {before} to open, and client usage "
            f"could not be verified: {assessment.reason}"
        )
        code, severity, status = (
            "wireless.wlan.auth_transition.unverified", Severity.WARNING, Status.WARN
        )
    else:
        reason = f"WLAN authentication changes from {before} to open; {assessment.reason}"
        code, severity, status = (
            "wireless.wlan.auth_transition", Severity.WARNING, Status.WARN
        )
    finding = Finding(
        source=FindingSource.CHECK,
        category=FindingCategory.NETWORK,
        code=code,
        severity=severity,
        confidence=Confidence(level=ConfidenceLevel.HIGH),
        message=(
            f"{reason}; the security downgrade requires review"
            if status is Status.WARN else reason
        ),
        affected_entities=(wlan_id,),
        evidence=evidence,
    )
    return CheckResult(
        check_id="wireless.wlan.auth_transition",
        status=status,
        findings=(finding,),
        coverage=Coverage(
            CoverageState.PARTIAL if assessment.unverified else CoverageState.COMPLETE,
            (reason, *assessment.unverified),
        ),
        confidence=Confidence(level=ConfidenceLevel.HIGH),
        reasoning=reason,
    )


def _wlan_changed_usage_result(
    provider: StateProvider,
    scope: OrgScope | SiteScope,
    wlan_id: str,
    changed_fields: tuple[str, ...],
    *,
    associations: WlanAssociations,
    band: str | None = None,
) -> CheckResult:
    """Gate a client-affecting WLAN update on seven-day session history and on
    the clients connected right now."""
    assessment = _assess_wlan_usage(
        _resolve_wlan_usage(provider, scope, wlan_id, band), associations, band=band
    )
    return _usage_gate_result(
        "wireless.wlan.change_usage",
        wlan_id,
        assessment,
        used_code="wireless.wlan.change.recent_usage",
        unverified_code="wireless.wlan.change.unverified",
        consequence="client-affecting changes require review",
        extra_evidence={"changed_fields": list(changed_fields)},
    )


_FIVE_GHZ_BANDS = frozenset({"5", "5-dedicated", "5-selectable"})
_SIX_GHZ_BANDS = frozenset({"6", "6-dedicated", "6-selectable"})


def _configured_bands(wlan: Mapping[str, Any]) -> frozenset[str] | None:
    bands = wlan.get("bands")
    if isinstance(bands, list) and all(isinstance(value, str) for value in bands):
        return frozenset(bands)
    return None


def _six_ghz_security_ready(wlan: Mapping[str, Any]) -> bool:
    auth = wlan.get("auth")
    if not isinstance(auth, Mapping):
        return False
    auth_type = auth.get("type")
    if auth_type == "open":
        return auth.get("owe") in {"enabled", "required"}
    if auth_type == "eap192":
        return True
    pairwise = auth.get("pairwise")
    return (
        auth_type in {"eap", "psk", "psk-tkip", "psk-wpa2-tkip"}
        and isinstance(pairwise, list)
        and "wpa3" in pairwise
    )


def _wlan_band_change_results(
    provider: StateProvider,
    scope: OrgScope | SiteScope,
    wlan_id: str,
    current: Mapping[str, Any],
    proposed: Mapping[str, Any],
    *,
    associations: WlanAssociations,
) -> tuple[CheckResult, ...]:
    """Evaluate exact Mist band removals and 6-GHz security transitions."""
    delta = classify_wlan_delta(current, proposed)
    if not delta.bands:
        return ()

    before = _configured_bands(current)
    after = _configured_bands(proposed)
    # The deprecated free-form `band` field, or a missing/invalid `bands`
    # baseline, cannot be mapped honestly to exact RF scopes.  Fall back to the
    # whole-WLAN seven-day gate instead of fabricating a safe band delta.
    if "band" in delta.bands or before is None or after is None:
        return (_wlan_changed_usage_result(
            provider, scope, wlan_id, delta.bands, associations=associations
        ),)

    results: list[CheckResult] = []
    added = after - before
    removed = before - after

    # Enabling 6 GHz is safe only when the WLAN was already using a compatible
    # WPA3/OWE security posture.  If enabling it also requires a security
    # transition, that combined change is reviewable even with no recent use.
    if added & _SIX_GHZ_BANDS and not _six_ghz_security_ready(current):
        reason = (
            "enabling 6 GHz requires introducing WPA3 or OWE/transition security "
            "to a WLAN that was not already 6-GHz compatible"
        )
        finding = Finding(
            source=FindingSource.CHECK,
            category=FindingCategory.NETWORK,
            code="wireless.wlan.band_change.security_transition",
            severity=Severity.WARNING,
            confidence=Confidence(level=ConfidenceLevel.HIGH),
            message=f"{reason}; review client compatibility",
            affected_entities=(wlan_id,),
            evidence={
                "added_bands": sorted(added & _SIX_GHZ_BANDS),
                "before_bands": sorted(before),
                "after_bands": sorted(after),
            },
        )
        results.append(CheckResult(
            check_id="wireless.wlan.band_change",
            status=Status.WARN,
            findings=(finding,),
            coverage=Coverage(CoverageState.COMPLETE, (reason,)),
            confidence=Confidence(level=ConfidenceLevel.HIGH),
            reasoning=reason,
        ))

    relevant_removals = set(removed & {"24"})
    # A 5-GHz removal is usage-gated only when 2.4 GHz will not remain as the
    # fallback.  Variant changes are real remove+add operations by policy.
    if "24" not in after:
        relevant_removals.update(removed & _FIVE_GHZ_BANDS)
    # All 6-GHz removals are explicitly safe.
    for band in sorted(relevant_removals):
        results.append(_wlan_changed_usage_result(
            provider, scope, wlan_id, ("bands",), band=band,
            associations=associations,
        ))

    if not results:
        reason = "band change only adds service or removes 6 GHz without client risk"
        results.append(CheckResult(
            check_id="wireless.wlan.band_change",
            status=Status.PASS,
            findings=(),
            coverage=Coverage(CoverageState.COMPLETE, (reason,)),
            confidence=Confidence(level=ConfidenceLevel.HIGH),
            reasoning=reason,
        ))
    return tuple(results)


def _org_wlan_usage_findings(
    provider: StateProvider,
    scope: OrgScope,
    wlan_id: str,
    rows_by_site: Mapping[str, Mapping[str, Any]],
    before: Mapping[str, Any],
    after: Mapping[str, Any] | None,
    raw_map: Mapping[str, RawSiteState | FetchError],
) -> list[Finding]:
    """Org WLAN gates: org-wide session history plus the current associations
    at every site where the WLAN is deployed."""
    associations = _org_wlan_associations(raw_map, wlan_id, rows_by_site)
    if after is None:
        return list(_wlan_delete_usage_result(
            provider, scope, wlan_id, associations=associations
        ).findings)
    findings: list[Finding] = []
    usage_paths = usage_gated_paths_for_update(before, after)
    if usage_paths:
        findings.extend(_wlan_changed_usage_result(
            provider, scope, wlan_id, usage_paths, associations=associations
        ).findings)
    for band_result in _wlan_band_change_results(
        provider, scope, wlan_id, before, after, associations=associations
    ):
        findings.extend(band_result.findings)
    auth_transition = _wlan_auth_transition_result(
        provider, scope, wlan_id, before, after, associations=associations
    )
    if auth_transition is not None:
        findings.extend(auth_transition.findings)
    return findings


def _nacrule_access_impact_result(
    provider: StateProvider,
    scope: OrgScope,
    baseline: NacRule,
    proposed: NacRule | None,
    changed_fields: tuple[str, ...],
) -> CheckResult:
    nacrule_id = baseline.id
    resolver = getattr(provider, "resolve_nacrule_usage", None)
    if resolver is None:
        context: NacRuleUsageContext | FetchError = FetchError(
            scope=scope,
            failures=(FetchFailure(
                object="nacrule_usage",
                error="provider does not expose historical NAC rule usage",
            ),),
            acquired_at=datetime.now(UTC),
            host="provider",
        )
    else:
        context = resolver(scope, nacrule_id, window_days=7)
    if isinstance(context, FetchError):
        reason = "NAC rule access impact during the last 7 days could not be verified"
        finding = Finding(
            source=FindingSource.CHECK,
            category=FindingCategory.NETWORK,
            code="nac.rule.access_impact.unverified",
            severity=Severity.WARNING,
            confidence=Confidence(level=ConfidenceLevel.HIGH),
            message=f"{reason}; the rule change requires review",
            affected_entities=(nacrule_id,),
            evidence={
                "window_days": 7,
                "changed_fields": list(changed_fields),
                "fetch_failures": [f.error for f in context.failures],
            },
            subject=ObjectRef("nacrule", nacrule_id, baseline.name),
            caused_by=(Cause(
                ref=ObjectRef("nacrule", nacrule_id, baseline.name),
                fields=changed_fields,
            ),),
        )
        return CheckResult(
            check_id="nac.rule.access_impact",
            status=Status.WARN,
            findings=(finding,),
            coverage=Coverage(CoverageState.PARTIAL, (reason,)),
            confidence=Confidence(level=ConfidenceLevel.HIGH),
            reasoning=reason,
        )
    assert isinstance(context, NacRuleUsageContext)
    if context.active_site_ids:
        access_loss = (
            baseline.action == "allow"
            and (
                proposed is None
                or not proposed.enabled
                or proposed.action == "block"
            )
        )
        reason = (
            f"NAC rule matched clients during the last {context.window_days} days; "
            + (
                "the change removes their proven allow decision"
                if access_loss
                else "the changed match/order/action can alter their access"
            )
        )
        finding = Finding(
            source=FindingSource.CHECK,
            category=FindingCategory.NETWORK,
            code=(
                "nac.rule.access_impact.allow_removed"
                if access_loss else "nac.rule.access_impact.recent_usage"
            ),
            severity=Severity.ERROR if access_loss else Severity.WARNING,
            confidence=Confidence(level=ConfidenceLevel.HIGH),
            message=reason,
            affected_entities=(nacrule_id,),
            evidence={
                "window_days": context.window_days,
                "checked_site_ids": list(context.checked_site_ids),
                "active_site_ids": list(context.active_site_ids),
                "changed_fields": list(changed_fields),
                "baseline_action": baseline.action,
                "proposed_action": proposed.action if proposed else None,
                "proposed_enabled": proposed.enabled if proposed else False,
            },
            subject=ObjectRef("nacrule", nacrule_id, baseline.name),
            caused_by=(Cause(
                ref=ObjectRef("nacrule", nacrule_id, baseline.name),
                fields=changed_fields,
            ),),
        )
        return CheckResult(
            check_id="nac.rule.access_impact",
            status=Status.FAIL if access_loss else Status.WARN,
            findings=(finding,),
            coverage=Coverage(CoverageState.COMPLETE, (reason,)),
            confidence=Confidence(level=ConfidenceLevel.HIGH),
            reasoning=reason,
        )
    reason = f"NAC rule had no client matches during the last {context.window_days} days"
    return CheckResult(
        check_id="nac.rule.access_impact",
        status=Status.PASS,
        findings=(),
        coverage=Coverage(CoverageState.COMPLETE, (reason,)),
        confidence=Confidence(level=ConfidenceLevel.HIGH),
        reasoning=reason,
    )


def _gw_screen_view(eff: dict[str, Any], *, full: bool) -> dict[str, Any]:
    # SOURCE-AWARE. For a gatewaytemplate edit, screen the FULL effective:
    # gatewaytemplate's OWN networks (or a vars edit rippling into networks) is NOT
    # in site_effective, so the switch derived gate never sees it -> dropping it
    # here would resolve a gatewaytemplate networks change SAFE (false-SAFE). For a
    # sitetemplate/site_setting edit, project to the gateway-consumed roots: a
    # networks change there IS in site_effective and the switch gate owns it (the
    # gateway namespace is org_networks), so screening it here would false-UNKNOWN.
    return eff if full else {k: eff[k] for k in GATEWAY_SCREENED_ROOTS if k in eff}


# Gateway-NAMESPACE roots the sitetemplate fold leaks into site_effective
# (merge_site_effective folds the FULL sitetemplate, and fold_layers preserves
# unknown roots). They are NOT switch/site roots — switch L3 is `other_ip_configs`
# and switch ports are device-level `port_config` (screened in the device_effective
# gate, not the site one) — so the switch/site derived gate must screen them OUT, or
# a gateway-only sitetemplate edit (e.g. ip_configs.*.ip) false-UNKNOWNs against the
# switch EFFECTIVE_ALLOWLIST. They ARE screened by the gateway derived gate on
# gateway_effective (and are inert when the site has no gateway). dhcpd_config/vars
# are deliberately excluded: per spec they are genuinely shared site roots the
# switch/site gate must keep screening.
_GATEWAY_ONLY_SITE_ROOTS: tuple[str, ...] = ("port_config", "ip_configs")


def _site_screen_view(eff: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in eff.items() if k not in _GATEWAY_ONLY_SITE_ROOTS}


def _stamp(findings: tuple[Finding, ...], subject: ObjectRef) -> tuple[Finding, ...]:
    """Attach the headline object to every L0 finding so the verdict says WHICH
    object (and the existing evidence path says which attribute)."""
    return tuple(replace(f, subject=subject) for f in findings)


_HIGH = Confidence(level=ConfidenceLevel.HIGH)


def _coverage_gap_finding(
    rejection: Rejection,
    *,
    artifact: str,
    subject: ObjectRef,
    affected_entities: tuple[str, ...] = (),
    paths: tuple[str, ...] = (),
    dhcp_row: str | None = None,
) -> Finding:
    evidence: dict[str, Any] = {
        "stage": rejection.stage,
        "artifact": artifact,
        "reasons": list(rejection.reasons),
    }
    if paths:
        evidence["paths"] = list(paths)
    if dhcp_row is not None:
        evidence["dhcp_row"] = dhcp_row
    return Finding(
        source=FindingSource.ADAPTER,
        category=FindingCategory.OPERATIONAL,
        code="coverage.gap",
        severity=Severity.WARNING,
        confidence=_HIGH,
        message=f"Coverage gap in {artifact}: {'; '.join(rejection.reasons)}",
        subject=subject,
        affected_entities=affected_entities,
        evidence=evidence,
    )


def _record_coverage_gap(
    coverage_gaps: list[Rejection],
    coverage_gap_findings: list[Finding],
    rejection: Rejection,
    *,
    artifact: str,
    subject: ObjectRef,
    affected_entities: tuple[str, ...] = (),
    paths: tuple[str, ...] = (),
    dhcp_row: str | None = None,
) -> None:
    coverage_gaps.append(rejection)
    coverage_gap_findings.append(
        _coverage_gap_finding(
            rejection,
            artifact=artifact,
            subject=subject,
            affected_entities=affected_entities,
            paths=paths,
            dhcp_row=dhcp_row,
        )
    )


def _changed_roots(payload: Mapping[str, Any]) -> frozenset[str]:
    """Top-level roots the op actually SETS — the only roots Mist processes (and
    thus re-validates) on a root-level-merge PUT. Dash-delete markers ('-attr')
    remove a root from the effective object, so they can never produce a
    violation and are excluded. This is the default L0 scope: it keeps L0 from
    flagging stale committed-OAS types on persisted roots the change never
    touched (which Mist already accepted)."""
    return frozenset(k for k in payload if not k.startswith("-"))


def _unknown(
    rejection: Rejection | None,
    *,
    adapter_findings: tuple[Finding, ...],
    run: RunContext,
    state_meta: StateMetaView | None = None,
    l0_fatal: bool = False,
    baseline_unavailable: bool = False,
    config_diffs: tuple[ObjectConfigDiff, ...] = (),
) -> Verdict:
    return replace(
        assemble(
            inputs=DecisionInputs(
                rejections=(rejection,) if rejection else (),
                l0_fatal=l0_fatal,
                baseline_unavailable=baseline_unavailable,
                check_results=(),
                adapter_findings=adapter_findings,
            ),
            ir_diff=_EMPTY_DIFF,
            state_meta=state_meta,
            trace_ref=run.run_id,
        ),
        config_diffs=config_diffs,
    )


def _rename_findings(op_type: str, device_id: str, risks: RenameRisks) -> tuple[Finding, ...]:
    """WARNING findings (-> REVIEW, never UNSAFE) for one unproven device rename."""
    subject = ObjectRef(op_type, device_id)
    out: list[Finding] = []
    for code, reasons, lead in (
        ("config.name_change.matcher", risks.matchers,
         "renaming the device can change a name-matched configuration rule"),
        ("config.name_change.unverified", risks.unverified,
         "the twin cannot prove the rename leaves name-matched rules unchanged"),
    ):
        if reasons:
            out.append(Finding(
                source=FindingSource.CHECK,
                category=FindingCategory.NETWORK,
                code=code,
                severity=Severity.WARNING,
                confidence=_HIGH,
                message=f"{lead}: {'; '.join(reasons)}",
                affected_entities=(device_id,),
                subject=subject,
                evidence={"reasons": list(reasons)},
                caused_by=(Cause(ref=subject, fields=("name",)),),
            ))
    return tuple(out)


def renamed_device_findings(
    baseline_raw: RawSiteState, proposed_raw: RawSiteState
) -> tuple[Finding, ...]:
    """The device-rename proof for full-pipeline runs (a rename mixed with other
    fields, or a composite batch's original -> composed pass). The compiler
    re-evaluates switch_matching for port_config only; the matched rule's other
    roots, gateway_matching and neighbor dynamic profiles stay blind, so every
    renamed device is held to the same proof as a pure rename, against both the
    baseline and the proposed configuration."""
    before = {str(d.get("id")): d for d in baseline_raw.devices}
    findings: list[Finding] = []
    for device in proposed_raw.devices:
        device_id = str(device.get("id"))
        old = before.get(device_id)
        new_name = str(device.get("name") or "")
        if old is None or str(old.get("name") or "") == new_name:
            continue
        renamed = {**old, "type": device.get("type", old.get("type"))}
        risks = [device_rename_risks(raw, renamed, new_name)
                 for raw in (baseline_raw, proposed_raw)]
        merged = RenameRisks(
            tuple(dict.fromkeys(r for risk in risks for r in risk.matchers)),
            tuple(dict.fromkeys(r for risk in risks for r in risk.unverified)),
        )
        findings.extend(_rename_findings("device", device_id, merged))
    return tuple(findings)


def _device_rename_verdict(
    plan: ChangePlan,
    assessment: NameChangeAssessment,
    *,
    provider: StateProvider,
    run: RunContext,
) -> Verdict:
    """SAFE only when, for every renamed device, the fetched site configuration
    proves no name-based matcher changes outcome; any blind spot is REVIEW."""
    findings: list[Finding] = []
    state_meta: StateMetaView | None = None
    site_id = plan.scope.site_id
    raw: RawSiteState | FetchError | None = None
    if site_id is not None:
        raw = provider.fetch_site(SiteScope(org_id=plan.scope.org_id, site_id=site_id))
        meta = raw.meta if isinstance(raw, RawSiteState) else StateMeta(
            acquired_at=raw.acquired_at, host=raw.host, fetched=(), failures=raw.failures
        )
        state_meta = build_state_meta(meta, now=datetime.now(UTC))

    names: dict[str, dict[str, Any]] = (
        {str(d.get("id")): dict(d) for d in raw.devices} if isinstance(raw, RawSiteState) else {}
    )
    for op in sorted(assessment.device_ops, key=lambda o: o.order):
        new_name = str(op.payload["name"])
        if raw is None:
            risks = RenameRisks(unverified=(
                "the plan has no site_id, so the device's site templates cannot be fetched",
            ))
        elif isinstance(raw, FetchError):
            risks = RenameRisks(unverified=tuple(
                f"site state could not be fetched ({f.object}: {f.error})" for f in raw.failures
            ) or ("site state could not be fetched",))
        elif (device := names.get(op.object_id)) is None:
            failed = [f"{f.object}: {f.error}" for f in raw.meta.failures]
            detail = f" (fetch failures: {'; '.join(failed)})" if failed else ""
            risks = RenameRisks(unverified=(
                f"device {op.object_id} is not in the fetched site inventory{detail}",
            ))
        else:
            risks = device_rename_risks(raw, device, new_name)
            device["name"] = new_name  # later ops in the plan rename from here
        findings.extend(_rename_findings(op.object_type, op.object_id, risks))

    if findings:
        complete = not any(f.code == "config.name_change.unverified" for f in findings)
        reasoning = (
            "device rename is not proven free of name-matched configuration changes; "
            "review required"
        )
        result = CheckResult(
            check_id="config.name_change",
            status=Status.WARN,
            findings=tuple(findings),
            coverage=Coverage(
                CoverageState.COMPLETE if complete else CoverageState.PARTIAL, (reasoning,)
            ),
            confidence=_HIGH,
            reasoning=reasoning,
        )
        return assemble(
            inputs=DecisionInputs(
                rejections=(), l0_fatal=False, baseline_unavailable=False,
                check_results=(result,),
            ),
            ir_diff=_EMPTY_DIFF,
            state_meta=state_meta,
            trace_ref=run.run_id,
        )

    reason = (
        f"name-only configuration update is safe for: {', '.join(assessment.object_types)} "
        "(no name-matched rule or neighbor dynamic port profile changes for the "
        f"renamed devices: {', '.join(op.object_id for op in assessment.device_ops)})"
    )
    result = CheckResult(
        check_id="config.name_change",
        status=Status.PASS,
        findings=(),
        coverage=Coverage(
            CoverageState.COMPLETE,
            ("fetched site configuration proves no name-based matcher changes outcome",),
        ),
        confidence=_HIGH,
        reasoning=reason,
    )
    verdict = assemble(
        inputs=DecisionInputs(
            rejections=(), l0_fatal=False, baseline_unavailable=False, check_results=(result,),
        ),
        ir_diff=_EMPTY_DIFF,
        state_meta=state_meta,
        trace_ref=run.run_id,
    )
    return replace(verdict, decision_reasons=(reason,))


def simulate_name_change(
    plan_data: Mapping[str, Any],
    *,
    run: RunContext | None = None,
    provider: StateProvider | None = None,
) -> Verdict | None:
    """Return a policy verdict for a pure name update, or ``None`` otherwise.

    Without a ``provider`` this runs before provider selection and state fetch,
    reasoning only about the request shape: every op must update exactly one
    non-empty top-level ``name`` value.  All other payloads continue through the
    normal default-deny simulation pipeline.  A plan that renames a DEVICE is
    never granted pre-fetch: it returns ``None`` until a ``provider`` is given,
    then the fetched site configuration must prove no name-matched rule changes.
    """
    run = run or RunContext()
    trace = run.trace
    assert trace is not None
    with trace.stage("name_change_rule"):
        plan = parse_change_plan(plan_data)
        if isinstance(plan, Rejection):
            return None
        assessment = assess_name_only_change(plan)
        if assessment is None:
            return None
        if assessment.device_ops:
            if provider is None:
                return None
            return _device_rename_verdict(plan, assessment, provider=provider, run=run)
        if not assessment.safe:
            return _unknown(
                Rejection(stage="name_change_rule", reasons=(assessment.reason,)),
                adapter_findings=(),
                run=run,
            )

        result = CheckResult(
            check_id="config.name_change",
            status=Status.PASS,
            findings=(),
            coverage=Coverage(
                CoverageState.COMPLETE,
                ("request changes only the top-level configuration object name",),
            ),
            confidence=_HIGH,
            reasoning=assessment.reason,
        )
        verdict = assemble(
            inputs=DecisionInputs(
                rejections=(),
                l0_fatal=False,
                baseline_unavailable=False,
                check_results=(result,),
            ),
            ir_diff=_EMPTY_DIFF,
            trace_ref=run.run_id,
        )
        return replace(verdict, decision_reasons=(assessment.reason,))


def _simulate_site_state(
    baseline_raw: RawSiteState,
    proposed_raw: RawSiteState,
    *,
    adapter: MistAdapter,
    registry: CheckRegistry,
    run: RunContext,
    state_meta: StateMetaView | None,
    adapter_findings: tuple[Finding, ...] = (),
    gateway_screen_full: bool = False,
    profile_proposed: IngestOutcome | None = None,
    extra_coverage_gaps: tuple[Rejection, ...] = (),
    extra_coverage_findings: tuple[Finding, ...] = (),
    extra_check_results: tuple[CheckResult, ...] = (),
) -> Verdict:
    """Stages 5-10 for ONE site: ingest baseline + proposed, dynamic gate,
    derived gate, diff + checks, verdict. Both `simulate` (single-site) and
    `simulate_org_template` (per assigned site) call this with pre-built
    baseline/proposed raw states — no fetch, no apply here."""
    trace = run.trace
    assert trace is not None  # RunContext.__post_init__ guarantees it

    with trace.stage("ingest.baseline"):
        # A compile/ingest CRASH (e.g. an unresolvable {{var}} on a gateway) is an
        # UNKNOWN, never a hard crash — and never a false-SAFE. Critical for the org
        # fan-out, which simulates many assigned sites: one site whose baseline does
        # not compile must not take down the whole org run (it becomes that site's
        # per-site UNKNOWN). Mirrors the `ir is None` path below.
        try:
            baseline = adapter.ingest(baseline_raw)
        except Exception as e:  # noqa: BLE001 — any ingest failure is UNKNOWN by the cardinal rule
            return _unknown(
                Rejection(stage="ingest", reasons=(f"baseline ingest crashed: {e}",)),
                adapter_findings=adapter_findings, run=run,
                state_meta=state_meta, baseline_unavailable=True,
            )
        if baseline.ir is None:
            return _unknown(
                None, adapter_findings=adapter_findings, run=run,
                state_meta=state_meta, baseline_unavailable=True,
            )
    with trace.stage("ingest.proposed"):
        try:
            proposed = adapter.ingest(proposed_raw)
        except Exception as e:  # noqa: BLE001
            return _unknown(
                Rejection(stage="ingest", reasons=(f"proposed ingest crashed: {e}",)),
                adapter_findings=adapter_findings, run=run, state_meta=state_meta,
            )
        if proposed.ir is None:
            return _unknown(
                Rejection(
                    stage="ingest",
                    reasons=tuple(
                        f"proposed-state ingest failed: {f.ingester}: {f.error}"
                        for f in proposed.report.failures
                    ),
                ),
                adapter_findings=adapter_findings, run=run, state_meta=state_meta,
            )
    with trace.stage("dynamic_gate"):
        adapter_findings += renamed_device_findings(baseline_raw, proposed_raw)
        adapter_findings += unresolved_dynamic_findings(
            baseline.device_effective, proposed.device_effective, proposed_raw.port_stats
        )
        adapter_findings += tuple(
            invalid_bridge_priority_findings(baseline.device_effective, proposed.device_effective)
        )
        adapter_findings += tuple(
            unresolved_dhcp_range_findings(baseline.site_effective, proposed.site_effective)
        )
    coverage_gaps: list[Rejection] = list(extra_coverage_gaps)
    coverage_gap_findings: list[Finding] = list(extra_coverage_findings)
    with trace.stage("derived_gate"):
        site_gaps = check_derived_gaps(
            _site_screen_view(baseline.site_effective), _site_screen_view(proposed.site_effective)
        )
        for site_gap in site_gaps:
            _record_coverage_gap(
                coverage_gaps,
                coverage_gap_findings,
                site_gap.rejection,
                artifact="site",
                subject=ObjectRef("site", baseline_raw.scope.site_id),
                paths=site_gap.paths,
                dhcp_row=site_gap.dhcp_row,
            )
        for did in sorted(set(baseline.device_effective) | set(proposed.device_effective)):
            device_gaps = check_derived_gaps(
                baseline.device_effective.get(did, {}),
                proposed.device_effective.get(did, {}),
                artifact=f"device {did}",
            )
            for device_gap in device_gaps:
                _record_coverage_gap(
                    coverage_gaps,
                    coverage_gap_findings,
                    device_gap.rejection,
                    artifact=f"device {did}",
                    subject=ObjectRef("device", did),
                    affected_entities=(did,),
                    paths=device_gap.paths,
                    dhcp_row=device_gap.dhcp_row,
                )
        for did in sorted(set(baseline.gateway_effective) | set(proposed.gateway_effective)):
            gateway_gaps = check_derived_gaps(
                _gw_screen_view(baseline.gateway_effective.get(did, {}), full=gateway_screen_full),
                _gw_screen_view(proposed.gateway_effective.get(did, {}), full=gateway_screen_full),
                allowlist=GATEWAY_EFFECTIVE_ALLOWLIST,
                artifact=f"gateway {did}",
            )
            for gateway_gap in gateway_gaps:
                _record_coverage_gap(
                    coverage_gaps,
                    coverage_gap_findings,
                    gateway_gap.rejection,
                    artifact=f"gateway {did}",
                    subject=ObjectRef("device", did),
                    affected_entities=(did,),
                    paths=gateway_gap.paths,
                    dhcp_row=gateway_gap.dhcp_row,
                )
    with trace.stage("checks"):
        diff = diff_ir(baseline.ir, proposed.ir)
        results = registry.run_all(
            CheckContext(
                baseline=AnalysisContext(baseline.ir),
                proposed=AnalysisContext(proposed.ir),
                diff=diff,
                delta_index=delta_index(diff),
            )
        ) + extra_check_results
        override_result = effective_override_result(
            site_id=baseline_raw.scope.site_id,
            baseline_lower=baseline.site_effective,
            proposed_lower=proposed.site_effective,
            baseline_devices=baseline.device_effective,
            proposed_devices=proposed.device_effective,
        )
        if override_result is not None:
            results += (override_result,)
    profile_outcome = profile_proposed if profile_proposed is not None else proposed
    dp_gaps = device_profile_gaps(
        proposed_raw.devices,
        {**baseline.device_effective, **baseline.gateway_effective},
        {**profile_outcome.device_effective, **profile_outcome.gateway_effective},
    )
    for dp_gap in dp_gaps:
        _record_coverage_gap(
            coverage_gaps,
            coverage_gap_findings,
            dp_gap.rejection,
            artifact=f"device {dp_gap.device_id}",
            subject=ObjectRef("device", dp_gap.device_id),
            affected_entities=(dp_gap.device_id,),
            paths=dp_gap.paths,
        )
    with trace.stage("verdict"):
        verdict = assemble(
            inputs=DecisionInputs(
                rejections=(),
                l0_fatal=False,
                baseline_unavailable=False,
                check_results=results,
                adapter_findings=(*adapter_findings, *coverage_gap_findings),
                coverage_gaps=tuple(coverage_gaps),
            ),
            ir_diff=diff,
            state_meta=state_meta,
            trace_ref=run.run_id,
        )
        return replace(
            verdict,
            diagrams=safe_build_diagrams(baseline.ir, proposed.ir, verdict.findings),
            visual_map=safe_build_visual_map(baseline.ir, proposed.ir, verdict.findings),
        )


@with_fetch_budget
def simulate(
    plan_data: Mapping[str, Any],
    *,
    provider: StateProvider,
    adapter: MistAdapter | None = None,
    registry: CheckRegistry | None = None,
    run: RunContext | None = None,
    l0_full_object: bool = False,
) -> Verdict:
    run = run or RunContext()
    policy_verdict = simulate_configuration_policy(plan_data, provider=provider, run=run)
    if policy_verdict is not None:
        return policy_verdict
    name_change_verdict = simulate_name_change(plan_data, run=run, provider=provider)
    if name_change_verdict is not None:
        return name_change_verdict
    trace = run.trace
    assert trace is not None  # RunContext.__post_init__ guarantees it
    adapter = adapter or MistAdapter()
    registry = registry or CheckRegistry(ALL_WIRED_CHECKS)
    adapter_findings: tuple[Finding, ...] = ()

    # 1 — pre-fetch gates
    with trace.stage("scope.pre"):
        plan = parse_change_plan(plan_data)
        if isinstance(plan, Rejection):
            return _unknown(plan, adapter_findings=adapter_findings, run=run)
        rejection = check_objects(plan)
        if rejection:
            return _unknown(rejection, adapter_findings=adapter_findings, run=run)

    # WLAN usage gates run per op after the fetch: they need the site's
    # current wireless associations as well as the session history.
    wlan_usage_results: list[CheckResult] = []

    # 2 — (L0 moved into the per-op loop: Mist update semantics are root-level
    # merge, so `required`/conditional validation is only meaningful against
    # the EFFECTIVE object, which needs the fetched current state)

    # 3 — fetch
    with trace.stage("fetch"):
        if plan.scope.site_id is None:  # an org fan-out plan reached single-site simulate
            return _unknown(
                Rejection(
                    stage="scope.pre",
                    reasons=(
                        "org fan-out plan has no site_id"
                        " — call simulate_org_plan, not simulate",
                    ),
                ),
                adapter_findings=adapter_findings, run=run,
            )
        raw = provider.fetch_site(SiteScope(org_id=plan.scope.org_id, site_id=plan.scope.site_id))
        if not isinstance(raw, RawSiteState):
            # the FetchError still carries host/acquired_at/failures — agents
            # must see WHAT failed even when no baseline is usable
            return _unknown(
                None,
                adapter_findings=adapter_findings,
                run=run,
                baseline_unavailable=True,
                state_meta=build_state_meta(
                    StateMeta(
                        acquired_at=raw.acquired_at,
                        host=raw.host,
                        fetched=(),
                        failures=raw.failures,
                    ),
                    now=datetime.now(UTC),
                ),
            )
    state_meta = build_state_meta(raw.meta, now=datetime.now(UTC))

    # 2+4+6 — per op against the ROLLING pre-op state: compute the EFFECTIVE
    # object (Mist root-level update semantics: present roots replace, omitted
    # roots persist, "-attr" deletes), L0-validate it, field-gate it, apply.
    proposed_raw = raw
    site_diffs: list[ObjectConfigDiff] = []
    # Field-gate COVERAGE gaps (out-of-scope leaves / no_local_overwrite ripple):
    # unlike a HARD rejection these do not short-circuit — the simulation runs on
    # the in-scope projection and the decision floors at UNKNOWN (never SAFE),
    # while a modeled UNSAFE still wins.
    field_gaps: list[Rejection] = []
    field_gap_findings: list[Finding] = []
    with trace.stage("l0+scope.post+apply", note=f"{len(plan.ops)} op(s)"):
        for op in sorted(plan.ops, key=lambda o: o.order):
            current = get_object(proposed_raw, op.object_type, op.object_id)
            if op.action == "create":
                if current is not None:
                    return _unknown(
                        Rejection(stage="apply", reasons=(
                            f"ops[order={op.order}]: {op.object_type} with id "
                            f"{op.object_id!r} already exists",)),
                        adapter_findings=adapter_findings, run=run,
                        state_meta=state_meta, config_diffs=tuple(site_diffs),
                    )
                created = {**dict(op.payload), "id": op.object_id, "for_site": True}
                site_diffs.append(object_config_diff(
                    object_type=op.object_type, object_id=op.object_id,
                    name=created.get("name") or created.get("ssid"),
                    action=op.action, before=None, after=created))
                result = adapter.validate(
                    replace(op, payload=created),
                    scope_roots=None if l0_full_object else _changed_roots(op.payload),
                    unknown_scope_roots=(
                        None if l0_full_object else frozenset(op.payload)
                    ),
                )
                subject = ObjectRef(
                    op.object_type, op.object_id,
                    name=created.get("name") or created.get("ssid"),
                )
                adapter_findings += _stamp(result.findings, subject)
                if result.fatal:
                    return _unknown(
                        None, adapter_findings=adapter_findings, run=run,
                        l0_fatal=True, state_meta=state_meta,
                        config_diffs=tuple(site_diffs),
                    )
                # A create has no persisted baseline row. Compare against an
                # identity-only, explicitly site-owned stub: this validates all
                # requested leaves while keeping the inherited-WLAN guard honest.
                hard, gaps = screen_op_split(
                    op.object_type,
                    {"id": op.object_id, "for_site": True},
                    created,
                )
                if hard:
                    return _unknown(
                        hard, adapter_findings=adapter_findings, run=run,
                        state_meta=state_meta, config_diffs=tuple(site_diffs),
                    )
                for gap in gaps:
                    _record_coverage_gap(
                        field_gaps, field_gap_findings, gap,
                        artifact=op.object_type, subject=subject,
                    )
                applied = adapter.apply(proposed_raw, (op,))
                if isinstance(applied, Rejection):
                    return _unknown(
                        applied, adapter_findings=adapter_findings, run=run,
                        state_meta=state_meta, config_diffs=tuple(site_diffs),
                    )
                proposed_raw = applied
                continue
            if current is None:
                return _unknown(
                    Rejection(stage="apply", reasons=(
                        f"ops[order={op.order}]: no {op.object_type} with id "
                        f"{op.object_id!r} in fetched state",)),
                    adapter_findings=adapter_findings, run=run,
                    state_meta=state_meta, config_diffs=tuple(site_diffs),
                )
            if op.action == "delete":
                if op.object_type == "wlan":
                    wlan_usage_results.append(_wlan_delete_usage_result(
                        provider,
                        SiteScope(plan.scope.org_id, plan.scope.site_id),
                        op.object_id,
                        associations=_site_wlan_associations(raw, op.object_id, current or {}),
                    ))
                site_diffs.append(object_config_diff(
                    object_type=op.object_type, object_id=op.object_id,
                    name=current.get("name"), action=op.action,
                    before=current, after=None))
                # delete diffs current vs current -> no changed leaves, so only the
                # HARD object-level screen can fire here.
                hard, _ = screen_op_split(op.object_type, current, current)
                if hard:
                    return _unknown(
                        hard, adapter_findings=adapter_findings, run=run,
                        state_meta=state_meta, config_diffs=tuple(site_diffs),
                    )
                applied = adapter.apply(proposed_raw, (op,))
                if isinstance(applied, Rejection):
                    return _unknown(
                        applied, adapter_findings=adapter_findings, run=run,
                        state_meta=state_meta, config_diffs=tuple(site_diffs),
                    )
                proposed_raw = applied
                continue
            conflicts = update_conflicts(op.payload)
            if conflicts:
                return _unknown(
                    Rejection(stage="apply", reasons=tuple(
                        f"ops[order={op.order}]: conflicting set AND '-{c}' delete "
                        "marker for the same attribute" for c in conflicts)),
                    adapter_findings=adapter_findings, run=run,
                    state_meta=state_meta, config_diffs=tuple(site_diffs),
                )
            effective = effective_update(current, op.payload)
            if op.object_type == "wlan":
                associations = _site_wlan_associations(raw, op.object_id, current or {})
                usage_paths = usage_gated_paths_for_update(current, effective)
                if usage_paths:
                    wlan_usage_results.append(_wlan_changed_usage_result(
                        provider,
                        SiteScope(plan.scope.org_id, plan.scope.site_id),
                        op.object_id,
                        usage_paths,
                        associations=associations,
                    ))
                wlan_usage_results.extend(_wlan_band_change_results(
                    provider,
                    SiteScope(plan.scope.org_id, plan.scope.site_id),
                    op.object_id,
                    current,
                    effective,
                    associations=associations,
                ))
                auth_transition = _wlan_auth_transition_result(
                    provider,
                    SiteScope(plan.scope.org_id, plan.scope.site_id),
                    op.object_id,
                    current,
                    effective,
                    associations=associations,
                )
                if auth_transition is not None:
                    wlan_usage_results.append(auth_transition)
            # Build the before→after NOW (pure structural data, independent of
            # validation) so it is available to every downstream early exit.
            site_diffs.append(object_config_diff(
                object_type=op.object_type, object_id=op.object_id,
                name=current.get("name"), action=op.action,
                before=current, after=effective))
            # The unknown-attribute walker validates the CHANGE, not the whole persisted
            # object: scope it to roots with actual value deltas. Full-object PUTs echo
            # every persisted root, which the closed OAS does not fully document; auditing
            # them all would false-flag pre-existing fields. l0_full_object audits all.
            unknown_roots = frozenset(
                p.split(".", 1)[0] for p in changed_paths(current, effective)
            )
            result = adapter.validate(
                replace(op, payload=effective),
                scope_roots=None if l0_full_object else _changed_roots(op.payload),
                unknown_scope_roots=None if l0_full_object else unknown_roots,
            )
            subject = ObjectRef(op.object_type, op.object_id, name=current.get("name"))
            adapter_findings += _stamp(result.findings, subject)
            if result.fatal:
                return _unknown(
                    None, adapter_findings=adapter_findings, run=run,
                    l0_fatal=True, state_meta=state_meta,
                    config_diffs=tuple(site_diffs),
                )
            hard, gaps = screen_op_split(op.object_type, current, effective)
            if hard:
                return _unknown(
                    hard, adapter_findings=adapter_findings, run=run,
                    state_meta=state_meta, config_diffs=tuple(site_diffs),
                )
            for gap in gaps:
                _record_coverage_gap(
                    field_gaps, field_gap_findings, gap,
                    artifact=op.object_type, subject=subject,
                )
            applied = adapter.apply(proposed_raw, (op,))  # apply owns the semantics
            if isinstance(applied, Rejection):
                return _unknown(
                    applied, adapter_findings=adapter_findings, run=run,
                    state_meta=state_meta, config_diffs=tuple(site_diffs),
                )
            proposed_raw = applied

    # Build the below-profile proposed: apply ONLY the non-device ops against the
    # baseline (raw), so device-level changes (above the profile) are excluded.
    # This lets the gate diff baseline vs below-profile: if the only changes are
    # device ops, the diff is empty -> gate passes (pure-device plan is safe).
    non_device_ops = tuple(
        op for op in plan.ops if op.object_type != "device"
    )
    if len(non_device_ops) == len(plan.ops):
        # No device ops -> below-profile == proposed_raw; skip the extra ingest.
        profile_proposed = None
    else:
        below_raw = adapter.apply(raw, non_device_ops)
        # This ingest runs BEFORE _simulate_site_state's crash guard, so it needs
        # its own: a compile/ingest CRASH (e.g. an unresolvable gateway {{var}} in
        # the baseline) is UNKNOWN by the cardinal rule, never a hard crash. Both
        # inputs are baseline-derived (raw / baseline+non-device ops), so the
        # baseline is what failed -> baseline_unavailable, mirroring the guard below.
        try:
            if isinstance(below_raw, Rejection):
                # If the non-device apply fails (e.g. missing target), fall back to
                # baseline so the gate sees no below-profile change (conservative:
                # the apply failure was already caught for the full op set above).
                profile_proposed = adapter.ingest(raw)
            else:
                profile_proposed = adapter.ingest(below_raw)
        except Exception as e:  # noqa: BLE001 — any ingest crash is UNKNOWN
            return _unknown(
                Rejection(stage="ingest", reasons=(f"baseline ingest crashed: {e}",)),
                adapter_findings=adapter_findings, run=run,
                state_meta=state_meta, baseline_unavailable=True,
                config_diffs=tuple(site_diffs),
            )

    verdict = _simulate_site_state(
        raw, proposed_raw,
        adapter=adapter, registry=registry, run=run,
        state_meta=state_meta, adapter_findings=adapter_findings,
        profile_proposed=profile_proposed,
        extra_coverage_gaps=tuple(field_gaps),
        extra_coverage_findings=tuple(field_gap_findings),
        extra_check_results=tuple(wlan_usage_results),
    )
    return replace(verdict, config_diffs=tuple(site_diffs))


@with_fetch_budget
def simulate_org_plan(
    plan_data: Mapping[str, Any],
    *,
    provider: StateProvider,
    adapter: MistAdapter | None = None,
    registry: CheckRegistry | None = None,
    run: RunContext | None = None,
    l0_full_object: bool = False,
) -> OrgVerdict:
    run = run or RunContext()
    adapter = adapter or MistAdapter()
    registry = registry or CheckRegistry(ALL_WIRED_CHECKS)

    def org_unknown(
        rejections: tuple[Rejection, ...], *, template_findings: tuple[Finding, ...] = (),
        changes: tuple[OrgChange, ...] = (), config_diffs: tuple[ObjectConfigDiff, ...] = (),
    ) -> OrgVerdict:
        return OrgVerdict(
            decision=Decision.UNKNOWN,
            decision_reasons=tuple(f"[{r.stage}] {x}" for r in rejections for x in r.reasons),
            changes=tuple(changes), per_site={}, driving_sites=(), site_failures={},
            template_findings=tuple(template_findings), org_rejections=tuple(rejections),
            config_diffs=tuple(config_diffs),
        )

    plan = parse_change_plan(plan_data)
    if isinstance(plan, Rejection):
        return org_unknown((plan,))  # changes=() — no parsed ops
    is_org = (
        bool(plan.ops)
        and all(op.object_type in ORG_OBJECT_TYPES for op in plan.ops)
        and not plan.scope.site_id
    )
    # P2b: name EVERY op the plan touches UP FRONT (org-shaped plans), BEFORE
    # check_objects, so an object_gate UNKNOWN (non-empty delete payload /
    # unsupported action) AND every later short-circuit still names all attempted
    # objects. Names hydrate as ops resolve.
    changes = [
        OrgChange(ref=ObjectRef(op.object_type, op.object_id, name=None), action=op.action)
        for op in plan.ops
    ] if is_org else []
    rejection = check_objects(plan)
    if rejection:
        return org_unknown((rejection,), changes=tuple(changes))
    if not is_org:
        return org_unknown((Rejection(
            stage="scope.pre",
            reasons=("site-scoped plan: call simulate, not simulate_org_plan",),
        ),))

    org_scope = OrgScope(org_id=plan.scope.org_id)
    overlays: list[OrgOverlay] = []
    template_findings: list[Finding] = []
    org_diffs: list[ObjectConfigDiff] = []
    prefetched_all_sites: dict[str, RawSiteState | FetchError] | None = None
    # (wlan_id, derived rows by site, before, after-or-None-for-delete): evaluated
    # once the affected sites are fetched, so org gates see current associations.
    pending_wlan_usage: list[
        tuple[str, Mapping[str, Mapping[str, Any]], Mapping[str, Any], Mapping[str, Any] | None]
    ] = []
    for i, op in enumerate(plan.ops):
        if op.object_type == "wlan":
            if op.action == "create":
                # An org WLAN can materialize at every site. Fetch that complete
                # scope before constructing the overlay so duplicate-SSID and AP
                # uplink VLAN checks run independently at every affected site.
                if prefetched_all_sites is None:
                    prefetched_all_sites = provider.fetch_sites(org_scope, site_ids=None)
                if not prefetched_all_sites:
                    return org_unknown((Rejection(
                        stage="fetch",
                        reasons=(
                            "org WLAN create could not discover any organization sites; "
                            "deployment scope cannot be verified",
                        ),
                    ),), changes=tuple(changes), config_diffs=tuple(org_diffs))

                name = op.payload.get("name") or op.payload.get("ssid")
                created = {
                    **dict(op.payload),
                    "id": op.object_id,
                    # The derived row is org-owned at each site.
                    "for_site": False,
                }
                ref = ObjectRef(op.object_type, op.object_id, name=name)
                changes[i] = OrgChange(ref=ref, action=op.action)
                org_diffs.append(object_config_diff(
                    object_type=op.object_type, object_id=op.object_id,
                    name=name, action=op.action, before=None, after=created))
                l0 = adapter.validate(
                    op,
                    scope_roots=None if l0_full_object else _changed_roots(op.payload),
                )
                if l0.fatal:
                    return org_unknown((Rejection(
                        stage="l0",
                        reasons=(
                            f"structurally-fatal L0 on proposed {op.object_type} "
                            f"{op.object_id}",
                        ),
                    ),), template_findings=tuple(template_findings),
                        changes=tuple(changes), config_diffs=tuple(org_diffs))
                template_findings.extend(_stamp(l0.findings, ref))
                fg = screen_op(
                    op.object_type,
                    {"id": op.object_id, "for_site": False},
                    created,
                    enforce_wlan_site_ownership=False,
                )
                if fg:
                    return org_unknown((fg,), template_findings=tuple(template_findings),
                                       changes=tuple(changes), config_diffs=tuple(org_diffs))
                site_ids = tuple(prefetched_all_sites)
                overlays.append(OrgOverlay(
                    object_type=op.object_type, object_id=op.object_id, name=name,
                    action=op.action, assigned_site_ids=frozenset(site_ids),
                    baseline={}, proposed=created,
                    wlan_baseline_by_site={sid: None for sid in site_ids},
                    wlan_proposed_by_site={sid: created for sid in site_ids},
                ))
                continue

            resolved_wlan = provider.resolve_org_wlan(org_scope, op.object_id)
            if not isinstance(resolved_wlan, OrgWlanContext):
                return org_unknown((Rejection(stage="fetch", reasons=tuple(
                    f"org-wlan lookup failed: {f.object}: {f.error}"
                    for f in resolved_wlan.failures
                ) or ("org-wlan lookup failed",)),),
                    template_findings=tuple(template_findings), changes=tuple(changes),
                    config_diffs=tuple(org_diffs))
            snapshot = dict(resolved_wlan.wlan)
            name = snapshot.get("name") or snapshot.get("ssid")
            ref = ObjectRef(op.object_type, op.object_id, name=name)
            changes[i] = OrgChange(ref=ref, action=op.action)
            baseline_by_site = {
                sid: dict(row) for sid, row in resolved_wlan.derived_rows_by_site.items()
            }
            proposed_by_site: dict[str, Mapping[str, Any] | None]
            if op.action == "delete":
                pending_wlan_usage.append((op.object_id, baseline_by_site, snapshot, None))
                proposed_org_wlan: Mapping[str, Any] | None = None
                proposed_by_site = {sid: None for sid in baseline_by_site}
                org_diffs.append(object_config_diff(
                    object_type=op.object_type, object_id=op.object_id,
                    name=name, action=op.action, before=snapshot, after=None))
            else:
                proposed_wlan = effective_update(snapshot, op.payload)
                pending_wlan_usage.append(
                    (op.object_id, baseline_by_site, snapshot, proposed_wlan)
                )
                org_diffs.append(object_config_diff(
                    object_type=op.object_type, object_id=op.object_id,
                    name=name, action=op.action, before=snapshot, after=proposed_wlan))
                l0 = adapter.validate(replace(op, payload=proposed_wlan),
                    scope_roots=None if l0_full_object else _changed_roots(op.payload))
                if l0.fatal:
                    return org_unknown((Rejection(stage="l0",
                        reasons=(f"structurally-fatal L0 on proposed {op.object_type} "
                                 f"{op.object_id}",)),),
                        template_findings=tuple(template_findings), changes=tuple(changes),
                        config_diffs=tuple(org_diffs))
                template_findings.extend(_stamp(l0.findings, ref))
                fg = screen_op(
                    op.object_type,
                    snapshot,
                    proposed_wlan,
                    enforce_wlan_site_ownership=False,
                )
                if fg:
                    return org_unknown((fg,), template_findings=tuple(template_findings),
                                       changes=tuple(changes), config_diffs=tuple(org_diffs))
                proposed_org_wlan = proposed_wlan
                proposed_by_site = {
                    sid: effective_update(row, op.payload) for sid, row in baseline_by_site.items()
                }
            overlays.append(OrgOverlay(
                object_type=op.object_type, object_id=op.object_id, name=name,
                action=op.action, assigned_site_ids=frozenset(baseline_by_site),
                baseline=snapshot, proposed=proposed_org_wlan,
                wlan_baseline_by_site=baseline_by_site,
                wlan_proposed_by_site=proposed_by_site,
            ))
            continue

        if op.object_type == "wlantemplate":
            if op.action != "delete":
                return org_unknown((Rejection(
                    stage="scope.pre",
                    reasons=("wlantemplate supports delete only in SP3",),
                ),),
                    template_findings=tuple(template_findings), changes=tuple(changes),
                    config_diffs=tuple(org_diffs))
            resolved_wlan_template = provider.resolve_org_wlan_template(org_scope, op.object_id)
            if not isinstance(resolved_wlan_template, OrgWlanTemplateContext):
                return org_unknown((Rejection(stage="fetch", reasons=tuple(
                    f"org-wlantemplate lookup failed: {f.object}: {f.error}"
                    for f in resolved_wlan_template.failures
                ) or ("org-wlantemplate lookup failed",)),),
                    template_findings=tuple(template_findings), changes=tuple(changes),
                    config_diffs=tuple(org_diffs))
            snapshot = dict(resolved_wlan_template.template)
            name = snapshot.get("name")
            ref = ObjectRef(op.object_type, op.object_id, name=name)
            changes[i] = OrgChange(ref=ref, action=op.action)
            org_diffs.append(object_config_diff(
                object_type=op.object_type, object_id=op.object_id,
                name=name, action=op.action, before=snapshot, after=None))
            rows_by_site = {
                sid: tuple(dict(row) for row in rows)
                for sid, rows in resolved_wlan_template.derived_rows_by_site.items()
            }
            overlays.append(OrgOverlay(
                object_type=op.object_type, object_id=op.object_id, name=name,
                action=op.action, assigned_site_ids=frozenset(rows_by_site),
                baseline=snapshot, proposed=None,
                wlan_template_rows_by_site=rows_by_site,
            ))
            continue

        resolved = provider.resolve_org_template(org_scope, op.object_id, op.object_type)
        # P3: thread template_findings through EVERY short-circuit so earlier ops'
        # non-fatal L0 findings stay auditable even if a LATER op fails.
        if not isinstance(resolved, OrgTemplateContext):
            return org_unknown((Rejection(stage="fetch", reasons=tuple(
                f"org-template lookup failed: {f.object}: {f.error}" for f in resolved.failures
            ) or ("org-template lookup failed",)),),
                template_findings=tuple(template_findings), changes=tuple(changes),
                config_diffs=tuple(org_diffs))
        snapshot = dict(resolved.template)
        ref = ObjectRef(op.object_type, op.object_id, name=snapshot.get("name"))
        changes[i] = OrgChange(ref=ref, action=op.action)  # hydrate the resolved name
        if op.action == "delete":
            proposed_layer: Mapping[str, Any] | None = None
            org_diffs.append(object_config_diff(
                object_type=op.object_type, object_id=op.object_id,
                name=snapshot.get("name"), action=op.action, before=snapshot, after=None))
        else:
            proposed_t = apply_template(snapshot, op.payload)
            if isinstance(proposed_t, Rejection):
                return org_unknown((proposed_t,),
                    template_findings=tuple(template_findings), changes=tuple(changes),
                    config_diffs=tuple(org_diffs))
            org_diffs.append(object_config_diff(
                object_type=op.object_type, object_id=op.object_id,
                name=snapshot.get("name"), action=op.action, before=snapshot, after=proposed_t))
            l0 = adapter.validate(replace(op, payload=proposed_t),
                scope_roots=None if l0_full_object else _changed_roots(op.payload))
            if l0.fatal:
                return org_unknown((Rejection(stage="l0",
                    reasons=(f"structurally-fatal L0 on proposed {op.object_type} "
                             f"{op.object_id}",)),),
                    template_findings=tuple(template_findings), changes=tuple(changes),
                    config_diffs=tuple(org_diffs))
            template_findings.extend(_stamp(l0.findings, ref))
            fg = screen_op(op.object_type, snapshot, proposed_t)
            if fg:
                return org_unknown((fg,), template_findings=tuple(template_findings),
                                   changes=tuple(changes), config_diffs=tuple(org_diffs))
            proposed_layer = proposed_t
        overlays.append(OrgOverlay(
            object_type=op.object_type, object_id=op.object_id, name=snapshot.get("name"),
            action=op.action, assigned_site_ids=frozenset(resolved.assigned_site_ids),
            baseline=snapshot, proposed=proposed_layer,
        ))
        # (the old org_diffs.append(...) at ~558 is removed — built above)

    ov_tuple = tuple(overlays)
    sites = affected_sites(ov_tuple)
    raw_map: Mapping[str, RawSiteState | FetchError] = (
        {}
        if not sites
        else prefetched_all_sites
        if prefetched_all_sites is not None
        else provider.fetch_sites(org_scope, site_ids=sites)
    )
    for wlan_id, wlan_rows, before, after in pending_wlan_usage:
        template_findings.extend(_org_wlan_usage_findings(
            provider, org_scope, wlan_id, wlan_rows, before, after, raw_map
        ))
    tf = tuple(template_findings)
    if not sites:
        decision, reasons, driving = decide_org({}, template_findings=tf, org_rejections=())
        reasons = reasons + tuple(
            f"{c.ref.kind} {c.ref.id}: no assigned sites — nothing ripples" for c in changes
        )
        return OrgVerdict(decision=decision, decision_reasons=reasons, changes=tuple(changes),
            per_site={}, driving_sites=driving, site_failures={},
            template_findings=tf, org_rejections=(),
            config_diffs=tuple(org_diffs))

    per_site: dict[str, Verdict] = {}
    site_failures: dict[str, str] = {}
    for sid in sites:
        fetched = raw_map.get(sid)
        if not isinstance(fetched, RawSiteState):
            failures = fetched.failures if fetched is not None else ()
            site_failures[sid] = "; ".join(
                f"{f.object}: {f.error}" for f in failures
            ) or "fetch failed"
            # preserve the FetchError's own acquired_at (like the single-site
            # total-fetch-failure path) so the failed site's freshness/age is
            # honest, not test-execution "now"
            acquired_at = fetched.acquired_at if fetched is not None else datetime.now(UTC)
            per_site[sid] = _unknown(
                None, adapter_findings=(), run=run, baseline_unavailable=True,
                state_meta=build_state_meta(
                    StateMeta(acquired_at=acquired_at, host=fetched.host if fetched else "",
                              fetched=(), failures=failures),
                    now=datetime.now(UTC),
                ),
            )
            continue
        base_raw, prop_raw = apply_overlays(fetched, sid, ov_tuple)
        sm = build_state_meta(fetched.meta, now=datetime.now(UTC))
        # P2a FAIL-SAFE: full gateway screening iff the site has a gatewaytemplate
        # overlay (full=True keeps the whole gateway effective -> a gatewaytemplate's
        # own networks IS screened -> never false-SAFE; cost is a possible
        # false-UNKNOWN on combined plans).
        gw_full = any(o.object_type == "gatewaytemplate" and sid in o.assigned_site_ids
                      for o in ov_tuple)
        per_site[sid] = _simulate_site_state(
            base_raw, prop_raw, adapter=adapter, registry=registry, run=run,
            state_meta=sm, adapter_findings=(), profile_proposed=None,
            gateway_screen_full=gw_full,
        )

    decision, reasons, driving = decide_org(per_site, template_findings=tf, org_rejections=())
    return OrgVerdict(
        decision=decision, decision_reasons=reasons, changes=tuple(changes),
        per_site=per_site, driving_sites=driving, site_failures=site_failures,
        template_findings=tf, org_rejections=(),
        config_diffs=tuple(org_diffs),
    )


simulate_org_template = simulate_org_plan  # back-compat alias (single-op is a 1-op plan)


# ---------------------------------------------------------------------------
# GS34 — org-NAC orchestrator
# ---------------------------------------------------------------------------
from digital_twin.adapters.mist.ingest.nac import build_nac_ir  # noqa: E402
from digital_twin.adapters.mist.validate import validate_payload  # noqa: E402
from digital_twin.checks.nac.delta import NacDeltaCheck  # noqa: E402
from digital_twin.checks.nac.shadowing import NacShadowingCheck  # noqa: E402
from digital_twin.config_diff import object_config_diff  # noqa: E402
from digital_twin.contracts import ObjectConfigDiff  # noqa: E402
from digital_twin.verdict.decision import decide  # noqa: E402
from digital_twin.verdict.org_nac_verdict import (  # noqa: E402
    OrgNacVerdict,
    nac_changes,
)


def _org_nac_unknown(
    rej: Rejection | None = None, *, adapter_findings: tuple[Finding, ...] = (),
    l0_fatal: bool = False, config_diffs: tuple[ObjectConfigDiff, ...] = (),
) -> OrgNacVerdict:
    decision, reasons = decide(DecisionInputs(
        rejections=(rej,) if rej else (), l0_fatal=l0_fatal, baseline_unavailable=False,
        check_results=(), adapter_findings=adapter_findings))
    return OrgNacVerdict(decision, reasons, (), (), adapter_findings,
                         (rej,) if rej else (), tuple(config_diffs))


@with_fetch_budget
def simulate_org_nac(
    plan_data: Mapping[str, Any],
    *,
    provider: StateProvider,
    run: RunContext | None = None,
    l0_full_object: bool = False,
) -> OrgNacVerdict:
    plan = parse_change_plan(plan_data)
    if isinstance(plan, Rejection):
        return _org_nac_unknown(plan)
    rej = check_objects(plan)
    if rej:
        return _org_nac_unknown(rej)

    fetch = provider.resolve_org_nac(OrgScope(org_id=plan.scope.org_id))
    if isinstance(fetch, FetchError):
        decision, reasons = decide(DecisionInputs(
            rejections=(), l0_fatal=False, baseline_unavailable=True,
            check_results=(), adapter_findings=()))
        return OrgNacVerdict(decision, reasons, (), (), (), ())

    # FIRST-wins, matching build_nac_ir's dedup — NOT a dict comprehension (which is
    # last-wins). Otherwise base_ir (built first-wins from fetch.rules) and proposed_raw
    # would disagree on a duplicate id → a phantom modify on a no-op, and updates would
    # apply to the row the ingester drops. The duplicate WARNING still comes from base_ir.
    baseline_raw: dict[str, dict[str, Any]] = {}
    for r in fetch.rules:
        rid = r.get("id")
        if rid and str(rid) not in baseline_raw:
            baseline_raw[str(rid)] = dict(r)
    proposed_raw: dict[str, dict[str, Any]] = dict(baseline_raw)
    adapter_findings: tuple[Finding, ...] = ()
    nac_diffs: list[ObjectConfigDiff] = []

    for op in sorted(plan.ops, key=lambda o: o.order):
        exists = op.object_id in baseline_raw
        if op.action in ("update", "delete") and not exists:
            return _org_nac_unknown(Rejection(stage="apply", reasons=(
                f"ops[order={op.order}]: no nacrule with id {op.object_id!r}",)),
                adapter_findings=adapter_findings, config_diffs=tuple(nac_diffs))
        if op.action == "create" and exists:
            return _org_nac_unknown(Rejection(stage="apply", reasons=(
                f"ops[order={op.order}]: nacrule id {op.object_id!r} already exists",)),
                adapter_findings=adapter_findings, config_diffs=tuple(nac_diffs))
        if op.action == "delete":
            nac_diffs.append(object_config_diff(
                object_type="nacrule", object_id=op.object_id,
                name=baseline_raw[op.object_id].get("name"),
                action="delete", before=baseline_raw[op.object_id], after={}))
            proposed_raw.pop(op.object_id, None)
            continue
        if update_conflicts(op.payload):
            return _org_nac_unknown(Rejection(stage="apply", reasons=(
                f"ops[order={op.order}]: conflicting set AND '-' delete marker",)),
                adapter_findings=adapter_findings, config_diffs=tuple(nac_diffs))
        current = baseline_raw.get(op.object_id, {"id": op.object_id})
        effective = effective_update(current, op.payload)
        if op.action == "create":
            effective["id"] = op.object_id
        nac_diffs.append(object_config_diff(
            object_type="nacrule", object_id=op.object_id,
            # create's `current` is only {"id": ...}; the new name lives in `effective`
            name=effective.get("name") if op.action == "create" else current.get("name"),
            action=op.action,
            before={} if op.action == "create" else current, after=effective))
        scope_roots = None if (op.action == "create" or l0_full_object) \
            else _changed_roots(op.payload)
        l0 = validate_payload("nacrule", effective, scope_roots=scope_roots)
        subject = ObjectRef("nacrule", op.object_id, name=current.get("name"))
        adapter_findings += _stamp(l0.findings, subject)
        if l0.fatal:
            return _org_nac_unknown(
                adapter_findings=adapter_findings, l0_fatal=True,
                config_diffs=tuple(nac_diffs))
        fg = screen_op("nacrule", current, effective)
        if fg:
            return _org_nac_unknown(fg, adapter_findings=adapter_findings,
                                    config_diffs=tuple(nac_diffs))
        proposed_raw[op.object_id] = effective

    # Build the baseline IR from the RAW fetch.rules (not the id-keyed baseline_raw) so
    # id-less / malformed FETCHED rows still emit their ingester warnings. (baseline_raw
    # is only the id-keyed subset the apply loop needs; building from it would drop those
    # rows silently — bypassing the load-bearing-ingest contract → hidden operational
    # findings.) Both states' findings reach the verdict; dedup a rule malformed in both.
    base_ir, base_findings = build_nac_ir(fetch.rules, fetch.tags)
    prop_ir, prop_findings = build_nac_ir(proposed_raw.values(), fetch.tags)
    seen: set[tuple[str, object]] = set()
    for f in (*base_findings, *prop_findings):
        key = (f.code, f.evidence.get("id"))
        if key not in seen:
            seen.add(key)
            adapter_findings += (f,)
    adapter_findings += fetch.tag_findings

    diff = diff_ir(base_ir, prop_ir)
    ctx = CheckContext(baseline=AnalysisContext(base_ir),
                       proposed=AnalysisContext(prop_ir), diff=diff)
    # Run through CheckRegistry, NOT direct c.run(ctx): the registry isolates a
    # crashing check to a CHECK_ERROR + OPERATIONAL finding (→ decide() floors
    # REVIEW), gates applies_to (NOT_APPLICABLE when the diff doesn't touch nac),
    # and resolves finding names centrally — so a check bug degrades, never escapes.
    results = CheckRegistry([NacDeltaCheck(), NacShadowingCheck()]).run_all(ctx)
    base_map = {r.id: r for r in base_ir.nacrules}
    prop_map = {r.id: r for r in prop_ir.nacrules}
    modified_fields = {
        modified.ref.id: modified.changed_fields
        for modified in diff.modified
        if modified.ref.kind == "nacrule"
    }
    removed_ids = {ref.id for ref in diff.removed if ref.kind == "nacrule"}
    usage_results = tuple(
        _nacrule_access_impact_result(
            provider,
            OrgScope(org_id=plan.scope.org_id),
            baseline=base_map[rule_id],
            proposed=prop_map.get(rule_id),
            changed_fields=(modified_fields.get(rule_id) or ("deleted",)),
        )
        for rule_id in sorted(set(modified_fields) | removed_ids)
        if set(modified_fields.get(rule_id, ("deleted",))) != {"name"}
    )
    results += usage_results

    decision, reasons = decide(DecisionInputs(
        rejections=(), l0_fatal=False, baseline_unavailable=False,
        check_results=results, adapter_findings=adapter_findings))
    return OrgNacVerdict(
        decision, reasons, nac_changes(diff, base_map, prop_map),
        results, adapter_findings, (),
        tuple(nac_diffs),
    )
