from digital_twin.adapters.mist.ingest.base import IngestContext
from digital_twin.adapters.mist.ingest.clients import ClientsIngester
from digital_twin.adapters.mist.ingest.switch import SwitchIngester
from digital_twin.ir import AttachKind, IRBuilder, IRCapability
from tests.adapters.mist.fixtures import SITE_EFFECTIVE, SWITCH_A, raw_site


def _ingest(wireless=(), wired=()):
    ctx = IngestContext(
        raw=raw_site(wireless_clients=tuple(wireless), wired_clients=tuple(wired)),
        site_effective=dict(SITE_EFFECTIVE),
        device_effective={"aa0000000001": {**SITE_EFFECTIVE, **SWITCH_A}},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    ClientsIngester().ingest(ctx)
    return ctx.builder.build()


def test_wireless_client_attaches_to_ap_with_vlan():
    ir = _ingest(
        wireless=[{
            "mac": "11:22:33:44:55:66",
            "ap_mac": "cc0000000001",
            "vlan_id": 30,
            "ssid": "Corp",
        }]
    )
    c = ir.clients[0]
    assert c.attach_kind is AttachKind.AP and c.attach_id == "cc0000000001" and c.vlan == 30
    assert c.ssid == "Corp"


def test_wireless_client_blank_or_missing_ssid_becomes_none():
    ir = _ingest(
        wireless=[
            {"mac": "11:22:33:44:55:66", "ap_mac": "cc0000000001", "ssid": "   "},
            {"mac": "11:22:33:44:55:77", "ap_mac": "cc0000000001"},
        ]
    )
    assert [c.ssid for c in ir.clients] == [None, None]


def test_wired_client_attaches_to_port():
    ir = _ingest(
        wired=[
            {"mac": "667788990011", "device_mac": "aa0000000001", "port_id": "ge-0/0/0", "vlan": 10}
        ]
    )
    c = ir.clients[0]
    assert c.attach_kind is AttachKind.PORT and c.attach_id == "aa0000000001:ge-0/0/0"
    assert c.vlan == 10
    assert c.ssid is None


def test_client_referencing_unknown_attachment_is_skipped_not_fatal():
    ir = _ingest(wireless=[{"mac": "aa", "ap_mac": "ffffffffffff", "vlan_id": 1}])
    assert ir.clients == ()


def test_capability_earned_only_when_both_client_fetches_succeeded():
    ctx = IngestContext(
        raw=raw_site(fetched=("site", "setting", "devices", "wireless_clients")),  # wired missing
        site_effective=dict(SITE_EFFECTIVE),
        device_effective={},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    assert ClientsIngester().ingest(ctx) == frozenset()


def test_zero_clients_with_successful_fetches_still_earns_capability():
    ctx = IngestContext(
        raw=raw_site(),
        site_effective=dict(SITE_EFFECTIVE),
        device_effective={},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    assert IRCapability.CLIENTS_ACTIVE in ClientsIngester().ingest(ctx)


def test_produces_capability():
    assert IRCapability.CLIENTS_ACTIVE in ClientsIngester().produces()
