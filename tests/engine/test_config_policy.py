from datetime import UTC, datetime

import pytest

from digital_twin.engine.pipeline import simulate
from digital_twin.providers.base import (
    FetchError,
    FetchFailure,
    ObjectReference,
    ObjectRelationshipContext,
    OrgNetworksContext,
    OrgScope,
    OrgSiteGroupContext,
    PskUsageContext,
    SiteScope,
)
from digital_twin.verdict.decision import Decision


class PolicyProvider:
    def __init__(
        self,
        *,
        sitegroup: OrgSiteGroupContext | FetchError | None = None,
        psk_usage: PskUsageContext | FetchError | None = None,
        networks: OrgNetworksContext | FetchError | None = None,
        relationships: ObjectRelationshipContext | FetchError | None = None,
    ) -> None:
        self.sitegroup = sitegroup
        self.psk_usage = psk_usage
        self.networks = networks
        self.relationships = relationships or ObjectRelationshipContext(
            target={"id": "obj-1", "name": "target"},
            references=(),
            checked_sources=("mock",),
        )
        self.psk_calls: list[tuple[OrgScope | SiteScope, str, int]] = []

    def resolve_org_sitegroup(
        self, scope: OrgScope, sitegroup_id: str
    ) -> OrgSiteGroupContext | FetchError:
        assert self.sitegroup is not None
        return self.sitegroup

    def resolve_psk_usage(
        self, scope: OrgScope | SiteScope, psk_id: str, *, window_days: int = 7
    ) -> PskUsageContext | FetchError:
        self.psk_calls.append((scope, psk_id, window_days))
        assert self.psk_usage is not None
        return self.psk_usage

    def resolve_org_networks(
        self, scope: OrgScope
    ) -> OrgNetworksContext | FetchError:
        assert self.networks is not None
        return self.networks

    def resolve_object_relationships(
        self, scope: OrgScope, object_type: str, object_id: str
    ) -> ObjectRelationshipContext | FetchError:
        return self.relationships


def _plan(
    object_type: str,
    *,
    action: str = "update",
    site_id: str | None = None,
    payload: dict | None = None,
) -> dict:
    scope = {"org_id": "o1"}
    if site_id:
        scope["site_id"] = site_id
    return {
        "source": "mist",
        "scope": scope,
        "ops": [
            {
                "action": action,
                "order": 0,
                "object_type": object_type,
                "object_id": "obj-1",
                "payload": {} if action == "delete" else (payload or {"enabled": True}),
            }
        ],
    }


def _ops_plan(*ops: dict) -> dict:
    return {"source": "mist", "scope": {"org_id": "o1"}, "ops": list(ops)}


def _cfg_op(action: str, object_type: str, object_id: str, payload: dict, order: int) -> dict:
    return {
        "action": action,
        "order": order,
        "object_type": object_type,
        "object_id": object_id,
        "payload": payload,
    }


@pytest.mark.parametrize(
    "object_type",
    ["org_info", "org_alarmtemplates"],
)
def test_static_safe_configuration_types(object_type):
    verdict = simulate(_plan(object_type), provider=PolicyProvider())
    assert verdict.decision is Decision.SAFE


@pytest.mark.parametrize(
    "object_type", ["networktemplate", "gatewaytemplate", "sitetemplate"]
)
def test_valid_unassigned_org_template_create_is_safe(object_type):
    verdict = simulate(
        _plan(object_type, action="create", payload={"name": "new template"}),
        provider=PolicyProvider(),
    )
    assert verdict.decision is Decision.SAFE, verdict.decision_reasons


def test_sitegroup_update_requires_review_because_membership_changes_targeting():
    verdict = simulate(
        _plan("org_sitegroups", payload={"site_ids": ["s1"]}),
        provider=PolicyProvider(),
    )
    assert verdict.decision is Decision.REVIEW
    assert verdict.findings[0].code == "config.sitegroup.membership.review"


@pytest.mark.parametrize(
    "object_type",
    [
        "org_avprofiles", "org_deviceprofiles", "org_idpprofiles",
        "org_aamwprofiles", "org_nactags", "org_rftemplates", "org_services",
        "org_servicepolicies", "org_vpns",
    ],
)
def test_relationship_sensitive_types_are_safe_to_create_and_change_when_unused(object_type):
    site_id = "s1" if object_type.startswith("site_") else None
    created = simulate(
        _plan(object_type, action="create", site_id=site_id),
        provider=PolicyProvider(),
    )
    changed = simulate(
        _plan(object_type, site_id=site_id),
        provider=PolicyProvider(),
    )
    assert created.decision is Decision.SAFE
    assert changed.decision is Decision.SAFE
    assert changed.check_results[0].coverage.state.value == "complete"


@pytest.mark.parametrize("object_type", ["org_sites", "org_wxtags", "site_wxtags"])
def test_targeting_objects_still_require_review_to_change(object_type):
    verdict = simulate(
        _plan(object_type, site_id="s1" if object_type.startswith("site_") else None),
        provider=PolicyProvider(),
    )
    assert verdict.decision is Decision.REVIEW


@pytest.mark.parametrize("object_type", ["org_wxrules", "site_wxrules"])
def test_wx_rule_changes_require_review(object_type):
    verdict = simulate(
        _plan(object_type, site_id="s1" if object_type.startswith("site_") else None),
        provider=PolicyProvider(),
    )
    assert verdict.decision is Decision.REVIEW


@pytest.mark.parametrize("object_type", ["org_webhooks", "site_webhooks"])
def test_webhook_changes_are_safe_for_network_connectivity(object_type):
    site_id = "s1" if object_type.startswith("site_") else None
    verdict = simulate(
        _plan(object_type, site_id=site_id, payload={"name": "renamed"}),
        provider=PolicyProvider(),
    )
    assert verdict.decision is Decision.SAFE
    assert verdict.check_results[0].check_id == "config.webhook"


def test_referenced_profile_update_is_review_and_delete_is_unsafe():
    context = ObjectRelationshipContext(
        target={"id": "obj-1", "name": "edge-security"},
        references=(ObjectReference(
            source_type="org_gatewaytemplates",
            source_id="gt-1",
            source_name="branch",
            path="$.idp.idpprofile_id",
        ),),
        checked_sources=("org_gatewaytemplates",),
    )
    provider = PolicyProvider(relationships=context)
    updated = simulate(_plan("org_idpprofiles"), provider=provider)
    deleted = simulate(_plan("org_idpprofiles", action="delete"), provider=provider)
    assert updated.decision is Decision.REVIEW
    assert deleted.decision is Decision.UNSAFE
    assert deleted.findings[0].evidence["references"][0]["source_id"] == "gt-1"


def test_referenced_update_expands_exact_dependents_with_partial_impact_coverage():
    context = ObjectRelationshipContext(
        target={"id": "obj-1", "name": "edge-security"},
        references=(ObjectReference(
            source_type="org_gatewaytemplates", source_id="gt-1", source_name="branch",
            path="$.idp.idpprofile_id",
        ),),
        checked_sources=("org_gatewaytemplates",),
    )
    verdict = simulate(_plan("org_idpprofiles"), provider=PolicyProvider(relationships=context))

    finding = next(f for f in verdict.findings if f.code == "config.referenced_update_impact")
    assert finding.evidence["affected_dependents"] == ["org_gatewaytemplates:gt-1"]
    assert verdict.check_results[0].coverage.state.value == "partial"


def test_batch_duplicate_mutation_is_rejected_and_reported():
    verdict = simulate(_ops_plan(
        _cfg_op("update", "org_services", "obj-1", {"name": "one"}, 0),
        _cfg_op("update", "org_services", "obj-1", {"name": "two"}, 1),
    ), provider=PolicyProvider())

    assert verdict.decision is Decision.UNKNOWN
    assert any(
        finding.code == "config.batch_integrity.duplicate_mutation"
        for finding in verdict.findings
    )


def test_batch_detects_new_reference_to_deleted_object():
    verdict = simulate(_ops_plan(
        _cfg_op("delete", "org_services", "obj-1", {}, 0),
        _cfg_op(
            "create", "org_servicepolicies", "policy-1",
            {"name": "policy", "service_id": "obj-1"}, 1,
        ),
    ), provider=PolicyProvider())

    assert verdict.decision is Decision.UNSAFE
    assert any(
        finding.code == "config.batch_integrity.dangling_reference"
        and finding.evidence.get("dangling_target_ids") == ["obj-1"]
        for finding in verdict.findings
    )


def test_batch_final_state_can_remove_reference_before_target_delete():
    context = ObjectRelationshipContext(
        target={"id": "obj-1", "name": "service-a"},
        references=(ObjectReference(
            source_type="org_servicepolicies", source_id="policy-1",
            path="$.service_id",
        ),),
        checked_sources=("org_servicepolicies",),
    )
    verdict = simulate(_ops_plan(
        _cfg_op("delete", "org_services", "obj-1", {}, 0),
        _cfg_op(
            "update", "org_servicepolicies", "policy-1",
            {"service_id": "replacement"}, 1,
        ),
    ), provider=PolicyProvider(relationships=context))

    assert not any(
        finding.code == "config.batch_integrity.dangling_reference"
        for finding in verdict.findings
    )
    assert any(
        result.check_id == "config.batch_integrity.references_resolved"
        for result in verdict.check_results
    )


def test_referenced_nactag_delete_requires_review_not_unsafe():
    context = ObjectRelationshipContext(
        target={"id": "obj-1", "name": "iot"},
        references=(ObjectReference(
            source_type="org_nacrules", source_id="rule-1", path="$.nactags[0]"
        ),),
        checked_sources=("org_nacrules",),
    )
    verdict = simulate(
        _plan("org_nactags", action="delete"),
        provider=PolicyProvider(relationships=context),
    )
    assert verdict.decision is Decision.REVIEW


def test_incomplete_relationship_discovery_never_declares_unused_safe():
    context = ObjectRelationshipContext(
        target={"id": "obj-1"},
        references=(),
        checked_sources=("org_gatewaytemplates",),
        failures=(FetchFailure("site_devices:s1", "timeout"),),
    )
    verdict = simulate(
        _plan("org_services", action="delete"),
        provider=PolicyProvider(relationships=context),
    )
    assert verdict.decision is Decision.REVIEW
    assert verdict.check_results[0].coverage.state.value == "partial"


def test_sitegroup_delete_without_assigned_sites_is_safe():
    verdict = simulate(
        _plan("org_sitegroups", action="delete"),
        provider=PolicyProvider(sitegroup=OrgSiteGroupContext(())),
    )
    assert verdict.decision is Decision.SAFE


def test_sitegroup_delete_with_assigned_sites_is_review():
    verdict = simulate(
        _plan("org_sitegroups", action="delete"),
        provider=PolicyProvider(sitegroup=OrgSiteGroupContext(("s1", "s2"))),
    )
    assert verdict.decision is Decision.REVIEW
    assert verdict.findings[0].evidence["assigned_site_ids"] == ["s1", "s2"]


def test_sitegroup_delete_fetch_gap_is_review_not_safe():
    failure = FetchError(
        scope=OrgScope("o1"),
        failures=(FetchFailure("org_sitegroup", "boom"),),
        acquired_at=datetime.now(UTC),
        host="api.mist.com",
    )
    verdict = simulate(
        _plan("org_sitegroups", action="delete"),
        provider=PolicyProvider(sitegroup=failure),
    )
    assert verdict.decision is Decision.REVIEW
    assert verdict.check_results[0].coverage.state.value == "partial"


def test_psk_create_is_safe_without_telemetry_lookup():
    provider = PolicyProvider()
    verdict = simulate(_plan("org_psks", action="create"), provider=provider)
    assert verdict.decision is Decision.SAFE
    assert provider.psk_calls == []


def test_recently_used_psk_delete_is_unsafe():
    usage = PskUsageContext(
        active_site_ids=("s1",), checked_site_ids=("s1",), failures=(), window_days=7
    )
    verdict = simulate(
        _plan("org_psks", action="delete"),
        provider=PolicyProvider(psk_usage=usage),
    )
    assert verdict.decision is Decision.UNSAFE


def test_unused_psk_update_is_safe_after_complete_seven_day_check():
    provider = PolicyProvider(
        psk_usage=PskUsageContext((), ("s1", "s2"), (), window_days=7)
    )
    verdict = simulate(_plan("org_psks"), provider=provider)
    assert verdict.decision is Decision.SAFE
    assert provider.psk_calls == [(OrgScope("o1"), "obj-1", 7)]


def test_recently_used_psk_update_is_review():
    provider = PolicyProvider(
        psk_usage=PskUsageContext(("s2",), ("s1", "s2"), (), window_days=7)
    )
    verdict = simulate(_plan("org_psks", action="update"), provider=provider)
    assert verdict.decision is Decision.REVIEW
    assert verdict.findings[0].evidence["active_site_ids"] == ["s2"]


def test_psk_telemetry_gap_is_review_even_when_no_usage_was_observed():
    provider = PolicyProvider(
        psk_usage=PskUsageContext(
            (),
            ("s1",),
            (FetchFailure("psk_sessions:s2", "timeout"),),
            window_days=7,
        )
    )
    verdict = simulate(_plan("org_psks"), provider=provider)
    assert verdict.decision is Decision.REVIEW


def test_site_psk_queries_only_its_site_scope():
    provider = PolicyProvider(
        psk_usage=PskUsageContext((), ("s1",), (), window_days=7)
    )
    verdict = simulate(_plan("site_psks", site_id="s1"), provider=provider)
    assert verdict.decision is Decision.SAFE
    assert provider.psk_calls == [(SiteScope("o1", "s1"), "obj-1", 7)]


def test_new_gateway_network_with_unique_subnet_is_safe():
    provider = PolicyProvider(networks=OrgNetworksContext((
        {"id": "n1", "name": "corp", "vlan_id": 10, "subnet": "10.10.0.0/24"},
    )))
    verdict = simulate(
        _plan("org_networks", action="create", payload={
            "name": "guest", "vlan_id": 20, "subnet": "10.20.0.0/24",
        }),
        provider=provider,
    )
    assert verdict.decision is Decision.SAFE, verdict.decision_reasons
    assert len(verdict.config_diffs) == 1
    config_diff = verdict.config_diffs[0]
    assert config_diff.object_type == "org_networks"
    assert config_diff.object_id == "obj-1"
    assert config_diff.action == "create"
    assert {change.path for change in config_diff.changes} == {
        "name", "subnet", "vlan_id",
    }


def test_gateway_network_update_and_delete_include_field_diffs():
    current = {
        "id": "obj-1",
        "name": "corp",
        "vlan_id": 10,
        "subnet": "10.1.0.0/24",
    }
    provider = PolicyProvider(
        networks=OrgNetworksContext((current,)),
        relationships=ObjectRelationshipContext(
            current, (), ("org_gatewaytemplates",),
        ),
    )

    updated = simulate(
        _plan("org_networks", payload={"subnet": "10.3.0.0/24"}),
        provider=provider,
    )
    assert len(updated.config_diffs) == 1
    assert updated.config_diffs[0].action == "update"
    assert [change.path for change in updated.config_diffs[0].changes] == ["subnet"]

    deleted = simulate(
        _plan("org_networks", action="delete"),
        provider=provider,
    )
    assert len(deleted.config_diffs) == 1
    assert deleted.config_diffs[0].action == "delete"
    assert {change.path for change in deleted.config_diffs[0].changes} == {
        "name", "subnet", "vlan_id",
    }


def test_assigned_rf_template_band_disable_requires_review():
    context = ObjectRelationshipContext(
        target={
            "id": "obj-1",
            "name": "office-rf",
            "radio_config": {"band_5": {"disabled": False, "power": 18}},
        },
        references=(ObjectReference(
            source_type="org_sites", source_id="site-1", path="$.rftemplate_id"
        ),),
        checked_sources=("org_sites",),
    )
    verdict = simulate(
        _plan("org_rftemplates", payload={
            "radio_config": {"band_5": {"disabled": True, "power": 18}}
        }),
        provider=PolicyProvider(relationships=context),
    )
    assert verdict.decision is Decision.REVIEW
    assert verdict.findings[0].code == "wireless.rf_coverage_regression"


def test_unreferenced_rf_template_change_is_safe():
    context = ObjectRelationshipContext(
        target={"id": "obj-1", "radio_config": {"band_5": {"power": 18}}},
        references=(), checked_sources=("org_sites",),
    )
    verdict = simulate(
        _plan("org_rftemplates", payload={
            "radio_config": {"band_5": {"power": 15}}
        }),
        provider=PolicyProvider(relationships=context),
    )
    assert verdict.decision is Decision.SAFE


def test_service_policy_broad_any_any_permit_is_unsafe():
    context = ObjectRelationshipContext(
        target={"id": "obj-1", "policies": []},
        references=(), checked_sources=("org_gatewaytemplates",),
    )
    verdict = simulate(
        _plan("org_servicepolicies", payload={
            "policies": [{"id": "allow-all", "src": "any", "dst": "any",
                          "action": "allow"}]
        }),
        provider=PolicyProvider(relationships=context),
    )
    assert verdict.decision is Decision.UNSAFE
    assert verdict.findings[0].code == "security.service_policy_semantics"


def test_preexisting_service_policy_hazard_is_not_reintroduced_by_metadata_edit():
    policies = [{"id": "allow-all", "src": "any", "dst": "any", "action": "allow"}]
    context = ObjectRelationshipContext(
        target={"id": "obj-1", "name": "old", "policies": policies},
        references=(), checked_sources=("org_gatewaytemplates",),
    )
    verdict = simulate(
        _plan("org_servicepolicies", payload={"name": "new"}),
        provider=PolicyProvider(relationships=context),
    )
    assert verdict.decision is Decision.SAFE


def test_new_gateway_network_with_existing_subnet_is_unsafe():
    provider = PolicyProvider(networks=OrgNetworksContext((
        {"id": "n1", "name": "corp", "vlan_id": 10, "subnet": "10.10.0.0/24"},
    )))
    verdict = simulate(
        _plan("org_networks", action="create", payload={
            "name": "guest", "vlan_id": 20, "subnet": "10.10.0.128/25",
        }),
        provider=provider,
    )
    assert verdict.decision is Decision.UNSAFE
    assert verdict.findings[0].code == "config.org_network.conflict"
    assert verdict.findings[0].severity.value == "error"


def test_gateway_network_create_validates_configuration():
    provider = PolicyProvider(networks=OrgNetworksContext(()))
    verdict = simulate(
        _plan("org_networks", action="create", payload={
            "name": "", "vlan_id": 5000, "subnet": "not-a-cidr",
        }),
        provider=provider,
    )
    assert verdict.decision is Decision.REVIEW
    assert verdict.findings[0].code == "config.org_network.invalid"


def test_gateway_network_batch_detects_conflicts_between_new_entries():
    provider = PolicyProvider(networks=OrgNetworksContext(()))
    plan = _plan("org_networks", action="create", payload={
        "name": "one", "vlan_id": 10, "subnet": "10.30.0.0/24",
    })
    plan["ops"].append({
        "action": "create",
        "order": 1,
        "object_type": "org_networks",
        "object_id": "obj-2",
        "payload": {"name": "two", "vlan_id": 20, "subnet": "10.30.0.0/24"},
    })
    verdict = simulate(plan, provider=provider)
    assert verdict.decision is Decision.UNSAFE
    assert [r.check_id for r in verdict.check_results] == [
        "config.org_network.create", "config.org_network.conflict"
    ]


def test_gateway_network_partial_update_validates_effective_object():
    current = {"id": "obj-1", "name": "corp", "vlan_id": 10, "subnet": "10.1.0.0/24"}
    provider = PolicyProvider(
        networks=OrgNetworksContext((
            current,
            {"id": "n2", "name": "guest", "vlan_id": 20, "subnet": "10.2.0.0/24"},
        )),
        relationships=ObjectRelationshipContext(current, (), ("org_gatewaytemplates",)),
    )
    verdict = simulate(
        _plan("org_networks", payload={"subnet": "10.3.0.0/24"}), provider=provider
    )
    assert verdict.decision is Decision.SAFE, verdict.decision_reasons


def test_gateway_network_partial_update_rejects_effective_overlap():
    current = {"id": "obj-1", "name": "corp", "vlan_id": 10, "subnet": "10.1.0.0/24"}
    provider = PolicyProvider(
        networks=OrgNetworksContext((
            current,
            {"id": "n2", "name": "guest", "vlan_id": 20, "subnet": "10.2.0.0/24"},
        )),
        relationships=ObjectRelationshipContext(current, (), ("org_gatewaytemplates",)),
    )
    verdict = simulate(
        _plan("org_networks", payload={"subnet": "10.2.0.128/25"}), provider=provider
    )
    assert verdict.decision is Decision.UNSAFE
