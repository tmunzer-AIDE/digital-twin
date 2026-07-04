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
from digital_twin.ir.entities import StpMode, StpPolicy
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


def _build(
    *, target_port: Port, peer: str | None, tie: str = "two_sided",
    with_client: bool = False,
) -> IRBuilder:
    """2 switches linked (A, B) with the target access port on A. `peer`
    selects the candidate non-BPDU peer variant:
      - "ap": an AP device + link tied to A's target port (LLDP tie per `tie`)
      - "client": an observed wired client attached to the target port, no
        modeled bridge peer on it
      - "bpdu_filter": a peer switch port (on B) linked to the target port,
        with bpdu_filter=True (tie per `tie`)
      - "switch_no_filter": a modeled peer SWITCH port (on B) linked to the
        target port, bpdu_filter=False -- a real bridge peer that WILL send
        BPDUs (Finding 1 regression: this must NOT read as "no modeled peer")
      - None: no peer evidence at all (unknown)
    `with_client=True` additionally attaches an observed wired client to the
    target port regardless of `peer` (Finding 1: client evidence co-existing
    with a modeled non-filtering bridge peer)."""
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
    elif peer == "switch_no_filter":
        b.add_port(Port(id="B:peer", device_id="B", name="peer", mode=PortMode.ACCESS,
                        native_vlan=10, bpdu_filter=False))
        b.add_link(link(_TARGET, "B:peer", prov=prov))
    if with_client:
        b.add_client(wired_client("aa:bb:cc:00:00:02", _TARGET, vlan=10))
    b.with_capability(IRCapability.WIRED_L2)
    return b


def _run_enable_stp_required(
    *, peer: str | None, tie: str = "two_sided", disable: bool = False,
    with_client: bool = False,
):
    base_policy = StpPolicy(stp_required=disable)
    prop_policy = StpPolicy(stp_required=not disable)
    base_port = _base_target_port(stp_policy=base_policy if disable else None)
    prop_port = dataclasses.replace(base_port, stp_policy=prop_policy)
    base_ir = _build(target_port=base_port, peer=peer, tie=tie, with_client=with_client).build()
    prop_ir = _build(target_port=prop_port, peer=peer, tie=tie, with_client=with_client).build()
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


def test_client_with_modeled_nonfilter_bridge_peer_is_no_blocking_risk():
    # Finding 1 (false-UNSAFE): the port has a modeled bridge (switch) peer
    # WITHOUT bpdu_filter, AND an observed wired client is also present. A
    # modeled non-filtering switch peer WILL send BPDUs -- stp_required
    # causes no blocking. The client tier must only fire when there is NO
    # modeled bridge peer at all, not merely no bpdu_filter peer.
    result = _run_enable_stp_required(peer="switch_no_filter", tie="two_sided", with_client=True)
    assert not _findall(result, "wired.stp.policy.blocking_risk")
    f = _find(result, "wired.stp.policy.policy_change")
    assert f.severity is Severity.WARNING


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


# --- .root_protect_risk: stp_no_root_port enabled on a port that is the only --
# path to the elected root -----------------------------------------------------
#
# .root_protect_risk fires ONLY when the delta enables stp_no_root_port
# (False/absent -> True) on a port that is the device's ONLY graph path to the
# component's elected root. ERROR/HIGH requires the elected root known at HIGH
# (_root_of returned a tuple with any_default_assumed False); only-path with a
# non-HIGH election degrades to WARNING + a coverage note. A redundant path
# means the floor covers the change (no risk code). The device itself being
# the elected root also means no risk code.

def _root_protect_target_port(**kw) -> Port:
    return Port(id=_TARGET, device_id="A", name="ge-0/0/1", mode=PortMode.ACCESS,
                native_vlan=10, **kw)


def _build_root_protect(
    *, topology: str, priorities: dict[str, int | None], target_port: Port
) -> IRBuilder:
    """`topology="chain"`: A<->B only (the target port IS the only path from A
    to the elected root). `topology="triangle"`: A<->B, A<->C, B<->C — a
    redundant path from A to the root exists via C even if the target port
    (A<->B) is disabled for root-port purposes.

    `priorities` maps device id -> stp_priority (None leaves the platform
    default / assumed)."""
    b = IRBuilder()
    for did in priorities:
        b.add_device(sw(did, stp_priority=priorities[did]))
    b.add_port(target_port)
    b.add_port(Port(id="B:down", device_id="B", name="down", mode=PortMode.TRUNK,
                     tagged_vlans=(10,)))
    b.add_link(link(_TARGET, "B:down"))
    if topology == "triangle":
        b.add_port(Port(id="A:c", device_id="A", name="c", mode=PortMode.TRUNK,
                         tagged_vlans=(10,)))
        b.add_port(Port(id="C:a", device_id="C", name="a", mode=PortMode.TRUNK,
                         tagged_vlans=(10,)))
        b.add_port(Port(id="B:c", device_id="B", name="c", mode=PortMode.TRUNK,
                         tagged_vlans=(10,)))
        b.add_port(Port(id="C:b", device_id="C", name="b", mode=PortMode.TRUNK,
                         tagged_vlans=(10,)))
        b.add_link(link("A:c", "C:a"))
        b.add_link(link("B:c", "C:b"))
    b.with_capability(IRCapability.WIRED_L2)
    return b


def _run_enable_no_root_port(
    *, topology: str, priorities: dict[str, int | None], disable: bool = False,
    also: dict | None = None, baseline_also: dict | None = None,
):
    base_policy = StpPolicy(stp_no_root_port=disable)
    prop_policy = StpPolicy(stp_no_root_port=not disable)
    base_extra = dict(baseline_also) if baseline_also else {}
    base_port = _root_protect_target_port(
        stp_policy=base_policy if disable else None, **base_extra
    )
    prop_extra = {**base_extra, **(also or {})}
    prop_port = dataclasses.replace(base_port, stp_policy=prop_policy, **prop_extra)
    base_ir = _build_root_protect(
        topology=topology, priorities=priorities, target_port=base_port
    ).build()
    prop_ir = _build_root_protect(
        topology=topology, priorities=priorities, target_port=prop_port
    ).build()
    return _run(base_ir, prop_ir)


def test_root_protect_on_only_path_to_high_root_is_error_high():
    # A(prio 32768) - B(prio 4096, root); the only A->B edge gets
    # stp_no_root_port=True -> A can never accept its root port -> blocks
    result = _run_enable_no_root_port(topology="chain", priorities={"A": 32768, "B": 4096})
    f = _find(result, "wired.stp.policy.root_protect_risk")
    assert f.severity is Severity.ERROR and f.confidence.level is ConfidenceLevel.HIGH
    assert f.evidence["elected_root"] == "B" and f.evidence["only_path"] is True
    assert not _findall(result, "wired.stp.policy.policy_change")
    decision, _ = decide(
        DecisionInputs(rejections=(), l0_fatal=False, baseline_unavailable=False,
                       check_results=(result,))
    )
    assert decision is Decision.UNSAFE


def test_root_protect_with_redundant_path_is_floor_only():
    result = _run_enable_no_root_port(
        topology="triangle", priorities={"A": 32768, "B": 4096, "C": 32768}
    )
    assert not _findall(result, "wired.stp.policy.root_protect_risk")
    assert _find(result, "wired.stp.policy.policy_change")


def test_root_protect_on_one_of_two_parallel_links_is_floor_only():
    # A has TWO standalone links to root B; stp_no_root_port lands on only
    # one (A:up1<->B:down1). Removing that port's edge from the component
    # MultiGraph leaves the sibling edge (A:up2<->B:down2) -> NOT only-path
    # -> no false root_protect_risk (a false fire here would be an UNSAFE
    # salience bug, cf. PR #24).
    target_pid = "A:up1"
    base_policy = StpPolicy(stp_no_root_port=False)
    prop_policy = StpPolicy(stp_no_root_port=True)
    base_port = Port(id=target_pid, device_id="A", name="up1", mode=PortMode.TRUNK,
                      tagged_vlans=(10,), stp_policy=base_policy)
    prop_port = dataclasses.replace(base_port, stp_policy=prop_policy)

    def _build_parallel(port: Port):
        b = IRBuilder()
        b.add_device(sw("A", stp_priority=32768)).add_device(sw("B", stp_priority=4096))
        b.add_port(port)
        b.add_port(Port(id="B:down1", device_id="B", name="down1", mode=PortMode.TRUNK,
                         tagged_vlans=(10,)))
        b.add_port(Port(id="A:up2", device_id="A", name="up2", mode=PortMode.TRUNK,
                         tagged_vlans=(10,)))
        b.add_port(Port(id="B:down2", device_id="B", name="down2", mode=PortMode.TRUNK,
                         tagged_vlans=(10,)))
        b.add_link(link(target_pid, "B:down1"))
        b.add_link(link("A:up2", "B:down2"))
        b.with_capability(IRCapability.WIRED_L2)
        return b.build()

    result = _run(_build_parallel(base_port), _build_parallel(prop_port))
    assert not _findall(result, "wired.stp.policy.root_protect_risk")
    f = _find(result, "wired.stp.policy.policy_change")
    assert f.severity is Severity.WARNING
    # sibling-fixture pair: the chain topology (single link, no parallel path)
    # still produces the risk -- proving this test's parallel-link topology
    # is what suppresses it, not some other change.
    chain_result = _run_enable_no_root_port(
        topology="chain", priorities={"A": 32768, "B": 4096}
    )
    assert _find(chain_result, "wired.stp.policy.root_protect_risk")


def test_root_protect_with_unprovable_election_is_warning_plus_note():
    # any stp_priority_invalid / default-assumed priority in the component:
    # ERROR requires the elected root known at HIGH — degrade, never guess
    result = _run_enable_no_root_port(topology="chain", priorities={"A": None, "B": 4096})
    f = _find(result, "wired.stp.policy.root_protect_risk")
    assert f.severity is Severity.WARNING
    assert any("root election" in n for n in result.coverage.notes)


def test_root_protect_with_abstained_election_is_warning_plus_note():
    # Finding 2: _root_of returns _ABSTAIN (stp_priority_invalid present in
    # the component) -- this must NOT be silently treated as "no election to
    # disturb". Spec: unprovable election -> WARNING root_protect_risk +
    # coverage note ("root election not provable..."), never silence. This is
    # the "chain" topology twin of the existing any_default_assumed
    # unprovable-election test, but via the OTHER abstention shape (_ABSTAIN).
    from digital_twin.ir.entities import Device, DeviceRole

    def ir(target_port: Port):
        b = IRBuilder()
        b.add_device(sw("A", stp_priority=32768))
        b.add_device(Device(id="B", role=DeviceRole.SWITCH, site="s1", stp_priority=4096,
                             stp_priority_invalid=True))
        b.add_port(target_port)
        b.add_port(Port(id="B:down", device_id="B", name="down", mode=PortMode.TRUNK,
                         tagged_vlans=(10,)))
        b.add_link(link(_TARGET, "B:down"))
        b.with_capability(IRCapability.WIRED_L2)
        return b.build()

    base_policy = StpPolicy(stp_no_root_port=False)
    prop_policy = StpPolicy(stp_no_root_port=True)
    base_port = _root_protect_target_port(stp_policy=base_policy)
    prop_port = dataclasses.replace(base_port, stp_policy=prop_policy)
    result = _run(ir(base_port), ir(prop_port))

    f = _find(result, "wired.stp.policy.root_protect_risk")
    assert f.severity is Severity.WARNING
    assert f.evidence["election_confidence"] == "unprovable"
    assert f.evidence["elected_root"] is None
    assert f.evidence["only_path"] is None
    assert any("root election not provable" in n for n in result.coverage.notes)
    assert result.coverage.state is CoverageState.PARTIAL
    # a delta-caused WARNING suppresses the port's floor
    assert not _findall(result, "wired.stp.policy.policy_change")


def test_root_protect_device_is_elected_root_is_no_risk():
    # A itself has the lowest priority -> A is the elected root; disabling its
    # own root-port acceptance on a link to a non-root peer is not a
    # root-protect risk (there is no root to lose the path to).
    result = _run_enable_no_root_port(topology="chain", priorities={"A": 4096, "B": 32768})
    assert not _findall(result, "wired.stp.policy.root_protect_risk")
    assert _find(result, "wired.stp.policy.policy_change")


def test_root_protect_disable_direction_is_floor_only():
    # True -> False is the disable direction: never a risk finding, floor only
    result = _run_enable_no_root_port(
        topology="chain", priorities={"A": 32768, "B": 4096}, disable=True
    )
    assert not _findall(result, "wired.stp.policy.root_protect_risk")
    f = _find(result, "wired.stp.policy.policy_change")
    assert f.severity is Severity.WARNING


# --- liveness guard: route-independent (Spec-2 hole, spec P1-1/P1r3-2) ------
#
# a port that does not participate in STP in the PROPOSED state cannot block
# the root path via root-protect, no matter which route (graph-only-path or
# unprovable-election) would otherwise fire. Covers a same-delta admin-disable
# or stp_disable flip AND a pre-existing bpdu_filter=True port. stp_edge is
# deliberately excluded (self-heals on BPDU receipt).

def test_root_protect_plus_admin_disable_in_one_delta_is_not_error():
    # liveness guard: a port disabled by the SAME delta cannot block the root
    # path via root-protect; harm owner is admin_disable; floor still fires
    result = _run_enable_no_root_port(topology="chain",
                                      priorities={"A": 32768, "B": 4096},
                                      also={"disabled": True})
    assert not [f for f in _findall(result, "wired.stp.policy.root_protect_risk")
                if f.severity is Severity.ERROR]
    assert _find(result, "wired.stp.policy.policy_change")


def test_root_protect_plus_stp_disable_in_one_delta_is_not_error():
    # the GRAPH-route variant (spec P1r3-2): the L2 graph KEEPS bpdu_filter'd
    # edges, so without a shared guard the graph route would still ERROR on a
    # port that no longer processes BPDUs -- the Spec-2 hole this closes
    result = _run_enable_no_root_port(topology="chain",
                                      priorities={"A": 32768, "B": 4096},
                                      also={"bpdu_filter": True})
    assert not [f for f in _findall(result, "wired.stp.policy.root_protect_risk")
                if f.severity is Severity.ERROR]
    assert _find(result, "wired.stp.policy.policy_change")


def test_root_protect_on_preexisting_bpdu_filtered_port_is_not_error():
    # P1 final round: baseline ALREADY has bpdu_filter=True (port never
    # processes BPDUs); enabling root-protect on it later is inert -- the
    # proposed-state guard must catch this, not just the same-delta flip
    result = _run_enable_no_root_port(topology="chain",
                                      priorities={"A": 32768, "B": 4096},
                                      baseline_also={"bpdu_filter": True})
    assert not [f for f in _findall(result, "wired.stp.policy.root_protect_risk")
                if f.severity is Severity.ERROR]
    assert _find(result, "wired.stp.policy.policy_change")


def test_root_protect_plus_stp_edge_in_one_delta_still_errors():
    # stp_edge is EXPLICITLY not in the guard (spec P1r2): edge self-heals on
    # BPDU receipt, so root-protect on the root path remains a real risk
    result = _run_enable_no_root_port(topology="chain",
                                      priorities={"A": 32768, "B": 4096},
                                      also={"stp_edge": True})
    f = _find(result, "wired.stp.policy.root_protect_risk")
    assert f.severity is Severity.ERROR


# --- observed-root route: Port.stp_role == "root" escalates ERROR/HIGH ------
#
# After the liveness guard: if the BASELINE port's OBSERVED stp_role is the
# literal string "root" -> ERROR/HIGH, evidence carries observed_role="root"
# and election_confidence="observed". This fires even where the graph
# election is unprovable (e.g. an external/off-fabric root) -- the observed
# live election result is definitive regardless of what the graph can prove.
# Escalate-only: any other role (or None) leaves the graph route's own
# behavior byte-identical to today. When BOTH routes independently conclude
# ERROR, exactly ONE finding is emitted with unioned evidence.

def _run_enable_no_root_port_with_role(
    *, role: str | None, election: str, disable: bool = False,
    also: dict | None = None, baseline_also: dict | None = None,
):
    """T2's `_run_enable_no_root_port` helper, plus an OBSERVED `stp_role` set
    identically on both baseline and proposed ports (the live fact predates
    and survives the delta -- it is not itself part of `also`/`baseline_also`,
    which only ever apply to the PROPOSED or BOTH sides for policy-adjacent
    knobs). `election="external"` reuses the existing unprovable-election
    fixture shape (a default-assumed priority in the component, per
    `test_root_protect_with_unprovable_election_is_warning_plus_note`) -- the
    motivating case where the graph cannot prove a root at HIGH confidence at
    all (e.g. it is off-fabric / external), yet the port's OBSERVED role is
    definitive."""
    priorities = {"A": None, "B": 4096} if election == "external" else {
        "A": 32768, "B": 4096
    }
    role_kwarg = {"stp_role": role} if role is not None else {}
    combined_baseline_also = {**role_kwarg, **(baseline_also or {})}
    return _run_enable_no_root_port(
        topology="chain", priorities=priorities, disable=disable,
        also=also, baseline_also=combined_baseline_also,
    )


def test_observed_root_role_escalates_even_with_external_root():
    # THE motivating case: graph election unprovable (external root) -> graph
    # route alone yields WARNING+note; observed stp_role="root" is the live
    # election result -> ERROR/HIGH
    result = _run_enable_no_root_port_with_role(role="root", election="external")
    f = _find(result, "wired.stp.policy.root_protect_risk")
    assert f.severity is Severity.ERROR
    assert f.confidence.level is ConfidenceLevel.HIGH
    assert f.evidence["observed_role"] == "root"
    assert f.evidence["election_confidence"] == "observed"

    decision, _ = decide(
        DecisionInputs(rejections=(), l0_fatal=False, baseline_unavailable=False,
                       check_results=(result,))
    )
    assert decision is Decision.UNSAFE

    # contrast: WITHOUT the observed role, the same fixture yields only the
    # graph route's unprovable WARNING tier -- proving the role is what
    # escalates it, not the topology/priorities.
    without_role = _run_enable_no_root_port_with_role(role=None, election="external")
    fw = _find(without_role, "wired.stp.policy.root_protect_risk")
    assert fw.severity is Severity.WARNING


def test_observed_designated_role_changes_nothing():
    # negative: only the literal "root" escalates
    result = _run_enable_no_root_port_with_role(role="designated", election="external")
    f = _find(result, "wired.stp.policy.root_protect_risk")
    assert f.severity is Severity.WARNING  # graph route's unprovable tier, unchanged


def test_both_routes_union_evidence():
    # HIGH graph election + only-path AND observed role="root": one ERROR
    # finding carrying only_path AND observed_role
    result = _run_enable_no_root_port_with_role(role="root", election="provable")
    matches = _findall(result, "wired.stp.policy.root_protect_risk")
    assert len(matches) == 1
    f = matches[0]
    assert f.severity is Severity.ERROR
    assert f.confidence.level is ConfidenceLevel.HIGH
    assert f.evidence["only_path"] is True
    assert f.evidence["elected_root"] == "B"
    assert f.evidence["observed_role"] == "root"
    assert f.evidence["election_confidence"] == "observed"


def test_observed_root_respects_the_liveness_guard():
    # role="root" + stp_disable in the same delta -> no ERROR (guard from T2)
    result = _run_enable_no_root_port_with_role(
        role="root", election="provable", also={"bpdu_filter": True}
    )
    assert not [f for f in _findall(result, "wired.stp.policy.root_protect_risk")
                if f.severity is Severity.ERROR]
    assert _find(result, "wired.stp.policy.policy_change")


def _build_root_protect_bpdu_filter_peer(
    *, priorities: dict[str, int | None], target_port: Port
) -> IRBuilder:
    # like _build_root_protect(topology="chain") but B:down has bpdu_filter=True
    # so .blocking_risk (stp_required) has a candidate peer to fire on, in
    # addition to .root_protect_risk (stp_no_root_port, only-path to root).
    b = IRBuilder()
    for did in priorities:
        b.add_device(sw(did, stp_priority=priorities[did]))
    b.add_port(target_port)
    b.add_port(Port(id="B:down", device_id="B", name="down", mode=PortMode.ACCESS,
                     native_vlan=10, bpdu_filter=True))
    b.add_link(link(_TARGET, "B:down"))
    b.with_capability(IRCapability.WIRED_L2)
    return b


def test_blocking_risk_and_root_protect_risk_coexist_on_one_port():
    # both stp_required and stp_no_root_port enabled together on the SAME
    # port: both risk codes can fire independently for one delta.
    base_policy = StpPolicy(stp_required=False, stp_no_root_port=False)
    prop_policy = StpPolicy(stp_required=True, stp_no_root_port=True)
    base_port = _root_protect_target_port(stp_policy=base_policy)
    prop_port = dataclasses.replace(base_port, stp_policy=prop_policy)
    base_ir = _build_root_protect_bpdu_filter_peer(
        priorities={"A": 32768, "B": 4096}, target_port=base_port
    ).build()
    prop_ir = _build_root_protect_bpdu_filter_peer(
        priorities={"A": 32768, "B": 4096}, target_port=prop_port
    ).build()
    result = _run(base_ir, prop_ir)
    assert _find(result, "wired.stp.policy.blocking_risk")
    assert _find(result, "wired.stp.policy.root_protect_risk")


# --- .link_mismatch: both ends of a modeled link disagree on use_vstp/stp_p2p -
#
# .link_mismatch fires when both ends of a MODELED link disagree on use_vstp
# or stp_p2p (effective value, tokens excluded), and the delta introduced or
# changed the disagreement. WARNING/MEDIUM by default; confidence follows the
# LINK's own tie confidence (HIGH two-sided, MEDIUM below) same as
# .blocking_risk's _tie_confidence. Keyed by (link, knob): both knobs
# mismatched on the same link -> two separate findings. Pre-existing
# disagreement merely touched (some OTHER knob changed) -> INFO context, which
# never satisfies the .policy_change floor (spec P2 round 2).

_UP, _DOWN = "A:up", "B:down"


def _link_mismatch_ir(
    *, up_policy: StpPolicy | None, down_policy: StpPolicy | None,
    tie: str = "two_sided", up_stp_mode: StpMode | None = None,
    down_stp_mode: StpMode | None = None,
) -> IRBuilder:
    prov = Provenance.LLDP_TWO_SIDED if tie == "two_sided" else Provenance.LLDP_ONE_SIDED
    up_port = Port(id=_UP, device_id="A", name="up", mode=PortMode.TRUNK,
                   tagged_vlans=(10,), stp_policy=up_policy)
    down_port = Port(id=_DOWN, device_id="B", name="down", mode=PortMode.TRUNK,
                     tagged_vlans=(10,), stp_policy=down_policy)
    if up_stp_mode is not None:
        up_port = dataclasses.replace(up_port, stp_mode=up_stp_mode)
    if down_stp_mode is not None:
        down_port = dataclasses.replace(down_port, stp_mode=down_stp_mode)
    b = IRBuilder().add_device(sw("A")).add_device(sw("B"))
    b.add_port(up_port).add_port(down_port)
    b.add_link(link(_UP, _DOWN, prov=prov))
    b.with_capability(IRCapability.WIRED_L2)
    return b


def _run_one_end_flip(*knobs: str, tie: str = "two_sided"):
    """Baseline: both ends default (no mismatch). Proposed: A:up flips the
    given knob(s) to True, B:down stays default -> a fresh mismatch per knob."""
    base_ir = _link_mismatch_ir(up_policy=None, down_policy=None, tie=tie).build()
    prop_up_policy = StpPolicy(**{k: True for k in knobs})
    prop_ir = _link_mismatch_ir(
        up_policy=prop_up_policy, down_policy=None, tie=tie
    ).build()
    return _run(base_ir, prop_ir)


def test_use_vstp_mismatch_on_modeled_link_is_warning():
    result = _run_one_end_flip("use_vstp")  # A:up use_vstp=True, B:down default
    f = _find(result, "wired.stp.policy.link_mismatch")
    assert f.severity is Severity.WARNING
    assert f.evidence["knob"] == "use_vstp"


def test_both_knobs_mismatched_yield_two_findings_keyed_by_link_and_knob():
    result = _run_one_end_flip("use_vstp", "stp_p2p")
    mm = _findall(result, "wired.stp.policy.link_mismatch")
    assert {f.evidence["knob"] for f in mm} == {"use_vstp", "stp_p2p"}
    assert len({(f.evidence["link"], f.evidence["knob"]) for f in mm}) == 2


def test_preexisting_mismatch_touched_is_info():
    # both states mismatch identically on use_vstp; the delta touches another
    # stp_policy knob (stp_p2p) on the SAME port -> use_vstp mismatch is
    # pre-existing context (INFO), not a fresh WARNING
    base_up = StpPolicy(use_vstp=True)
    prop_up = StpPolicy(use_vstp=True, stp_p2p=True)
    base_ir = _link_mismatch_ir(up_policy=base_up, down_policy=None).build()
    prop_ir = _link_mismatch_ir(up_policy=prop_up, down_policy=None).build()
    result = _run(base_ir, prop_ir)
    mm = _findall(result, "wired.stp.policy.link_mismatch")
    info = [f for f in mm if f.severity is Severity.INFO]
    warn = [f for f in mm if f.severity is Severity.WARNING]
    assert any(f.evidence["knob"] == "use_vstp" for f in info)
    assert not any(f.evidence["knob"] == "use_vstp" for f in warn)
    # stp_p2p is a fresh mismatch introduced by the delta -> WARNING
    assert any(f.evidence["knob"] == "stp_p2p" for f in warn)


def test_preexisting_mismatch_on_unrelated_link_is_not_emitted():
    # Finding 3: a pre-existing use_vstp mismatch on link A:up<->B:down is
    # untouched by a delta that only changes a THIRD device/port's (C:p)
    # stp_policy. The INFO pre-existing-mismatch finding must be scoped to
    # links the delta actually touched -- an unrelated port's policy change
    # must not emit link_mismatch (INFO or otherwise) for A<->B. C:p still
    # gets its own .policy_change floor.
    base_up = StpPolicy(use_vstp=True)  # pre-existing mismatch vs B:down's default False

    def _ir(c_policy: StpPolicy | None):
        b = IRBuilder().add_device(sw("A")).add_device(sw("B")).add_device(sw("C"))
        up_port = Port(id=_UP, device_id="A", name="up", mode=PortMode.TRUNK,
                       tagged_vlans=(10,), stp_policy=base_up)
        down_port = Port(id=_DOWN, device_id="B", name="down", mode=PortMode.TRUNK,
                          tagged_vlans=(10,))
        c_port = Port(id="C:p", device_id="C", name="p", mode=PortMode.ACCESS,
                      native_vlan=10, stp_policy=c_policy)
        b.add_port(up_port).add_port(down_port).add_port(c_port)
        b.add_link(link(_UP, _DOWN))
        b.with_capability(IRCapability.WIRED_L2)
        return b.build()

    base_ir = _ir(None)
    prop_ir = _ir(StpPolicy(stp_p2p=True))
    result = _run(base_ir, prop_ir)
    mm = _findall(result, "wired.stp.policy.link_mismatch")
    assert not mm, mm
    change = _find(result, "wired.stp.policy.policy_change")
    assert change.evidence["port"] == "C:p"


def _run_value_change_with_preexisting_mismatch():
    # use_vstp mismatched identically in both states (pre-existing, untouched
    # by the delta); stp_p2p VALUE changes on the same port (True -> a
    # different truthy value is impossible for bool, so flip False -> True,
    # which is itself the "value change" the floor must cover)
    base_up = StpPolicy(use_vstp=True, stp_p2p=False)
    prop_up = StpPolicy(use_vstp=True, stp_p2p=True)
    base_ir = _link_mismatch_ir(up_policy=base_up, down_policy=None).build()
    prop_ir = _link_mismatch_ir(up_policy=prop_up, down_policy=None).build()
    return _run(base_ir, prop_ir)


def test_info_mismatch_never_satisfies_the_floor():
    # spec P2 round 2: a knob VALUE change on a port with a pre-existing
    # (unchanged) mismatch must yield the INFO context finding AND a
    # delta-caused WARNING .policy_change — INFO-only would be SAFE-able
    result = _run_value_change_with_preexisting_mismatch()
    infos = [f for f in result.findings if f.severity is Severity.INFO]
    warns = [f for f in result.findings if f.severity is Severity.WARNING]
    assert infos and warns
    assert any(f.code == "wired.stp.policy.policy_change" for f in warns)
    assert result.status is Status.WARN  # never PASS on INFO alone


def test_observed_stp_mode_lands_in_evidence_when_present():
    base_ir = _link_mismatch_ir(up_policy=None, down_policy=None).build()
    prop_up_policy = StpPolicy(use_vstp=True)
    prop_ir = _link_mismatch_ir(
        up_policy=prop_up_policy, down_policy=None,
        up_stp_mode=StpMode.VSTP, down_stp_mode=StpMode.RSTP,
    ).build()
    result = _run(base_ir, prop_ir)
    f = _find(result, "wired.stp.policy.link_mismatch")
    assert f.evidence["observed_modes"] == {_UP: StpMode.VSTP, _DOWN: StpMode.RSTP}


def test_link_mismatch_confidence_follows_link_tie_one_sided_is_medium():
    result = _run_one_end_flip("use_vstp", tie="one_sided")
    f = _find(result, "wired.stp.policy.link_mismatch")
    assert f.confidence.level is ConfidenceLevel.MEDIUM


def test_link_mismatch_confidence_follows_link_tie_two_sided_is_high():
    result = _run_one_end_flip("use_vstp", tie="two_sided")
    f = _find(result, "wired.stp.policy.link_mismatch")
    assert f.confidence.level is ConfidenceLevel.HIGH
    assert f.severity is Severity.WARNING  # degradation, not outage — never ERROR


def test_token_end_is_excluded_from_mismatch_and_floors_only():
    # one end's use_vstp is an unresolved: token — token -> floor path, never
    # a mismatch claim (tokens can't produce a precise prediction)
    prop_up_policy = StpPolicy(use_vstp="unresolved:{{v}}")
    base_ir = _link_mismatch_ir(up_policy=None, down_policy=None).build()
    prop_ir = _link_mismatch_ir(up_policy=prop_up_policy, down_policy=None).build()
    result = _run(base_ir, prop_ir)
    assert not _findall(result, "wired.stp.policy.link_mismatch")
    f = _find(result, "wired.stp.policy.policy_change")
    assert "use_vstp" in f.evidence["knobs"]
    assert any("unresolved" in n for n in result.coverage.notes)


def test_delta_resolving_a_preexisting_mismatch_is_floor_only():
    # carried-over pin from Task 6's review: baseline has a use_vstp mismatch
    # (A:up=True, B:down=False); the delta RESOLVES it (A:up flips to False,
    # matching B:down's default False) -> both ends agree in the proposed
    # state, so .link_mismatch must NOT fire (neither WARNING — no fresh
    # disagreement — nor INFO — no disagreement survives to be "pre-existing
    # context" of). The changed port still floors .policy_change WARNING
    # (use_vstp changed on A:up), since the bridge-domain impact of the
    # RESOLUTION itself is not provable either.
    base_up = StpPolicy(use_vstp=True)
    prop_up = StpPolicy(use_vstp=False)
    base_ir = _link_mismatch_ir(up_policy=base_up, down_policy=None).build()
    prop_ir = _link_mismatch_ir(up_policy=prop_up, down_policy=None).build()
    result = _run(base_ir, prop_ir)
    mm = _findall(result, "wired.stp.policy.link_mismatch")
    assert not mm, mm
    f = _find(result, "wired.stp.policy.policy_change")
    assert f.severity is Severity.WARNING
    assert "use_vstp" in f.evidence["knobs"]
