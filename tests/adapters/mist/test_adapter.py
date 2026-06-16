"""Tests for MistAdapter.ingest — gateway_effective map + materialization."""

from __future__ import annotations

from digital_twin.adapters.mist.adapter import MistAdapter
from digital_twin.ir import device_id
from tests.adapters.mist.fixtures import raw_site

# A minimal gateway device: the device itself has no port_config —
# the gatewaytemplate supplies it.
_GW_MAC = "cc0000000001"
_GATEWAY_DEV: dict = {
    "type": "gateway",
    "mac": _GW_MAC,
    "id": "00000000-0000-0000-2000-cc0000000001",
    "ip_configs": {"corp": {"type": "static", "ip": "10.0.0.1"}},
}

# Gatewaytemplate adds a LAN port that the device itself does NOT have.
_GATEWAYTEMPLATE: dict = {
    "port_config": {
        "ge-0/0/3": {"usage": "lan", "networks": ["corp"]},
        "ge-0/0/0": {"usage": "wan"},
    },
}

_ORG_NETWORKS = (
    {"name": "corp", "vlan_id": 10, "subnet": "10.0.0.0/24"},
)


def _raw_with_gateway(*, gatewaytemplate: dict | None = None):
    """Build a RawSiteState that contains a gateway device + optional gatewaytemplate."""
    return raw_site(
        devices=(_GATEWAY_DEV,),
        org_networks=_ORG_NETWORKS,
    )


def _raw_with_gateway_and_template():
    """Build a RawSiteState with gateway device AND a gatewaytemplate."""
    from datetime import UTC, datetime

    from digital_twin.providers.base import RawSiteState, SiteScope, StateMeta
    from tests.adapters.mist.fixtures import ALL_FETCHED, SITE_EFFECTIVE

    return RawSiteState(
        scope=SiteScope(org_id="o1", site_id="s1"),
        site={"id": "s1", "networktemplate_id": None},
        setting=SITE_EFFECTIVE,
        networktemplate=None,
        devices=(_GATEWAY_DEV,),
        device_stats=(),
        port_stats=(),
        wireless_clients=(),
        wired_clients=(),
        wlans=(),
        org_networks=_ORG_NETWORKS,
        derived_setting=None,
        sitetemplate=None,
        gatewaytemplate=_GATEWAYTEMPLATE,
        meta=StateMeta(
            acquired_at=datetime.now(UTC),
            host="test",
            fetched=ALL_FETCHED,
            failures=(),
        ),
    )


# ---------------------------------------------------------------------------
# Test (a): gateway_effective is a non-empty dict keyed by device_id(mac)
# ---------------------------------------------------------------------------


def test_gateway_effective_is_keyed_by_device_id():
    """IngestOutcome.gateway_effective contains the gateway device's mac key."""
    raw = _raw_with_gateway_and_template()
    out = MistAdapter().ingest(raw)
    gw_id = device_id(_GW_MAC)
    assert gw_id in out.gateway_effective, (
        f"Expected {gw_id!r} in gateway_effective, got {list(out.gateway_effective)}"
    )
    assert isinstance(out.gateway_effective[gw_id], dict)


# ---------------------------------------------------------------------------
# Test (b): gatewaytemplate port_config is reflected in the gateway IR
# ---------------------------------------------------------------------------


def test_gatewaytemplate_port_config_reflected_in_gateway_ir():
    """A port from the gatewaytemplate (absent on the raw device) must appear
    in the materialized IR — proving that compile_gateway_device was applied
    and the result was fed into the ingest rather than the raw device."""
    raw = _raw_with_gateway_and_template()
    out = MistAdapter().ingest(raw)
    assert out.ir is not None

    gw_id = device_id(_GW_MAC)

    # The gatewaytemplate supplies ge-0/0/3 (LAN) and ge-0/0/0 (WAN).
    # The device itself has NO port_config, so without materialization,
    # no ports for this gateway appear in the IR.
    lan_port_key = f"{gw_id}:ge-0/0/3"
    assert lan_port_key in out.ir.ports, (
        f"Expected gateway LAN port {lan_port_key!r} in IR ports "
        f"(materialization missing?). IR ports: {list(out.ir.ports)[:10]}"
    )

    # The effective map itself should carry the folded port_config.
    assert "ge-0/0/3" in out.gateway_effective[gw_id].get("port_config", {}), (
        f"gateway_effective port_config missing ge-0/0/3: "
        f"{out.gateway_effective[gw_id].get('port_config')}"
    )


# ---------------------------------------------------------------------------
# Test (c): gatewaytemplate.dhcpd_config is materialized into gateway device
# ---------------------------------------------------------------------------


def _raw_with_gateway_dhcpd():
    """RawSiteState with a gatewaytemplate that carries dhcpd_config AND a
    site_setting that also carries dhcpd_config (the latter must be excluded)."""
    from datetime import UTC, datetime

    from digital_twin.providers.base import RawSiteState, SiteScope, StateMeta
    from tests.adapters.mist.fixtures import ALL_FETCHED, SITE_EFFECTIVE

    gw_dev = {
        "type": "gateway",
        "mac": _GW_MAC,
        "id": "00000000-0000-0000-2000-cc0000000001",
    }
    gt_with_dhcp = {
        **_GATEWAYTEMPLATE,
        "dhcpd_config": {
            "gw_scope": {"type": "local", "ip_start": "10.100.0.10",
                         "ip_end": "10.100.0.99"},
        },
    }
    # site_setting also carries a dhcpd_config (switch/site namespace —
    # must NOT pollute the gateway effective)
    site_setting_with_dhcp = {
        **SITE_EFFECTIVE,
        "dhcpd_config": {
            "site_scope": {"type": "local", "ip_start": "192.168.1.10",
                           "ip_end": "192.168.1.99"},
        },
    }
    return RawSiteState(
        scope=SiteScope(org_id="o1", site_id="s1"),
        site={"id": "s1", "networktemplate_id": None},
        setting=site_setting_with_dhcp,
        networktemplate=None,
        devices=(gw_dev,),
        device_stats=(),
        port_stats=(),
        wireless_clients=(),
        wired_clients=(),
        wlans=(),
        org_networks=_ORG_NETWORKS,
        derived_setting=None,
        sitetemplate=None,
        gatewaytemplate=gt_with_dhcp,
        meta=StateMeta(
            acquired_at=datetime.now(UTC),
            host="test",
            fetched=ALL_FETCHED,
            failures=(),
        ),
    )


def test_gatewaytemplate_dhcpd_config_in_gateway_effective():
    """gatewaytemplate.dhcpd_config must appear in gateway_effective (it
    reaches the gateway), while site_setting.dhcpd_config must not."""
    raw = _raw_with_gateway_dhcpd()
    out = MistAdapter().ingest(raw)
    gw_id = device_id(_GW_MAC)

    dhcp = out.gateway_effective[gw_id].get("dhcpd_config", {})
    assert "gw_scope" in dhcp, (
        f"gatewaytemplate dhcpd_config scope missing from gateway_effective: {dhcp}"
    )
    assert "site_scope" not in dhcp, (
        f"site_setting.dhcpd_config must NOT reach gateway_effective: {dhcp}"
    )
