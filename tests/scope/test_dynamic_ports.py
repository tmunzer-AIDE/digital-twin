"""Dynamic-port honesty gate (real-world false-SAFE, 2026-06-10).

Mist dynamic port profiles assign a port's usage AT RUNTIME (port_config entries
with `dynamic_usage`); the twin models such ports at their STATIC usage (M1
limit). So when a delta REDEFINES a `port_usages` entry or a `networks` vlan on
an object whose blast radius includes dynamic ports, the twin cannot bound the
impact — that must surface as a WARNING finding (-> REVIEW), never silent SAFE.
"""

from __future__ import annotations

from digital_twin.contracts import Severity
from digital_twin.scope.dynamic_ports import dynamic_profile_findings

SWITCH_CUR = {
    "type": "switch",
    "mac": "aa0000000001",
    "port_config": {
        "ge-0/0/19": {"usage": "ap"},
        "ge-0/0/30": {"usage": "default", "dynamic_usage": "dynamic"},
        "mge-0/0/19": {"usage": "ap", "dynamic_usage": "dynamic"},
    },
    "port_usages": {"ap": {"mode": "trunk", "all_networks": True}},
}


def _eff(**roots):
    return {**SWITCH_CUR, **roots}


def test_usage_redefinition_with_dynamic_ports_is_flagged():
    effective = _eff(port_usages={"ap": {"mode": "access", "port_network": "default"}})
    findings = dynamic_profile_findings("device", SWITCH_CUR, effective, devices=())
    assert len(findings) == 1
    f = findings[0]
    assert f.severity is Severity.WARNING
    assert f.code == "scope.dynamic_ports.unverifiable"
    assert "ge-0/0/30" in str(f.evidence)


def test_no_finding_when_usage_unchanged():
    effective = _eff(name="renamed")
    assert dynamic_profile_findings("device", SWITCH_CUR, effective, devices=()) == ()


def test_no_finding_without_dynamic_ports():
    cur = {**SWITCH_CUR, "port_config": {"ge-0/0/19": {"usage": "ap"}}}
    effective = {**cur, "port_usages": {"ap": {"mode": "access"}}}
    assert dynamic_profile_findings("device", cur, effective, devices=()) == ()


def test_site_setting_change_scans_all_devices():
    cur = {"networks": {"corp": {"vlan_id": 10}}}
    effective = {"networks": {"corp": {"vlan_id": 20}}}
    findings = dynamic_profile_findings(
        "site_setting", cur, effective, devices=(SWITCH_CUR, {"type": "ap", "mac": "cc01"})
    )
    assert len(findings) == 1
    assert "aa0000000001" in str(findings[0].evidence)


def test_networks_change_alone_triggers_on_dynamic_device():
    effective = _eff(networks={"guest": {"vlan_id": 2}})
    cur = {**SWITCH_CUR, "networks": {}}
    assert dynamic_profile_findings("device", cur, effective, devices=()) != ()
