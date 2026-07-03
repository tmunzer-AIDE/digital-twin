"""wired.stp.policy: the four StpPolicy knobs (stp_required, stp_no_root_port,
stp_p2p, use_vstp) NEVER resolve SAFE in this slice — a changed policy always
floors REVIEW via `.policy_change` (WARNING/MEDIUM). Precise codes are a later
task; v1 emits only the floor + `unresolved:` token coverage notes.
applies_to is changed_fields-precise: an unrelated port edit must not wake it."""

import dataclasses

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, CheckResult, CoverageState, Status
from digital_twin.checks.wired.stp_policy import StpPolicyCheck
from digital_twin.contracts import Finding, Severity
from digital_twin.ir import ConfidenceLevel, IRBuilder, IRCapability, Port, PortMode, diff_ir
from digital_twin.ir.entities import StpPolicy
from digital_twin.ir.provenance import Provenance
from digital_twin.verdict.decision import Decision, DecisionInputs, decide
from tests.factories import ap, link, sw, wired_client


def _base_port(**kw):
    return Port(id="S:ge-0/0/1", device_id="S", name="ge-0/0/1", mode=PortMode.ACCESS,
                native_vlan=10, **kw)


def _ir_with_port(port):
    b = IRBuilder().add_device(sw("S"))
    b.add_port(port)
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def _ir_no_port():
    b = IRBuilder().add_device(sw("S"))
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def _run(base, prop):
    return StpPolicyCheck().run(CheckContext(
        baseline=AnalysisContext(base), proposed=AnalysisContext(prop), diff=diff_ir(base, prop)))


def _run_flip(knob, value):
    base_port = _base_port()
    prop_port = dataclasses.replace(base_port, stp_policy=StpPolicy(**{knob: value}))
    return _run(_ir_with_port(base_port), _ir_with_port(prop_port))


def test_any_stp_policy_change_floors_review_via_policy_change():
    result = _run_flip("stp_p2p", True)
    f = result.findings[0]
    assert f.code == "wired.stp.policy.policy_change"
    assert f.severity is Severity.WARNING
    assert f.confidence.level is ConfidenceLevel.MEDIUM
    assert result.status is Status.WARN


def test_unresolved_token_lands_on_the_floor_with_a_note():
    result = _run_flip("use_vstp", "unresolved:{{vstp}}")
    assert result.findings[0].code == "wired.stp.policy.policy_change"
    assert any("unresolved" in n for n in result.coverage.notes)
    assert result.coverage.state is CoverageState.PARTIAL


def _diff_with_description_only_change():
    # an unrelated port-field edit (mtu) that leaves stp_policy untouched and
    # does not affect port identity (unlike `name`, which is id-derived)
    base_port = _base_port()
    prop_port = dataclasses.replace(base_port, mtu=1500)
    return diff_ir(_ir_with_port(base_port), _ir_with_port(prop_port))


def _diff_with_port_added_carrying_policy():
    prop_port = _base_port(stp_policy=StpPolicy(stp_p2p=True))
    return diff_ir(_ir_no_port(), _ir_with_port(prop_port))


def test_unrelated_port_change_does_not_wake_the_check():
    check = StpPolicyCheck()
    assert check.applies_to(_diff_with_description_only_change()) is False


def test_port_add_and_remove_wake_the_check():
    check = StpPolicyCheck()
    assert check.applies_to(_diff_with_port_added_carrying_policy()) is True
    # remove is the mirror case
    remove_diff = diff_ir(_ir_with_port(_base_port(stp_policy=StpPolicy(stp_p2p=True))),
                           _ir_no_port())
    assert check.applies_to(remove_diff) is True


def test_no_stp_policy_fixture_can_resolve_safe():
    # structural guard: every fixture in this module that changes stp_policy
    # must yield >=1 finding from this check (the floor makes SAFE impossible)
    for knob in ("stp_required", "stp_no_root_port", "stp_p2p", "use_vstp"):
        assert _run_flip(knob, True).findings, knob


def test_run_names_the_changed_knobs():
    result = _run_flip("stp_required", True)
    f = result.findings[0]
    assert "stp_required" in f.evidence["knobs"]
    assert "stp_required" in f.message
    assert f.subject is not None and f.subject.kind == "port" and f.subject.id == "S:ge-0/0/1"


def test_multiple_knobs_changed_together_are_all_named():
    base_port = _base_port()
    prop_port = dataclasses.replace(
        base_port, stp_policy=StpPolicy(stp_required=True, use_vstp=True))
    result = _run(_ir_with_port(base_port), _ir_with_port(prop_port))
    f = result.findings[0]
    assert set(f.evidence["knobs"]) == {"stp_required", "use_vstp"}


def test_no_change_is_silent():
    port = _base_port()
    assert _run(_ir_with_port(port), _ir_with_port(port)).findings == ()


def test_caused_by_points_at_the_changed_port():
    result = _run_flip("stp_p2p", True)
    f = result.findings[0]
    assert len(f.caused_by) > 0
    assert f.caused_by[0].ref.kind == "port"
    assert f.caused_by[0].ref.id == "S:ge-0/0/1"


# --- .blocking_risk: stp_required enabled on a no-BPDU-peer port ------------
#
# .blocking_risk fires ONLY when the delta enables stp_required (False/absent
# -> True) AND there is a CANDIDATE non-BPDU peer: an LLDP-tied AP, an observed
# wired client with no modeled bridge peer, or a modeled peer port with
# bpdu_filter=True. ERROR iff the peer evidence is HIGH (two-sided tie);
# a one-sided tie caps at WARNING/MEDIUM. Unknown/no-peer evidence does NOT
# qualify (spec P2): floor + coverage note only. A fire suppresses that
# port's .policy_change (most-specific precedence); disabling stp_required is
# floor-only; a pre-existing True untouched by the delta is INFO context.

def _find(result: CheckResult, code: str) -> Finding:
    return next(f for f in result.findings if f.code == code)


def _findall(result: CheckResult, code: str) -> tuple[Finding, ...]:
    return tuple(f for f in result.findings if f.code == code)


_TARGET = "A:ge-0/0/1"


def _base_target_port(**kw) -> Port:
    return Port(id=_TARGET, device_id="A", name="ge-0/0/1", mode=PortMode.ACCESS,
                native_vlan=10, **kw)


def _build(*, target_port: Port, peer: str | None, tie: str = "two_sided") -> IRBuilder:
    """2 switches linked (A, B) with the target access port on A. `peer`
    selects the candidate non-BPDU peer variant:
      - "ap": an AP device + link tied to A's target port (LLDP tie per `tie`)
      - "client": an observed wired client attached to the target port, no
        modeled bridge peer on it
      - "bpdu_filter": a peer switch port (on B) linked to the target port,
        with bpdu_filter=True (tie per `tie`)
      - None: no peer evidence at all (unknown)
    """
    prov = Provenance.LLDP_TWO_SIDED if tie == "two_sided" else Provenance.LLDP_ONE_SIDED
    b = IRBuilder().add_device(sw("A")).add_device(sw("B"))
    b.add_port(target_port)
    # a separate inter-switch link A<->B unrelated to the target port, so the
    # topology is not just the bare target port in isolation
    b.add_port(Port(id="A:up", device_id="A", name="up", mode=PortMode.TRUNK, tagged_vlans=(10,)))
    b.add_port(Port(id="B:down", device_id="B", name="down", mode=PortMode.TRUNK,
                    tagged_vlans=(10,)))
    b.add_link(link("A:up", "B:down"))
    if peer == "ap":
        b.add_device(ap("AP1"))
        b.add_port(Port(id="AP1:eth0", device_id="AP1", name="eth0", mode=PortMode.TRUNK))
        b.add_link(link(_TARGET, "AP1:eth0", prov=prov))
    elif peer == "client":
        b.add_client(wired_client("aa:bb:cc:00:00:01", _TARGET, vlan=10))
    elif peer == "bpdu_filter":
        b.add_port(Port(id="B:peer", device_id="B", name="peer", mode=PortMode.ACCESS,
                        native_vlan=10, bpdu_filter=True))
        b.add_link(link(_TARGET, "B:peer", prov=prov))
    b.with_capability(IRCapability.WIRED_L2)
    return b


def _run_enable_stp_required(*, peer: str | None, tie: str = "two_sided", disable: bool = False):
    base_policy = StpPolicy(stp_required=disable)
    prop_policy = StpPolicy(stp_required=not disable)
    base_port = _base_target_port(stp_policy=base_policy if disable else None)
    prop_port = dataclasses.replace(base_port, stp_policy=prop_policy)
    base_ir = _build(target_port=base_port, peer=peer, tie=tie).build()
    prop_ir = _build(target_port=prop_port, peer=peer, tie=tie).build()
    return _run(base_ir, prop_ir)


def test_blocking_risk_ap_peer_two_sided_is_error_high():
    result = _run_enable_stp_required(peer="ap", tie="two_sided")
    f = _find(result, "wired.stp.policy.blocking_risk")
    assert f.severity is Severity.ERROR and f.confidence.level is ConfidenceLevel.HIGH
    assert f.evidence["peer_kind"] == "ap"
    assert f.evidence["peer"] == "AP1"
    assert "severity_reason" in f.evidence
    assert "occupants_behind" in f.evidence
    assert result.status is Status.FAIL
    # no .policy_change on this port: blocking_risk suppresses the floor
    assert not _findall(result, "wired.stp.policy.policy_change")
    decision, _ = decide(
        DecisionInputs(rejections=(), l0_fatal=False, baseline_unavailable=False,
                       check_results=(result,))
    )
    assert decision is Decision.UNSAFE


def test_blocking_risk_wired_client_no_bridge_peer_is_error_high():
    result = _run_enable_stp_required(peer="client", tie="two_sided")
    f = _find(result, "wired.stp.policy.blocking_risk")
    assert f.severity is Severity.ERROR and f.confidence.level is ConfidenceLevel.HIGH
    assert f.evidence["peer_kind"] == "client"
    assert result.status is Status.FAIL
    assert not _findall(result, "wired.stp.policy.policy_change")


def test_blocking_risk_bpdu_filter_peer_two_sided_is_error_high():
    result = _run_enable_stp_required(peer="bpdu_filter", tie="two_sided")
    f = _find(result, "wired.stp.policy.blocking_risk")
    assert f.severity is Severity.ERROR and f.confidence.level is ConfidenceLevel.HIGH
    assert f.evidence["peer_kind"] == "bpdu_filter"
    assert f.evidence["peer"] == "B:peer"
    assert result.status is Status.FAIL
    assert not _findall(result, "wired.stp.policy.policy_change")


def test_blocking_risk_one_sided_ap_tie_is_warning_medium():
    result = _run_enable_stp_required(peer="ap", tie="one_sided")
    f = _find(result, "wired.stp.policy.blocking_risk")
    assert f.severity is Severity.WARNING and f.confidence.level is ConfidenceLevel.MEDIUM
    assert f.evidence["peer_kind"] == "ap"
    assert result.status is Status.WARN
    assert not _findall(result, "wired.stp.policy.policy_change")


def test_blocking_risk_one_sided_bpdu_filter_tie_is_warning_medium():
    result = _run_enable_stp_required(peer="bpdu_filter", tie="one_sided")
    f = _find(result, "wired.stp.policy.blocking_risk")
    assert f.severity is Severity.WARNING and f.confidence.level is ConfidenceLevel.MEDIUM
    assert f.evidence["peer_kind"] == "bpdu_filter"
    assert result.status is Status.WARN
    assert not _findall(result, "wired.stp.policy.policy_change")


def test_unknown_peer_is_floor_plus_note_not_blocking_risk():
    # spec P2: no peer evidence -> the model cannot claim "peer won't send
    # BPDUs"; floor + coverage note, NO blocking_risk
    result = _run_enable_stp_required(peer=None)
    assert not _findall(result, "wired.stp.policy.blocking_risk")
    assert _find(result, "wired.stp.policy.policy_change")
    assert any("peer unobserved" in n for n in result.coverage.notes)


def test_disabling_stp_required_is_floor_only():
    # True -> False is the disable direction: never a risk finding, floor only
    result = _run_enable_stp_required(peer="ap", tie="two_sided", disable=True)
    assert not _findall(result, "wired.stp.policy.blocking_risk")
    f = _find(result, "wired.stp.policy.policy_change")
    assert f.severity is Severity.WARNING


def test_preexisting_stp_required_on_ap_port_untouched_is_info_context():
    # baseline already True on the AP-facing port; the DELTA touches a
    # different knob on that same port (use_vstp) -> stp_required itself is
    # pre-existing, untouched context (INFO), not a fresh blocking_risk
    base_policy = StpPolicy(stp_required=True)
    prop_policy = StpPolicy(stp_required=True, use_vstp=True)
    base_port = _base_target_port(stp_policy=base_policy)
    prop_port = dataclasses.replace(base_port, stp_policy=prop_policy)
    base_ir = _build(target_port=base_port, peer="ap", tie="two_sided").build()
    prop_ir = _build(target_port=prop_port, peer="ap", tie="two_sided").build()
    result = _run(base_ir, prop_ir)
    assert not _findall(result, "wired.stp.policy.blocking_risk")
    info = [f for f in result.findings if f.severity is Severity.INFO]
    assert any("stp_required" in f.message for f in info)
    # the floor still covers the ACTUALLY changed knob (use_vstp)
    change = _find(result, "wired.stp.policy.policy_change")
    assert "use_vstp" in change.evidence["knobs"]
    assert change.severity is Severity.WARNING


def test_blocking_risk_fires_from_baseline_only_ap_tie():
    # Regression for the review finding: peer/occupancy evidence must be
    # UNIONED across baseline+proposed, not read from proposed alone. This
    # state (baseline has the AP tie, proposed lacks it) can't arise from
    # apply_plan today -- built directly via the fixtures to prove the union,
    # not just document it.
    base_policy = StpPolicy(stp_required=False)
    prop_policy = StpPolicy(stp_required=True)
    base_port = _base_target_port(stp_policy=base_policy)
    prop_port = dataclasses.replace(base_port, stp_policy=prop_policy)
    base_ir = _build(target_port=base_port, peer="ap", tie="two_sided").build()
    # proposed: same topology minus the AP device/port/link -- the AP tie
    # exists ONLY in baseline.
    prop_ir = (
        IRBuilder().add_device(sw("A")).add_device(sw("B"))
        .add_port(prop_port)
        .add_port(Port(id="A:up", device_id="A", name="up", mode=PortMode.TRUNK,
                       tagged_vlans=(10,)))
        .add_port(Port(id="B:down", device_id="B", name="down", mode=PortMode.TRUNK,
                       tagged_vlans=(10,)))
        .add_link(link("A:up", "B:down"))
        .with_capability(IRCapability.WIRED_L2)
        .build()
    )
    result = _run(base_ir, prop_ir)
    f = _find(result, "wired.stp.policy.blocking_risk")
    assert f.severity is Severity.ERROR and f.confidence.level is ConfidenceLevel.HIGH
    assert f.evidence["peer_kind"] == "ap"
    assert f.evidence["peer"] == "AP1"
    assert result.status is Status.FAIL
    assert not _findall(result, "wired.stp.policy.policy_change")


def test_new_port_with_stp_required_true_and_ap_peer_is_error_high():
    # port ADD carrying stp_required=True counts as an enable per spec
    prop_port = _base_target_port(stp_policy=StpPolicy(stp_required=True))
    base_ir = (
        IRBuilder().add_device(sw("A")).add_device(sw("B"))
        .with_capability(IRCapability.WIRED_L2).build()
    )
    prop_ir = _build(target_port=prop_port, peer="ap", tie="two_sided").build()
    result = _run(base_ir, prop_ir)
    f = _find(result, "wired.stp.policy.blocking_risk")
    assert f.severity is Severity.ERROR and f.confidence.level is ConfidenceLevel.HIGH
