"""Client evidence against captured Mist search and association responses."""

from collections import Counter

import pytest

from digital_twin.adapters.mist.adapter import MistAdapter
from digital_twin.adapters.mist.ingest.lldp import LldpIngester
from digital_twin.adapters.mist.ingest.switch import SwitchIngester
from digital_twin.engine.pipeline import simulate
from digital_twin.ir import ClientKind
from digital_twin.observability.replay.store import FixtureProvider, load_fixture_doc
from digital_twin.verdict.decision import Decision

from .builders import fixture_doc, plan_for, write_doc


@pytest.mark.parametrize("search_shape", ["captured", "current_scalars", "history_only"])
def test_captured_wireless_outage_survives_wired_search_overlap(search_shape, tmp_path):
    doc = fixture_doc()
    wireless_macs = {row["mac"] for row in doc["wireless_clients"]}
    wired_macs = {row["mac"] for row in doc["wired_clients"]}
    assert wireless_macs <= wired_macs
    ssid, count = Counter(row["ssid"] for row in doc["wireless_clients"]).most_common(1)[0]
    assert count == 11
    if search_shape == "current_scalars":
        # The separate search normalizer must not turn incidental learning
        # behind an AP or switch into a conflicting direct attachment.
        for row in doc["wired_clients"]:
            for key in ("device_mac", "port_id", "vlan", "ip"):
                row[key] = row.get(f"last_{key}")
    elif search_shape == "history_only":
        for row in doc["wired_clients"]:
            for key in ("last_device_mac", "last_port_id", "last_vlan", "last_ip"):
                row.pop(key, None)
    wlan = {
        "id": "captured-busy-ssid", "name": ssid, "ssid": ssid, "enabled": True,
        "for_site": True, "isolation": False, "apply_to": "site",
    }
    doc["wlans"] = [wlan]
    doc["meta"]["fetched"].append("wlans")
    raw = load_fixture_doc(doc)
    lldp_only = MistAdapter(ingesters=[SwitchIngester(), LldpIngester()]).ingest(raw).ir
    ir = MistAdapter().ingest(raw).ir
    assert ir is not None and lldp_only is not None
    clients = {client.mac: client for client in ir.clients}
    assert {c.mac for c in ir.clients if c.kind is ClientKind.WIRELESS} == wireless_macs
    # Physical neighbor sightings remain evidence even when their addresses
    # are also learned at another switch's uplink.
    assert len(lldp_only.clients) == 27
    for neighbor in lldp_only.clients:
        assert clients[neighbor.mac].attach_id == neighbor.attach_id
    for row in doc["wireless_clients"]:
        enrichment = ir.client_enrichment[row["mac"]]
        if row.get("family"):
            assert enrichment.family == row["family"]
        if row.get("hostname"):
            assert enrichment.hostname == row["hostname"]

    plan = plan_for(doc, [{
        "action": "update", "order": 0, "object_type": "wlan", "object_id": wlan["id"],
        "payload": {"enabled": False},
    }])
    verdict = simulate(plan, provider=FixtureProvider(write_doc(doc, tmp_path / "site.json")))
    assert verdict.decision is Decision.UNSAFE, verdict.decision_reasons
    proof = next(f for f in verdict.findings
                 if f.code == "wireless.wlan.client_impact.coverage_lost")
    assert set(proof.affected_entities) == {
        row["mac"] for row in doc["wireless_clients"] if row["ssid"] == ssid
    }
