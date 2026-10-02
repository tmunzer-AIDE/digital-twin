"""Regressions for false positive verdicts reproduced by the full-stack audit."""

import pytest

from digital_twin.engine.pipeline import simulate, simulate_org_nac
from digital_twin.providers.base import NacFetch
from digital_twin.verdict.decision import Decision
from tests.engine.test_pipeline import (
    SITE,
    FakeProvider,
    _plan,
    _raw_wlan,
    _wireless_client,
    _wlan,
)
from tests.engine.test_simulate_org_nac import FakeProvider as NacProvider
from tests.engine.test_simulate_org_nac import _op as nac_op
from tests.engine.test_simulate_org_nac import _plan as nac_plan
from tests.engine.test_simulate_org_nac import _rule


@pytest.mark.parametrize("root", ["routing_policies", "evpn_options", "radio_config", "new_domain"])
def test_empty_unmodeled_configuration_domain_cannot_disappear(root):
    op = {
        "action": "update",
        "order": 0,
        "object_type": "site_setting",
        "object_id": SITE,
        "payload": {root: {}},
    }
    verdict = simulate(_plan([op]), provider=FakeProvider(_raw_wlan()))
    assert verdict.decision is Decision.UNKNOWN
    assert any(root in reason for reason in verdict.decision_reasons)


@pytest.mark.parametrize("old,new", [("eap", "psk"), ("eap", "open"), ("open", "eap")])
def test_wlan_authentication_transition_is_unresolved_even_with_clients_and_isolation(old, new):
    wlan = _wlan() | {"auth": {"type": old}, "isolation": True}
    raw = _raw_wlan(wlan, clients=(_wireless_client(),))
    op = {
        "action": "update",
        "order": 0,
        "object_type": "wlan",
        "object_id": "w1",
        "payload": {"auth": {"type": new}},
    }
    verdict = simulate(_plan([op]), provider=FakeProvider(raw))
    assert verdict.decision is Decision.UNKNOWN
    assert any("auth.type" in reason for reason in verdict.decision_reasons)


def test_unmodeled_empty_nac_filter_cannot_be_treated_as_a_catch_all():
    rule = _rule("a", 1)
    verdict = simulate_org_nac(
        nac_plan(nac_op("update", "a", {"matching": {"future_filter": {}}})),
        provider=NacProvider(NacFetch((rule,), ())),
    )
    assert verdict.decision is Decision.UNKNOWN
    assert any("future_filter" in reason for reason in verdict.decision_reasons)


@pytest.mark.parametrize("extra", [{"dry_run": True}, {"matching": {"future_filter": {}}}])
def test_unchanged_unmodeled_nac_rule_cannot_prove_a_later_rule_is_shadowed(extra):
    first = _rule("a", 1) | extra
    later = _rule("b", 2)
    verdict = simulate_org_nac(
        nac_plan(nac_op("update", "b", {"action": "block"})),
        provider=NacProvider(NacFetch((first, later), ())),
    )
    assert verdict.decision in (Decision.REVIEW, Decision.UNKNOWN)
    assert any(f.code == "nac.ingest.opaque" for f in verdict.adapter_findings)
    assert not any(
        f.code.startswith("nac.rule.shadowed.") and f.evidence["shadower"]["id"] == "a"
        for result in verdict.check_results
        for f in result.findings
    )


@pytest.mark.parametrize(
    "payload, blocked_path",
    [
        ({"ospf_config": {"reference_bandwidth": "100g"}}, "ospf_config.reference_bandwidth"),
        ({"ospf_config": {"import_policy": "reject-corp"}}, "ospf_config.import_policy"),
        ({"ospf_areas": {"0": {"type": "stub"}}}, "ospf_areas.0.type"),
        (
            {"ospf_areas": {"0": {"networks": {"corp": {"hello_interval": 10}}}}},
            "ospf_areas.0.networks.corp.hello_interval",
        ),
        (
            {"ospf_areas": {"0": {"networks": {"corp": {"auth_type": "md5"}}}}},
            "ospf_areas.0.networks.corp.auth_type",
        ),
        (
            {"bgp_config": {"underlay": {"networks": ["10.0.0.0/24"]}}},
            "bgp_config.underlay.networks",
        ),
        (
            {"bgp_config": {"underlay": {"import_policy": "reject-corp"}}},
            "bgp_config.underlay.import_policy",
        ),
        ({"bgp_config": {"underlay": {"hold_time": 30}}}, "bgp_config.underlay.hold_time"),
        (
            {"bgp_config": {"underlay": {"bfd_minimum_interval": 1000}}},
            "bgp_config.underlay.bfd_minimum_interval",
        ),
        (
            {"bgp_config": {"underlay": {"neighbors": {"10.0.0.2": {"multihop_ttl": 5}}}}},
            "bgp_config.underlay.neighbors.10.0.0.2.multihop_ttl",
        ),
        ({"evpn_config": {"enabled": True}}, "evpn_config.enabled"),
        ({"evpn_options": {"overlay": "ebgp"}}, "evpn_options.overlay"),
        ({"vrf_config": {"enabled": True}}, "vrf_config.enabled"),
        (
            {"routing_policies": {"reject-corp": {"terms": []}}},
            "routing_policies.reject-corp.terms",
        ),
    ],
)
def test_unmodeled_routing_and_fabric_changes_are_not_certified(payload, blocked_path):
    verdict = simulate(
        _plan(
            [
                {
                    "action": "update",
                    "order": 0,
                    "object_type": "device",
                    "object_id": "dev-a",
                    "payload": payload,
                }
            ]
        ),
        provider=FakeProvider(),
    )
    assert verdict.decision is Decision.UNKNOWN
    assert any(blocked_path in reason for reason in verdict.decision_reasons)
