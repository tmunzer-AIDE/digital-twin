"""Missing client observations must never prove a restrictive change safe."""

from dataclasses import replace

import pytest

from digital_twin.adapters.mist.adapter import MistAdapter
from digital_twin.checks.base import CoverageState
from digital_twin.engine.pipeline import simulate
from digital_twin.ir import IRCapability, diff_ir
from digital_twin.verdict.decision import Decision
from tests.engine.test_pipeline import (
    SWITCH,
    FakeProvider,
    _op,
    _plan,
    _raw,
    _raw_wlan,
    _wireless_client,
    _wlan,
    _wlan_registry,
)

_CLIENT = {"device_mac": SWITCH["mac"], "port_id": "ge-0/0/0", "mac": "c1", "vlan": 10}


def _wired_raw(clients):
    raw = _raw()
    switch = {**SWITCH, "port_config": {
        **SWITCH["port_config"],
        "ge-0/0/0": {"usage": "office", "no_local_overwrite": False},
    }}
    return replace(
        raw, devices=(switch,), wired_clients=tuple(clients),
        meta=replace(raw.meta, fetched=(
            "devices", "wired_clients", "wireless_clients", "port_stats", "device_stats", "wlans",
        )),
    )


@pytest.mark.parametrize("client", [
    {k: v for k, v in _CLIENT.items() if k != missing}
    for missing in ("mac", "device_mac", "port_id")
] + [
    {**_CLIENT, "device_mac": "ffffffffffff"},
    {**_CLIENT, "port_id": "ge-0/0/99"},
])
def test_restrictive_mac_limit_with_unattachable_client_requires_review(client):
    raw = _wired_raw([client])
    plan = _plan([_op(object_type="device", object_id="dev-a", payload={
        "local_port_config": {"ge-0/0/0": {"usage": "office", "mac_limit": 1}},
    })])
    verdict = simulate(plan, provider=FakeProvider(raw))
    assert verdict.decision is Decision.REVIEW
    result = next(r for r in verdict.check_results if r.check_id == "wired.port.mac_limit_exceeded")
    assert any(f.code.endswith(".unverified") for f in result.findings)
    assert result.coverage.state is CoverageState.PARTIAL
    assert result.coverage.notes
    impact = next(r for r in verdict.check_results if r.check_id == "wired.client.impact")
    assert any("client telemetry" in n for n in impact.coverage.notes)


@pytest.mark.parametrize("client", [
    {"mac": "c1"},
    {"ap_mac": "cc0000000001"},
    {"mac": "c1", "ap_mac": "ffffffffffff", "ssid": "corp"},
])
def test_wlan_disable_with_unattachable_client_requires_review(client):
    raw = _raw_wlan(_wlan(), clients=(client,))
    verdict = simulate(
        _plan([_op("wlan", "w1", {"enabled": False})]),
        provider=FakeProvider(raw), registry=_wlan_registry(),
    )
    assert verdict.decision is Decision.REVIEW
    assert any(f.code == "wireless.wlan.client_impact.unverified" for f in verdict.findings)


def test_valid_client_is_retained_when_other_rows_are_unattachable():
    outcome = MistAdapter().ingest(_wired_raw([_CLIENT, {"mac": "c2"}]))
    assert outcome.ir is not None
    assert outcome.report.ok
    assert [c.mac for c in outcome.ir.clients] == ["c1"]
    assert IRCapability.CLIENTS_ACTIVE not in outcome.ir.capabilities
    assert len(outcome.ir.client_telemetry_gaps) == 1


def test_gap_diagnostics_are_bounded_for_large_batches():
    outcome = MistAdapter().ingest(_wired_raw([{"mac": "unresolved"}] * 1000))
    assert outcome.ir is not None
    assert len(outcome.ir.client_telemetry_gaps) == 1
    assert "1000 row(s)" in outcome.ir.client_telemetry_gaps[0]
    assert "[0, 1, 2]" in outcome.ir.client_telemetry_gaps[0]


def test_partial_telemetry_retains_a_proven_mac_limit_exceedance():
    raw = _wired_raw([_CLIENT, {**_CLIENT, "mac": "c2"}, {"mac": "unresolved"}])
    verdict = simulate(_plan([_op("device", "dev-a", {
        "local_port_config": {"ge-0/0/0": {"usage": "office", "mac_limit": 1}},
    })]), provider=FakeProvider(raw))
    assert verdict.decision is Decision.REVIEW
    result = next(r for r in verdict.check_results if r.check_id == "wired.port.mac_limit_exceeded")
    assert any(f.code.endswith(".exceeded") for f in result.findings)
    assert result.coverage.state is CoverageState.PARTIAL


def test_partial_telemetry_retains_a_proven_wireless_outage():
    raw = _raw_wlan(_wlan(), clients=(_wireless_client(), {"mac": "unresolved"}))
    verdict = simulate(
        _plan([_op("wlan", "w1", {"enabled": False})]),
        provider=FakeProvider(raw), registry=_wlan_registry(),
    )
    assert verdict.decision is Decision.UNSAFE
    assert {f.code for f in verdict.findings} >= {
        "wireless.wlan.client_impact.coverage_lost", "wireless.wlan.client_impact.unverified",
    }
    assert verdict.check_results[0].coverage.state is CoverageState.PARTIAL


def test_empty_successful_telemetry_is_still_known():
    outcome = MistAdapter().ingest(_wired_raw([]))
    assert outcome.ir is not None
    assert IRCapability.CLIENTS_ACTIVE in outcome.ir.capabilities
    assert outcome.ir.client_telemetry_gaps == ()


def test_duplicate_valid_observation_does_not_create_a_gap():
    outcome = MistAdapter().ingest(_wired_raw([_CLIENT, _CLIENT]))
    assert outcome.ir is not None
    assert len(outcome.ir.clients) == 1
    assert IRCapability.CLIENTS_ACTIVE in outcome.ir.capabilities


def test_unattachable_clients_do_not_floor_a_noop():
    verdict = simulate(
        _plan([_op(payload={})]),
        provider=FakeProvider(_wired_raw([{"mac": "c1"}])),
    )
    assert verdict.decision is Decision.SAFE


def test_observation_gaps_are_not_configuration_deltas():
    raw = _raw_wlan(_wlan(), clients=(_wireless_client(),))
    baseline = MistAdapter().ingest(raw).ir
    assert baseline is not None
    proposed = replace(baseline, client_telemetry_gaps=("unresolved observation",))
    diff = diff_ir(baseline, proposed)
    assert not diff.added and not diff.removed and not diff.modified
