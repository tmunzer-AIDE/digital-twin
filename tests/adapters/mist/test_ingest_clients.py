import pytest

from digital_twin.adapters.mist.adapter import MistAdapter
from digital_twin.adapters.mist.ingest.base import IngestContext
from digital_twin.adapters.mist.ingest.client_enrichment import ClientEnrichmentIngester
from digital_twin.adapters.mist.ingest.clients import ClientsIngester
from digital_twin.adapters.mist.ingest.lldp import LldpIngester
from digital_twin.adapters.mist.ingest.switch import SwitchIngester
from digital_twin.ir import AttachKind, ClientKind, IRBuilder, IRCapability
from tests.adapters.mist.fixtures import AP_1, SITE_EFFECTIVE, SWITCH_A, raw_site


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


@pytest.mark.parametrize("field,first,second", [("vlan_id", 10, 20), ("ssid", "corp", "guest")])
def test_missing_initial_identity_does_not_hide_later_conflicting_values(field, first, second):
    row = {"mac": "112233445566", "ap_mac": "cc0000000001"}
    ir = _ingest(wireless=[row, {**row, field: first}, {**row, field: second}])
    assert not ir.clients
    assert any("conflicting duplicate identity" in reason for reason in ir.client_telemetry_gaps)


@pytest.mark.parametrize("port", ["ge-0/0/0", "ge-0/0/1"])
def test_telemetry_conflict_with_an_lldp_client_withdraws_the_initial_sighting(port):
    row = {"mac": "112233445566", "device_mac": SWITCH_A["mac"], "port_id": port, "vlan": 10}
    other_port = "ge-0/0/1" if port == "ge-0/0/0" else "ge-0/0/0"
    ctx = IngestContext(
        raw=raw_site(port_stats=({
            "mac": SWITCH_A["mac"], "port_id": other_port, "neighbor_mac": row["mac"],
        },), wired_clients=(row,)),
        site_effective=dict(SITE_EFFECTIVE),
        device_effective={"aa0000000001": {**SITE_EFFECTIVE, **SWITCH_A}},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    LldpIngester().ingest(ctx)
    assert ctx.builder.has_client(row["mac"])
    assert not ClientsIngester().ingest(ctx)
    assert not ctx.builder.has_client(row["mac"])
    assert not ctx.builder.build().clients


def _search_row(**over):
    row = {
        "mac": "112233445566",
        "device_mac": [SWITCH_A["mac"], "bb0000000009"],
        "port_id": ["ge-0/0/1", "xe-0/1/3"],
        "vlan": [10, 30],
        "ip": ["192.0.2.2", "192.0.2.3"],
        "last_device_mac": SWITCH_A["mac"],
        "last_port_id": "ge-0/0/0",
        "last_vlan": 10,
        "last_ip": "192.0.2.3",
    }
    row.update(over)
    return {k: v for k, v in row.items() if v is not None}


def _ingest_caps(*, wired=(), wireless=(), port_stats=()):
    outcome = MistAdapter().ingest(raw_site(
        wired_clients=tuple(wired), wireless_clients=tuple(wireless), port_stats=tuple(port_stats),
    ))
    assert outcome.ir is not None, outcome.report
    return outcome.ir


def test_wired_search_prefers_latest_attachment_vlan_and_ip_over_history():
    ir = _ingest_caps(wired=[_search_row()])
    (client,) = ir.clients
    assert client.attach_id == f"{SWITCH_A['mac']}:ge-0/0/0"
    assert client.vlan == 10 and client.ip == "192.0.2.3"
    assert IRCapability.CLIENTS_ACTIVE in ir.capabilities


def test_wired_search_singleton_history_is_unambiguous():
    row = _search_row(device_mac=[SWITCH_A["mac"]], port_id=["ge-0/0/1"], vlan=[30],
                      last_device_mac=None, last_port_id=None, last_vlan=None)
    ir = _ingest_caps(wired=[row])
    (client,) = ir.clients
    assert client.attach_id == f"{SWITCH_A['mac']}:ge-0/0/1" and client.vlan == 30
    assert IRCapability.CLIENTS_ACTIVE in ir.capabilities


@pytest.mark.parametrize("row", [
    _search_row(last_device_mac=None, last_port_id=None, last_vlan=None),
    _search_row(last_device_mac="ffffffffffff"),
    _search_row(device_mac=[SWITCH_A["mac"]], port_id=["ge-0/0/1"], last_port_id=None),
])
def test_wired_search_unknown_or_incomplete_current_attachment_stays_a_gap(row):
    ir = _ingest_caps(wired=[row])
    assert not ir.clients
    assert IRCapability.CLIENTS_ACTIVE not in ir.capabilities
    assert ir.client_telemetry_gaps


@pytest.mark.parametrize("last_port", ["ge-0/0/0", "ge-0/0/1"])
def test_wired_search_same_kind_conflicts_still_withdraw_the_client(last_port):
    other = "ge-0/0/1" if last_port == "ge-0/0/0" else "ge-0/0/0"
    ir = _ingest_caps(wired=[_search_row(last_port_id=last_port), _search_row(last_port_id=other)])
    assert not ir.clients
    assert any("conflicting duplicate identity" in gap for gap in ir.client_telemetry_gaps)


@pytest.mark.parametrize("wired", [
    [_search_row()],
    [_search_row(last_device_mac="ffffffffffff")],
    [{"mac": "112233445566"}],
    [_search_row(), _search_row(last_port_id="ge-0/0/1")],
])
def test_wired_duplicates_cannot_erase_a_wireless_association(wired):
    ir = _ingest_caps(wired=wired, wireless=[{
        "mac": "11:22:33:44:55:66", "ap_mac": AP_1["mac"], "ssid": "corp", "vlan_id": 30,
    }])
    (client,) = ir.clients
    assert client.kind is ClientKind.WIRELESS
    assert client.attach_id == AP_1["mac"] and client.ssid == "corp" and client.vlan == 30


def test_unattachable_wireless_row_cannot_erase_a_wired_attachment():
    ir = _ingest_caps(wired=[_search_row()], wireless=[{
        "mac": "11:22:33:44:55:66", "ap_mac": "ffffffffffff",
    }])
    (client,) = ir.clients
    assert client.kind is ClientKind.WIRED
    assert IRCapability.CLIENTS_ACTIVE not in ir.capabilities


@pytest.mark.parametrize("reverse", [False, True])
def test_transit_learning_and_direct_wired_attachment_are_consistent_in_either_order(reverse):
    rows = [_search_row(last_port_id="ge-0/0/0"), _search_row(last_port_id="ge-0/0/1")]
    ir = _ingest_caps(wired=rows[::-1] if reverse else rows, port_stats=[{
        "mac": SWITCH_A["mac"], "port_id": "ge-0/0/0", "neighbor_mac": AP_1["mac"],
    }])
    (client,) = ir.clients
    assert client.attach_id == f"{SWITCH_A['mac']}:ge-0/0/1"
    assert IRCapability.CLIENTS_ACTIVE in ir.capabilities
    assert not ir.client_telemetry_gaps


@pytest.mark.parametrize("port_stat", [
    {"neighbor_mac": AP_1["mac"]},
    {"uplink": True},
])
def test_transit_learning_alone_cannot_prove_a_direct_client(port_stat):
    ir = _ingest_caps(wired=[_search_row()], port_stats=[{
        "mac": SWITCH_A["mac"], "port_id": "ge-0/0/0", **port_stat,
    }])
    assert not ir.clients
    assert IRCapability.CLIENTS_ACTIVE not in ir.capabilities
    assert any("transit port without direct attachment" in gap for gap in ir.client_telemetry_gaps)


def test_coalescing_an_lldp_client_preserves_enrichment_even_when_it_runs_first():
    row = _search_row(last_hostname="edge-host")
    raw = raw_site(wired_clients=(row,), port_stats=({
        "mac": SWITCH_A["mac"], "port_id": "ge-0/0/0", "neighbor_mac": row["mac"],
    },))
    outcome = MistAdapter(ingesters=[
        SwitchIngester(), LldpIngester(), ClientEnrichmentIngester(), ClientsIngester(),
    ]).ingest(raw)
    assert outcome.ir is not None
    (client,) = outcome.ir.clients
    assert client.vlan == 10
    assert outcome.ir.client_enrichment[client.mac].hostname == "edge-host"
