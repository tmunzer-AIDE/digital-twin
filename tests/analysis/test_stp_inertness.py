"""StpInertness (Spec-6 Tasks 2-3): licensed per-knob inertness decisions.

Reuses the Spec-5 bridge-id topology (root aa01 prio 0; leaf bb02 prio 4096;
transits cc03 8192 / dd04 12288; all links 1g two-sided) whose full prediction
is known: every port designated/forwarding except cc03:ge-0/0/1, dd04:ge-0/0/1,
bb02:ge-0/0/1 (root/forwarding) and bb02:ge-0/0/2 (alternate/blocking).
`_fully_observed` stamps observed telemetry AGREEING with all 8 predictions so
the single component is agreement_clean and every row matched."""
from __future__ import annotations

import dataclasses

from digital_twin.analysis.context import AnalysisContext
from digital_twin.analysis.stp_inertness import InertnessDecision, StpInertness
from digital_twin.ir import IRBuilder, IRCapability
from digital_twin.ir.entities import LinkKind, StpPolicy
from tests.analysis.test_stp_reachability import _bridge_id_topology, _set_observed
from tests.factories import link, make_port, sw

# port -> (observed role, observed state) agreeing with the Spec-4 prediction
_EXPECTED_ROLES: dict[str, tuple[str, str]] = {
    "aa01:ge-0/0/1": ("designated", "forwarding"),
    "aa01:ge-0/0/2": ("designated", "forwarding"),
    "cc03:ge-0/0/1": ("root", "forwarding"),
    "cc03:ge-0/0/2": ("designated", "forwarding"),
    "dd04:ge-0/0/1": ("root", "forwarding"),
    "dd04:ge-0/0/2": ("designated", "forwarding"),
    "bb02:ge-0/0/1": ("root", "forwarding"),
    "bb02:ge-0/0/2": ("alternate", "blocking"),
}


def _bridge_ir(*, with_vlan: bool = False):
    b = IRBuilder()
    _bridge_id_topology(b, prune_vlan10=with_vlan, carry_both_paths=with_vlan)
    return b.with_capability(IRCapability.WIRED_L2).build()


def _fully_observed(ir, *, skip: frozenset[str] = frozenset()):
    for pid, (role, state) in _EXPECTED_ROLES.items():
        if pid in skip:
            continue
        ir = _set_observed(ir, pid, role=role, state=state)
    return ir


def _with_policy(ir, pid: str, **knobs):
    port = ir.ports[pid]
    new_port = dataclasses.replace(port, stp_policy=StpPolicy(**knobs))
    new_ports = dict(ir.ports)
    new_ports[pid] = new_port
    return dataclasses.replace(ir, ports=new_ports)


def _with_priority(ir, did: str, prio: int):
    dev = ir.devices[did]
    new_devices = dict(ir.devices)
    new_devices[did] = dataclasses.replace(dev, stp_priority=prio)
    return dataclasses.replace(ir, devices=new_devices)


def _lag_pair_ir():
    """Two switches joined by a 2-member LAG: the Spec-4 engine caps LAG
    member predictions at MEDIUM — the license-(d) confidence fixture."""
    b = IRBuilder().add_device(sw("aa01", stp_priority=0)).add_device(
        sw("bb02", stp_priority=4096)
    )
    for name in ("ge-0/0/1", "ge-0/0/2"):
        b.add_port(make_port("aa01", name, observed_speed="1g"))
        b.add_port(make_port("bb02", name, observed_speed="1g"))
    b.add_link(link("aa01:ge-0/0/1", "bb02:ge-0/0/1", kind=LinkKind.LAG, bundle="ae0"))
    b.add_link(link("aa01:ge-0/0/2", "bb02:ge-0/0/2", kind=LinkKind.LAG, bundle="ae0"))
    return b.with_capability(IRCapability.WIRED_L2).build()


def _inertness(base_ir, prop_ir) -> StpInertness:
    return StpInertness(AnalysisContext(base_ir), AnalysisContext(prop_ir))


def test_fixture_sanity_all_rows_matched_component_clean():
    # pins the fixture the whole suite rests on: 8 predicted ports, all
    # matched, one clean component — if the engine's prediction ever shifts,
    # THIS test names the drift rather than a license test failing obscurely
    from digital_twin.analysis.stp_agreement import compare_to_observed

    ir = _fully_observed(_bridge_ir())
    actx = AnalysisContext(ir)
    report = compare_to_observed(actx.stp_tree(), actx.ir)
    assert report.matched == 8 and report.mismatched_high == 0
    assert len(report.components) == 1 and report.components[0].agreement_clean


def _decide_root_protect(base_ir, prop_ir, pid="cc03:ge-0/0/2") -> InertnessDecision:
    return _inertness(base_ir, prop_ir).decide(pid, "stp_no_root_port", False, True)


def test_license_a_port_missing_from_baseline_floors():
    base = _fully_observed(_bridge_ir())
    d = _inertness(base, base).decide("zz99:ge-0/0/1", "stp_no_root_port", False, True)
    assert not d.inert and any("baseline" in r for r in d.reasons)


def test_license_b_unvalidatable_target_row_floors():
    # target port telemetry-dark (no observed role) -> row unvalidatable
    base = _fully_observed(_bridge_ir(), skip=frozenset({"cc03:ge-0/0/2"}))
    d = _decide_root_protect(base, base)
    assert not d.inert and any("matched" in r for r in d.reasons)


def test_license_b_non_tree_access_port_floors_even_if_observed_designated():
    # R1-P1-2: an access port with NO PortPrediction can never be licensed,
    # even when its observed role is designated
    base = _fully_observed(_bridge_ir(with_vlan=True))
    base = _set_observed(base, "bb02:acc", role="designated", state="forwarding")
    d = _decide_root_protect(base, base, pid="bb02:acc")
    assert not d.inert and any("matched" in r for r in d.reasons)


def test_license_c_component_dirty_mismatch_floors_matched_target():
    # adjustment 5: target row matched, but ANOTHER port in the component
    # mismatches -> agreement_clean False -> floor
    base = _fully_observed(_bridge_ir())
    base = _set_observed(base, "bb02:ge-0/0/2", role="root", state="forwarding")
    d = _decide_root_protect(base, base)
    assert not d.inert and any("clean" in r for r in d.reasons)


def test_license_c_component_dirty_bpdu_inconsistent_floors_matched_target():
    base = _fully_observed(_bridge_ir())
    base = _set_observed(
        base, "bb02:ge-0/0/2", role="disabled-bpdu-inconsistent", state="blocking"
    )
    d = _decide_root_protect(base, base)
    assert not d.inert and any("clean" in r for r in d.reasons)


def test_license_d_medium_confidence_lag_position_floors():
    # plan-review P1: clause (d)'s HIGH requirement, isolated — the row IS
    # matched (b holds), the component IS clean (c holds), the position IS
    # identical (same IR both sides), but the LAG cap makes it MEDIUM
    from digital_twin.analysis.stp_agreement import compare_to_observed
    from digital_twin.ir.confidence import ConfidenceLevel

    ir = _lag_pair_ir()
    for pid in ("aa01:ge-0/0/1", "aa01:ge-0/0/2"):
        ir = _set_observed(ir, pid, role="designated", state="forwarding")
    actx = AnalysisContext(ir)
    report = compare_to_observed(actx.stp_tree(), actx.ir)
    rows = {r.port_id: r for r in report.ports}
    assert rows["aa01:ge-0/0/1"].bucket == "matched"  # sanity: (b) holds
    assert rows["aa01:ge-0/0/1"].predicted.confidence is ConfidenceLevel.MEDIUM
    d = _inertness(ir, ir).decide("aa01:ge-0/0/1", "stp_no_root_port", False, True)
    assert not d.inert and any("license (d)" in r for r in d.reasons)


def test_license_d_delta_moving_tree_position_floors():
    # proposed disables the cc03<->bb02 edge -> bb02 re-roots via dd04 ->
    # bb02:ge-0/0/2 flips alternate->root: position changed -> floor
    base = _fully_observed(_bridge_ir())
    prop = base
    for pid in ("cc03:ge-0/0/2", "bb02:ge-0/0/1"):
        port = prop.ports[pid]
        new_ports = dict(prop.ports)
        new_ports[pid] = dataclasses.replace(port, disabled=True)
        prop = dataclasses.replace(prop, ports=new_ports)
    d = _inertness(base, prop).decide("bb02:ge-0/0/2", "stp_no_root_port", False, True)
    assert not d.inert and any("position" in r for r in d.reasons)


def test_non_eligible_knob_floors():
    base = _fully_observed(_bridge_ir())
    for knob in ("stp_p2p", "use_vstp"):
        d = _inertness(base, base).decide("cc03:ge-0/0/2", knob, False, True)
        assert not d.inert and any("eligible" in r for r in d.reasons)


def test_unresolved_token_floors():
    base = _fully_observed(_bridge_ir())
    d = _inertness(base, base).decide(
        "cc03:ge-0/0/2", "stp_no_root_port", False, "unresolved:{{rp}}"
    )
    assert not d.inert and any("token" in r for r in d.reasons)


def test_shared_agreement_param_is_used_verbatim():
    from digital_twin.analysis.stp_agreement import compare_to_observed

    base = _fully_observed(_bridge_ir())
    actx = AnalysisContext(base)
    report = compare_to_observed(actx.stp_tree(), actx.ir)
    si = StpInertness(actx, AnalysisContext(base), agreement=report)
    assert si._agreement is report
