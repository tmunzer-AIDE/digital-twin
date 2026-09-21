"""State-provider overlay used while evaluating an ordered composite ChangePlan.

Each specialized evaluator still receives the provider contract it already knows.
This wrapper retains proposed objects between segments so a later segment observes
earlier mutations instead of re-reading only the committed baseline.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from digital_twin.adapters.mist.apply.objects import (
    create_object,
    delete_object,
    effective_update,
    get_object,
    replace_object,
)
from digital_twin.contracts import ChangeOp
from digital_twin.engine.org_overlay import OrgOverlay, apply_overlays
from digital_twin.engine.org_template import apply_template
from digital_twin.providers.base import (
    FetchError,
    NacRuleUsageContext,
    ObjectReference,
    ObjectRelationshipContext,
    OrgNetworksContext,
    OrgScope,
    OrgTemplateContext,
    OrgWlanContext,
    OrgWlanTemplateContext,
    RawSiteState,
    SiteScope,
    StateProvider,
    WlanUsageContext,
)

_NAME_REFERENCE_HINTS: dict[str, tuple[str, ...]] = {
    "org_networks": ("network", "networks"),
    "org_services": ("service", "services"),
    "org_servicepolicies": ("servicepolicy", "service_policy"),
    "org_vpns": ("vpn", "vpns"),
}


def _reference_paths(
    value: Any,
    *,
    object_type: str,
    object_id: str,
    object_name: str | None,
    path: str = "$",
    parent_key: str = "",
) -> tuple[str, ...]:
    if isinstance(value, Mapping):
        return tuple(
            found
            for key, child in value.items()
            for found in _reference_paths(
                child,
                object_type=object_type,
                object_id=object_id,
                object_name=object_name,
                path=f"{path}.{key}",
                parent_key=str(key).casefold(),
            )
        )
    if isinstance(value, list | tuple):
        return tuple(
            found
            for index, child in enumerate(value)
            for found in _reference_paths(
                child,
                object_type=object_type,
                object_id=object_id,
                object_name=object_name,
                path=f"{path}[{index}]",
                parent_key=parent_key,
            )
        )
    if isinstance(value, str):
        if value == object_id:
            return (path,)
        hints = _NAME_REFERENCE_HINTS.get(object_type, ())
        if object_name and value == object_name and any(hint in parent_key for hint in hints):
            return (path,)
    return ()


class ProposedStateProvider:
    """Delegate provider reads while retaining earlier proposed-state mutations."""

    def __init__(self, inner: StateProvider) -> None:
        self._inner = inner
        self._sites: dict[str, RawSiteState | FetchError] = {}
        # Immutable pre-batch snapshots. `_sites` advances after each segment;
        # these copies let the composite driver perform one authoritative
        # original -> fully-composed final-state evaluation at the end.
        self._baseline_sites: dict[str, RawSiteState | FetchError] = {}
        self._org_networks: list[dict[str, Any]] | FetchError | None = None
        self._templates: dict[tuple[str, str], OrgTemplateContext | FetchError] = {}
        self._wlans: dict[str, OrgWlanContext | FetchError] = {}
        self._wlan_templates: dict[str, OrgWlanTemplateContext | FetchError] = {}
        self._org_source_overlays: dict[tuple[str, str], Mapping[str, Any] | None] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def resolve_org_networks(self, scope: OrgScope) -> OrgNetworksContext | FetchError:
        if self._org_networks is None:
            resolved = self._inner.resolve_org_networks(scope)
            if isinstance(resolved, FetchError):
                self._org_networks = resolved
            else:
                self._org_networks = [dict(row) for row in resolved.networks]
        if isinstance(self._org_networks, FetchError):
            return self._org_networks
        return OrgNetworksContext(tuple(self._org_networks))

    def resolve_org_template(
        self, scope: OrgScope, template_id: str, object_type: str
    ) -> OrgTemplateContext | FetchError:
        key = (object_type, template_id)
        if key not in self._templates:
            self._templates[key] = self._inner.resolve_org_template(
                scope, template_id, object_type
            )
        return self._templates[key]

    def resolve_org_wlan(
        self, scope: OrgScope, wlan_id: str
    ) -> OrgWlanContext | FetchError:
        if wlan_id not in self._wlans:
            self._wlans[wlan_id] = self._inner.resolve_org_wlan(scope, wlan_id)
        return self._wlans[wlan_id]

    def resolve_org_wlan_template(
        self, scope: OrgScope, template_id: str
    ) -> OrgWlanTemplateContext | FetchError:
        if template_id not in self._wlan_templates:
            self._wlan_templates[template_id] = self._inner.resolve_org_wlan_template(
                scope, template_id
            )
        return self._wlan_templates[template_id]

    def resolve_object_relationships(
        self, scope: OrgScope, object_type: str, object_id: str
    ) -> ObjectRelationshipContext | FetchError:
        resolved = self._inner.resolve_object_relationships(scope, object_type, object_id)
        if isinstance(resolved, FetchError):
            return resolved
        target_name = resolved.target.get("name")
        references = list(resolved.references)

        for (source_type, source_id), row in self._org_source_overlays.items():
            references = [
                ref for ref in references
                if not (ref.source_type == source_type and ref.source_id == source_id)
            ]
            if row is not None:
                references.extend(ObjectReference(
                    source_type=source_type,
                    source_id=source_id,
                    source_name=(str(row.get("name")) if row.get("name") is not None else None),
                    path=path,
                ) for path in _reference_paths(
                    row,
                    object_type=object_type,
                    object_id=object_id,
                    object_name=str(target_name) if isinstance(target_name, str) else None,
                ))

        for site_id, state in self._sites.items():
            if not isinstance(state, RawSiteState):
                continue
            references = [
                ref for ref in references
                if not (
                    ref.site_id == site_id
                    and ref.source_type in {"site_devices", "site_settings"}
                )
            ]
            site_sources = [
                ("site_settings", site_id, state.setting),
                *(("site_devices", str(row.get("id") or row.get("mac") or "unknown"), row)
                  for row in state.devices),
            ]
            for source_type, source_id, row in site_sources:
                references.extend(ObjectReference(
                    source_type=source_type,
                    source_id=source_id,
                    source_name=(str(row.get("name") or row.get("hostname"))
                                 if row.get("name") or row.get("hostname") else None),
                    site_id=site_id,
                    path=path,
                ) for path in _reference_paths(
                    row,
                    object_type=object_type,
                    object_id=object_id,
                    object_name=str(target_name) if isinstance(target_name, str) else None,
                ))
        return ObjectRelationshipContext(
            target=resolved.target,
            references=tuple(dict.fromkeys(references)),
            checked_sources=resolved.checked_sources,
            failures=resolved.failures,
        )

    def resolve_wlan_usage(
        self, scope: OrgScope | SiteScope, wlan_id: str, *, window_days: int = 7
    ) -> WlanUsageContext | FetchError:
        return self._inner.resolve_wlan_usage(scope, wlan_id, window_days=window_days)

    def resolve_nacrule_usage(
        self, scope: OrgScope, nacrule_id: str, *, window_days: int = 7
    ) -> NacRuleUsageContext | FetchError:
        return self._inner.resolve_nacrule_usage(
            scope, nacrule_id, window_days=window_days
        )

    def fetch_site(
        self, scope: SiteScope, *, include_derived: bool = False
    ) -> RawSiteState | FetchError:
        cached = self._sites.get(scope.site_id)
        if cached is None:
            cached = self._inner.fetch_site(scope, include_derived=include_derived)
            self._sites[scope.site_id] = cached
            self._baseline_sites.setdefault(scope.site_id, cached)
        return self._with_org_networks(cached)

    def fetch_sites(
        self,
        scope: OrgScope,
        site_ids: Sequence[str] | None = None,
        *,
        include_derived: bool = False,
    ) -> dict[str, RawSiteState | FetchError]:
        requested = None if site_ids is None else tuple(str(sid) for sid in site_ids)
        fetched = self._inner.fetch_sites(
            scope, requested, include_derived=include_derived
        )
        for site_id, state in fetched.items():
            self._sites.setdefault(site_id, state)
            self._baseline_sites.setdefault(site_id, state)
        selected = self._sites if requested is None else {
            site_id: self._sites[site_id]
            for site_id in requested
            if site_id in self._sites
        }
        return {
            site_id: self._with_org_networks(state)
            for site_id, state in selected.items()
        }

    def cumulative_site_states(self) -> dict[str, tuple[RawSiteState, RawSiteState]]:
        """Original and fully composed states for every successfully fetched site."""
        out: dict[str, tuple[RawSiteState, RawSiteState]] = {}
        for site_id, baseline in self._baseline_sites.items():
            proposed = self._sites.get(site_id)
            final = self._with_org_networks(proposed) if proposed is not None else None
            if (
                isinstance(baseline, RawSiteState)
                and isinstance(final, RawSiteState)
                and baseline != final
            ):
                out[site_id] = (baseline, final)
        return out

    def apply_org_network_ops(self, scope: OrgScope, ops: tuple[ChangeOp, ...]) -> None:
        """Advance the org network namespace after a policy segment."""
        resolved = self.resolve_org_networks(scope)
        if isinstance(resolved, FetchError):
            return
        rows = [dict(row) for row in resolved.networks]
        for op in sorted(ops, key=lambda item: item.order):
            if op.object_type != "org_networks":
                continue
            index = next(
                (i for i, row in enumerate(rows) if str(row.get("id")) == op.object_id),
                None,
            )
            if op.action == "create":
                rows.append({**dict(op.payload), "id": op.object_id})
            elif op.action == "update" and index is not None:
                rows[index] = effective_update(rows[index], op.payload)
            elif op.action == "delete" and index is not None:
                rows.pop(index)
        self._org_networks = rows

    def apply_policy_ops(self, scope: OrgScope, ops: tuple[ChangeOp, ...]) -> None:
        """Advance policy-managed objects after a composite segment.

        WLAN-template assignment is a policy decision, but its derived WLAN rows
        are site state. Retaining those rows makes a later operation in the same
        ordered batch observe the assignment it follows.
        """
        self.apply_org_network_ops(scope, ops)
        for op in sorted(ops, key=lambda item: item.order):
            if op.object_type != "wlantemplate" or op.action != "update":
                continue
            context = self._wlan_templates.get(op.object_id)
            if not isinstance(context, OrgWlanTemplateContext):
                continue

            template = effective_update(context.template, op.payload)
            site_ids = template.get("site_ids", [])
            sitegroup_ids = template.get("sitegroup_ids", [])
            if (
                not isinstance(site_ids, list)
                or any(not isinstance(site_id, str) or not site_id for site_id in site_ids)
                or not isinstance(sitegroup_ids, list)
                or any(not isinstance(group_id, str) or not group_id for group_id in sitegroup_ids)
            ):
                continue

            targets = set(site_ids)
            complete = True
            for group_id in sitegroup_ids:
                group = self._inner.resolve_org_sitegroup(scope, group_id)
                if isinstance(group, FetchError):
                    complete = False
                    break
                targets.update(group.assigned_site_ids)
            if not complete:
                continue

            previous = set(context.derived_rows_by_site)
            affected = previous | targets
            if affected:
                self.fetch_sites(scope, site_ids=sorted(affected))
            derived: dict[str, tuple[Mapping[str, Any], ...]] = {}
            for site_id in sorted(affected):
                state = self._sites.get(site_id)
                if not isinstance(state, RawSiteState):
                    continue
                retained = tuple(
                    row
                    for row in state.wlans
                    if str(row.get("template_id") or "") != op.object_id
                )
                assigned = (
                    tuple(
                        {
                            **dict(row),
                            "template_id": op.object_id,
                            "for_site": False,
                        }
                        for row in context.template_wlans
                    )
                    if site_id in targets
                    else ()
                )
                self._sites[site_id] = replace(state, wlans=retained + assigned)
                if assigned:
                    derived[site_id] = assigned

            self._wlan_templates[op.object_id] = OrgWlanTemplateContext(
                template=template,
                derived_rows_by_site=derived,
                template_wlans=context.template_wlans,
                template_wlans_complete=context.template_wlans_complete,
            )

    def apply_org_ops(self, scope: OrgScope, ops: tuple[ChangeOp, ...]) -> None:
        """Advance template and org-WLAN effects for later site segments."""
        overlays: list[OrgOverlay] = []
        for op in sorted(ops, key=lambda item: item.order):
            if op.object_type == "wlan":
                if op.action == "create":
                    if not self._sites:
                        self.fetch_sites(scope, site_ids=None)
                    created = {**dict(op.payload), "id": op.object_id, "for_site": False}
                    site_ids = frozenset(self._sites)
                    overlays.append(OrgOverlay(
                        object_type="wlan",
                        object_id=op.object_id,
                        name=created.get("name") or created.get("ssid"),
                        action=op.action,
                        assigned_site_ids=site_ids,
                        baseline={},
                        proposed=created,
                        wlan_baseline_by_site={sid: None for sid in site_ids},
                        wlan_proposed_by_site={sid: created for sid in site_ids},
                    ))
                    continue
                wlan_context = self._wlans.get(op.object_id)
                if not isinstance(wlan_context, OrgWlanContext):
                    continue
                baseline_by_site = {
                    sid: dict(row) for sid, row in wlan_context.derived_rows_by_site.items()
                }
                proposed_org: Mapping[str, Any] | None
                proposed_by_site: dict[str, Mapping[str, Any] | None]
                if op.action == "delete":
                    proposed_org = None
                    proposed_by_site = {sid: None for sid in baseline_by_site}
                else:
                    proposed_org = effective_update(wlan_context.wlan, op.payload)
                    proposed_by_site = {
                        sid: effective_update(row, op.payload)
                        for sid, row in baseline_by_site.items()
                    }
                overlays.append(OrgOverlay(
                    object_type="wlan",
                    object_id=op.object_id,
                    name=wlan_context.wlan.get("name") or wlan_context.wlan.get("ssid"),
                    action=op.action,
                    assigned_site_ids=frozenset(baseline_by_site),
                    baseline=wlan_context.wlan,
                    proposed=proposed_org,
                    wlan_baseline_by_site=baseline_by_site,
                    wlan_proposed_by_site=proposed_by_site,
                ))
                continue

            if op.object_type == "wlantemplate":
                wlan_template_context = self._wlan_templates.get(op.object_id)
                if not isinstance(wlan_template_context, OrgWlanTemplateContext):
                    continue
                rows = {
                    sid: tuple(dict(row) for row in site_rows)
                    for sid, site_rows in wlan_template_context.derived_rows_by_site.items()
                }
                overlays.append(OrgOverlay(
                    object_type="wlantemplate",
                    object_id=op.object_id,
                    name=wlan_template_context.template.get("name"),
                    action=op.action,
                    assigned_site_ids=frozenset(rows),
                    baseline=wlan_template_context.template,
                    proposed=None,
                    wlan_template_rows_by_site=rows,
                ))
                continue

            template_context = self._templates.get((op.object_type, op.object_id))
            if not isinstance(template_context, OrgTemplateContext):
                continue
            proposed = None if op.action == "delete" else apply_template(
                template_context.template, op.payload
            )
            if not isinstance(proposed, Mapping) and proposed is not None:
                continue
            self._org_source_overlays[
                (f"org_{op.object_type}s", op.object_id)
            ] = proposed
            overlays.append(OrgOverlay(
                object_type=op.object_type,
                object_id=op.object_id,
                name=template_context.template.get("name"),
                action=op.action,
                assigned_site_ids=frozenset(template_context.assigned_site_ids),
                baseline=template_context.template,
                proposed=proposed,
            ))

        for site_id in {sid for overlay in overlays for sid in overlay.assigned_site_ids}:
            state = self._sites.get(site_id)
            if state is None:
                state = self.fetch_site(SiteScope(scope.org_id, site_id))
            if isinstance(state, RawSiteState):
                _, proposed_state = apply_overlays(state, site_id, tuple(overlays))
                self._sites[site_id] = proposed_state

    def apply_site_ops(self, scope: SiteScope, ops: tuple[ChangeOp, ...]) -> None:
        """Advance one site's directly modeled configuration between segments."""
        state = self.fetch_site(scope)
        if not isinstance(state, RawSiteState):
            return
        proposed = state
        for op in sorted(ops, key=lambda item: item.order):
            current = get_object(proposed, op.object_type, op.object_id)
            if op.action == "create" and current is None:
                try:
                    proposed = create_object(
                        proposed, op.object_type, op.object_id, op.payload
                    )
                except ValueError:
                    continue
            elif op.action == "update" and current is not None:
                proposed = replace_object(
                    proposed, op.object_type, op.object_id, op.payload
                )
            elif op.action == "delete" and current is not None:
                try:
                    proposed = delete_object(proposed, op.object_type, op.object_id)
                except ValueError:
                    continue
        self._sites[scope.site_id] = proposed

    def _with_org_networks(
        self, state: RawSiteState | FetchError
    ) -> RawSiteState | FetchError:
        if not isinstance(state, RawSiteState) or not isinstance(self._org_networks, list):
            return state
        return replace(state, org_networks=tuple(self._org_networks))
