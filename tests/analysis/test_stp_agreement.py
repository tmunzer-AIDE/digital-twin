"""Agreement comparator (Spec-4 Task 6): joins stp_tree predictions against
OBSERVED telemetry (Port.stp_role / .stp_state). Pure — no findings, no
verdict-facing wiring (see stp_tree.py module docstring for the invariant).
"""
from __future__ import annotations

import dataclasses

from digital_twin.analysis.stp_agreement import compare_to_observed
from digital_twin.analysis.stp_tree import predict_stp_tree
from digital_twin.ir import IRBuilder
from tests.factories import link, make_port, sw


def _two_switch_ir():
    """aa01 (priority 0, root) <-> bb02 (priority 4096) over a single 1g
    link: aa01:ge-0/0/1 -> designated/HIGH; bb02:ge-0/0/1 -> root/HIGH."""
    b = IRBuilder()
    b.add_device(sw("aa01", stp_priority=0)).add_device(sw("bb02", stp_priority=4096))
    b.add_port(make_port("aa01", "ge-0/0/1", observed_speed="1g"))
    b.add_port(make_port("bb02", "ge-0/0/1", observed_speed="1g"))
    b.add_link(link("aa01:ge-0/0/1", "bb02:ge-0/0/1"))
    return b.build()


def _set_observed(ir, pid: str, *, role: str | None = None, state: str | None = None):
    """Rebuild ir.ports with the given port's stp_role/stp_state replaced.
    IR.ports is a Mapping so we go through dataclasses.replace on the IR."""
    port = ir.ports[pid]
    new_port = dataclasses.replace(port, stp_role=role, stp_state=state)
    new_ports = dict(ir.ports)
    new_ports[pid] = new_port
    return dataclasses.replace(ir, ports=new_ports)


def test_matched_role_and_state():
    ir = _two_switch_ir()
    prediction = predict_stp_tree(ir)
    ir = _set_observed(ir, "bb02:ge-0/0/1", role="root", state="forwarding")
    ir = _set_observed(ir, "aa01:ge-0/0/1", role="designated", state="forwarding")
    report = compare_to_observed(prediction, ir)
    assert report.matched == 2
    assert report.mismatched_high == 0
    assert report.mismatched_medium == 0
    assert report.mismatched_low == 0
    assert report.unvalidatable == 0
    assert report.bpdu_inconsistent == 0
    assert not any(c.disagreement for c in report.components)


def test_role_match_state_mismatch_is_still_mismatch():
    ir = _two_switch_ir()
    prediction = predict_stp_tree(ir)
    # Predicted root port is forwarding; role matches but observed state is
    # "blocking" -> still a mismatch (role match alone isn't enough).
    ir = _set_observed(ir, "bb02:ge-0/0/1", role="root", state="blocking")
    ir = _set_observed(ir, "aa01:ge-0/0/1", role="designated", state="forwarding")
    report = compare_to_observed(prediction, ir)
    root_port_pred = prediction.components[0].ports["bb02:ge-0/0/1"]
    bucket = {
        "HIGH": "mismatched_high",
        "MEDIUM": "mismatched_medium",
        "LOW": "mismatched_low",
    }[root_port_pred.confidence.name]
    assert getattr(report, bucket) == 1
    assert report.matched == 1  # the other port (aa01 side) still matches


def test_mismatch_buckets_key_on_prediction_confidence_exactly():
    # HIGH -> mismatched_high, MEDIUM -> mismatched_medium, LOW -> mismatched_low
    ir = _two_switch_ir()
    prediction = predict_stp_tree(ir)
    root_port_pred = prediction.components[0].ports["bb02:ge-0/0/1"]
    desig_port_pred = prediction.components[0].ports["aa01:ge-0/0/1"]
    # Both predictions start HIGH-confidence; flipping the observed role
    # below forces a mismatch at that same tier.
    assert root_port_pred.confidence.name == "HIGH"
    assert desig_port_pred.confidence.name == "HIGH"

    ir2 = _set_observed(ir, "bb02:ge-0/0/1", role="alternate", state="blocking")
    ir2 = _set_observed(ir2, "aa01:ge-0/0/1", role="designated", state="forwarding")
    report = compare_to_observed(prediction, ir2)
    assert report.mismatched_high == 1
    assert report.matched == 1

    # Synthesize MEDIUM/LOW predictions directly to pin bucket keying without
    # depending on engine internals producing those tiers from this topology.
    medium_pred = dataclasses.replace(root_port_pred, confidence=_confidence("MEDIUM"))
    low_pred = dataclasses.replace(root_port_pred, confidence=_confidence("LOW"))
    for pred, expected_bucket in ((medium_pred, "mismatched_medium"), (low_pred, "mismatched_low")):
        synth_prediction = _swap_port_prediction(prediction, "bb02:ge-0/0/1", pred)
        ir3 = _set_observed(ir, "bb02:ge-0/0/1", role="alternate", state="blocking")
        ir3 = _set_observed(ir3, "aa01:ge-0/0/1", role="designated", state="forwarding")
        rep = compare_to_observed(synth_prediction, ir3)
        assert getattr(rep, expected_bucket) == 1


def _confidence(name: str):
    from digital_twin.ir.confidence import ConfidenceLevel

    return ConfidenceLevel[name]


def _swap_port_prediction(prediction, pid, new_pred):
    comps = []
    for comp in prediction.components:
        if pid in comp.ports:
            ports = dict(comp.ports)
            ports[pid] = new_pred
            comp = dataclasses.replace(comp, ports=ports)
        comps.append(comp)
    return dataclasses.replace(prediction, components=tuple(comps))


def test_absent_or_empty_observed_role_unvalidatable():
    ir = _two_switch_ir()
    prediction = predict_stp_tree(ir)
    # Leave both ports at their entity default (stp_role=None) -> unvalidatable.
    report = compare_to_observed(prediction, ir)
    assert report.unvalidatable == 2
    assert report.matched == 0
    assert report.mismatched_high == 0
    assert not any(c.disagreement for c in report.components)

    # Explicit "" must behave identically to None (never a mismatch).
    ir_empty = _set_observed(ir, "bb02:ge-0/0/1", role="", state="")
    ir_empty = _set_observed(ir_empty, "aa01:ge-0/0/1", role="", state="")
    report_empty = compare_to_observed(prediction, ir_empty)
    assert report_empty.unvalidatable == 2
    assert report_empty.matched == 0


def test_unknown_observed_token_unvalidatable_not_mismatch():
    ir = _two_switch_ir()
    prediction = predict_stp_tree(ir)
    ir = _set_observed(ir, "bb02:ge-0/0/1", role="master", state=None)
    ir = _set_observed(ir, "aa01:ge-0/0/1", role="designated", state="forwarding")
    report = compare_to_observed(prediction, ir)
    assert report.unvalidatable == 1
    assert report.mismatched_high == 0
    assert report.mismatched_medium == 0
    assert report.mismatched_low == 0
    assert report.matched == 1


def test_bpdu_inconsistent_reported_separately():
    ir = _two_switch_ir()
    prediction = predict_stp_tree(ir)
    ir = _set_observed(ir, "bb02:ge-0/0/1", role="disabled-bpdu-inconsistent", state=None)
    ir = _set_observed(ir, "aa01:ge-0/0/1", role="designated", state="forwarding")
    report = compare_to_observed(prediction, ir)
    assert report.bpdu_inconsistent == 1
    assert report.mismatched_high == 0
    assert report.mismatched_medium == 0
    assert report.mismatched_low == 0
    assert report.unvalidatable == 0
    assert report.matched == 1
    # bpdu_inconsistent is a protection-state signal, not a role mismatch —
    # it must not flip the component disagreement flag.
    assert not any(c.disagreement for c in report.components)


def test_per_component_rollup_flags_disagreement():
    ir = _two_switch_ir()
    prediction = predict_stp_tree(ir)
    assert len(prediction.components) == 1
    ir = _set_observed(ir, "bb02:ge-0/0/1", role="alternate", state="blocking")
    ir = _set_observed(ir, "aa01:ge-0/0/1", role="designated", state="forwarding")
    report = compare_to_observed(prediction, ir)
    assert len(report.components) == 1
    assert report.components[0].disagreement is True
    assert report.components[0].nodes == frozenset({"aa01", "bb02"})


def test_empty_string_state_with_matching_role_is_matched():
    """Empty string state ("") is a present-but-empty non-observation (PR #43
    convention). When role matches prediction, empty state should NOT prevent
    a matched bucket — state comparison must skip when state is None OR "".
    """
    ir = _two_switch_ir()
    prediction = predict_stp_tree(ir)
    # Set observed role to match prediction, but state to "" (not observed).
    # This must land in matched bucket, not mismatched_*.
    ir = _set_observed(ir, "bb02:ge-0/0/1", role="root", state="")
    ir = _set_observed(ir, "aa01:ge-0/0/1", role="designated", state="")
    report = compare_to_observed(prediction, ir)
    assert report.matched == 2
    assert report.mismatched_high == 0
    assert report.mismatched_medium == 0
    assert report.mismatched_low == 0
    assert report.unvalidatable == 0


def test_empty_string_state_does_not_rescue_role_mismatch():
    """Empty string state must not rescue a role mismatch into anything other
    than mismatched_{tier}. The state guard only skips comparison when BOTH
    role and state are observed; role mismatch is always a mismatch.
    """
    ir = _two_switch_ir()
    prediction = predict_stp_tree(ir)
    # Set observed role to DIFFER from prediction, with state "".
    # This must still land in mismatched_high (role mismatch), not matched.
    ir = _set_observed(ir, "bb02:ge-0/0/1", role="alternate", state="")
    ir = _set_observed(ir, "aa01:ge-0/0/1", role="designated", state="forwarding")
    report = compare_to_observed(prediction, ir)
    assert report.mismatched_high == 1
    assert report.matched == 1


def test_ports_are_deterministically_sorted_within_and_across_components():
    ir = _two_switch_ir()
    prediction = predict_stp_tree(ir)
    report = compare_to_observed(prediction, ir)
    port_ids = [row.port_id for row in report.ports]
    assert port_ids == sorted(port_ids)
