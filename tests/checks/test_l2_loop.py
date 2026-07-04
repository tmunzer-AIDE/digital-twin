"""l2.loop spec table: cycle + all-STP = PASS; + STP disabled = FAIL(HIGH);
+ STP unknown = WARN(LOW). Only NEW cycles are attributed to the delta."""

from dataclasses import replace

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, CheckResult, Status
from digital_twin.checks.wired.l2_loop import L2LoopCheck
from digital_twin.contracts import Finding, FindingCategory, Severity
from digital_twin.ir import ConfidenceLevel, IRBuilder, IRCapability, Vlan, diff_ir
from digital_twin.verdict.decision import Decision, DecisionInputs, decide
from tests.factories import access_port, link, sw, trunk_port


def _ring_ir(stp: bool | None, parallel: bool):
    """A-B with one link (tree) or two standalone links (cycle)."""
    b = IRBuilder()
    b.add_device(sw("A")).add_device(sw("B"))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    for dev, peer in (("A", "B"), ("B", "A")):
        p1 = trunk_port(dev, f"to-{peer}-1", tagged=(10,))
        b.add_port(replace(p1, stp_enabled=stp))
        if parallel:
            p2 = trunk_port(dev, f"to-{peer}-2", tagged=(10,))
            b.add_port(replace(p2, stp_enabled=stp))
    b.add_link(link("A:to-B-1", "B:to-A-1"))
    if parallel:
        b.add_link(link("A:to-B-2", "B:to-A-2"))
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def _ctx(baseline, proposed) -> CheckContext:
    return CheckContext(
        baseline=AnalysisContext(baseline),
        proposed=AnalysisContext(proposed),
        diff=diff_ir(baseline, proposed),
    )


def test_new_cycle_with_stp_everywhere_passes():
    ctx = _ctx(_ring_ir(stp=True, parallel=False), _ring_ir(stp=True, parallel=True))
    result = L2LoopCheck().run(ctx)
    assert result.status is Status.PASS  # protected redundancy, not a loop


def test_new_cycle_with_stp_disabled_fails_high():
    ctx = _ctx(_ring_ir(stp=False, parallel=False), _ring_ir(stp=False, parallel=True))
    result = L2LoopCheck().run(ctx)
    assert result.status is Status.FAIL
    f = result.findings[0]
    assert f.severity is Severity.ERROR and f.category is FindingCategory.NETWORK
    assert f.confidence.level is ConfidenceLevel.HIGH


def test_new_cycle_with_stp_unknown_warns_low():
    ctx = _ctx(_ring_ir(stp=None, parallel=False), _ring_ir(stp=None, parallel=True))
    result = L2LoopCheck().run(ctx)
    assert result.status is Status.WARN
    assert result.findings[0].confidence.level is ConfidenceLevel.LOW


def test_stp_regression_on_existing_cycle_fails():
    # the cycle's NODE SET is unchanged, but the delta disables STP on its
    # ports — the spec's attributable condition is "cycle + STP disabled",
    # which IS newly introduced here. Must FAIL, not hide behind "preexisting".
    ctx = _ctx(_ring_ir(stp=True, parallel=True), _ring_ir(stp=False, parallel=True))
    result = L2LoopCheck().run(ctx)
    assert result.status is Status.FAIL
    assert any(f.code == "wired.l2.loop.unprotected" for f in result.findings)


def test_stp_becoming_unknown_on_existing_cycle_warns():
    ctx = _ctx(_ring_ir(stp=True, parallel=True), _ring_ir(stp=None, parallel=True))
    result = L2LoopCheck().run(ctx)
    assert result.status is Status.WARN


def test_preexisting_cycle_is_context_not_failure():
    same = _ring_ir(stp=False, parallel=True)
    ctx = _ctx(same, _ring_ir(stp=False, parallel=True))
    result = L2LoopCheck().run(ctx)
    assert result.status is Status.PASS  # not introduced by the delta
    assert any(f.severity is Severity.INFO for f in result.findings)  # reported as context


def test_applies_to_link_and_port_changes_only():
    check = L2LoopCheck()
    base, prop = _ring_ir(True, False), _ring_ir(True, True)
    assert check.applies_to(diff_ir(base, prop)) is True
    assert check.applies_to(diff_ir(base, base)) is False


# ---------- caused_by attribution tests ------------------------------------------


def test_loop_finding_caused_by_stp_flip():
    """A cycle whose STP was enabled in baseline becomes disabled (rank 0->2) — the
    flip on the cycle ports is named in caused_by."""
    # baseline: cycle (parallel) with STP enabled; proposed: same cycle, STP disabled
    ctx = _ctx(_ring_ir(stp=True, parallel=True), _ring_ir(stp=False, parallel=True))
    result = L2LoopCheck().run(ctx)
    assert result.status is Status.FAIL
    f = result.findings[0]
    assert f.code == "wired.l2.loop.unprotected"
    # ports with stp_enabled flipped are named; ports are "A:to-B-1", "A:to-B-2",
    # "B:to-A-1", "B:to-A-2" (both sides in both links)
    cause_ids = {c.ref.id for c in f.caused_by}
    # at least one of the cycle member ports whose stp_enabled changed is named
    assert len(f.caused_by) > 0
    assert any(pid in cause_ids for pid in ("A:to-B-1", "A:to-B-2", "B:to-A-1", "B:to-A-2"))
    assert all("stp_enabled" in c.fields for c in f.caused_by)


def test_loop_finding_caused_by_added_closing_link():
    """Adding a second link closes the cycle (baseline: tree, proposed: parallel) —
    the added link is named in caused_by."""
    base = _ring_ir(stp=True, parallel=False)
    prop = _ring_ir(stp=True, parallel=True)
    ctx = _ctx(base, prop)
    result = L2LoopCheck().run(ctx)
    # fully STP-protected -> PASS (protected row), but cause is still set
    assert result.status is Status.PASS
    # one finding: the wired.l2.loop.protected row (attributable because cycle is NEW)
    protected = [f for f in result.findings if f.code == "wired.l2.loop.protected"]
    assert protected, f"expected a protected finding, got {[f.code for f in result.findings]}"
    f = protected[0]
    cause_ids = {c.ref.id for c in f.caused_by}
    # the closing link "A:to-B-2__B:to-A-2" is in the delta (added)
    assert ("A:to-B-2__B:to-A-2" in cause_ids) or ("B:to-A-2__A:to-B-2" in cause_ids) or any(
        "to-B-2" in cid or "to-A-2" in cid for cid in cause_ids
    ), f"expected closing link in caused_by, got {cause_ids}"


def test_preexisting_loop_finding_caused_by_empty():
    """A pre-existing cycle (condition unchanged) must have caused_by == () — the delta
    did not arm it, so we must not fabricate a cause."""
    same = _ring_ir(stp=False, parallel=True)
    ctx = _ctx(same, _ring_ir(stp=False, parallel=True))
    result = L2LoopCheck().run(ctx)
    assert result.status is Status.PASS
    preexisting = [f for f in result.findings if f.code == "wired.l2.loop.preexisting"]
    assert preexisting, "expected a preexisting finding"
    assert preexisting[0].caused_by == (), (
        f"preexisting loop caused_by must be () but got {preexisting[0].caused_by}"
    )


# ---------- self_loop: observed physical self-loops guard stp_disable ------------
#
# Port.self_loop_peer/self_loop_reciprocal (Tasks 1-3) are OBSERVED, diff-
# ignored facts: LLDP shows the chassis seeing ITSELF on another port. A
# `stp_disable` delta (Port.bpdu_filter False->True — the ONLY config mapping
# of that leaf; stp_enabled is telemetry, never the trigger) on a self-looped
# port turns a contained physical mis-wire into an active broadcast-storm risk.
# reciprocal (both rows name each other) -> ERROR/HIGH/UNSAFE; one-sided claim
# -> WARNING/MEDIUM (never ERROR, spec evidence-tier gate); any OTHER port diff
# on the pair -> INFO context; untouched -> silent.

_SL_A = "A:p8"
_SL_B = "A:p9"


def _self_loop_ports(
    *, reciprocal: bool, stp_state: str | None = None, stp_role: str | None = None,
):
    """Two standalone access ports (no Link — a self-loop is a chassis-sees-
    itself LLDP artifact, not a modeled physical link) each claiming the other
    as its self-loop peer. `reciprocal=False` -> only p8 claims p9 (one-sided)."""
    p8 = access_port("A", "p8", 10)
    p8 = replace(p8, self_loop_peer=_SL_B, self_loop_reciprocal=reciprocal,
                 stp_state=stp_state, stp_role=stp_role)
    p9 = access_port("A", "p9", 10)
    if reciprocal:
        p9 = replace(p9, self_loop_peer=_SL_A, self_loop_reciprocal=True,
                     stp_state=stp_state, stp_role=stp_role)
    return p8, p9


def _self_loop_ir(*, reciprocal: bool, bpdu_filter_a: bool, bpdu_filter_b: bool = False,
                   stp_edge_a: bool = False, stp_state: str | None = None,
                   stp_role: str | None = None):
    p8, p9 = _self_loop_ports(reciprocal=reciprocal, stp_state=stp_state, stp_role=stp_role)
    p8 = replace(p8, bpdu_filter=bpdu_filter_a, stp_edge=stp_edge_a)
    p9 = replace(p9, bpdu_filter=bpdu_filter_b)
    b = IRBuilder()
    b.add_device(sw("A"))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    b.add_port(p8).add_port(p9)
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def _run_self_loop(
    *, reciprocal: bool, flip: str | None, elsewhere: bool = False,
    stp_state: str | None = None, stp_role: str | None = None,
) -> CheckResult:
    base = _self_loop_ir(reciprocal=reciprocal, bpdu_filter_a=False,
                          stp_state=stp_state, stp_role=stp_role)
    if flip == "bpdu_filter":
        prop = _self_loop_ir(reciprocal=reciprocal, bpdu_filter_a=True,
                              stp_state=stp_state, stp_role=stp_role)
    elif flip == "stp_edge":
        prop = _self_loop_ir(reciprocal=reciprocal, bpdu_filter_a=False, stp_edge_a=True,
                              stp_state=stp_state, stp_role=stp_role)
    else:
        prop = _self_loop_ir(reciprocal=reciprocal, bpdu_filter_a=False,
                              stp_state=stp_state, stp_role=stp_role)
    if elsewhere:
        # an unrelated port, far from the self-looped pair, changes — the
        # self-loop pass must stay silent about A:p8/A:p9
        other_base = access_port("A", "p10", 20)
        other_prop = replace(other_base, mtu=1500)
        base = _add_port(base, other_base)
        prop = _add_port(prop, other_prop)
    return L2LoopCheck().run(_ctx(base, prop))


def _add_port(ir, port):
    b = IRBuilder()
    b.add_device(sw("A"))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    b.add_vlan(Vlan(vlan_id=20, name="other", scope="s1"))
    for p in ir.ports.values():
        b.add_port(p)
    b.add_port(port)
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def _find(result: CheckResult, code: str) -> Finding:
    return next(f for f in result.findings if f.code == code)


def _findall(result: CheckResult, code: str) -> tuple[Finding, ...]:
    return tuple(f for f in result.findings if f.code == code)


def test_stp_disable_on_reciprocal_self_loop_is_error_high_unsafe():
    # delta flips Port.bpdu_filter False->True (the stp_disable leaf) on one
    # end of a RECIPROCAL observed self-loop -> contained loop becomes a storm
    result = _run_self_loop(reciprocal=True, flip="bpdu_filter")
    f = _find(result, "wired.l2.loop.self_loop")
    assert f.severity is Severity.ERROR
    assert f.confidence.level is ConfidenceLevel.HIGH
    assert set(f.evidence["ports"]) == {_SL_A, _SL_B}
    decision, _ = decide(
        DecisionInputs(rejections=(), l0_fatal=False, baseline_unavailable=False,
                        check_results=(result,))
    )
    assert decision is Decision.UNSAFE


def test_one_sided_self_loop_evidence_caps_at_warning_medium():
    result = _run_self_loop(reciprocal=False, flip="bpdu_filter")
    f = _find(result, "wired.l2.loop.self_loop")
    assert f.severity is Severity.WARNING
    assert f.confidence.level is ConfidenceLevel.MEDIUM


def test_other_change_on_self_looped_port_is_info_context():
    # flip a REAL Port field that is not the trigger (Port has no description
    # field — that's a config leaf, exercised by the e2e pin below)
    result = _run_self_loop(reciprocal=True, flip="stp_edge")
    f = _find(result, "wired.l2.loop.self_loop")
    assert f.severity is Severity.INFO
    # review P1: the INFO context must not taint the CHECK result — status
    # stays PASS (nothing WARNING+ from this check) and the result confidence
    # stays HIGH (the INFO's confidence is EXCLUDED from the roll-up), so the
    # decision layer never floors REVIEW because of context
    assert result.status is Status.PASS
    assert result.confidence is not None
    assert result.confidence.level is ConfidenceLevel.HIGH


def test_unrelated_delta_is_silent_about_the_self_loop():
    result = _run_self_loop(reciprocal=True, flip=None, elsewhere=True)
    assert not _findall(result, "wired.l2.loop.self_loop")


def test_observed_states_land_in_evidence_when_present():
    result = _run_self_loop(
        reciprocal=True, flip="bpdu_filter", stp_state="forwarding", stp_role="designated",
    )
    f = _find(result, "wired.l2.loop.self_loop")
    assert "observed_states" in f.evidence
    states = f.evidence["observed_states"]
    assert states[_SL_A]["state"] == "forwarding"
    assert states[_SL_A]["role"] == "designated"
    assert states[_SL_B]["state"] == "forwarding"
    assert states[_SL_B]["role"] == "designated"


def test_self_loop_finding_is_one_per_pair_not_per_end():
    result = _run_self_loop(reciprocal=True, flip="bpdu_filter")
    assert len(_findall(result, "wired.l2.loop.self_loop")) == 1


def test_self_loop_finding_caused_by_the_bpdu_filter_flip():
    result = _run_self_loop(reciprocal=True, flip="bpdu_filter")
    f = _find(result, "wired.l2.loop.self_loop")
    assert len(f.caused_by) > 0
    cause_ids = {c.ref.id for c in f.caused_by}
    assert _SL_A in cause_ids
    assert all("bpdu_filter" in c.fields for c in f.caused_by if c.ref.id == _SL_A)
