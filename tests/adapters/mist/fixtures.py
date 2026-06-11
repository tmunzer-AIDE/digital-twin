"""Synthetic Mist-shaped payload builders shared across adapter tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from digital_twin.providers.base import RawSiteState, SiteScope, StateMeta

SITE_EFFECTIVE: dict[str, Any] = {
    "networks": {"corp": {"vlan_id": 10}, "voice": {"vlan_id": 30}},
    "port_usages": {
        "office": {"mode": "access", "port_network": "corp"},
        "uplink": {"mode": "trunk", "port_network": "corp", "networks": ["voice"]},
        "all": {"mode": "trunk", "all_networks": True},
    },
}

SWITCH_A: dict[str, Any] = {
    "mac": "aa0000000001",
    "id": "dev-a",
    "type": "switch",
    "model": "EX4100-48P",
    "name": "sw-a",
    "port_config": {
        "ge-0/0/0-1": {"usage": "office"},
        "ge-0/0/47": {"usage": "uplink"},
    },
    "other_ip_configs": {"corp": {"type": "static", "ip": "10.0.10.1", "netmask": "255.255.255.0"}},
}

AP_1: dict[str, Any] = {
    "mac": "cc0000000001",
    "id": "dev-ap1",
    "type": "ap",
    "model": "AP45",
    "name": "ap-1",
}

ALL_FETCHED = (
    "site",
    "setting",
    "networktemplate",
    "devices",
    "device_stats",
    "port_stats",
    "wireless_clients",
    "wired_clients",
    "wlans",
    "org_networks",
)


def raw_site(
    devices: tuple[dict[str, Any], ...] = (SWITCH_A, AP_1),
    port_stats: tuple[dict[str, Any], ...] = (),
    device_stats: tuple[dict[str, Any], ...] = (),
    wireless_clients: tuple[dict[str, Any], ...] = (),
    wired_clients: tuple[dict[str, Any], ...] = (),
    wlans: tuple[dict[str, Any], ...] = (),
    org_networks: tuple[dict[str, Any], ...] = (),
    fetched: tuple[str, ...] = ALL_FETCHED,
) -> RawSiteState:
    return RawSiteState(
        scope=SiteScope(org_id="o1", site_id="s1"),
        site={"id": "s1", "networktemplate_id": None},
        setting=SITE_EFFECTIVE,  # tests treat setting as already-effective
        networktemplate=None,
        devices=devices,
        device_stats=device_stats,
        port_stats=port_stats,
        wireless_clients=wireless_clients,
        wired_clients=wired_clients,
        wlans=wlans,
        org_networks=org_networks,
        derived_setting=None,
        meta=StateMeta(acquired_at=datetime.now(UTC), host="test", fetched=fetched, failures=()),
    )
