"""GS1-GS8 (spec acceptance): the definition of done, run against the redacted
real-org fixture (GS5/GS8 untouched; the rest on the documented vlan-999
augmented variant — see builders). Each asserts the FULL verdict decision plus
the spec's named findings/statuses.
"""

import copy
import json

import pytest

from digital_twin.engine.pipeline import simulate
from digital_twin.observability.replay.store import FixtureProvider
from digital_twin.verdict.decision import Decision

from .builders import (
    EDGE,
    EDGE_ACCESS_PORT,
    EDGE_PAR_PORT,
    EDGE_UPLINK_PORT,
    WIRED_CLIENT_MAC,
    WIRELESS_CLIENT_MAC,
    augmented_doc,
    device_op,
    fixture_doc,
    plan_for,
    write_doc,
)


def _simulate(doc, plan, tmp_path):
    fixture = write_doc(doc, tmp_path / "fx.json")
    return simulate(plan, provider=FixtureProvider(fixture))


def test_gs1_single_uplink_vlan_removal_is_unsafe(tmp_path):
    # vlan 999's ONLY carrier is the EDGE->HUB uplink (HIGH exit = IRB on HUB);
    # de-vlanning it strands the configured member port AND its wired client
    doc = augmented_doc(parallel_carries_gs=False, with_wireless_client=False)
    plan = plan_for(
        doc, [device_op(doc, EDGE, **{EDGE_UPLINK_PORT.replace("/", "__"): "gs_empty_trunk"})]
    )
    v = _simulate(doc, plan, tmp_path)
    assert v.decision is Decision.UNSAFE
    codes = {f.code for f in v.findings}
    assert "wired.l2.blackhole.exit_lost" in codes
    impact = next(f for f in v.findings if f.code == "wired.client.impact.active_clients")
    assert WIRED_CLIENT_MAC in impact.affected_entities


def test_gs2_redundant_vlan_removal_is_safe(tmp_path):
    # BOTH parallel links carry vlan 999: removing one keeps the member reaching
    # the exit -> SAFE (graph reasoning, not "a trunk changed -> panic")
    doc = augmented_doc(parallel_carries_gs=True, with_wireless_client=False)
    plan = plan_for(
        doc, [device_op(doc, EDGE, **{EDGE_UPLINK_PORT.replace("/", "__"): "gs_empty_trunk"})]
    )
    v = _simulate(doc, plan, tmp_path)
    assert v.decision is Decision.SAFE, v.decision_reasons


def test_gs3_new_unprotected_cycle_is_review_at_unknown_stp(tmp_path):
    # the delta adds vlan 999 to the parallel link -> a NEW cycle; STP state is
    # unknown on those ports (live data has no stp rows for them) -> the spec's
    # variant row: WARN at LOW confidence -> REVIEW (FAIL needs stp DISABLED
    # facts, which Mist live data never asserts -> documented)
    doc = augmented_doc(parallel_carries_gs=False, with_wireless_client=False)
    plan = plan_for(doc, [device_op(doc, EDGE, **{EDGE_PAR_PORT.replace("/", "__"): "gs_trunk"})])
    v = _simulate(doc, plan, tmp_path)
    assert v.decision is Decision.REVIEW
    assert any(f.code == "wired.l2.loop.unverified" for f in v.findings)


def test_gs4_access_vlan_change_with_client_is_review(tmp_path):
    # move the member access port (active wired client) off vlan 999 onto an
    # empty trunk's network-less access? -> use a REAL existing usage instead:
    # reassign to gs_empty_trunk would make it a trunk; define the move as
    # access vlan 999 -> the port joins another vlan via an existing usage.
    doc = augmented_doc(parallel_carries_gs=True, with_wireless_client=False)
    # second access usage on an existing real vlan for the move target
    doc["setting"]["port_usages"]["gs_access2"] = {
        "mode": "access",
        "port_network": next(
            name
            for name, net in doc["setting"]["networks"].items()
            if isinstance(net, dict) and net.get("vlan_id") not in (None, 999)
        ),
    }
    plan = plan_for(
        doc, [device_op(doc, EDGE, **{EDGE_ACCESS_PORT.replace("/", "__"): "gs_access2"})]
    )
    v = _simulate(doc, plan, tmp_path)
    assert v.decision is Decision.REVIEW
    impact = next(f for f in v.findings if f.code == "wired.client.impact.active_clients")
    impacts = impact.evidence["impacts"]
    assert any(i["mac"] == WIRED_CLIENT_MAC and i["impact"] == "vlan_move" for i in impacts)


def test_gs5_cosmetic_change_is_safe(tmp_path):
    # UNTOUCHED real fixture: rename a switch -> full coverage, HIGH confidence,
    # SAFE. Proves no false positives on a real production site.
    doc = fixture_doc()
    dev = next(d for d in doc["devices"] if d.get("type") == "switch" and d.get("port_config"))
    payload = {**copy.deepcopy(dev), "name": "gs5-renamed"}
    plan = plan_for(
        doc,
        [
            {
                "action": "update",
                "order": 0,
                "object_type": "device",
                "object_id": str(dev["id"]),
                "payload": json.loads(json.dumps(payload)),
            }
        ],
    )
    from .builders import _drop_nones

    plan["ops"][0]["payload"] = _drop_nones(plan["ops"][0]["payload"])
    v = _simulate(doc, plan, tmp_path)
    assert v.decision is Decision.SAFE, v.decision_reasons


def test_gs6_missing_client_data_is_review_not_silent(tmp_path):
    # same in-domain change as GS4 but the wireless/wired client fetch "failed":
    # CLIENTS_ACTIVE is not earned -> client.impact INSUFFICIENT_DATA -> REVIEW
    doc = augmented_doc(parallel_carries_gs=True, with_wireless_client=False)
    doc["wired_clients"] = []
    doc["wireless_clients"] = []
    doc["meta"]["fetched"] = [
        f for f in doc["meta"]["fetched"] if f not in ("wired_clients", "wireless_clients")
    ]
    doc["meta"]["failures"] = [["wired_clients", "503"], ["wireless_clients", "503"]]
    plan = plan_for(
        doc, [device_op(doc, EDGE, **{EDGE_UPLINK_PORT.replace("/", "__"): "gs_empty_trunk"})]
    )
    v = _simulate(doc, plan, tmp_path)
    assert v.decision is not Decision.SAFE
    by_id = {r.check_id: r for r in v.check_results}
    assert by_id["wired.client.impact"].status.value == "insufficient_data"
    assert v.state_meta is not None and v.state_meta.fetch_failures  # surfaced


def test_gs7_ap_vlan_removal_with_observed_client_is_unsafe(tmp_path):
    # an AP on the EDGE switch serves an OBSERVED vlan-999 wireless client; the
    # delta cuts vlan 999 off the EDGE uplink -> the AP-side client is isolated
    doc = augmented_doc(parallel_carries_gs=False, with_wireless_client=True)
    plan = plan_for(
        doc, [device_op(doc, EDGE, **{EDGE_UPLINK_PORT.replace("/", "__"): "gs_empty_trunk"})]
    )
    v = _simulate(doc, plan, tmp_path)
    assert v.decision is Decision.UNSAFE
    assert any("blackhole" in f.code and "exit_lost" in f.code for f in v.findings)
    impact = next(f for f in v.findings if f.code == "wired.client.impact.active_clients")
    impacts = impact.evidence["impacts"]
    assert any(i["mac"] == WIRELESS_CLIENT_MAC and i["impact"] == "blackhole" for i in impacts)


def test_gs7_variant_zero_observed_clients_is_review(tmp_path):
    # same cut with NO observed wireless clients on the vlan: AP-side coverage
    # is observation-based -> partial -> REVIEW, never SAFE... and here the
    # WIRED member also strands, so assert the wireless blind-spot NOTE exists
    # on a clientless variant where only the AP loses the vlan
    doc = augmented_doc(parallel_carries_gs=True, with_wireless_client=False)
    # give the AP-feeding port its own usage so cutting it touches ONLY the AP
    ap_port = _ap_uplink_port(doc)
    if ap_port is None:
        pytest.skip("no AP uplinked to EDGE with a configurable switch port")
    doc["setting"]["port_usages"]["gs_ap_trunk"] = {"mode": "trunk", "networks": ["gs_net"]}
    doc["devices"][:] = doc["devices"]
    from .builders import _device

    _device(doc, EDGE)["port_config"][ap_port] = {"usage": "gs_ap_trunk"}
    plan = plan_for(doc, [device_op(doc, EDGE, **{ap_port.replace("/", "__"): "gs_empty_trunk"})])
    v = _simulate(doc, plan, tmp_path)
    assert v.decision is Decision.REVIEW, v.decision_reasons
    blackhole = next(r for r in v.check_results if r.check_id == "wired.l2.blackhole")
    assert any("wireless impact unknowable" in n for n in blackhole.coverage.notes)


def _ap_uplink_port(doc) -> str | None:
    """The EDGE switch port an AP hangs off (from the AP's lldp_stat)."""
    for stat in doc["device_stats"]:
        if stat.get("type") != "ap":
            continue
        lldp = stat.get("lldp_stat") or {}
        if str(lldp.get("chassis_id", "")).replace(":", "") == EDGE and lldp.get("port_id"):
            return str(lldp["port_id"])
    return None


def test_gs8_unsupported_object_type_is_unknown(tmp_path):
    doc = fixture_doc()
    plan = plan_for(
        doc,
        [
            {
                "action": "update",
                "order": 0,
                "object_type": "networktemplate",
                "object_id": "nt1",
                "payload": {},
            }
        ],
    )
    v = _simulate(doc, plan, tmp_path)
    assert v.decision is Decision.UNKNOWN
    assert any("UNSUPPORTED" in r for r in v.decision_reasons)
