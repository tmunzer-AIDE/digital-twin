from datetime import UTC, datetime

import pytest

from digital_twin.engine.pipeline import simulate
from digital_twin.providers.base import (
    FetchError,
    FetchFailure,
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
    ) -> None:
        self.sitegroup = sitegroup
        self.psk_usage = psk_usage
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


@pytest.mark.parametrize(
    "object_type",
    ["org_info", "org_alarmtemplates"],
)
def test_static_safe_configuration_types(object_type):
    verdict = simulate(_plan(object_type), provider=PolicyProvider())
    assert verdict.decision is Decision.SAFE


@pytest.mark.parametrize("object_type", ["org_webhooks", "site_webhooks"])
def test_webhook_changes_are_review_including_name_only(object_type):
    site_id = "s1" if object_type.startswith("site_") else None
    verdict = simulate(
        _plan(object_type, site_id=site_id, payload={"name": "renamed"}),
        provider=PolicyProvider(),
    )
    assert verdict.decision is Decision.REVIEW
    assert verdict.findings[0].code == "config.webhook.review"


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


def test_unused_psk_update_is_safe_after_complete_seven_day_check():
    provider = PolicyProvider(
        psk_usage=PskUsageContext((), ("s1", "s2"), (), window_days=7)
    )
    verdict = simulate(_plan("org_psks"), provider=provider)
    assert verdict.decision is Decision.SAFE
    assert provider.psk_calls == [(OrgScope("o1"), "obj-1", 7)]


def test_recently_used_psk_delete_is_review():
    provider = PolicyProvider(
        psk_usage=PskUsageContext(("s2",), ("s1", "s2"), (), window_days=7)
    )
    verdict = simulate(_plan("org_psks", action="delete"), provider=provider)
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
