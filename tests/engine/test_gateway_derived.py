"""Tests for Task 12: gateway effective screening in derived gate.

(a) Unit-tests for _gw_screen_view: full=True passes everything through;
    full=False projects to GATEWAY_SCREENED_ROOTS only.
(b) Integration: _simulate_site_state rejects an out-of-scope gateway leaf
    (ip_configs.*.netmask differs, netmask is NOT in GATEWAY_EFFECTIVE_ALLOWLIST)
    with a derived_gate UNKNOWN verdict.
"""

from __future__ import annotations

from datetime import UTC, datetime

from digital_twin.adapters.mist.adapter import MistAdapter
from digital_twin.checks.registry import CheckRegistry
from digital_twin.engine.pipeline import (
    GATEWAY_SCREENED_ROOTS,
    _gw_screen_view,
    _simulate_site_state,
)
from digital_twin.engine.run_context import RunContext
from digital_twin.providers.base import RawSiteState, SiteScope, StateMeta
from digital_twin.verdict.decision import Decision
from digital_twin.verdict.state_meta import build_state_meta

# ---------------------------------------------------------------------------
# (a) unit-tests for _gw_screen_view
# ---------------------------------------------------------------------------


def _eff_with_networks() -> dict:
    """A gateway effective that contains both screened and non-screened roots."""
    return {
        "port_config": {"ge-0/0/0": {"port_network": "corp"}},
        "ip_configs": {"corp": {"ip": "10.0.0.1", "netmask": "255.255.255.0"}},
        "dhcpd_config": {"corp": {"type": "local"}},
        "vars": {"some_var": "value"},
        "networks": {"corp": {"vlan_id": 10}},  # NOT in GATEWAY_SCREENED_ROOTS
    }


def test_gw_screen_view_full_true_returns_eff_unchanged():
    """full=True: EVERY key survives (including 'networks')."""
    eff = _eff_with_networks()
    result = _gw_screen_view(eff, full=True)
    assert result is eff  # same object, no copy


def test_gw_screen_view_full_false_drops_networks():
    """full=False: 'networks' is not in GATEWAY_SCREENED_ROOTS -> dropped."""
    eff = _eff_with_networks()
    result = _gw_screen_view(eff, full=False)
    assert "networks" not in result


def test_gw_screen_view_full_false_keeps_port_config():
    eff = _eff_with_networks()
    result = _gw_screen_view(eff, full=False)
    assert "port_config" in result


def test_gw_screen_view_full_false_keeps_ip_configs():
    eff = _eff_with_networks()
    result = _gw_screen_view(eff, full=False)
    assert "ip_configs" in result


def test_gw_screen_view_full_false_keeps_dhcpd_config():
    eff = _eff_with_networks()
    result = _gw_screen_view(eff, full=False)
    assert "dhcpd_config" in result


def test_gw_screen_view_full_false_keeps_vars():
    eff = _eff_with_networks()
    result = _gw_screen_view(eff, full=False)
    assert "vars" in result


def test_gw_screen_view_full_false_preserves_values():
    """The projected values are the same objects (not copies)."""
    eff = _eff_with_networks()
    result = _gw_screen_view(eff, full=False)
    for k in GATEWAY_SCREENED_ROOTS:
        if k in eff:
            assert result[k] is eff[k]


def test_gw_screen_view_full_false_missing_root_not_added():
    """Roots absent from eff are silently dropped (not added as empty)."""
    eff = {"port_config": {"ge-0/0/0": {"port_network": "corp"}}}
    result = _gw_screen_view(eff, full=False)
    assert set(result.keys()) == {"port_config"}


# ---------------------------------------------------------------------------
# (b) _simulate_site_state rejects out-of-scope gateway effective leaf
# ---------------------------------------------------------------------------


def _meta() -> StateMeta:
    return StateMeta(
        acquired_at=datetime.now(UTC),
        host="test",
        fetched=("site", "setting", "devices", "wireless_clients", "wired_clients"),
        failures=(),
    )


GATEWAY_MAC = "bb0000000001"
GATEWAY_ID = "gw-1"

# ip_configs.*.ip is in GATEWAY_EFFECTIVE_ALLOWLIST; netmask is NOT.
_GATEWAY_BASE = {
    "mac": GATEWAY_MAC,
    "id": GATEWAY_ID,
    "type": "gateway",
    "model": "SRX300",
    "ip_configs": {"corp": {"ip": "10.0.0.1", "netmask": "255.255.255.0"}},
}
_GATEWAY_PROP = {
    **_GATEWAY_BASE,
    "ip_configs": {"corp": {"ip": "10.0.0.1", "netmask": "255.255.254.0"}},  # netmask changed!
}


def _raw(gateway: dict) -> RawSiteState:
    return RawSiteState(
        scope=SiteScope(org_id="o1", site_id="s1"),
        site={"id": "s1"},
        setting={},  # minimal: no switch config needed
        networktemplate=None,
        devices=(gateway,),
        device_stats=(),
        port_stats=(),
        wireless_clients=(),
        wired_clients=(),
        derived_setting=None,
        meta=_meta(),
    )


def test_out_of_scope_gateway_leaf_rejected_as_unknown():
    """A gateway whose ip_configs.*.netmask differs (netmask is NOT in
    GATEWAY_EFFECTIVE_ALLOWLIST) -> derived_gate UNKNOWN."""
    baseline_raw = _raw(_GATEWAY_BASE)
    proposed_raw = _raw(_GATEWAY_PROP)

    adapter = MistAdapter()
    registry = CheckRegistry([])
    run = RunContext()
    sm = build_state_meta(_meta(), now=datetime.now(UTC))

    verdict = _simulate_site_state(
        baseline_raw, proposed_raw,
        adapter=adapter,
        registry=registry,
        run=run,
        state_meta=sm,
    )
    assert verdict.decision is Decision.UNKNOWN
    assert any("derived_gate" in r for r in verdict.decision_reasons), verdict.decision_reasons


def _raw_with_sitetemplate(sitetemplate: dict, devices: tuple = ()) -> RawSiteState:
    return RawSiteState(
        scope=SiteScope(org_id="o1", site_id="s1"),
        site={"id": "s1"},
        setting={"networks": {"corp": {"vlan_id": 10}}},
        networktemplate=None,
        devices=devices,
        device_stats=(),
        port_stats=(),
        wireless_clients=(),
        wired_clients=(),
        derived_setting=None,
        meta=_meta(),
        sitetemplate=sitetemplate,
    )


def test_sitetemplate_gateway_only_leaf_does_not_taint_switch_gate():
    """REGRESSION (PR #4 review P1, spec role-projection line 801): a sitetemplate
    edit to a family-distinct gateway-only leaf (ip_configs.*.ip) MUST NOT trip the
    switch/site derived gate. merge_site_effective folds the FULL sitetemplate into
    site_effective and fold_layers preserves the unknown `ip_configs` root, so the
    leaked root reaches the switch/site gate whose EFFECTIVE_ALLOWLIST does not list
    it (switch L3 is other_ip_configs) -> it false-UNKNOWNed before the
    _site_screen_view fix. The gateway namespace is owned by the gateway derived gate;
    with no gateway device here the edit affects nothing -> the switch verdict is
    unchanged (no derived_gate rejection attributable to the site config)."""
    # A switch is present so the switch IR / site gate path is genuinely exercised.
    switch = {"mac": "aa0000000099", "id": "sw-9", "type": "switch", "model": "EX4100-48P"}
    baseline_raw = _raw_with_sitetemplate(
        {"ip_configs": {"corp": {"ip": "10.0.0.1"}}}, devices=(switch,)
    )
    proposed_raw = _raw_with_sitetemplate(
        {"ip_configs": {"corp": {"ip": "10.0.0.2"}}}, devices=(switch,)
    )

    verdict = _simulate_site_state(
        baseline_raw, proposed_raw,
        adapter=MistAdapter(),
        registry=CheckRegistry([]),
        run=RunContext(),
        state_meta=build_state_meta(_meta(), now=datetime.now(UTC)),
    )
    assert not any(
        "derived_gate" in r and "site config" in r for r in verdict.decision_reasons
    ), verdict.decision_reasons


def test_in_scope_gateway_leaf_not_rejected():
    """A gateway whose ip_configs.*.ip differs (ip IS in GATEWAY_EFFECTIVE_ALLOWLIST)
    -> NOT rejected at derived_gate (may be SAFE or have check results)."""
    gateway_proposed = {
        **_GATEWAY_BASE,
        "ip_configs": {"corp": {"ip": "10.0.1.1", "netmask": "255.255.255.0"}},  # ip changed
    }
    baseline_raw = _raw(_GATEWAY_BASE)
    proposed_raw = _raw(gateway_proposed)

    adapter = MistAdapter()
    registry = CheckRegistry([])
    run = RunContext()
    sm = build_state_meta(_meta(), now=datetime.now(UTC))

    verdict = _simulate_site_state(
        baseline_raw, proposed_raw,
        adapter=adapter,
        registry=registry,
        run=run,
        state_meta=sm,
    )
    # Must NOT be a derived_gate rejection
    assert not any("derived_gate" in r for r in verdict.decision_reasons), verdict.decision_reasons
