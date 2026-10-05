"""Safety-boundary regressions from the October 5 Mist attribute review."""

from copy import deepcopy
from dataclasses import replace

import pytest

from digital_twin.adapters.mist.adapter import MistAdapter
from digital_twin.adapters.mist.apply.objects import effective_update
from digital_twin.engine.pipeline import simulate, simulate_org_template
from digital_twin.ir import IRCapability
from digital_twin.scope.derived_gate import check_derived
from digital_twin.scope.field_gate import screen_op
from digital_twin.verdict.decision import Decision
from tests.engine.test_org_pipeline import _FakeProvider, _site
from tests.engine.test_pipeline import (
    FakeProvider,
    _op,
    _plan,
    _raw,
    _raw_wlan,
    _wireless_client,
    _wlan,
)


def _metadata_raw():
    raw = _raw()
    return replace(raw, meta=replace(raw.meta, fetched=(
        "devices", "port_stats", "device_stats", "wireless_clients", "wired_clients", "wlans",
    )))


@pytest.mark.parametrize("object_type,tokens,before,after", [
    ("device", ("image1_url",), "https://example.test/old.png", "https://example.test/new.png"),
    ("device", ("image2_url",), "https://example.test/old.png", "https://example.test/new.png"),
    ("device", ("image3_url",), "https://example.test/old.png", "https://example.test/new.png"),
    ("device", ("local_port_config", "ge-0/0/0", "note"), "old", "new"),
    ("device", ("port_usages", "office", "description"), "old", "new"),
    ("site_setting", ("port_usages", "office", "description"), "old", "new"),
    ("site_setting", ("vars_annotations", "dhcp_ip", "note"), "old", "new"),
    ("site_setting", ("vars_annotations", "dhcp_ip", "type"), "generic", "mxtunnel_id"),
])
@pytest.mark.parametrize("action", ["add", "change", "remove"])
def test_cosmetic_add_change_remove_is_visible_and_safe(object_type, tokens, before, after, action):
    raw = _metadata_raw()
    current = deepcopy(dict(raw.devices[0] if object_type == "device" else raw.setting))
    if object_type == "device" and tokens[0] == "port_usages":
        current["port_usages"] = deepcopy(raw.setting["port_usages"])
    node = current
    for key in tokens[:-1]:
        node = node.setdefault(key, {})
    if tokens[0] == "local_port_config":
        node.setdefault("usage", "office")
    if action != "add":
        node[tokens[-1]] = before
    proposed = deepcopy(current)
    node = proposed
    for key in tokens[:-1]:
        node = node[key]
    if action == "remove":
        node.pop(tokens[-1])
    else:
        node[tokens[-1]] = after
    raw = (
        replace(raw, devices=(current,)) if object_type == "device"
        else replace(raw, setting=current)
    )
    oid = "dev-a" if object_type == "device" else raw.scope.site_id
    payload = {tokens[0]: proposed[tokens[0]]} if tokens[0] in proposed else {f"-{tokens[0]}": ""}
    verdict = simulate(_plan([_op(object_type, oid, payload)]), provider=FakeProvider(raw))
    assert verdict.decision is Decision.SAFE, verdict.decision_reasons
    assert verdict.ir_diff.is_empty()
    assert any(
        c.path == ".".join(tokens) for d in verdict.config_diffs for c in d.changes
    )


@pytest.mark.parametrize("object_type", ["networktemplate", "gatewaytemplate", "sitetemplate"])
def test_template_display_name_is_safe_with_assigned_site(object_type):
    old = {"id": "nt1", "name": "old"}
    site = _site("s1", setting={}, devices=(), nt=old if object_type == "networktemplate" else None)
    if object_type == "gatewaytemplate":
        site = replace(site, gatewaytemplate=old)
    if object_type == "sitetemplate":
        site = replace(site, sitetemplate=old)
    plan = {"source": "mist", "scope": {"org_id": "o1"}, "ops": [{
        "action": "update", "order": 0, "object_type": object_type,
        "object_id": "nt1", "payload": {"name": "new"},
    }]}
    verdict = simulate_org_template(plan, provider=_FakeProvider({"s1": site}, old))
    assert verdict.decision is Decision.SAFE, verdict.decision_reasons
    assert verdict.config_diffs[0].changes[0].path == "name"


@pytest.mark.parametrize("payload,path", [
    ({"port_usages": {"office.storm_control": {"percentage": 42}}}, "percentage"),
    ({"bgp_config": {"underlay": {"neighbors": {
        "10.0.0.2": {"future": {"neighbor_as": 65000}},
    }}}}, "future.neighbor_as"),
])
def test_dotted_keys_and_nested_neighbors_cannot_bypass_the_scope_boundary(payload, path):
    current = _metadata_raw().setting
    rejection = screen_op("site_setting", current, {**current, **payload})
    assert rejection is not None
    assert any(path in r for r in rejection.reasons)
    assert check_derived({}, payload) is not None
    verdict = simulate(_plan([_op(payload=payload)]), provider=FakeProvider(_metadata_raw()))
    assert verdict.decision is Decision.UNKNOWN
    assert any(path in r for r in verdict.decision_reasons)


def test_real_dotted_network_name_and_ip_key_are_single_map_tokens():
    payload = {"networks": {"corp.example": {"vlan_id": 10}}}
    assert screen_op("site_setting", {}, payload) is None
    assert check_derived({}, payload) is None
    payload = {"bgp_config": {"underlay": {"neighbors": {"10.0.0.2": {"neighbor_as": 65000}}}}}
    assert screen_op("site_setting", {}, payload) is None
    assert check_derived({}, payload) is None


@pytest.mark.parametrize("setting", [
    "ip_config", "stp_config", "port_mirroring",
])
def test_switch_rename_cannot_activate_an_uncompiled_matching_rule_setting(setting):
    raw = _metadata_raw()
    template = {"switch_matching": {"enable": True, "rules": [
        {"match_name": "sw-a", "port_config": {}, setting: {}},
        {"match_name": "sw-b", "port_config": {}, setting: {"new_option": True}},
    ]}}
    raw = replace(raw, networktemplate=template)
    verdict = simulate(
        _plan([_op("device", "dev-a", {"name": "sw-b"})]), provider=FakeProvider(raw),
    )
    assert verdict.decision is Decision.UNKNOWN
    assert any("switch_matching.selected." + setting in r for r in verdict.decision_reasons)


def test_switch_rename_with_only_known_port_rules_retains_normal_simulation():
    raw = replace(_metadata_raw(), networktemplate={"switch_matching": {"enable": True, "rules": [
        {"match_name": "sw-a", "port_config": {}},
        {"match_name": "sw-b", "port_config": {}},
    ]}})
    verdict = simulate(
        _plan([_op("device", "dev-a", {"name": "sw-b"})]), provider=FakeProvider(raw)
    )
    assert verdict.decision is Decision.SAFE


def test_switch_rename_with_unsupported_selector_cannot_assume_the_rule_misses():
    raw = replace(_metadata_raw(), networktemplate={"switch_matching": {"enable": True, "rules": [
        {"match_future": "something", "port_config": {}},
    ]}})
    verdict = simulate(
        _plan([_op("device", "dev-a", {"name": "sw-b"})]), provider=FakeProvider(raw)
    )
    assert verdict.decision is Decision.UNKNOWN
    assert any("unsupported match grammar" in r for r in verdict.decision_reasons)


def test_gateway_template_type_change_is_neither_preserved_nor_ignored():
    old = {"id": "nt1", "type": "srx"}
    assert effective_update(old, {"type": "ssr"}, object_type="gatewaytemplate")["type"] == "ssr"
    plan = {"source": "mist", "scope": {"org_id": "o1"}, "ops": [{
        "action": "update", "order": 0, "object_type": "gatewaytemplate",
        "object_id": "nt1", "payload": {"type": "ssr"},
    }]}
    verdict = simulate_org_template(plan, provider=_FakeProvider({}, old))
    assert verdict.decision is Decision.UNKNOWN
    assert any("type" in r for r in verdict.decision_reasons)
    assert verdict.config_diffs[0].changes[0].path == "type"


def test_nondevice_context_does_not_hide_device_only_metadata():
    assert screen_op("nacrule", {}, {"model": "future-filter"}) is not None
    assert screen_op("wlan", {}, {"connected": False}) is not None


def test_local_overwrite_activation_checks_nested_unmodeled_leaves():
    old = {"type": "switch", "port_config": {"ge-0/0/0": {"no_local_overwrite": True}},
           "local_port_config": {"ge-0/0/0": {"storm_control": {"future": True}}}}
    new = {**old, "port_config": {"ge-0/0/0": {"no_local_overwrite": False}}}
    rejection = screen_op("device", old, new)
    assert rejection is not None
    assert "storm_control.future" in rejection.reasons[0]


def test_conflicting_normalized_client_identity_is_a_coverage_gap():
    row = _wireless_client()
    raw = _raw_wlan(_wlan(), clients=(row, {**row, "mac": "112233445566", "ssid": "guest"}))
    outcome = MistAdapter().ingest(raw)
    assert outcome.ir is not None
    assert IRCapability.CLIENTS_ACTIVE not in outcome.ir.capabilities
    assert "conflicting duplicate identity" in outcome.ir.client_telemetry_gaps[0]


def test_identical_client_observations_remain_complete():
    row = _wireless_client()
    outcome = MistAdapter().ingest(
        _raw_wlan(_wlan(), clients=(row, {**row, "mac": "112233445566"}))
    )
    assert outcome.ir is not None
    assert IRCapability.CLIENTS_ACTIVE in outcome.ir.capabilities
    assert len(outcome.ir.clients) == 1


def test_a_conflicting_second_port_observation_cannot_certify_port_shutdown():
    raw = _metadata_raw()
    rows = tuple({"mac": "112233445566", "device_mac": raw.devices[0]["mac"],
                  "port_id": f"ge-0/0/{i}", "vlan": 10} for i in (0, 1))
    raw = replace(raw, wired_clients=rows)
    verdict = simulate(_plan([_op("device", "dev-a", {
        "port_config_overwrite": {"ge-0/0/1": {"disabled": True}},
    })]), provider=FakeProvider(raw))
    assert verdict.decision is not Decision.SAFE
    assert any("conflicting duplicate identity" in r for r in verdict.decision_reasons) or any(
        "conflicting duplicate identity" in note
        for result in verdict.check_results for note in result.coverage.notes
    )


def test_cosmetic_usage_description_does_not_taint_a_profiled_switch():
    raw = _metadata_raw()
    raw = replace(raw, devices=({**raw.devices[0], "deviceprofile_id": "p1"},))
    usages = deepcopy(raw.setting["port_usages"])
    usages["office"]["description"] = "Office desks"
    verdict = simulate(_plan([_op(payload={"port_usages": usages})]), provider=FakeProvider(raw))
    assert verdict.decision is Decision.SAFE, verdict.decision_reasons
    assert verdict.ir_diff.is_empty()


def test_cosmetic_edits_do_not_mask_an_independent_wireless_outage():
    raw = _raw_wlan(_wlan(), clients=(_wireless_client(),))
    verdict = simulate(_plan([
        _op("device", "dev-a", {"image2_url": "https://example.test/image.png"}),
        _op("wlan", "w1", {"enabled": False}, order=1),
    ]), provider=FakeProvider(raw))
    assert verdict.decision is Decision.UNSAFE
    assert any(f.code == "wireless.wlan.client_impact.coverage_lost" for f in verdict.findings)


@pytest.mark.parametrize("payload", [
    {"image2_url": 42},
    {"local_port_config": {"ge-0/0/0": {"note": False}}},
])
def test_cosmetic_exception_does_not_bypass_structural_validation(payload):
    verdict = simulate(
        _plan([_op("device", "dev-a", payload)]), provider=FakeProvider(_metadata_raw())
    )
    assert verdict.decision is not Decision.SAFE
    assert any(f.code == "l0.schema.violation" for f in verdict.findings)


def test_port_usage_assignment_cannot_activate_existing_opaque_qos_as_safe():
    raw = _metadata_raw()
    setting = deepcopy(raw.setting)
    setting["port_usages"]["new"] = {
        "mode": "access", "port_network": "corp", "qos": {"future": True},
    }
    raw = replace(raw, setting=setting)
    verdict = simulate(_plan([_op("device", "dev-a", {
        "port_config": {"ge-0/0/0-1": {"usage": "new"}},
    })]), provider=FakeProvider(raw))
    assert verdict.decision is Decision.UNKNOWN, verdict.decision_reasons
    assert any("port_usages.new.qos.future" in r for r in verdict.decision_reasons)


def test_cosmetic_change_remains_safe_with_preexisting_opaque_qos():
    raw = _metadata_raw()
    setting = deepcopy(raw.setting)
    setting["port_usages"]["office"]["qos"] = {"future": True}
    raw = replace(raw, setting=setting)
    proposed = deepcopy(setting["port_usages"])
    proposed["office"]["description"] = "Office desks"
    verdict = simulate(_plan([_op(payload={"port_usages": proposed})]), provider=FakeProvider(raw))
    assert verdict.decision is Decision.SAFE, verdict.decision_reasons
    assert verdict.ir_diff.is_empty()


def test_disabling_wlan_with_zero_observed_clients_requires_review():
    raw = _raw_wlan(_wlan())
    verdict = simulate(_plan([_op("wlan", "w1", {"enabled": False})]), provider=FakeProvider(raw))
    assert verdict.decision is Decision.REVIEW, verdict.decision_reasons
    assert any(f.evidence.get("reason") == "future_or_disconnected_clients_unverified"
               for f in verdict.findings)


def test_independent_proven_outage_still_wins_over_dependency_gap():
    raw = _raw_wlan(_wlan(), clients=(_wireless_client(),))
    setting = deepcopy(raw.setting)
    setting["port_usages"]["new"] = {
        "mode": "access", "port_network": "corp", "qos": {"future": True},
    }
    raw = replace(raw, setting=setting)
    verdict = simulate(_plan([
        _op("device", "dev-a", {"port_config": {"ge-0/0/0-1": {"usage": "new"}}}),
        _op("wlan", "w1", {"enabled": False}, order=1),
    ]), provider=FakeProvider(raw))
    assert verdict.decision is Decision.UNSAFE
    assert any(f.code == "coverage.gap" for f in verdict.findings)


def test_rename_with_dynamic_downstream_name_matching_is_unknown():
    from tests.scope.test_observation_gate import _linked_raw

    raw = _linked_raw(macs=True, dynamic=True)
    verdict = simulate(_plan([_op("device", "dev-b", {"name": "renamed"})]),
                       provider=FakeProvider(raw))
    assert verdict.decision is Decision.UNKNOWN, verdict.decision_reasons
    assert any("rename invalidates observed LLDP dynamic usage" in r
               for r in verdict.decision_reasons)


def test_rename_keeps_name_only_observed_topology_without_fabricating_an_outage():
    from tests.scope.test_observation_gate import _linked_raw

    raw = _linked_raw()
    verdict = simulate(_plan([_op("device", "dev-b", {"name": "renamed"})]),
                       provider=FakeProvider(raw))
    assert verdict.decision is Decision.SAFE, verdict.decision_reasons
    assert not verdict.ir_diff.touches("link")


def test_notes_only_edit_remains_safe_with_ambiguous_name_only_lldp():
    from tests.scope.test_observation_gate import _linked_raw

    raw = _linked_raw()
    raw = replace(raw, devices=(*raw.devices,
                  {**raw.devices[1], "id": "dev-c", "mac": "dd0000000003"}))
    verdict = simulate(_plan([_op("device", "dev-a", {"notes": "Reviewed label"})]),
                       provider=FakeProvider(raw))
    assert verdict.decision is Decision.SAFE, verdict.decision_reasons


def test_rename_with_noncanonical_neighbor_chassis_still_requires_unknown():
    from tests.scope.test_observation_gate import _linked_raw

    raw = _linked_raw(macs=True, dynamic=True)
    raw = replace(raw, port_stats=({**raw.port_stats[0], "neighbor_mac": "ee0000000009"},
                                   raw.port_stats[1]))
    verdict = simulate(_plan([_op("device", "dev-b", {"name": "renamed"})]),
                       provider=FakeProvider(raw))
    assert verdict.decision is Decision.UNKNOWN, verdict.decision_reasons


def test_forwarding_edit_with_ambiguous_name_only_lldp_still_requires_unknown():
    from tests.scope.test_observation_gate import _linked_raw

    raw = _linked_raw()
    raw = replace(raw, devices=(*raw.devices,
                  {**raw.devices[1], "id": "dev-c", "mac": "dd0000000003"}))
    verdict = simulate(_plan([_op("device", "dev-a", {
        "port_config": {"ge-0/0/0-1": {"usage": "office", "poe_disabled": True}},
    })]), provider=FakeProvider(raw))
    assert verdict.decision is Decision.UNKNOWN, verdict.decision_reasons
    assert any("ambiguous LLDP system name" in r for r in verdict.decision_reasons)


def test_local_only_port_usage_change_rescreens_existing_opaque_settings():
    raw = _metadata_raw()
    device = {**raw.devices[0], "port_config": {}, "local_port_config": {
        "ge-0/0/0": {"usage": "office", "enable_qos": True},
    }}
    setting = deepcopy(raw.setting)
    setting["port_usages"]["guest"] = deepcopy(setting["port_usages"]["office"])
    raw = replace(raw, devices=(device,), setting=setting)
    verdict = simulate(_plan([_op("device", "dev-a", {
        "local_port_config": {"ge-0/0/0": {"usage": "guest", "enable_qos": True}},
    })]), provider=FakeProvider(raw))
    assert verdict.decision is Decision.UNKNOWN, verdict.decision_reasons
    assert any("local_port_config.ge-0/0/0.enable_qos: unsupported dependency" in r
               for r in verdict.decision_reasons)


def test_dynamic_target_edit_rescreens_the_port_that_selects_it():
    raw = _metadata_raw()
    setting = deepcopy(raw.setting)
    setting["port_usages"]["guest"] = deepcopy(setting["port_usages"]["office"])
    setting["port_usages"]["dyn"] = {"mode": "dynamic", "rules": [
        {"src": "lldp_system_name", "equals": "peer", "usage": "office"},
    ]}
    device = {**raw.devices[0], "port_config": {"ge-0/0/0": {
        "usage": "guest", "dynamic_usage": "dyn", "enable_qos": True,
    }}}
    raw = replace(raw, setting=setting, devices=(device,), port_stats=({
        "mac": device["mac"], "port_id": "ge-0/0/0", "up": True,
        "neighbor_system_name": "peer",
    },))
    usages = deepcopy(setting["port_usages"])
    usages["office"]["poe_disabled"] = True
    verdict = simulate(_plan([_op(payload={"port_usages": usages})]), provider=FakeProvider(raw))
    assert verdict.decision is Decision.UNKNOWN, verdict.decision_reasons
    assert any("port_config.ge-0/0/0.enable_qos: unsupported dependency" in r
               for r in verdict.decision_reasons)
