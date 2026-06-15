"""GS1-GS8 (spec acceptance): the definition of done, run against the redacted
real-org fixture (GS5/GS8 untouched; the rest on the documented vlan-999
augmented variant — see builders). Each asserts the FULL verdict decision plus
the spec's named findings/statuses.
"""

import copy
import json

import pytest

from digital_twin.checks.base import CoverageState
from digital_twin.engine.pipeline import simulate, simulate_org_template
from digital_twin.observability.replay.store import FixtureProvider
from digital_twin.verdict.decision import Decision

from .builders import (
    EDGE,
    EDGE_ACCESS_PORT,
    EDGE_PAR_PORT,
    EDGE_UPLINK_PORT,
    OSPF_NETS,
    WIRED_CLIENT_MAC,
    WIRELESS_CLIENT_MAC,
    ap_devlan_doc,
    ap_unresolved_wlan_doc,
    ap_wlan_doc,
    augmented_doc,
    device_op,
    dynamic_ap_wlan_doc,
    fixture_doc,
    multisite_add_unused_vlan,
    multisite_remove_corp,
    multisite_template_with_no_assigned_sites,
    multisite_with_failed_site,
    ospf_doc,
    ospf_op,
    plan_for,
    write_doc,
)


def _simulate(doc, plan, tmp_path):
    fixture = write_doc(doc, tmp_path / "fx.json")
    return simulate(plan, provider=FixtureProvider(fixture))


def _simulate_org(doc, plan, tmp_path):
    fixture = write_doc(doc, tmp_path / "ms.json")
    return simulate_org_template(plan, provider=FixtureProvider(fixture))


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


def test_gs9_ap_uplink_loses_exitless_wlan_vlan_is_review(tmp_path):
    # An AP bridges a tagged WLAN data vlan (3001) that has NO IRB exit and NO
    # observed client. Flipping its uplink trunk->access drops 3001 -> the WLANs
    # there are blackholed, but the twin cannot see the WLAN config. Unlike
    # GS7-variant (the dropped vlan HAD an exit), here the vlan is exit-less, so
    # the old exit-gated blind-spot guard missed it and the verdict was SAFE.
    # It is a blind spot, not safe: floor to REVIEW with the AP blind-spot note.
    doc, op = ap_devlan_doc()
    plan = plan_for(doc, [op])
    v = _simulate(doc, plan, tmp_path)
    assert v.decision is Decision.REVIEW, v.decision_reasons
    blackhole = next(r for r in v.check_results if r.check_id == "wired.l2.blackhole")
    assert blackhole.coverage.state is not CoverageState.COMPLETE
    assert any("3001" in n for n in blackhole.coverage.notes), blackhole.coverage.notes


def test_gs10_ap_wlan_vlan_severed_with_exit_is_unsafe(tmp_path):
    # Fix B: a site WLAN (no observed clients) needs a tagged vlan on the AP's
    # uplink; the vlan has a local IRB. Flipping the uplink trunk->access drops
    # it -> the AP's WLAN is blackholed. KNOWN from config -> a real UNSAFE, not
    # just a coverage REVIEW (and idle/clientless WLANs are now covered).
    doc, op = ap_wlan_doc(wlan_vlan=3100, exit_for_wlan=True)
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.UNSAFE, v.decision_reasons
    assert any("blackhole" in f.code and "exit_lost" in f.code for f in v.findings)


def test_gs10_ap_wlan_vlan_severed_without_exit_is_review(tmp_path):
    # same, but the WLAN vlan is pure-L2 (no IRB): the twin can't assert an
    # egress, so the honest verdict is REVIEW (exit unlocatable), never SAFE.
    doc, op = ap_wlan_doc(wlan_vlan=3101, exit_for_wlan=False)
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.REVIEW, v.decision_reasons


def test_gs10_unresolvable_wlan_is_review_with_coverage_note(tmp_path):
    # a wxtag-scoped WLAN: the twin can't pin which APs/VLANs it needs. Severing
    # the AP's uplink with such a WLAN present must be REVIEW with an explicit
    # coverage note naming the unverifiable WLAN — never SAFE.
    doc, op = ap_unresolved_wlan_doc()
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.REVIEW, v.decision_reasons
    blackhole = next(r for r in v.check_results if r.check_id == "wired.l2.blackhole")
    assert any("guest" in n for n in blackhole.coverage.notes), blackhole.coverage.notes


def test_gs11_usage_redefinition_with_dynamic_ports_is_review(tmp_path):
    # the 2026-06-10 live false-SAFE: AP uplinks get usage 'ap' at RUNTIME via
    # dynamic port profiles; the model keeps static usages, so redefining the
    # 'ap' usage (trunk->access) is a topological no-op in the model while
    # blackholing real APs. Honest verdict: REVIEW with the dynamic-port note.
    doc = augmented_doc(parallel_carries_gs=True, with_wireless_client=False)
    from .builders import _device, _drop_nones

    edge = _device(doc, EDGE)
    edge["port_config"]["ge-0/0/30"] = {"usage": "default", "dynamic_usage": "dynamic"}
    # partial payload, real-use shape: keep every existing usage's attrs, flip
    # ONE usage's mode (root-level update replaces the port_usages root)
    usages = copy.deepcopy(_drop_nones(edge.get("port_usages") or {}))
    usages["gs_dyn_target"] = {"mode": "access", "port_network": "gs_net"}
    op = {
        "action": "update",
        "order": 0,
        "object_type": "device",
        "object_id": str(edge["id"]),
        "payload": {"type": "switch", "port_usages": usages},
    }
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.REVIEW, v.decision_reasons
    assert any(f.code == "scope.dynamic_ports.unverifiable" for f in v.findings)


def test_gs13_disabling_all_uplinks_is_unsafe(tmp_path):
    # the user's plan-05 case (2026-06-10): disabling a switch's uplink(s)
    # physically severs the switch AND everything on it (clients, APs, their
    # WLANs) from the rest of the network. No per-vlan exit reasoning needed —
    # the severance itself is provable at the severed links' confidence.
    doc = augmented_doc(parallel_carries_gs=True, with_wireless_client=False)
    plan = plan_for(
        doc,
        [
            device_op(
                doc,
                EDGE,
                **{
                    EDGE_UPLINK_PORT.replace("/", "__"): "disabled",
                    EDGE_PAR_PORT.replace("/", "__"): "disabled",
                },
            )
        ],
    )
    v = _simulate(doc, plan, tmp_path)
    assert v.decision is Decision.UNSAFE, v.decision_reasons
    assert any(f.code == "wired.l2.isolation.severed" for f in v.findings)


def test_gs13_variant_redundant_uplink_left_up_is_not_severed(tmp_path):
    # disabling ONE of two physical uplinks leaves the segment connected —
    # the isolation check must reason about the graph, not "an uplink died"
    doc = augmented_doc(parallel_carries_gs=True, with_wireless_client=False)
    plan = plan_for(
        doc, [device_op(doc, EDGE, **{EDGE_UPLINK_PORT.replace("/", "__"): "disabled"})]
    )
    v = _simulate(doc, plan, tmp_path)
    isolation = next(r for r in v.check_results if r.check_id == "wired.l2.isolation")
    assert isolation.status.value == "pass", isolation
    assert not any("isolation" in f.code for f in v.findings)


def test_gs14_resolved_dynamic_port_gets_a_precise_unsafe(tmp_path):
    # the original real-world case, fully modeled: the AP-feeding port runs
    # usage gs_ap_trunk at RUNTIME via a dynamic rule (lldp neighbor 'AP_*');
    # redefining that usage trunk->access severs the AP's WLAN vlan, which has
    # an IRB exit -> a real UNSAFE naming the loss — and NO blanket dynamic-
    # ports finding, because the runtime usage was resolved.
    doc, op = dynamic_ap_wlan_doc(with_stats_row=True)
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.UNSAFE, v.decision_reasons
    assert any("blackhole" in f.code and "exit_lost" in f.code for f in v.findings)
    assert not any("dynamic_ports" in f.code for f in v.findings)


def test_gs14_variant_unresolvable_dynamic_port_is_review(tmp_path):
    # same world WITHOUT the port-stats row: connected-or-not is unknowable ->
    # the unresolved-dynamic gate floors to REVIEW, never silent SAFE
    doc, op = dynamic_ap_wlan_doc(with_stats_row=False)
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.REVIEW, v.decision_reasons
    assert any(f.code == "scope.dynamic_ports.unverifiable" for f in v.findings)


def test_gs15_dynamic_rule_edit_is_in_scope_and_gets_a_real_verdict(tmp_path):
    # editing a dynamic profile's RULES is a modeled change (the runtime
    # resolver consumes them): re-pointing the matching rule away from the AP
    # de-trunks its resolved uplink -> the WLAN vlan (with IRB exit) is severed
    # -> UNSAFE, not UNKNOWN, not a blanket gate
    doc, _ = dynamic_ap_wlan_doc(with_stats_row=True)
    from .builders import _device, _drop_nones

    edge = _device(doc, EDGE)
    usages = copy.deepcopy(_drop_nones(edge.get("port_usages") or {}))
    usages["gs_dyn"] = {
        "mode": "dynamic",
        "rules": [
            {"src": "lldp_system_name", "expression": "[0:3]", "equals": "ZZ_",
             "usage": "gs_ap_trunk"}
        ],
    }
    op = {
        "action": "update",
        "order": 0,
        "object_type": "device",
        "object_id": str(edge["id"]),
        "payload": {"type": "switch", "port_usages": usages},
    }
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.UNSAFE, v.decision_reasons
    assert any("blackhole" in f.code and "exit_lost" in f.code for f in v.findings)


def test_gs16_cutting_poe_to_an_ap_is_unsafe(tmp_path):
    # the repeatedly-hit real case: poe_disabled on the switch port feeding an
    # AP (observed drawing power) kills the AP and its wireless clients. No
    # VLAN change — pure power loss -> UNSAFE naming the AP.
    doc = augmented_doc(parallel_carries_gs=True, with_wireless_client=True)
    from .builders import _device, ap_uplink_on

    ap_port = ap_uplink_on(doc, EDGE)[1]
    doc["port_stats"] = list(doc["port_stats"]) + [
        {"mac": EDGE, "port_id": ap_port, "up": True, "poe_on": True, "power_draw": 6.6}
    ]
    _device(doc, EDGE)  # ensure present
    doc["setting"]["port_usages"]["gs_ap_nopoe"] = {
        "mode": "trunk",
        "networks": ["gs_net"],
        "poe_disabled": True,
    }
    op = device_op(doc, EDGE, **{ap_port.replace("/", "__"): "gs_ap_nopoe"})
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.UNSAFE, v.decision_reasons
    f = next(f for f in v.findings if f.code == "wired.poe.disconnect.power_loss")
    assert f.evidence["affected_wireless_clients"] == 1


def test_gs17_poe_cut_with_unknown_powered_state_is_review(tmp_path):
    # review finding (2026-06-10): missing PoE telemetry must not read as "not
    # drawing power". Cutting PoE on a port that is UP but exposes no `poe_on`
    # stat (real rows lack it) -> the powered state is unknowable -> REVIEW,
    # never a silent PASS (a camera/phone could be on it).
    doc = augmented_doc(parallel_carries_gs=True)
    from .builders import _device

    _device(doc, EDGE)["port_config"]["ge-0/0/96"] = {"usage": "gs_poe_trunk"}
    doc["setting"]["port_usages"]["gs_poe_trunk"] = {"mode": "trunk", "networks": ["gs_net"]}
    doc["setting"]["port_usages"]["gs_nopoe_trunk"] = {
        "mode": "trunk",
        "networks": ["gs_net"],
        "poe_disabled": True,
    }
    doc["port_stats"] = list(doc["port_stats"]) + [
        {"mac": EDGE, "port_id": "ge-0/0/96", "up": True}  # no poe_on stat
    ]
    op = device_op(doc, EDGE, **{"ge-0__0__96": "gs_nopoe_trunk"})
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.REVIEW, v.decision_reasons
    f = next(f for f in v.findings if f.code == "wired.poe.disconnect.unverified")
    assert f.evidence["port"].endswith("ge-0/0/96")


def test_gs18_introduced_native_vlan_mismatch_is_unsafe(tmp_path):
    # both ends of the augmented parallel link get config natives (998) in the
    # baseline; the op moves ONE side's native to 997 -> untagged traffic now
    # crosses between vlan 998 and 997 on a HIGH two-sided link -> UNSAFE.
    from .builders import GS_NET, HUB, HUB_PAR_PORT, _device

    doc = augmented_doc(parallel_carries_gs=True)
    doc["setting"]["networks"]["gs_nat"] = {"vlan_id": 998}
    doc["setting"]["networks"]["gs_nat2"] = {"vlan_id": 997}
    doc["setting"]["port_usages"]["gs_nat_trunk"] = {
        "mode": "trunk", "networks": [GS_NET], "port_network": "gs_nat"
    }
    doc["setting"]["port_usages"]["gs_nat2_trunk"] = {
        "mode": "trunk", "networks": [GS_NET], "port_network": "gs_nat2"
    }
    _device(doc, EDGE)["port_config"][EDGE_PAR_PORT] = {"usage": "gs_nat_trunk"}
    _device(doc, HUB)["port_config"][HUB_PAR_PORT] = {"usage": "gs_nat_trunk"}
    op = device_op(doc, EDGE, **{EDGE_PAR_PORT.replace("/", "__"): "gs_nat2_trunk"})
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.UNSAFE, v.decision_reasons
    f = next(f for f in v.findings if f.code == "wired.l2.native_mismatch.introduced")
    assert {f.evidence["a_native"], f.evidence["b_native"]} == {997, 998}


def test_gs19_introduced_mtu_mismatch_is_unsafe(tmp_path):
    # both ends of the augmented parallel link run jumbo (9200) in the
    # baseline; the op drops ONE side to 1500 — frames over 1500 now silently
    # die on a HIGH two-sided link -> UNSAFE naming both MTUs.
    from .builders import GS_NET, HUB, HUB_PAR_PORT, _device

    doc = augmented_doc(parallel_carries_gs=True)
    doc["setting"]["port_usages"]["gs_jumbo"] = {
        "mode": "trunk", "networks": [GS_NET], "mtu": 9200
    }
    doc["setting"]["port_usages"]["gs_std"] = {
        "mode": "trunk", "networks": [GS_NET], "mtu": 1500
    }
    _device(doc, EDGE)["port_config"][EDGE_PAR_PORT] = {"usage": "gs_jumbo"}
    _device(doc, HUB)["port_config"][HUB_PAR_PORT] = {"usage": "gs_jumbo"}
    op = device_op(doc, EDGE, **{EDGE_PAR_PORT.replace("/", "__"): "gs_std"})
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.UNSAFE, v.decision_reasons
    f = next(f for f in v.findings if f.code == "wired.l2.mtu_mismatch.introduced")
    assert {f.evidence["a_mtu"], f.evidence["b_mtu"]} == {9200, 1500}


def test_gs19_variant_ap_uplink_mtu_change_is_review(tmp_path):
    # AP transparency is a VLAN property, not an MTU one: the AP end of the
    # uplink has an MTU we don't model, so changing the switch side to jumbo
    # is an unverifiable mismatch -> REVIEW, never a silent pass.
    from .builders import GS_NET, _device, ap_uplink_on

    doc = augmented_doc(parallel_carries_gs=True)
    ap_port = ap_uplink_on(doc, EDGE)[1]
    doc["setting"]["port_usages"]["gs_ap_jumbo"] = {
        "mode": "trunk", "networks": [GS_NET], "mtu": 9200
    }
    _device(doc, EDGE)  # ensure present
    op = device_op(doc, EDGE, **{ap_port.replace("/", "__"): "gs_ap_jumbo"})
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.REVIEW, v.decision_reasons
    f = next(f for f in v.findings if f.code == "wired.l2.mtu_mismatch.unverified")
    assert f.evidence["a_mtu"] == 9200


def test_gs21_bpdu_filter_on_an_uplink_is_unsafe(tmp_path):
    # MVP STP-BPDU: stp_disable (drop BPDUs) lands on a real switch-to-switch
    # uplink -> the port stops participating in loop protection exactly where
    # a loop would hurt -> UNSAFE.
    from .builders import GS_NET, _device

    doc = augmented_doc(parallel_carries_gs=True)
    doc["setting"]["port_usages"]["gs_nostp"] = {
        "mode": "trunk", "networks": [GS_NET], "stp_disable": True
    }
    _device(doc, EDGE)  # ensure present
    op = device_op(doc, EDGE, **{EDGE_PAR_PORT.replace("/", "__"): "gs_nostp"})
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.UNSAFE, v.decision_reasons
    f = next(f for f in v.findings if f.code == "wired.stp.edge_on_uplink.bpdu_filter")
    assert f.evidence["port"].endswith(EDGE_PAR_PORT)


def test_gs21_variant_bridge_priority_moves_the_root_is_review(tmp_path):
    # dropping EDGE's bridge priority to 4096 (everything else on the 32768
    # default) re-elects the root bridge -> reconvergence across the component
    from .builders import _device, _drop_nones

    doc = augmented_doc(parallel_carries_gs=True)
    dev = copy.deepcopy(_device(doc, EDGE))
    dev["stp_config"] = {"bridge_priority": "4096"}
    op = {
        "action": "update",
        "order": 0,
        "object_type": "device",
        "object_id": str(dev["id"]),
        "payload": _drop_nones(dev),
    }
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.REVIEW, v.decision_reasons
    f = next(f for f in v.findings if f.code == "wired.stp.root_change.moved")
    assert f.evidence["proposed_root"] == EDGE


def test_gs21_variant_invalid_bridge_priority_is_review(tmp_path):
    # the field is in scope and L0-valid (OAS types it as plain string), but
    # the VALUE is uninterpretable — simulating it as the default would be a
    # quiet false state. The adapter flags it -> REVIEW, never silence.
    from .builders import _device, _drop_nones

    doc = augmented_doc(parallel_carries_gs=True)
    dev = copy.deepcopy(_device(doc, EDGE))
    dev["stp_config"] = {"bridge_priority": "banana"}
    op = {
        "action": "update",
        "order": 0,
        "object_type": "device",
        "object_id": str(dev["id"]),
        "payload": _drop_nones(dev),
    }
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.REVIEW, v.decision_reasons
    f = next(f for f in v.findings if f.code == "scope.stp.bridge_priority_invalid")
    assert f.evidence["proposed"] == "banana"
    # the election ABSTAINS: no contradictory concrete root-move prediction
    assert not any(f.code == "wired.stp.root_change.moved" for f in v.findings)


def _gs22_removed_doc_and_op(*, gateway_namespace_fetched):
    # ROUTE-GW staging: a routed network whose only modeled L3 interface
    # (EDGE's IRB) is deleted by the op. With the gateway namespace FETCHED
    # (empty — the gs networks aren't gateway-attached) the SRX is modeled
    # and the removal is a confident break; UNFETCHED, the SRX could hold an
    # invisible replacement.
    from .builders import _device, _drop_nones

    doc = augmented_doc(parallel_carries_gs=True)
    if gateway_namespace_fetched:
        doc["org_networks"] = []
        doc["meta"]["fetched"] = list(doc["meta"]["fetched"]) + ["org_networks"]
    doc["setting"]["networks"]["gs_routed"] = {"vlan_id": 998, "subnet": "203.0.113.0/24"}
    _device(doc, EDGE).setdefault("other_ip_configs", {})["gs_routed"] = {
        "type": "static", "ip": "203.0.113.1", "netmask": "255.255.255.0"
    }
    dev = copy.deepcopy(_device(doc, EDGE))
    dev["other_ip_configs"] = {
        k: v for k, v in dev["other_ip_configs"].items() if k != "gs_routed"
    }
    op = {
        "action": "update",
        "order": 0,
        "object_type": "device",
        "object_id": str(dev["id"]),
        "payload": _drop_nones(dev),
    }
    return doc, op


def test_gs22_removing_the_irb_of_a_routed_network_is_unsafe(tmp_path):
    # no members needed: the config break itself is the harm -> UNSAFE
    doc, op = _gs22_removed_doc_and_op(gateway_namespace_fetched=True)
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.UNSAFE, v.decision_reasons
    f = next(f for f in v.findings if f.code == "wired.l3.gateway_gap.removed")
    assert f.evidence["vlan"] == 998


def test_gs22_variant_removed_with_blind_gateway_is_review(tmp_path):
    # the SRX's namespace was never fetched: the replacement L3 interface
    # could live there invisibly -> the removal claim caps at REVIEW
    doc, op = _gs22_removed_doc_and_op(gateway_namespace_fetched=False)
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.REVIEW, v.decision_reasons
    f = next(f for f in v.findings if f.code == "wired.l3.gateway_gap.removed")
    assert f.severity.value == "warning"


def test_gs22_variant_newly_routed_network_without_l3_is_review(tmp_path):
    # declaring routed intent for a network nothing serves: the L3 interface
    # could live on an unmodeled box -> REVIEW, never silence
    from .builders import _device, _drop_nones

    doc = augmented_doc(parallel_carries_gs=True)
    dev = copy.deepcopy(_device(doc, EDGE))
    dev.setdefault("networks", {})["gs_unserved"] = {
        "vlan_id": 997, "subnet": "203.0.113.0/24"
    }
    op = {
        "action": "update",
        "order": 0,
        "object_type": "device",
        "object_id": str(dev["id"]),
        "payload": _drop_nones(dev),
    }
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.REVIEW, v.decision_reasons
    f = next(f for f in v.findings if f.code == "wired.l3.gateway_gap.unserved")
    assert f.evidence["vlan"] == 997


def _gs24_doc(*, dhcp_net, gateway_namespace_fetched=True):
    doc = augmented_doc(parallel_carries_gs=True)
    if gateway_namespace_fetched:
        # the fixture SRX's own dhcpd entry (LD_VLAN2) must RESOLVE, or its
        # dhcp_unresolved flag honestly caps every removal claim at REVIEW
        doc["org_networks"] = [{"name": "LD_VLAN2", "vlan_id": 2}]
        doc["meta"]["fetched"] = list(doc["meta"]["fetched"]) + ["org_networks"]
    if dhcp_net != GS_NET_NAME:
        doc["setting"]["networks"][dhcp_net] = {"vlan_id": 997}
    doc["setting"]["dhcpd_config"] = {dhcp_net: {"type": "local"}}
    op = {
        "action": "update",
        "order": 0,
        "object_type": "site_setting",
        "object_id": doc["scope"]["site_id"],
        "payload": {"dhcpd_config": {dhcp_net: {"type": "none"}}},
    }
    return doc, op


GS_NET_NAME = "gs_net"


def test_gs24_removing_the_dhcp_path_of_a_client_vlan_is_unsafe(tmp_path):
    # vlan 999 has an observed wired client; the site-level DHCP server for it
    # is switched to type 'none' -> clients lose addressing at lease renewal
    doc, op = _gs24_doc(dhcp_net=GS_NET_NAME)
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.UNSAFE, v.decision_reasons
    f = next(f for f in v.findings if f.code == "wired.dhcp.path.removed")
    assert f.evidence["vlan"] == 999 and f.evidence["observed_clients"] >= 1


def test_gs24_variant_clientless_vlan_dhcp_removal_is_review(tmp_path):
    # same removal on a vlan with no observed clients: future joiners still
    # break -> REVIEW, not UNSAFE and never silence
    doc, op = _gs24_doc(dhcp_net="gs_dhcp_only")
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.REVIEW, v.decision_reasons
    f = next(f for f in v.findings if f.code == "wired.dhcp.path.removed")
    assert f.evidence["vlan"] == 997 and f.severity.value == "warning"


def _gs25_doc(*, stage_overlap_in_baseline=False):
    # org namespace staged fetched so the SRX's LD_VLAN2 dhcpd entry resolves
    # (same rationale as _gs24_doc). ALL network names the SRX's LAN port
    # references must resolve — one unresolvable name leaves the port
    # VLAN-BLIND, every edge toward the SRX is then carriage-ASSUMED (MEDIUM)
    # and the snooping check honestly abstains instead of reading trust.
    # parallel_carries_gs=False: the parallel-999 doc carries a PRE-EXISTING
    # cycle on carriage-assumed (MEDIUM) edges whose context caps the loop
    # check's result confidence below HIGH — that floors EVERY verdict at
    # REVIEW, making the SAFE expectations unreachable.
    doc = augmented_doc(parallel_carries_gs=False)
    doc["org_networks"] = [
        {"name": "LD_VLAN2", "vlan_id": 2},
        {"name": "LD_VLAN24", "vlan_id": 24},
        {"name": "IoTLAN", "vlan_id": 1003},
        {"name": "GuestLAN", "vlan_id": 1004},
        {"name": "VLAN-170-Rogue-DHCP", "vlan_id": 170},
        {"name": "default-vlan", "vlan_id": 1},
    ]
    doc["meta"]["fetched"] = list(doc["meta"]["fetched"]) + ["org_networks"]
    # the recorded site setting carries three pre-existing OAS violations
    # ('' day_of_week enums, a numeric flag the schema types as string); L0
    # validates the full EFFECTIVE object, so any site_setting op would floor
    # at REVIEW for faults the delta never touched — cleared per null==absent
    doc["setting"]["auto_upgrade"]["day_of_week"] = None
    doc["setting"]["flags"]["numStagingVbles"] = None
    doc["setting"]["gateway_mgmt"]["auto_signature_update"]["day_of_week"] = None
    if stage_overlap_in_baseline:
        doc["setting"].setdefault("networks", {})["gs25_pre"] = {"vlan_id": 995}
        doc["setting"].setdefault("dhcpd_config", {})["gs25_pre"] = {
            "type": "local", "ip_start": "198.51.100.10", "ip_end": "198.51.110.10",
        }
    return doc


def test_gs25a_introducing_an_overlapping_scope_is_review(tmp_path):
    # new site scope 198.51.100.10-110.10 overlaps the SRX's LD_VLAN2 range
    # (198.51.99.233-198.51.196.88) -> WARNING -> REVIEW. The vlan-996 network
    # is staged in BASELINE: site_setting updates REPLACE present roots, so a
    # networks payload would delete every existing network (and trip the
    # dynamic-port honesty gate) — the delta under test is the SCOPE only.
    doc = _gs25_doc()
    doc["setting"]["networks"]["gs25_net"] = {"vlan_id": 996}
    op = {
        "action": "update", "order": 0, "object_type": "site_setting",
        "object_id": doc["scope"]["site_id"],
        "payload": {
            "dhcpd_config": {"gs25_net": {
                "type": "local", "ip_start": "198.51.100.10",
                "ip_end": "198.51.110.10", "gateway": "198.51.100.1",
            }},
        },
    }
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.REVIEW, v.decision_reasons
    f = next(f for f in v.findings if f.code == "wired.dhcp.scope_lint.overlap")
    assert f.severity.value == "warning"


def test_gs25a_variant_preexisting_overlap_stays_safe_info(tmp_path):
    # the overlap already exists in baseline; the delta adds a NON-overlapping
    # scope -> the old collision is INFO context, never a verdict floor.
    # dhcpd_config is a REPLACED root: the payload merges the baseline map so
    # the pre-existing gs25_pre scope is not silently deleted by the op.
    doc = _gs25_doc(stage_overlap_in_baseline=True)
    doc["setting"]["networks"]["gs25_far"] = {"vlan_id": 994}
    op = {
        "action": "update", "order": 0, "object_type": "site_setting",
        "object_id": doc["scope"]["site_id"],
        "payload": {
            "dhcpd_config": {
                **doc["setting"]["dhcpd_config"],
                "gs25_far": {
                    "type": "local",
                    "ip_start": "198.51.200.10", "ip_end": "198.51.210.10",
                },
            },
        },
    }
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.SAFE, v.decision_reasons
    f = next(f for f in v.findings if f.code == "wired.dhcp.scope_lint.overlap")
    assert f.severity.value == "info"


def _gs25b_target(doc):
    """(switch_device_dict, gw_facing_port) derived from the fixture itself —
    robust to redaction re-captures. Also clears the switch's pre-existing
    INVALID remote_syslog.time_format '' (it violates the OAS enum, and L0
    validates the full EFFECTIVE object — any op on this switch would floor
    at REVIEW for a fault the delta never touched)."""
    gw_mac = next(d["mac"] for d in doc["devices"] if d.get("type") == "gateway")
    row = next(r for r in doc["port_stats"] if r.get("neighbor_mac") == gw_mac)
    sw = next(d for d in doc["devices"] if d.get("mac") == row["mac"])
    if (sw.get("remote_syslog") or {}).get("time_format") == "":
        sw["remote_syslog"]["time_format"] = None  # null == absent (canon)
    return sw, str(row["port_id"])


def test_gs25b_snooping_with_untrusted_uplink_is_review(tmp_path):
    # enable snooping for vlan2 on the gateway-facing switch AND explicitly
    # distrust the gateway-facing port (allow_dhcpd=false beats trunk):
    # the SRX is vlan 2's only modeled source -> offers drop -> REVIEW
    doc = _gs25_doc()
    sw, gw_port = _gs25b_target(doc)
    op = {
        "action": "update", "order": 0, "object_type": "device",
        "object_id": sw["id"],
        "payload": {
            "dhcp_snooping": {"enabled": True, "networks": ["vlan2"]},
            "local_port_config": {gw_port: {"allow_dhcpd": False}},
        },
    }
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.REVIEW, v.decision_reasons
    f = next(f for f in v.findings if f.code == "wired.dhcp.snooping.untrusted_path")
    assert f.evidence["vlan"] == 2


def test_gs25b_variant_trusted_uplink_is_safe(tmp_path):
    # same snooping enable WITHOUT distrusting the port: the gateway-facing
    # trunk is trusted by default -> one trusted path is enough -> SAFE
    doc = _gs25_doc()
    sw, _ = _gs25b_target(doc)
    op = {
        "action": "update", "order": 0, "object_type": "device",
        "object_id": sw["id"],
        "payload": {"dhcp_snooping": {"enabled": True, "networks": ["vlan2"]}},
    }
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.SAFE, v.decision_reasons
    assert not [f for f in v.findings if "snooping" in f.code]


def test_gs25b_variant_unknown_trust_is_review_via_partial_no_finding(tmp_path):
    # spec: blindness floors the verdict honestly (PARTIAL -> REVIEW) without
    # inventing a dropped-offer conclusion. Stage the gw-facing port's usage
    # to an UNDEFINED name in BASELINE (both sides equally blind -> peer-blind
    # suppressions hold); the only delta is the snooping enable.
    doc = _gs25_doc()
    sw, gw_port = _gs25b_target(doc)
    sw.setdefault("port_config", {})[gw_port] = {"usage": "gs25_undefined_usage"}
    op = {
        "action": "update", "order": 0, "object_type": "device",
        "object_id": sw["id"],
        "payload": {"dhcp_snooping": {"enabled": True, "networks": ["vlan2"]}},
    }
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.REVIEW, v.decision_reasons
    assert not [f for f in v.findings if f.code.endswith("untrusted_path")]
    # pin the MECHANISM: the REVIEW must come from the snooping check's
    # visible abstention, not some unrelated floor
    assert any("wired.dhcp.snooping" in r and "partial" in r for r in v.decision_reasons)


def _resolve_fixture_dynamic_ports(doc):
    """The recording leaves dynamically-profiled ports runtime-UNRESOLVED for
    two reasons orthogonal to GS22-GW: one dynamic rule keys on
    lldp_system_description (never observed -> every UP dynamic port is
    inconclusive) and some rule-assigned members have no port_stats row at
    all. Any networks-root op then trips scope.dynamic_ports.unverifiable
    and floors SAFE expectations at REVIEW. Stage observability: drop the
    unobservable-src rule (the remaining name rules then miss CONCLUSIVELY
    -> the static usage stands, same as before) and record the stat-less
    members as down (nothing connected -> static usage stands)."""
    from digital_twin.adapters.mist.compile.switch import compile_device
    from digital_twin.adapters.mist.ingest.ports import resolve_port_bases

    dyn = doc["networktemplate"]["port_usages"]["dynamic"]
    dyn["rules"] = [r for r in dyn["rules"] if r.get("src") == "lldp_system_name"]
    nt = doc.get("networktemplate")
    for dev in doc["devices"]:
        if dev.get("type") != "switch":
            continue
        mac = str(dev["mac"])
        have = {str(r["port_id"]) for r in doc["port_stats"] if str(r.get("mac")) == mac}
        eff = compile_device(dict(nt) if nt else None, dict(doc["setting"]), dict(dev))
        for member, attrs in resolve_port_bases(eff).items():
            if attrs.get("dynamic_usage") and member not in have:
                doc["port_stats"].append({"mac": mac, "port_id": member, "up": False})


def _gs22gw_doc(*, vlan2_gateway="198.51.194.227", stage_mismatch=False):
    # org staging additionally resolves the SRX's ip_configs entry 'test'
    # to vlan 2 (CONFIG/HIGH L3Intf — the known owner of the declared
    # gateway); vlan2's declared gateway rides the site networks row
    doc = _gs25_doc()
    _resolve_fixture_dynamic_ports(doc)
    doc["org_networks"].append({"name": "test", "vlan_id": 2})
    nets = doc["setting"].setdefault("networks", {})
    nets["vlan2"] = {**nets.get("vlan2", {"vlan_id": "2"}), "gateway": vlan2_gateway}
    if stage_mismatch:
        nets["gs22_m"] = {"vlan_id": 992, "gateway": "10.9.0.1"}
        doc["setting"].setdefault("dhcpd_config", {})["gs22_m"] = {
            "type": "local", "ip_start": "10.9.0.10", "ip_end": "10.9.0.99",
            "gateway": "10.9.0.99",
        }
    return doc


def _site_networks_op(doc, mutate):
    """Full-map networks update (root-replace semantics — the GS25 lesson:
    a partial networks payload would DELETE every other baseline row)."""
    nets = {k: dict(v) for k, v in (doc["setting"].get("networks") or {}).items()}
    mutate(nets)
    return {
        "action": "update", "order": 0, "object_type": "site_setting",
        "object_id": doc["scope"]["site_id"],
        "payload": {"networks": nets},
    }


def test_gs22gw_a_breaking_the_gateway_owner_is_unsafe(tmp_path):
    # baseline: vlan2's declared gateway IS the SRX ip_configs address
    # (owned, CONFIG/HIGH). The op moves the declared gateway to an address
    # no modeled interface owns -> known owner broken -> UNSAFE
    doc = _gs22gw_doc()
    op = _site_networks_op(
        doc, lambda nets: nets["vlan2"].__setitem__("gateway", "198.51.194.250")
    )
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.UNSAFE, v.decision_reasons
    f = next(f for f in v.findings if f.code == "wired.l3.gateway_gap.gateway_unowned")
    assert f.severity.value == "error" and f.evidence["vlan"] == 2


def test_gs22gw_b_preexisting_unowned_gateway_is_safe_info(tmp_path):
    # the unowned declared gateway already exists in baseline; the delta
    # adds an unrelated plain vlan -> INFO context, never a floor
    doc = _gs22gw_doc(vlan2_gateway="198.51.194.250")
    op = _site_networks_op(
        doc, lambda nets: nets.__setitem__("gs22_plain", {"vlan_id": 993})
    )
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.SAFE, v.decision_reasons
    f = next(f for f in v.findings if f.code == "wired.l3.gateway_gap.gateway_unowned")
    assert f.severity.value == "info"


def test_gs22gw_c_dhcp_gateway_mismatch_is_review(tmp_path):
    # op introduces a site scope handing out a gateway that contradicts the
    # network's declared one -> WARNING -> REVIEW
    doc = _gs22gw_doc()
    op = _site_networks_op(
        doc, lambda nets: nets.__setitem__("gs22_m", {"vlan_id": 992,
                                                      "gateway": "10.9.0.1"})
    )
    op["payload"]["dhcpd_config"] = {
        **(doc["setting"].get("dhcpd_config") or {}),
        "gs22_m": {"type": "local", "ip_start": "10.9.0.10",
                   "ip_end": "10.9.0.99", "gateway": "10.9.0.99"},
    }
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.REVIEW, v.decision_reasons
    f = next(f for f in v.findings
             if f.code == "wired.dhcp.scope_lint.gateway_mismatch")
    assert f.severity.value == "warning"


def test_gs22gw_d_preexisting_mismatch_is_safe_info(tmp_path):
    # mismatch pre-staged in baseline; op adds an unrelated coherent scope
    doc = _gs22gw_doc(stage_mismatch=True)
    op = _site_networks_op(
        doc, lambda nets: nets.__setitem__("gs22_far", {"vlan_id": 991,
                                                        "gateway": "10.50.0.1"})
    )
    op["payload"]["dhcpd_config"] = {
        **(doc["setting"].get("dhcpd_config") or {}),
        "gs22_far": {"type": "local", "ip_start": "10.50.0.10",
                     "ip_end": "10.50.0.99", "gateway": "10.50.0.1"},
    }
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.SAFE, v.decision_reasons
    f = next(f for f in v.findings
             if f.code == "wired.dhcp.scope_lint.gateway_mismatch")
    assert f.severity.value == "info"


# --- GS22-SUB: templated-subnet false-SAFE (Vlan.subnet_unresolved) ---


def _gs22sub_templated_removed_doc_and_op():
    # GS22-SUB twin of the GS22 .removed scenario: the routed network's subnet
    # is TEMPLATED ({{var}}), so it reads as unresolved-routed, NOT "not
    # routed". Removing its only modeled L3 interface must NOT fire .removed
    # (routed-ness is unproven) — the check ABSTAINS -> PARTIAL -> REVIEW,
    # where the LITERAL-subnet twin (test_gs22_removing_the_irb...) is UNSAFE.
    from .builders import _device, _drop_nones

    doc = augmented_doc(parallel_carries_gs=True)
    # org networks carries the routed intent with a TEMPLATED subnet — the
    # realistic path: org networks are not var-resolved by compile, so the
    # {{}} survives to ingest where it reads as unresolved-routed (a {{}} in
    # the switch effective config would hard-fail resolve_vars at compile).
    doc["org_networks"] = [
        {"name": "gs_routed", "vlan_id": 998, "subnet": "{{gs_routed_subnet}}"}
    ]
    doc["meta"]["fetched"] = list(doc["meta"]["fetched"]) + ["org_networks"]
    doc["setting"]["networks"]["gs_routed"] = {"vlan_id": 998}  # known by id, no subnet
    _device(doc, EDGE).setdefault("other_ip_configs", {})["gs_routed"] = {
        "type": "static", "ip": "203.0.113.1", "netmask": "255.255.255.0"
    }
    dev = copy.deepcopy(_device(doc, EDGE))
    dev["other_ip_configs"] = {
        k: v for k, v in dev["other_ip_configs"].items() if k != "gs_routed"
    }
    op = {
        "action": "update", "order": 0, "object_type": "device",
        "object_id": str(dev["id"]), "payload": _drop_nones(dev),
    }
    return doc, op


def test_gs22sub_a_templated_subnet_removed_l3_is_review_not_unsafe(tmp_path):
    # the false-SAFE closed: a templated subnet read as None="not routed" and
    # silenced .removed -> SAFE. Now it is unresolved-routed -> abstain -> REVIEW
    doc, op = _gs22sub_templated_removed_doc_and_op()
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.REVIEW, v.decision_reasons
    assert not any(f.code == "wired.l3.gateway_gap.removed" for f in v.findings)
    gg = next(r for r in v.check_results if r.check_id == "wired.l3.gateway_gap")
    assert gg.coverage.state is CoverageState.PARTIAL
    assert any("unreadable or ambiguous" in n for n in gg.coverage.notes), gg.coverage.notes


def test_gs22sub_b_nonwinning_device_row_subnet_conflict_is_review(tmp_path):
    # a device-local network row declares a subnet DISAGREEING with the winning
    # (site) row -> Vlan.subnet flips literal->unresolved -> the vlan is
    # MODIFIED -> abstain -> REVIEW. Without the conflict rule the device row is
    # silently dropped (subnet stays literal, unchanged -> SAFE): the false-SAFE.
    from .builders import _device, _drop_nones

    doc = augmented_doc(parallel_carries_gs=True)
    doc["org_networks"] = []
    doc["meta"]["fetched"] = list(doc["meta"]["fetched"]) + ["org_networks"]
    doc["setting"]["networks"]["gs_conf"] = {"vlan_id": 993, "subnet": "203.0.113.0/24"}
    _device(doc, EDGE).setdefault("networks", {})["gs_conf"] = {
        "vlan_id": 993, "subnet": "203.0.113.0/24"  # agreeing in baseline
    }
    dev = copy.deepcopy(_device(doc, EDGE))
    dev["networks"]["gs_conf"]["subnet"] = "198.51.0.0/24"  # conflict
    op = {
        "action": "update", "order": 0, "object_type": "device",
        "object_id": str(dev["id"]), "payload": _drop_nones(dev),
    }
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.REVIEW, v.decision_reasons
    assert not any(
        f.code.startswith("wired.l3.gateway_gap.") and f.evidence.get("vlan") == 993
        for f in v.findings
    )
    gg = next(r for r in v.check_results if r.check_id == "wired.l3.gateway_gap")
    assert gg.coverage.state is CoverageState.PARTIAL
    assert any("unreadable or ambiguous" in n for n in gg.coverage.notes), gg.coverage.notes


def test_gs22sub_c_unrelated_delta_leaves_templated_vlan_safe(tmp_path):
    # relevance discipline: a templated-subnet routed-unserved vlan the delta
    # does NOT touch contributes no abstain note -> SAFE (no global taint). The
    # delta is a cosmetic notes change (touches no networks) so the unresolved
    # vlan 995 stays out of the diff entirely.
    from .builders import _device

    doc = augmented_doc(parallel_carries_gs=True)
    doc["org_networks"] = [{"name": "gs_templated", "vlan_id": 995, "subnet": "{{x}}"}]
    doc["meta"]["fetched"] = list(doc["meta"]["fetched"]) + ["org_networks"]
    doc["setting"]["networks"]["gs_templated"] = {"vlan_id": 995}  # known by id
    op = {
        "action": "update", "order": 0, "object_type": "device",
        "object_id": str(_device(doc, EDGE)["id"]),
        "payload": {"type": "switch",
                    "notes": "gs22-sub control: cosmetic, no network impact"},
    }
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    # the unresolved vlan 995 is present in the IR but never floors the verdict:
    # the cosmetic delta touches no vlan/l3intf, so gateway_gap does not even run
    # (NOT_APPLICABLE) — no abstain note, no PARTIAL, SAFE holds
    assert v.decision is Decision.SAFE, v.decision_reasons
    gg = next(r for r in v.check_results if r.check_id == "wired.l3.gateway_gap")
    assert gg.coverage.state is not CoverageState.PARTIAL
    assert not any("unreadable or ambiguous" in n for n in gg.coverage.notes)


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


# --- GS26: OSPF exit withdrawal -------------------------------------------

def test_gs26a_passive_stub_withdrawal_is_review(tmp_path):
    # device keeps its active transit; a passive stub leaves OSPF -> REVIEW
    doc = ospf_doc({"ospf_transit": {}, "ospf_corp": {"passive": True}})
    op = ospf_op(doc, {"ospf_transit": {}})  # ospf_corp withdrawn
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.REVIEW, v.decision_reasons
    assert "wired.l3.ospf_withdrawal.advertised_removed" in {f.code for f in v.findings}


def test_gs26b_bare_active_withdrawal_collapse_with_clients_is_unsafe(tmp_path):
    # bare {} active transit (default-active) removed = last adjacency collapses;
    # an islanded routed segment (ospf_corp, vlan 971) has an observed client
    doc = ospf_doc({"ospf_transit": {}, "ospf_corp": {"passive": True}}, client_vlan=971)
    op = ospf_op(doc, {"ospf_corp": {"passive": True}})  # active transit withdrawn
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.UNSAFE, v.decision_reasons
    assert "wired.l3.ospf_withdrawal.egress_lost" in {f.code for f in v.findings}


def test_gs26c_disable_ospf_with_clients_is_unsafe(tmp_path):
    # ospf_config.enabled -> False removes ALL adjacencies at once; an observed
    # client on vlan 970 (the transit net) loses its modeled egress -> UNSAFE
    doc = ospf_doc({"ospf_transit": {}}, client_vlan=970)
    op = ospf_op(doc, None, disable=True)
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.UNSAFE, v.decision_reasons
    assert "wired.l3.ospf_withdrawal.egress_lost" in {f.code for f in v.findings}


def test_gs26d_addition_to_ospf_is_safe(tmp_path):
    # baseline: only transit in OSPF; op ADDS ospf_corp -> not a withdrawal
    doc = ospf_doc({"ospf_transit": {}})
    _corp_vid, _corp_subnet = OSPF_NETS["ospf_corp"]
    doc["setting"]["networks"]["ospf_corp"] = {"vlan_id": _corp_vid, "subnet": _corp_subnet}
    op = ospf_op(doc, {"ospf_transit": {}, "ospf_corp": {"passive": True}})
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.SAFE, v.decision_reasons


def test_gs26e_noncollapsing_passive_flip_is_review(tmp_path):
    # two active interfaces; flip ONE to passive -> device keeps an adjacency
    # -> .transit_mutation REVIEW (not SAFE, not UNSAFE)
    doc = ospf_doc({"ospf_transit": {}, "ospf_corp": {}})
    op = ospf_op(doc, {"ospf_transit": {}, "ospf_corp": {"passive": True}})
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.REVIEW, v.decision_reasons
    assert "wired.l3.ospf_withdrawal.transit_mutation" in {f.code for f in v.findings}


# --- MS: multi-site / org networktemplate simulation ----------------------

def test_ms_a_template_network_removal_breaks_one_site_unsafe(tmp_path):
    # the template edit removes corp from the uplink trunk -> site A loses its
    # exit (UNSAFE), site B unaffected (SAFE) -> org rollup UNSAFE naming A
    doc, plan = multisite_remove_corp()
    ov = _simulate_org(doc, plan, tmp_path)
    assert ov.decision is Decision.UNSAFE, ov.decision_reasons
    assert "siteA" in ov.driving_sites and ov.per_site["siteB"].decision is Decision.SAFE


def test_ms_b_one_site_fetch_fails_rolls_up_unknown(tmp_path):
    doc, plan = multisite_with_failed_site()
    ov = _simulate_org(doc, plan, tmp_path)
    assert ov.decision is Decision.UNKNOWN
    assert "siteB" in ov.site_failures


def test_ms_c_cosmetic_template_edit_is_safe(tmp_path):
    doc, plan = multisite_add_unused_vlan()
    ov = _simulate_org(doc, plan, tmp_path)
    assert ov.decision is Decision.SAFE, ov.decision_reasons


def test_ms_d_zero_assigned_sites_is_safe(tmp_path):
    doc, plan = multisite_template_with_no_assigned_sites()
    ov = _simulate_org(doc, plan, tmp_path)
    assert ov.decision is Decision.SAFE
