from copy import deepcopy
from dataclasses import replace

import pytest

from digital_twin.adapters.mist.adapter import MistAdapter
from digital_twin.ir import IRCapability, diff_ir
from digital_twin.scope.observation_gate import observation_gaps, proposed_observations
from tests.engine.test_pipeline import AP, _raw


def _observe(baseline, proposed, baseline_effective, *, requires_topology=True):
    bound = proposed_observations(baseline, proposed)
    outcome = MistAdapter().ingest(bound)
    return bound, observation_gaps(
        baseline, bound, baseline_effective, outcome.device_effective,
        requires_topology=requires_topology,
    )


def _linked_raw(*, macs=False, dynamic=False):
    raw = _raw()
    first = deepcopy(raw.devices[0])
    second = {**first, "id": "dev-b", "mac": "bb0000000002", "name": "sw-b"}
    rows = [
        {"mac": first["mac"], "port_id": "ge-0/0/0", "up": True,
         "neighbor_system_name": "sw-b", "neighbor_port_desc": "ge-0/0/0"},
        {"mac": second["mac"], "port_id": "ge-0/0/0", "up": True,
         "neighbor_system_name": "sw-a", "neighbor_port_desc": "ge-0/0/0"},
    ]
    if macs:
        rows[0]["neighbor_mac"] = second["mac"]
        rows[1]["neighbor_mac"] = first["mac"]
    setting = deepcopy(raw.setting)
    if dynamic:
        setting["port_usages"]["dyn"] = {
            "mode": "dynamic", "rules": [
                {"src": "lldp_system_name", "equals": "sw-b", "usage": "office"},
            ],
        }
        first["port_config"]["ge-0/0/0-1"]["dynamic_usage"] = "dyn"
    return replace(raw, devices=(first, second), port_stats=tuple(rows), setting=setting,
                   meta=replace(raw.meta, fetched=("devices", "port_stats", "device_stats")))


def test_name_only_lldp_stays_bound_to_baseline_identity_after_peer_rename():
    raw = _linked_raw()
    adapter = MistAdapter()
    baseline = adapter.ingest(raw)
    proposed = replace(raw, devices=(raw.devices[0], {**raw.devices[1], "name": "renamed"}))
    bound, gaps = _observe(raw, proposed, baseline.device_effective)
    outcome = adapter.ingest(bound)
    assert not gaps
    assert baseline.ir is not None and outcome.ir is not None
    assert len(baseline.ir.links) == len(outcome.ir.links) == 1
    assert baseline.ir.links == outcome.ir.links
    assert not diff_ir(baseline.ir, outcome.ir).touches("link")


def test_ap_name_only_lldp_uplink_stays_bound_to_baseline_switch_identity():
    raw = _raw()
    raw = replace(raw, devices=(*raw.devices, AP), device_stats=({
        "type": "ap", "mac": AP["mac"],
        "lldp_stat": {"system_name": "sw-a", "port_id": "ge-0/0/0"},
    },))
    adapter = MistAdapter()
    baseline = adapter.ingest(raw)
    proposed = replace(raw, devices=({**raw.devices[0], "name": "renamed"}, AP))
    bound, gaps = _observe(raw, proposed, baseline.device_effective)
    outcome = adapter.ingest(bound)
    assert not gaps
    assert baseline.ir is not None and outcome.ir is not None
    assert len(baseline.ir.links) == 1
    assert baseline.ir.links == outcome.ir.links


@pytest.mark.parametrize("macs", [False, True])
def test_dynamic_observation_is_invalidated_after_peer_rename(macs):
    raw = _linked_raw(macs=macs, dynamic=True)
    adapter = MistAdapter()
    baseline = adapter.ingest(raw)
    proposed = replace(raw, devices=(raw.devices[0], {**raw.devices[1], "name": "renamed"}))
    bound, gaps = _observe(raw, proposed, baseline.device_effective)
    assert gaps and any("proposed runtime profile is unverified" in r
                        for g in gaps for r in g.reasons)
    assert bound.port_stats[0]["_twin_dynamic_observation_stale"] is True
    outcome = adapter.ingest(bound)
    assert outcome.ir is not None
    assert outcome.ir.ports["aa0000000001:ge-0/0/0"].native_vlan is None
    assert bound.port_stats[0]["neighbor_system_name"] == "sw-b"


def test_duplicate_name_is_never_a_last_wins_lldp_identity():
    raw = _linked_raw()
    third = {**raw.devices[1], "id": "dev-c", "mac": "dd0000000003"}
    raw = replace(raw, devices=(*raw.devices, third))
    outcome = MistAdapter().ingest(raw)
    assert outcome.ir is not None
    assert IRCapability.L2_TOPOLOGY not in outcome.ir.capabilities
    # Reciprocal unambiguous observation may remain; no fabricated two-sided
    # link to the last device named sw-b is allowed.
    assert not any("dd0000000003" in link.a_port or "dd0000000003" in link.b_port
                   for link in outcome.ir.links)
    _, gaps = _observe(raw, raw, outcome.device_effective)
    assert any("ambiguous LLDP system name" in r for g in gaps for r in g.reasons)


def test_duplicate_names_do_not_taint_mac_based_observations():
    raw = _linked_raw(macs=True)
    raw = replace(raw, devices=(*raw.devices,
                  {**raw.devices[1], "id": "dev-c", "mac": "dd0000000003"}))
    outcome = MistAdapter().ingest(raw)
    assert outcome.ir is not None
    assert IRCapability.L2_TOPOLOGY in outcome.ir.capabilities
    _, gaps = _observe(raw, raw, outcome.device_effective)
    assert not gaps


def test_rename_to_an_existing_managed_name_requires_coverage():
    raw = _linked_raw(macs=True)
    baseline = MistAdapter().ingest(raw)
    proposed = replace(raw, devices=(raw.devices[0], {**raw.devices[1], "name": "sw-a"}))
    _, gaps = _observe(raw, proposed, baseline.device_effective)
    assert any("rename creates an ambiguous managed device name" in r
               for g in gaps for r in g.reasons)


def test_peer_rename_invalidates_a_newly_activated_dynamic_profile_too():
    raw = _linked_raw(dynamic=True)
    # The definition exists, but baseline ports use only the static profile.
    baseline_device = deepcopy(raw.devices[0])
    baseline_device["port_config"]["ge-0/0/0-1"].pop("dynamic_usage")
    baseline_raw = replace(raw, devices=(baseline_device, raw.devices[1]))
    baseline = MistAdapter().ingest(baseline_raw)
    proposed = replace(raw, devices=(raw.devices[0], {**raw.devices[1], "name": "renamed"}))
    bound, gaps = _observe(baseline_raw, proposed, baseline.device_effective)
    assert gaps
    assert bound.port_stats[0]["_twin_dynamic_observation_stale"] is True


def test_peer_rename_expires_name_rules_when_chassis_mac_is_not_the_mist_mac():
    raw = _linked_raw(macs=True, dynamic=True)
    row = {**raw.port_stats[0], "neighbor_mac": "ee0000000009"}
    raw = replace(raw, port_stats=(row, raw.port_stats[1]))
    baseline = MistAdapter().ingest(raw)
    proposed = replace(raw, devices=(raw.devices[0], {**raw.devices[1], "name": "renamed"}))
    bound, gaps = _observe(raw, proposed, baseline.device_effective)
    assert gaps
    assert bound.port_stats[0]["_twin_dynamic_observation_stale"] is True


def test_ambiguous_lldp_identity_is_only_a_gap_for_topology_dependent_changes():
    raw = _linked_raw()
    raw = replace(raw, devices=(*raw.devices,
                  {**raw.devices[1], "id": "dev-c", "mac": "dd0000000003"}))
    outcome = MistAdapter().ingest(raw)
    _, gaps = _observe(raw, raw, outcome.device_effective, requires_topology=False)
    assert not gaps
