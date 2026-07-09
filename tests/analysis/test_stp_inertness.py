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


def _with_speed(ir, pid: str, speed: str | None):
    port = ir.ports[pid]
    new_ports = dict(ir.ports)
    new_ports[pid] = dataclasses.replace(port, observed_speed=speed)
    return dataclasses.replace(ir, ports=new_ports)


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


# --- knob rules (Task 3) -----------------------------------------------------


def test_root_protect_on_designated_port_is_inert_both_directions():
    base = _fully_observed(_bridge_ir())
    si = _inertness(base, base)
    enable = si.decide("cc03:ge-0/0/2", "stp_no_root_port", False, True)
    disable = si.decide("cc03:ge-0/0/2", "stp_no_root_port", True, False)
    assert enable.inert and disable.inert
    assert enable.evidence["predicted_role"] == "designated"


def test_root_protect_on_root_bridge_ports_is_inert():
    # every aa01 (root bridge) port is designated -> inert
    base = _fully_observed(_bridge_ir())
    d = _inertness(base, base).decide("aa01:ge-0/0/2", "stp_no_root_port", False, True)
    assert d.inert


def test_root_protect_on_alternate_port_floors():
    # bb02:ge-0/0/2 is alternate/blocking: root-protect would go
    # root-inconsistent on superior BPDUs — resilience change, REVIEW
    base = _fully_observed(_bridge_ir())
    d = _inertness(base, base).decide("bb02:ge-0/0/2", "stp_no_root_port", False, True)
    assert not d.inert and any("designated" in r for r in d.reasons)


def test_root_protect_on_root_port_floors_at_the_rule():
    # cc03:ge-0/0/1 is the observed+predicted root port; the CHECK's
    # observed-root ERROR route wins in practice, but the module itself must
    # also refuse (defense in depth — never rely on caller ordering)
    base = _fully_observed(_bridge_ir())
    d = _inertness(base, base).decide("cc03:ge-0/0/1", "stp_no_root_port", False, True)
    assert not d.inert


def test_required_enable_receiving_root_target_with_designated_peer_is_inert():
    # DIRECTION-CORRECT positive (PR #47 review P1): the target bb02:ge-0/0/1
    # is the validated ROOT (receiving) end; its peer cc03:ge-0/0/2 the
    # validated DESIGNATED (sending) end — inbound BPDUs demonstrably arrive.
    base = _fully_observed(_bridge_ir())
    d = _inertness(base, base).decide("bb02:ge-0/0/1", "stp_required", False, True)
    assert d.inert
    assert d.evidence["peer"] == "cc03:ge-0/0/2"
    assert d.evidence["peer_predicted_role"] == "designated"


def test_required_enable_on_designated_target_floors_direction():
    # PR #47 review P1 regression (the exact shape that WRONGLY granted):
    # a designated target facing a root peer proves the target SENDS BPDUs,
    # not that it receives them — the root peer emits no steady-state BPDUs,
    # so enabling the receive-dependent requirement here is NOT provably inert.
    base = _fully_observed(_bridge_ir())
    d = _inertness(base, base).decide("cc03:ge-0/0/2", "stp_required", False, True)
    assert not d.inert
    assert any("root" in r and "RECEIVES" in r for r in d.reasons)


def test_required_enable_with_telemetry_dark_peer_floors():
    # R1-P1: peer row unvalidatable — switch + no-filter is NOT positive
    # evidence that BPDUs flow (target is the direction-correct root end)
    base = _fully_observed(_bridge_ir(), skip=frozenset({"cc03:ge-0/0/2"}))
    d = _inertness(base, base).decide("bb02:ge-0/0/1", "stp_required", False, True)
    assert not d.inert and any("peer" in r for r in d.reasons)


def test_required_enable_with_bpdu_filter_peer_floors():
    base = _fully_observed(_bridge_ir())
    port = base.ports["cc03:ge-0/0/2"]
    new_ports = dict(base.ports)
    new_ports["cc03:ge-0/0/2"] = dataclasses.replace(port, bpdu_filter=True)
    base = dataclasses.replace(base, ports=new_ports)
    d = _inertness(base, base).decide("bb02:ge-0/0/1", "stp_required", False, True)
    assert not d.inert


def test_required_enable_with_no_modeled_link_floors():
    # bb02:acc (with_vlan fixture) has no link at all
    base = _fully_observed(_bridge_ir(with_vlan=True))
    base = _set_observed(base, "bb02:acc", role="designated", state="forwarding")
    d = _inertness(base, base).decide("bb02:acc", "stp_required", False, True)
    assert not d.inert


def test_required_enable_peer_position_degraded_by_delta_floors_peer_clause():
    # plan-review P1, isolating (direction-corrected after PR #47 review P1):
    # the TARGET bb02:ge-0/0/1 (root) keeps an identical HIGH position in
    # BOTH states; ONLY the peer's evidence degrades. Proposed blanks the
    # peer end cc03:ge-0/0/2's observed_speed: the designated decision folds
    # its OWN end's cost_defaulted (stp_tree step 4) and drops to LOW, while
    # the target's root-port key and confidence fold only the neighbor's RPC
    # and the target's own end cost — both untouched, and bb02's alternative
    # path via dd04 is unchanged so no role moves anywhere. The failure MUST
    # name the peer-position clause — proving the peer validation is
    # load-bearing, not shadowed by license (d) on the target. (Roles on a
    # shared segment are complementary, so a peer ROLE move always moves the
    # target too — confidence degradation is the peer clause's independently
    # testable content.)
    base = _fully_observed(_bridge_ir())
    prop = _with_speed(base, "cc03:ge-0/0/2", None)
    si = _inertness(base, prop)
    # sanity: the target's OWN license fully holds across this delta (the
    # disable-rule grant succeeds — license + observed forwarding), so any
    # enable failure below is attributable to the peer clauses alone
    assert si.decide("bb02:ge-0/0/1", "stp_required", True, False).inert
    d = si.decide("bb02:ge-0/0/1", "stp_required", False, True)
    assert not d.inert
    assert any("peer tree position" in r for r in d.reasons)


def test_required_disable_on_observed_forwarding_is_inert():
    base = _fully_observed(_bridge_ir())
    d = _inertness(base, base).decide("cc03:ge-0/0/2", "stp_required", True, False)
    assert d.inert


def test_required_disable_on_observed_blocking_floors():
    # bb02:ge-0/0/2 observed blocking: if the requirement were the operative
    # hold, removing it could unblock into a loop — never assumed benign.
    # (Also floors at the license? No: the row IS matched. The RULE floors it.)
    base = _fully_observed(_bridge_ir())
    d = _inertness(base, base).decide("bb02:ge-0/0/2", "stp_required", True, False)
    assert not d.inert and any("forwarding" in r for r in d.reasons)
