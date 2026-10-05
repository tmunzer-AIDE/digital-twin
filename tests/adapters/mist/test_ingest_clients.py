from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

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
    if row.get("last_device_mac") is not None or row.get("last_port_id") is not None:
        # A paired current sighting is authoritative; aggregated history and
        # last_* alone cannot establish which observed port is an edge port.
        row["device_mac_port"] = [{
            "device_mac": row.get("last_device_mac"),
            "port_id": row.get("last_port_id"), "vlan": row.get("last_vlan"),
        }]
    return {k: v for k, v in row.items() if v is not None}


def _ingest_caps(*, wired=(), wireless=(), port_stats=()):
    outcome = MistAdapter().ingest(raw_site(
        wired_clients=tuple(wired), wireless_clients=tuple(wireless), port_stats=tuple(port_stats),
    ))
    assert outcome.ir is not None, outcome.report
    return outcome.ir


def test_wired_search_uses_paired_current_attachment_and_latest_ip():
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
    assert IRCapability.CLIENTS_ACTIVE not in ir.capabilities
    assert ir.client_telemetry_gaps


def test_wireless_client_learned_on_its_ap_uplink_remains_complete():
    ir = _ingest_caps(wired=[_search_row()], wireless=[{
        "mac": "11:22:33:44:55:66", "ap_mac": AP_1["mac"], "ssid": "corp", "vlan_id": 30,
    }], port_stats=[{
        "mac": SWITCH_A["mac"], "port_id": "ge-0/0/0", "neighbor_mac": AP_1["mac"],
    }])
    (client,) = ir.clients
    assert client.kind is ClientKind.WIRELESS
    assert IRCapability.CLIENTS_ACTIVE in ir.capabilities
    assert not ir.client_telemetry_gaps


def test_direct_lldp_sighting_cannot_silently_lose_to_a_wireless_association():
    ir = _ingest_caps(wireless=[{
        "mac": "11:22:33:44:55:66", "ap_mac": AP_1["mac"], "ssid": "corp",
    }], port_stats=[{
        "mac": SWITCH_A["mac"], "port_id": "ge-0/0/0", "neighbor_mac": "112233445566",
    }])
    (client,) = ir.clients
    assert client.kind is ClientKind.WIRELESS
    assert IRCapability.CLIENTS_ACTIVE not in ir.capabilities
    assert any(
        "conflicting wired and wireless attachment" in gap for gap in ir.client_telemetry_gaps
    )


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


def test_inter_switch_transit_learning_alone_cannot_prove_a_direct_client():
    ir = _ingest_caps(wired=[_search_row()], port_stats=[{
        "mac": SWITCH_A["mac"], "port_id": "ge-0/0/0", "uplink": True,
    }])
    assert not ir.clients
    assert IRCapability.CLIENTS_ACTIVE not in ir.capabilities
    assert any("transit port without direct attachment" in gap for gap in ir.client_telemetry_gaps)


def test_ap_facing_learning_alone_does_not_invent_a_wired_client():
    ir = _ingest_caps(wired=[_search_row()], port_stats=[{
        "mac": SWITCH_A["mac"], "port_id": "ge-0/0/0", "neighbor_mac": AP_1["mac"],
    }])
    assert not ir.clients
    assert IRCapability.CLIENTS_ACTIVE in ir.capabilities
    assert not ir.client_telemetry_gaps


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


# Mist's wired-client SEARCH (searchSiteWiredClients / searchOrgWiredClients)
# returns ONE row per MAC with its MAC-table history aggregated: device_mac /
# port_id / vlan are de-duplicated LISTS that cannot be paired with each other,
# device_mac_port holds one record per (switch, port, vlan) sighting, and
# last_device_mac / last_port_id / last_vlan is merely the NEWEST sighting. A MAC
# is learned on every switch along its path, so most sightings are inter-switch
# or AP-facing ports. Shape taken from the recorded live response
# (tests/golden/fixtures/site.json).
SW_A, SW_B, AP = "aa0000000001", "bb0000000002", "cc0000000001"
SWITCH_B = {**SWITCH_A, "mac": SW_B, "id": "dev-b", "name": "sw-b"}
NOW = datetime(2026, 6, 10, 15, 45, tzinfo=UTC)
# SW_A:ge-0/0/47 <-> SW_B:ge-0/0/47 (two-sided LLDP); SW_A:ge-0/0/10 -> AP eth0.
# SW_A:ge-0/0/0-1 are its edge (office) ports.
TOPOLOGY = (
    {"mac": SW_A, "port_id": "ge-0/0/47", "up": True,
     "neighbor_mac": SW_B, "neighbor_port_desc": "ge-0/0/47"},
    {"mac": SW_B, "port_id": "ge-0/0/47", "up": True,
     "neighbor_mac": SW_A, "neighbor_port_desc": "ge-0/0/47"},
    {"mac": SW_A, "port_id": "ge-0/0/10", "up": True,
     "neighbor_mac": AP, "neighbor_port_desc": "eth0"},
)


def _seen(device, port, vlan, *, ago=timedelta(minutes=2)):
    """One device_mac_port record, every field the live response carries."""
    at = (NOW - ago).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
    return {"device_mac": device, "domain": "", "dynamic_filter": "", "ip": "", "ip6": "",
            "node": "", "port_id": port, "port_parent": "", "source": "mac", "start": at,
            "vlan": vlan, "when": at}


def _live_row(mac, *seen, ip=()):
    newest = max(seen, key=lambda s: s["when"])
    return {
        "mac": mac, "client_mac": "", "manufacture": "Unknown", "random_mac": False,
        "org_id": "o1", "site_id": "s1", "timestamp": NOW.timestamp(),
        "auth_method": "", "auth_state": "", "dhcp_client_options": [],
        "hostname": [], "username": [], "ip": list(ip), "ip6": [],
        "device_mac": sorted({s["device_mac"] for s in seen}),
        "port_id": sorted({s["port_id"] for s in seen}),
        "vlan": sorted({s["vlan"] for s in seen}),
        "device_mac_port": list(seen),
        "last_device_mac": newest["device_mac"], "last_port_id": newest["port_id"],
        "last_vlan": newest["vlan"],
    }


def _ingest_live(*rows: dict[str, Any], extra_port_stats: tuple[dict[str, Any], ...] = ()):
    raw = raw_site(devices=(SWITCH_A, SWITCH_B, AP_1),
                   port_stats=(*TOPOLOGY, *extra_port_stats), wired_clients=rows)
    ctx = IngestContext(
        raw=replace(raw, meta=replace(raw.meta, acquired_at=NOW)),
        site_effective=dict(SITE_EFFECTIVE),
        device_effective={SW_A: {**SITE_EFFECTIVE, **SWITCH_A},
                          SW_B: {**SITE_EFFECTIVE, **SWITCH_B}},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    LldpIngester().ingest(ctx)
    caps = ClientsIngester().ingest(ctx)
    return ctx.builder.build(), caps


def test_live_row_attaches_to_its_edge_port_not_its_newest_transit_sighting():
    # learned on its access port, then (newer) on the far switch's uplink: last_*
    # names the uplink, but the client is on the edge port
    ir, caps = _ingest_live(_live_row(
        "dca6321e2f86",
        _seen(SW_A, "ge-0/0/0", 10, ago=timedelta(minutes=3)),
        _seen(SW_B, "ge-0/0/47", 10, ago=timedelta(minutes=1)),
    ))
    (c,) = ir.clients
    assert c.attach_kind is AttachKind.PORT and c.attach_id == "aa0000000001:ge-0/0/0"
    assert c.vlan == 10
    assert IRCapability.CLIENTS_ACTIVE in caps and ir.client_telemetry_gaps == ()


def test_live_row_seen_on_two_edge_ports_is_a_conflicting_gap():
    ir, caps = _ingest_live(_live_row(
        "dca6321e2f86",
        _seen(SW_A, "ge-0/0/0", 10, ago=timedelta(minutes=3)),
        _seen(SW_A, "ge-0/0/1", 10, ago=timedelta(minutes=1)),
    ))
    assert ir.clients == ()
    assert IRCapability.CLIENTS_ACTIVE not in caps
    assert any("conflicting" in gap for gap in ir.client_telemetry_gaps)


def test_live_row_with_two_vlans_on_its_edge_port_is_a_conflicting_gap():
    ir, caps = _ingest_live(_live_row(
        "dca6321e2f86",
        _seen(SW_A, "ge-0/0/0", 10, ago=timedelta(minutes=3)),
        _seen(SW_A, "ge-0/0/0", 30, ago=timedelta(minutes=1)),
    ))
    assert ir.clients == ()
    assert IRCapability.CLIENTS_ACTIVE not in caps


def test_edge_sighting_older_than_the_search_window_is_history_not_a_conflict():
    # the client moved off ge-0/0/1 months ago; only ge-0/0/0 is current
    ir, caps = _ingest_live(_live_row(
        "dca6321e2f86",
        _seen(SW_A, "ge-0/0/1", 10, ago=timedelta(days=200)),
        _seen(SW_A, "ge-0/0/0", 10, ago=timedelta(minutes=2)),
    ))
    (c,) = ir.clients
    assert c.attach_id == "aa0000000001:ge-0/0/0"
    assert IRCapability.CLIENTS_ACTIVE in caps


def test_stale_edge_sighting_is_not_an_attachment():
    # its only edge sighting is months old; today it is seen on an uplink only
    ir, caps = _ingest_live(_live_row(
        "dca6321e2f86",
        _seen(SW_A, "ge-0/0/0", 10, ago=timedelta(days=200)),
        _seen(SW_B, "ge-0/0/47", 10, ago=timedelta(minutes=1)),
    ))
    assert ir.clients == ()
    assert IRCapability.CLIENTS_ACTIVE not in caps


def test_edge_sighting_from_hours_before_its_newest_sighting_is_history():
    # one report cycle apart is the same path; two hours apart, the MAC has left
    # ge-0/0/0 (and its new edge port is unobserved), even within the search day
    ir, caps = _ingest_live(_live_row(
        "dca6321e2f86",
        _seen(SW_A, "ge-0/0/0", 10, ago=timedelta(hours=2)),
        _seen(SW_B, "ge-0/0/47", 10, ago=timedelta(minutes=1)),
    ))
    assert ir.clients == ()
    assert IRCapability.CLIENTS_ACTIVE not in caps


def test_mac_learned_only_on_inter_switch_links_stays_a_gap():
    # the edge port is on a switch whose MAC table was not reported: unplaceable
    ir, caps = _ingest_live(_live_row(
        "dca6321e2f86",
        _seen(SW_A, "ge-0/0/47", 10, ago=timedelta(minutes=2)),
        _seen(SW_B, "ge-0/0/47", 10, ago=timedelta(minutes=1)),
    ))
    assert ir.clients == ()
    assert IRCapability.CLIENTS_ACTIVE not in caps
    assert any("inter-switch" in gap for gap in ir.client_telemetry_gaps)


def test_mac_learned_through_an_ap_is_not_a_wired_client_nor_a_gap():
    # frames enter the switch fabric from the AP: a wireless client (the wireless
    # stats are its authority), never a host on the AP-facing switch port
    ir, caps = _ingest_live(_live_row(
        "dca6321e2f86",
        _seen(SW_A, "ge-0/0/10", 10, ago=timedelta(minutes=2)),
        _seen(SW_B, "ge-0/0/47", 10, ago=timedelta(minutes=1)),
    ))
    assert ir.clients == ()
    assert IRCapability.CLIENTS_ACTIVE in caps and ir.client_telemetry_gaps == ()


def test_a_managed_devices_own_mac_is_not_a_client():
    ir, caps = _ingest_live(_live_row(SW_B, _seen(SW_A, "ge-0/0/47", 1)))
    assert ir.clients == ()
    assert IRCapability.CLIENTS_ACTIVE in caps and ir.client_telemetry_gaps == ()


def test_uplink_flagged_port_is_not_an_edge_candidate():
    # Mist flags ge-0/0/1 as an uplink (no LLDP link): a sighting there is transit
    ir, caps = _ingest_live(
        _live_row(
            "dca6321e2f86",
            _seen(SW_A, "ge-0/0/0", 10, ago=timedelta(minutes=3)),
            _seen(SW_A, "ge-0/0/1", 10, ago=timedelta(minutes=1)),
        ),
        extra_port_stats=({"mac": SW_A, "port_id": "ge-0/0/1", "up": True, "uplink": True},),
    )
    (c,) = ir.clients
    assert c.attach_id == "aa0000000001:ge-0/0/0"
    assert IRCapability.CLIENTS_ACTIVE in caps


def test_aggregate_interface_sighting_is_not_an_attachment():
    # LLDP links sit on the LAG members, so ae0 itself has no link: a MAC learned
    # on the bundle must not be pinned to it
    ir, caps = _ingest_live(
        _live_row("dca6321e2f86", _seen(SW_A, "ae0", 10)),
        extra_port_stats=({"mac": SW_A, "port_id": "ae0", "up": True},),
    )
    assert ir.clients == ()
    assert IRCapability.CLIENTS_ACTIVE not in caps


def test_sighting_time_without_an_offset_is_utc():
    edge = _seen(SW_A, "ge-0/0/0", 10, ago=timedelta(hours=2))
    edge["when"] = edge["when"].removesuffix("+0000")
    ir, caps = _ingest_live(_live_row(
        "dca6321e2f86", edge, _seen(SW_B, "ge-0/0/47", 10, ago=timedelta(minutes=1)),
    ))
    assert ir.clients == ()  # two hours behind the newest sighting: history
    assert IRCapability.CLIENTS_ACTIVE not in caps


def test_live_row_ip_list_becomes_one_ip_or_none():
    ir, _ = _ingest_live(
        _live_row("dca6321e2f86", _seen(SW_A, "ge-0/0/0", 10), ip=["198.51.100.7"]),
        _live_row("dca6321e2f87", _seen(SW_A, "ge-0/0/1", 10),
                  ip=["198.51.100.8", "198.51.100.9"]),
    )
    assert {c.mac: c.ip for c in ir.clients} == {
        "dca6321e2f86": "198.51.100.7", "dca6321e2f87": None,
    }


# Rows without per-sighting records (hand-written / older shapes): plain values
# are one sighting; several devices or ports cannot be paired, so cannot be placed.
def _ingest_history_caps(wired):
    ctx = IngestContext(
        raw=raw_site(wired_clients=tuple(wired)),
        site_effective=dict(SITE_EFFECTIVE),
        device_effective={"aa0000000001": {**SITE_EFFECTIVE, **SWITCH_A}},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    caps = ClientsIngester().ingest(ctx)
    return ctx.builder.build(), caps


def _list_row(**over):
    row = {"mac": "dca6321e2f86", "device_mac": ["aa0000000001"], "port_id": ["ge-0/0/1"],
           "vlan": [30]}
    row.update(over)
    return row


def test_one_entry_lists_are_one_sighting():
    ir, caps = _ingest_history_caps([_list_row()])
    (c,) = ir.clients
    assert c.attach_id == "aa0000000001:ge-0/0/1" and c.vlan == 30
    assert IRCapability.CLIENTS_ACTIVE in caps


def test_unpaired_multi_port_lists_stay_a_gap_even_with_last_fields():
    # last_* names one sighting; the others cannot be told edge from transit
    row = _list_row(device_mac=["aa0000000001", "bb0000000009"], port_id=["ge-0/0/1", "xe-0/1/3"],
                    vlan=[10, 30], last_device_mac="aa0000000001", last_port_id="ge-0/0/0",
                    last_vlan=10)
    ir, caps = _ingest_history_caps([row])
    assert ir.clients == ()
    assert IRCapability.CLIENTS_ACTIVE not in caps


def test_one_port_with_several_vlans_is_a_conflicting_gap():
    ir, caps = _ingest_history_caps([_list_row(vlan=[10, 30])])
    assert ir.clients == ()
    assert IRCapability.CLIENTS_ACTIVE not in caps


def test_sighting_on_an_unknown_switch_stays_a_gap():
    ir, caps = _ingest_history_caps([_list_row(device_mac=["ffffffffffff"])])
    assert ir.clients == ()
    assert IRCapability.CLIENTS_ACTIVE not in caps
    assert any("unknown port attachment" in gap for gap in ir.client_telemetry_gaps)
