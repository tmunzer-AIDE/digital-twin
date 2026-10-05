"""Device renames vs Mist's name-based matchers (regression: pre-fetch false SAFE).

Mist selects device configuration by device NAME in three places:
`switch_matching` / `gateway_matching` rules (`match_name`, `match_name[a:b]`)
and dynamic port profiles that match a neighbor's `lldp_system_name`. A rename
that flips one of them swaps the matched rule wholesale (port_config, ip_config,
stp_config, ...), so a device rename is SAFE only when the twin proves no such
matcher changes outcome; anything it cannot prove floors to REVIEW.

The switch rule mirrors the TM-LAB "DNT-NTR" network template rule
"ex4100-f-12p" (`match_model[0:6]` + `match_name[3:9]`). Both rules below carry
the SAME port_config and differ only in ip_config (the management network) —
the part of a matched rule the switch compiler does not model.
"""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from digital_twin.engine.pipeline import simulate
from digital_twin.providers.base import (
    FetchError,
    FetchFailure,
    RawSiteState,
    SiteScope,
    StateMeta,
)
from digital_twin.verdict.decision import Decision

SITE = "s1"
SWITCH = {
    "id": "sw-1",
    "mac": "aa0000000001",
    "type": "switch",
    "model": "EX4100-F-12P",
    "role": "standalone",
    "name": "sw-ex4100-01",
}
GATEWAY = {
    "id": "gw-1",
    "mac": "bb0000000001",
    "type": "gateway",
    "model": "SRX300",
    "name": "gw-paris",
}
AP = {"id": "ap-1", "mac": "cc0000000001", "type": "ap", "model": "AP45", "name": "AP_lobby"}

NAME_RULE = {
    "name": "ex4100-f-12p",
    "match_model[0:6]": "EX4100",
    "match_name[3:9]": "ex4100",
    "match_role": "standalone",
    "ip_config": {"type": "dhcp", "network": "infra"},
    "port_config": {"xe-0/1/3": {"usage": "uplink"}},
}
DEFAULT_RULE = {
    "name": "default",
    "ip_config": {"type": "dhcp", "network": "mgt"},
    "port_config": {"xe-0/1/3": {"usage": "uplink"}},
}
SWITCH_LAYER = {
    "networks": {"mgt": {"vlan_id": 172}, "infra": {"vlan_id": 100}},
    "port_usages": {
        "uplink": {"mode": "trunk", "all_networks": True, "port_network": "mgt"},
    },
}


def _switch_matching(*rules):
    return {"enable": True, "rules": [*rules, DEFAULT_RULE]}


NETWORKTEMPLATE = {"id": "nt1", **SWITCH_LAYER, "switch_matching": _switch_matching(NAME_RULE)}


def _raw(
    *,
    devices=(SWITCH,),
    networktemplate=NETWORKTEMPLATE,
    setting=None,
    gatewaytemplate=None,
    failures=(),
    wlans=(),
    fetched=("site", "setting", "networktemplate", "devices", "wlans"),
) -> RawSiteState:
    site = {"id": SITE}
    if networktemplate is not None:
        site["networktemplate_id"] = "nt1"
    if gatewaytemplate is not None:
        site["gatewaytemplate_id"] = "gt1"
    return RawSiteState(
        scope=SiteScope(org_id="o1", site_id=SITE),
        site=site,
        setting=setting if setting is not None else {},
        networktemplate=networktemplate,
        devices=tuple(devices),
        device_stats=(),
        port_stats=(),
        wireless_clients=(),
        wired_clients=(),
        derived_setting=None,
        meta=StateMeta(
            acquired_at=datetime.now(UTC),
            host="t",
            fetched=tuple(fetched),
            failures=tuple(failures),
        ),
        gatewaytemplate=gatewaytemplate,
        wlans=tuple(wlans),
    )


class _Provider:
    def __init__(self, state):
        self.state = state
        self.fetches = 0

    def fetch_site(self, scope, *, include_derived=False):
        self.fetches += 1
        return self.state

    def fetch_sites(self, scope, site_ids=None, *, include_derived=False):
        self.fetches += 1
        return {SITE: self.state}


def _rename(device_id, name, *, site_id: str | None = SITE, object_type="device", extra=None):
    scope = {"org_id": "o1", **({"site_id": site_id} if site_id else {})}
    return {
        "source": "mist",
        "scope": scope,
        "ops": [
            {
                "action": "update",
                "order": 0,
                "object_type": object_type,
                "object_id": device_id,
                "payload": {"name": name, **(extra or {})},
            }
        ],
    }


def _codes(verdict):
    return {f.code for f in verdict.findings}


# --- switch_matching: the reported false SAFE ---------------------------------


@pytest.mark.parametrize(
    "layer",
    ["networktemplate", "site_setting", "site_setting.switch"],
)
def test_switch_rename_that_flips_a_match_name_rule_is_review(layer):
    # "sw-ex4100-01"[3:9] == "ex4100" matches rule ex4100-f-12p (ip_config infra);
    # "core-01"[3:9] == "e-01" falls through to `default` (ip_config mgt).
    if layer == "networktemplate":
        state = _raw()
    elif layer == "site_setting":
        state = _raw(
            networktemplate=None,
            setting={**SWITCH_LAYER, "switch_matching": _switch_matching(NAME_RULE)},
        )
    else:
        state = _raw(
            networktemplate=None,
            setting={**SWITCH_LAYER, "switch": {"switch_matching": _switch_matching(NAME_RULE)}},
        )

    v = simulate(_rename("sw-1", "core-01"), provider=_Provider(state))

    assert v.decision is Decision.REVIEW, v.decision_reasons
    finding = next(f for f in v.findings if f.code == "config.name_change.matcher")
    assert finding.subject is not None and finding.subject.id == "sw-1"
    assert "ex4100-f-12p" in finding.message
    assert "match_name[3:9]" in finding.message


def test_switch_rename_that_keeps_the_matched_rule_is_safe():
    # "sw-ex4100-02"[3:9] is still "ex4100": rule ex4100-f-12p keeps matching
    provider = _Provider(_raw())
    v = simulate(_rename("sw-1", "sw-ex4100-02"), provider=provider)

    assert v.decision is Decision.SAFE, v.decision_reasons
    assert v.check_results[0].check_id == "config.name_change"
    assert provider.fetches == 1  # the SAFE was earned against fetched templates


def test_switch_rename_without_name_rules_is_safe():
    # named after its model: a model criterion must never be read off the name
    model_only = {"name": "ex4100", "match_model[0:6]": "EX4100", "port_config": {}}
    state = _raw(
        devices=({**SWITCH, "name": "EX4100-lobby"},),
        networktemplate={"id": "nt1", "switch_matching": _switch_matching(model_only)},
    )
    v = simulate(_rename("sw-1", "core-01"), provider=_Provider(state))
    assert v.decision is Decision.SAFE, v.decision_reasons


@pytest.mark.parametrize(
    "rule",
    [
        # match case-semantics are unpinned: "sw-" vs "SW-" matches case-insensitively
        {"name": "case", "match_name[0:3]": "SW-", "port_config": {}},
        # a template variable in the value cannot be evaluated pre-resolution
        {"name": "var", "match_name[0:3]": "{{prefix}}", "port_config": {}},
        # the OAS also reads as comparing name[0:3] to the VALUE's own [0:3]
        {"name": "value-slice", "match_name[0:3]": "sw-ex4100", "port_config": {}},
        # an unknown criterion may read the name — never assumed name-independent
        {"name": "unknown", "match_hostname": "sw-ex4100-01", "port_config": {}},
    ],
    ids=["case-folding", "template-var", "value-slice-reading", "unknown-criterion"],
)
def test_unprovable_name_criteria_floor_to_review(rule):
    state = _raw(networktemplate={"id": "nt1", "switch_matching": _switch_matching(rule)})
    v = simulate(_rename("sw-1", "xx-ex4100-01"), provider=_Provider(state))
    assert v.decision is Decision.REVIEW, v.decision_reasons


def test_unreadable_switch_matching_rules_are_review():
    state = _raw(networktemplate={"id": "nt1", "switch_matching": {"enable": True,
                                                                  "rules": "oops"}})
    v = simulate(_rename("sw-1", "sw-ex4100-02"), provider=_Provider(state))
    assert v.decision is Decision.REVIEW, v.decision_reasons
    assert "config.name_change.unverified" in _codes(v)


def test_disabled_switch_matching_is_not_trusted_to_ignore_name_rules():
    # how layers merge `enable` is not modeled for this proof: rules count anyway
    sm = {**_switch_matching(NAME_RULE), "enable": False}
    state = _raw(networktemplate={"id": "nt1", "switch_matching": sm})
    v = simulate(_rename("sw-1", "core-01"), provider=_Provider(state))
    assert v.decision is Decision.REVIEW, v.decision_reasons


# --- evidence failure modes ---------------------------------------------------


def test_switch_rename_is_review_when_site_templates_cannot_be_fetched():
    error = FetchError(
        scope=SiteScope("o1", SITE),
        failures=(FetchFailure(object="networktemplate", error="HTTP 503"),),
        acquired_at=datetime.now(UTC),
        host="t",
    )
    v = simulate(_rename("sw-1", "sw-ex4100-02"), provider=_Provider(error))
    assert v.decision is Decision.REVIEW, v.decision_reasons
    assert "config.name_change.unverified" in _codes(v)


def test_rename_of_device_missing_from_fetched_inventory_is_review():
    state = _raw(devices=(), failures=(FetchFailure(object="devices", error="HTTP 500"),))
    v = simulate(_rename("sw-1", "sw-ex4100-02"), provider=_Provider(state))
    assert v.decision is Decision.REVIEW, v.decision_reasons
    assert "config.name_change.unverified" in _codes(v)


@pytest.mark.parametrize(
    ("layer", "device", "expected"),
    [
        ("networktemplate", SWITCH, Decision.REVIEW),
        ("sitetemplate", SWITCH, Decision.REVIEW),
        # switch templates hold the dynamic LLDP rules a gateway can trip
        ("networktemplate", GATEWAY, Decision.REVIEW),
        ("gatewaytemplate", GATEWAY, Decision.REVIEW),
        # a gateway template carries gateway_matching only: no switch/AP matcher
        ("gatewaytemplate", SWITCH, Decision.SAFE),
    ],
)
def test_assigned_template_absent_from_state_reviews_the_renames_it_governs(
    layer, device, expected
):
    # e.g. a replay fixture predating the layer: "None" means NOT FETCHED here
    state = _raw(devices=(device,), networktemplate=None)
    state = replace(state, site={**state.site, f"{layer}_id": "t1"})
    v = simulate(_rename(device["id"], "renamed-01"), provider=_Provider(state))
    assert v.decision is expected, v.decision_reasons
    if expected is Decision.REVIEW:
        assert "config.name_change.unverified" in _codes(v)


def test_device_rename_without_site_scope_is_review():
    provider = _Provider(_raw())
    v = simulate(_rename("sw-1", "sw-ex4100-02", site_id=None), provider=provider)
    assert v.decision is Decision.REVIEW, v.decision_reasons
    assert "config.name_change.unverified" in _codes(v)


def test_device_of_unknown_type_is_review():
    odd = {**SWITCH, "type": "mxedge"}
    v = simulate(_rename("sw-1", "sw-ex4100-02"), provider=_Provider(_raw(devices=(odd,))))
    assert v.decision is Decision.REVIEW, v.decision_reasons


def test_bridge_style_device_type_is_held_to_the_same_proof():
    v = simulate(
        _rename("sw-1", "core-01", object_type="site_devices"), provider=_Provider(_raw())
    )
    assert v.decision is Decision.REVIEW, v.decision_reasons


# --- gateways: gateway_matching -----------------------------------------------


@pytest.mark.parametrize(
    ("new_name", "expected"),
    [("par-gw", Decision.REVIEW), ("gw-lyon", Decision.SAFE)],
)
def test_gateway_rename_is_judged_against_gateway_matching(new_name, expected):
    gatewaytemplate = {
        "id": "gt1",
        "gateway_matching": {
            "enable": True,
            "rules": [
                {
                    "name": "branch-gw",
                    "match_name[0:3]": "gw-",
                    "port_config": {"ge-0/0/1": {"usage": "lan"}},
                },
            ],
        },
    }
    provider = _Provider(_raw(devices=(GATEWAY,), networktemplate=None,
                              gatewaytemplate=gatewaytemplate))
    v = simulate(_rename("gw-1", new_name), provider=provider)
    assert v.decision is expected, v.decision_reasons
    assert provider.fetches == 1


# --- APs: a neighbor switch's dynamic port profile on lldp_system_name --------


DYNAMIC_USAGES = {
    "aps": {"mode": "trunk", "all_networks": True, "port_network": "mgt"},
    "dyn": {
        "mode": "dynamic",
        "rules": [
            # name-independent sibling (as in DNT-NTR's own `dynamic` profile)
            {"src": "radius_dynamicfilter", "equals": "wireless", "usage": "aps"},
            {"src": "lldp_system_name", "expression": "[0:3]", "equals": "AP_",
             "usage": "aps"},
        ],
    },
}


@pytest.mark.parametrize("placement", ["site_setting", "networktemplate", "switch_device"])
@pytest.mark.parametrize(
    ("new_name", "expected"),
    [("lobby", Decision.REVIEW), ("AP_hall", Decision.SAFE)],
)
def test_ap_rename_is_judged_against_lldp_system_name_dynamic_rules(
    placement, new_name, expected
):
    switch, setting, template = SWITCH, None, NETWORKTEMPLATE
    if placement == "site_setting":
        setting = {"port_usages": DYNAMIC_USAGES}
    elif placement == "networktemplate":
        template = {**NETWORKTEMPLATE, "port_usages": DYNAMIC_USAGES}
    else:
        switch = {**SWITCH, "port_usages": DYNAMIC_USAGES}
    # the switch_matching name rule is present but never applies to an AP
    provider = _Provider(
        _raw(devices=(switch, AP), setting=setting, networktemplate=template)
    )
    v = simulate(_rename("ap-1", new_name), provider=provider)
    assert v.decision is expected, v.decision_reasons
    assert provider.fetches == 1


@pytest.mark.parametrize(
    "rule",
    [
        {"src": "lldp_neighbor_hostname", "equals": "AP_", "usage": "aps"},
        {"src": "lldp_system_name", "expression": "[0:3]", "equals": "{{ap_prefix}}",
         "usage": "aps"},
        {"src": "lldp_system_name", "expression": "regex(^AP)", "equals": "AP",
         "usage": "aps"},
    ],
    ids=["unknown-source", "template-var", "unparseable-expression"],
)
def test_unprovable_dynamic_rules_floor_an_ap_rename_to_review(rule):
    setting = {"port_usages": {"dyn": {"mode": "dynamic", "rules": [rule]}}}
    v = simulate(
        _rename("ap-1", "AP_hall"), provider=_Provider(_raw(devices=(SWITCH, AP), setting=setting))
    )
    assert v.decision is Decision.REVIEW, v.decision_reasons


# --- APs: WLAN DHCP option 82 carrying {{AP_NAME}} to an external DHCP server --


def _wlan(circuit_id, *, enabled=True):
    return {
        "id": "w1", "ssid": "corp", "enabled": True, "apply_to": "site",
        "inject_dhcp_option_82": {"enabled": enabled, "circuit_id": circuit_id},
    }


@pytest.mark.parametrize(
    ("wlan", "expected"),
    [
        (_wlan("{{AP_NAME}}:{{SSID}}"), Decision.REVIEW),
        (_wlan("{{AP_MAC}}:{{SSID}}"), Decision.SAFE),
        (_wlan("{{AP_NAME}}", enabled=False), Decision.SAFE),
    ],
    ids=["ap-name-injected", "mac-only", "option-82-disabled"],
)
def test_ap_rename_reviews_when_a_wlan_injects_the_ap_name_into_dhcp(wlan, expected):
    # the circuit id reaches a DHCP server the twin cannot see; if it keys
    # address or policy assignment on the AP name, clients move
    state = _raw(devices=(SWITCH, AP), wlans=(wlan,))
    v = simulate(_rename("ap-1", "AP_hall"), provider=_Provider(state))
    assert v.decision is expected, v.decision_reasons


@pytest.mark.parametrize(
    ("fetched", "failures"),
    [
        (("site", "setting", "networktemplate", "devices"), ()),
        (("site", "setting", "networktemplate", "devices"),
         (FetchFailure(object="wlans", error="HTTP 500"),)),
    ],
    ids=["not-fetched", "fetch-failed"],
)
def test_ap_rename_is_review_without_the_site_wlans(fetched, failures):
    state = _raw(devices=(SWITCH, AP), fetched=fetched, failures=failures)
    v = simulate(_rename("ap-1", "AP_hall"), provider=_Provider(state))
    assert v.decision is Decision.REVIEW, v.decision_reasons
    assert "config.name_change.unverified" in _codes(v)


def test_switch_rename_does_not_need_the_site_wlans():
    state = _raw(fetched=("site", "setting", "networktemplate", "devices"))
    v = simulate(_rename("sw-1", "sw-ex4100-02"), provider=_Provider(state))
    assert v.decision is Decision.SAFE, v.decision_reasons


# --- the normal pipeline: a rename mixed with other modeled fields ------------


def test_switch_update_mixing_a_rule_flipping_rename_is_review():
    # not a pure rename -> the full pipeline, whose compiler only consumes the
    # matched rule's port_config (identical here); the ip_config swap is a
    # blind spot that must floor the verdict
    v = simulate(
        _rename("sw-1", "core-01", extra={"notes": "moved to core"}),
        provider=_Provider(_raw()),
    )
    assert v.decision is not Decision.SAFE, v.decision_reasons
    assert "config.name_change.matcher" in _codes(v)


def test_switch_update_with_a_rule_preserving_rename_has_no_matcher_finding():
    v = simulate(
        _rename("sw-1", "sw-ex4100-02", extra={"notes": "relabel"}),
        provider=_Provider(_raw()),
    )
    assert "config.name_change.matcher" not in _codes(v)
    assert "config.name_change.unverified" not in _codes(v)


def test_proof_uses_the_renamed_device_only():
    # a second switch whose name matches the rule is untouched by this rename
    other = {**SWITCH, "id": "sw-2", "name": "sw-ex4100-99"}
    v = simulate(
        _rename("sw-1", "sw-ex4100-02"), provider=_Provider(_raw(devices=(SWITCH, other)))
    )
    assert v.decision is Decision.SAFE, v.decision_reasons


def test_rename_to_the_current_name_is_safe():
    flipped = replace(_raw(), devices=({**SWITCH, "name": "core-01"},))
    v = simulate(_rename("sw-1", "core-01"), provider=_Provider(flipped))
    assert v.decision is Decision.SAFE, v.decision_reasons
