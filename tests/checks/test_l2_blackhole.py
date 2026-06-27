"""l2.blackhole: FAIL only when a member component HAD a HIGH-confidence exit
path in IR and LOSES it in IR'; MEDIUM/LOW exit -> WARN; no locatable exit ->
INSUFFICIENT_DATA for that vlan (never PASS); pre-existing strands = context."""

from dataclasses import replace

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.wired.l2_blackhole import L2BlackholeCheck
from digital_twin.contracts import ObjectRef, Severity
from digital_twin.ir import ConfidenceLevel, IRBuilder, IRCapability, Vlan, diff_ir
from digital_twin.verdict.decision import Decision, DecisionInputs, decide
from tests.factories import access_port, irb, link, sw, trunk_port


def _ir(*, connected: bool, with_irb: bool = True, with_member: bool = True):
    """A(member)--B(IRB). connected=False cuts the link (the delta's effect)."""
    b = IRBuilder()
    b.add_device(sw("A")).add_device(sw("B"))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    if with_member:
        b.add_port(access_port("A", "acc", 10))
    b.add_port(trunk_port("A", "up", tagged=(10,)))
    b.add_port(trunk_port("B", "down", tagged=(10,)))
    if connected:
        b.add_link(link("A:up", "B:down"))
    if with_irb:
        b.add_l3intf(irb("B", 10))
    b.with_capability(IRCapability.WIRED_L2).with_capability(IRCapability.L3_EXITS)
    return b.build()


def _ctx(baseline, proposed):
    return CheckContext(
        baseline=AnalysisContext(baseline),
        proposed=AnalysisContext(proposed),
        diff=diff_ir(baseline, proposed),
    )


def test_losing_a_high_confidence_exit_fails():
    result = L2BlackholeCheck().run(_ctx(_ir(connected=True), _ir(connected=False)))
    assert result.status is Status.FAIL
    f = result.findings[0]
    assert f.code == "wired.l2.blackhole.exit_lost"
    assert f.severity is Severity.CRITICAL   # gateway path lost -> top severity
    assert "10" in f.message  # names the vlan


def test_exit_lost_below_high_confidence_is_warning():
    # A below-HIGH (LOW) exit confidence downgrades exit_lost to WARNING —
    # only HIGH-confidence "gateway path lost" rises to CRITICAL.
    # Topology: A--B--GW. Baseline: A reaches GW via B (B--GW is LLDP_ONE_SIDED
    # -> LOW exit). Proposed: A's link to B is cut; B still has the LOW-confidence
    # uplink to GW, so the exit IS locatable (BOUNDARY_UPLINK/LOW) — but A's
    # member component no longer reaches it -> exit_lost at LOW -> WARNING.
    from digital_twin.ir.entities import Device, DeviceRole
    from digital_twin.ir.provenance import Provenance

    def site(a_connected: bool):
        b = IRBuilder()
        b.add_device(sw("A"))
        b.add_device(sw("B"))
        b.add_device(Device(id="GW", role=DeviceRole.GATEWAY, site="s1"))
        b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
        b.add_port(access_port("A", "acc", 10))
        b.add_port(trunk_port("A", "up", tagged=(10,)))
        b.add_port(trunk_port("B", "to-A", tagged=(10,)))
        b.add_port(trunk_port("B", "to-gw", tagged=(10,)))
        b.add_port(trunk_port("GW", "down", tagged=(10,)))
        if a_connected:
            b.add_link(link("A:up", "B:to-A"))  # HIGH (CONFIG)
        b.add_link(link("B:to-gw", "GW:down", prov=Provenance.LLDP_ONE_SIDED))  # LOW exit
        b.with_capability(IRCapability.WIRED_L2).with_capability(IRCapability.L3_EXITS)
        return b.build()

    result = L2BlackholeCheck().run(_ctx(site(True), site(False)))
    f = next(x for x in result.findings if x.code == "wired.l2.blackhole.exit_lost")
    assert f.severity is Severity.WARNING  # below-HIGH exit confidence: not CRITICAL


def test_new_member_stranded_high_confidence_is_error():
    # new_member_stranded at HIGH confidence stays ERROR — "never reached" is
    # not as severe as "lost the gateway". This locks the split so Task 1's
    # CRITICAL change never accidentally affects this code.
    base = _ir(connected=False, with_member=False)
    prop = _ir(connected=False, with_member=True)
    result = L2BlackholeCheck().run(_ctx(base, prop))
    f = next(x for x in result.findings if x.code == "wired.l2.blackhole.new_member_stranded")
    assert f.severity is Severity.ERROR  # "never reached", not "lost the gateway"


def test_still_connected_passes():
    result = L2BlackholeCheck().run(_ctx(_ir(connected=True), _ir(connected=True)))
    assert result.status is Status.PASS


def test_no_locatable_exit_is_insufficient_data():
    base = _ir(connected=True, with_irb=False)
    prop = _ir(connected=False, with_irb=False)
    result = L2BlackholeCheck().run(_ctx(base, prop))
    assert result.status is Status.INSUFFICIENT_DATA  # exit unlocatable, never PASS


def test_unchanged_unlocatable_strand_is_context_not_insufficient():
    # the vlan's structure is IDENTICAL in baseline and proposed (the delta did
    # not touch it): a pre-existing unlocatable-exit strand is CONTEXT, else
    # every cosmetic change on a site with such vlans would cry REVIEW forever
    base = _ir(connected=False, with_irb=False)
    prop = _ir(connected=False, with_irb=False)
    result = L2BlackholeCheck().run(_ctx(base, prop))
    assert result.status is Status.PASS
    assert any("preexisting_unlocatable" in f.code for f in result.findings)


def test_newly_added_member_on_isolated_switch_fails():
    # the review's P1: the delta ADDS the first member (access port) on a switch
    # with no path to the vlan's exit — a newly INTRODUCED blackhole, not
    # "preexisting" (configured-but-empty access ports count as membership)
    base = _ir(connected=False, with_member=False)
    prop = _ir(connected=False, with_member=True)
    result = L2BlackholeCheck().run(_ctx(base, prop))
    assert result.status is Status.FAIL
    assert any(f.code == "wired.l2.blackhole.new_member_stranded" for f in result.findings)


def test_new_member_port_on_already_stranded_node_fails():
    # the review's P1 round 3: A:acc1 is already blackholed (context), but the
    # delta adds A:acc2 — a NEW member port with no path to the exit. Node-level
    # overlap must not hide it; attribution is per member PORT.
    def site(ports):
        b = IRBuilder()
        b.add_device(sw("A")).add_device(sw("CORE"))
        b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
        b.add_l3intf(irb("CORE", 10))
        b.add_port(trunk_port("CORE", "down", tagged=(10,)))
        for p in ports:
            b.add_port(access_port("A", p, 10))
        b.with_capability(IRCapability.WIRED_L2).with_capability(IRCapability.L3_EXITS)
        return b.build()

    result = L2BlackholeCheck().run(_ctx(site(["acc1"]), site(["acc1", "acc2"])))
    assert result.status is Status.FAIL
    f = next(x for x in result.findings if x.code == "wired.l2.blackhole.new_member_stranded")
    assert "A:acc2" in f.evidence["new_member_ports"]
    assert "A:acc1" not in f.evidence["new_member_ports"]  # acc1 IS pre-existing


def test_transit_only_vlan_low_exit_does_not_taint_confidence():
    # the review's P2: a vlan with NO members doesn't rely on its exit — its
    # LOW boundary-uplink confidence must not floor the whole check (and with
    # it every unrelated benign change) to REVIEW
    from digital_twin.ir import ConfidenceLevel
    from digital_twin.ir.entities import Device, DeviceRole
    from digital_twin.ir.provenance import Provenance

    def transit(suffix: str):
        b = IRBuilder()
        b.add_device(sw("A"))
        b.add_device(Device(id="GW", role=DeviceRole.GATEWAY, site="s1"))
        b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
        b.add_port(trunk_port("A", "up", tagged=(10,)))
        b.add_port(trunk_port("GW", "down", tagged=(10,)))
        b.add_link(link("A:up", "GW:down", prov=Provenance.LLDP_ONE_SIDED))  # LOW exit
        b.add_vlan(Vlan(vlan_id=99, name="x", scope="s1"))
        b.add_port(access_port("A", f"acc-{suffix}", 99))  # unrelated benign diff
        b.add_l3intf(irb("A", 99))
        b.with_capability(IRCapability.WIRED_L2).with_capability(IRCapability.L3_EXITS)
        return b.build()

    result = L2BlackholeCheck().run(_ctx(transit("a"), transit("b")))
    assert result.status is Status.PASS
    assert result.confidence is not None
    assert result.confidence.level is ConfidenceLevel.HIGH  # LOW exit not consulted


def _gs7_site(connected: bool):
    """GS7: AP with an observed vlan-30 wireless client; delta cuts the AP uplink."""
    from tests.factories import ap, wireless_client

    b = IRBuilder()
    b.add_device(sw("SW")).add_device(ap("AP1"))
    b.add_vlan(Vlan(vlan_id=30, name="voice", scope="s1"))
    b.add_port(trunk_port("SW", "to-ap", tagged=(30,)))
    b.add_port(trunk_port("AP1", "eth0", tagged=(30,)))
    if connected:
        b.add_link(link("AP1:eth0", "SW:to-ap"))
    b.add_l3intf(irb("SW", 30))
    b.add_client(wireless_client("ww:01", "AP1", vlan=30))
    for c in (IRCapability.WIRED_L2, IRCapability.L3_EXITS, IRCapability.CLIENTS_ACTIVE):
        b.with_capability(c)
    return b.build()


def test_gs7_wireless_client_isolated_by_uplink_removal_fails():
    # observed wireless clients ARE membership (observation-based, per spec):
    # cutting the AP uplink strands the vlan-30 wireless client -> FAIL
    result = L2BlackholeCheck().run(_ctx(_gs7_site(True), _gs7_site(False)))
    assert result.status is Status.FAIL
    f = next(x for x in result.findings if "exit_lost" in x.code)
    assert "AP1" in f.affected_entities


def test_gs7_zero_client_ap_isolation_floors_partial_coverage():
    # the spec's zero-observed variant: the AP carried vlan 30 in baseline and
    # the delta cuts it off, but NO clients are currently observed. The wireless
    # impact is UNKNOWABLE (future clients), not absent -> coverage PARTIAL
    # (-> REVIEW), never a clean complete PASS.
    from digital_twin.checks.base import CoverageState
    from tests.factories import ap

    def site(connected: bool):
        b = IRBuilder()
        b.add_device(sw("SW")).add_device(ap("AP1"))
        b.add_vlan(Vlan(vlan_id=30, name="voice", scope="s1"))
        b.add_port(trunk_port("SW", "to-ap", tagged=(30,)))
        b.add_port(trunk_port("AP1", "eth0", tagged=(30,)))
        if connected:
            b.add_link(link("AP1:eth0", "SW:to-ap"))
        b.add_l3intf(irb("SW", 30))
        for c in (IRCapability.WIRED_L2, IRCapability.L3_EXITS, IRCapability.CLIENTS_ACTIVE):
            b.with_capability(c)
        return b.build()

    result = L2BlackholeCheck().run(_ctx(site(True), site(False)))
    assert result.status is Status.PASS  # nothing observable broke...
    assert result.coverage.state is CoverageState.PARTIAL  # ...but it's a blind spot
    assert any("AP1" in n and "30" in n for n in result.coverage.notes)


def test_no_ap_change_keeps_complete_coverage():
    # with client data present and no AP-side change there is no blind spot —
    # a stable wired-only site must not floor every delta to REVIEW
    from digital_twin.checks.base import CoverageState

    def wired_site(connected: bool):
        b = IRBuilder()
        b.add_device(sw("A")).add_device(sw("B"))
        b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
        b.add_port(access_port("A", "acc", 10))
        b.add_port(trunk_port("A", "up", tagged=(10,)))
        b.add_port(trunk_port("B", "down", tagged=(10,)))
        if connected:
            b.add_link(link("A:up", "B:down"))
        b.add_l3intf(irb("B", 10))
        for c in (IRCapability.WIRED_L2, IRCapability.L3_EXITS, IRCapability.CLIENTS_ACTIVE):
            b.with_capability(c)
        return b.build()

    result = L2BlackholeCheck().run(_ctx(wired_site(True), wired_site(True)))
    assert result.coverage.state is CoverageState.COMPLETE


def test_wireless_membership_marks_coverage_partial_when_vlan_changed():
    # spec: AP-side membership is observation-based — a KNOWN coverage gap, but
    # only for conclusions that RELIED on it (the delta touched that vlan);
    # an unchanged wireless vlan must not floor every cosmetic delta to REVIEW
    from digital_twin.checks.base import CoverageState

    changed = L2BlackholeCheck().run(_ctx(_gs7_site(True), _gs7_site(False)))
    assert changed.coverage.state is CoverageState.PARTIAL
    assert any("observation-based" in n for n in changed.coverage.notes)

    unchanged = L2BlackholeCheck().run(_ctx(_gs7_site(True), _gs7_site(True)))
    assert unchanged.coverage.state is CoverageState.COMPLETE


def test_preexisting_strand_is_context_not_failure():
    # already disconnected in baseline -> not attributed to the delta
    base = _ir(connected=False)
    prop = _ir(connected=False)
    result = L2BlackholeCheck().run(_ctx(base, prop))
    assert result.status is Status.PASS
    assert any(f.severity is Severity.INFO for f in result.findings)


# --- CA-T12: cause attribution on exit_lost / exit_unlocatable -----------------------


def _ids(causes):
    return sorted((c.ref.kind, c.ref.id) for c in causes)


def _exit_lost_ir(cut: bool):
    """A -- B -- C on vlan 10, exit IRB on A. C holds a member access port. The
    delta drops vlan 10 from B's trunk port toward C, stranding {C} -> exit_lost,
    attributed to the changed boundary trunk port B:to-C."""
    b = IRBuilder()
    for d in ("A", "B", "C"):
        b.add_device(sw(d))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    b.add_l3intf(irb("A", 10))
    b.add_port(trunk_port("A", "to-B", tagged=(10,)))
    b.add_port(trunk_port("B", "to-A", tagged=(10,)))
    b.add_link(link("A:to-B", "B:to-A"))
    b.add_port(trunk_port("B", "to-C", tagged=() if cut else (10,)))
    b.add_port(trunk_port("C", "to-B", tagged=(10,)))
    b.add_link(link("B:to-C", "C:to-B"))
    b.add_port(access_port("C", "acc", 10))
    b.with_capability(IRCapability.WIRED_L2).with_capability(IRCapability.L3_EXITS)
    return b.build()


def test_exit_lost_names_boundary_trunk_port():
    result = L2BlackholeCheck().run(_ctx(_exit_lost_ir(cut=False), _exit_lost_ir(cut=True)))
    f = next(f for f in result.findings if f.code == "wired.l2.blackhole.exit_lost")
    assert _ids(f.caused_by) == [("port", "B:to-C")]


def _unlocatable_ir(rm_irb: bool):
    """A -- B on vlan 10, B holds a member. The ONLY exit is the IRB on A; the
    delta removes it, so the vlan has members but NO locatable exit ->
    exit_unlocatable, attributed to the removed l3intf A:l3:irb:10."""
    b = IRBuilder()
    b.add_device(sw("A")).add_device(sw("B"))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    b.add_port(trunk_port("A", "to-B", tagged=(10,)))
    b.add_port(trunk_port("B", "to-A", tagged=(10,)))
    b.add_link(link("A:to-B", "B:to-A"))
    b.add_port(access_port("B", "acc", 10))
    if not rm_irb:
        b.add_l3intf(irb("A", 10))
    b.with_capability(IRCapability.WIRED_L2).with_capability(IRCapability.L3_EXITS)
    return b.build()


def test_exit_unlocatable_names_removed_irb():
    base, prop = _unlocatable_ir(rm_irb=False), _unlocatable_ir(rm_irb=True)
    result = L2BlackholeCheck().run(_ctx(base, prop))
    f = next(f for f in result.findings if f.code == "wired.l2.blackhole.exit_unlocatable")
    assert _ids(f.caused_by) == [("l3intf", "A:l3:irb:10")]


def _never_had_exit_ir(extra_member: bool):
    """vlan 10 NEVER had a locatable exit (no IRB, no uplink boundary) in EITHER
    baseline or proposed. A -- B carry it; B holds a member, so it is stranded in
    both sides. The delta ADDS a fresh stranded vlan-10 access port on an ISOLATED
    switch C (no link to anyone). This genuinely changes vlan 10's structure
    (`_vlan_changed` True via the new {C} component) so the `exit_unlocatable`
    branch fires — yet it is honest-empty: causes_for_blackhole = boundary edges
    LOST (none — nothing was removed, only added) ∪ removed exit l3intf (none —
    there never was one) = (). The new member is in the delta but it is an ADD,
    not a cut, so nothing attributable caused the unlocatable exit."""
    b = IRBuilder()
    b.add_device(sw("A")).add_device(sw("B"))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    b.add_port(trunk_port("A", "to-B", tagged=(10,)))
    b.add_port(trunk_port("B", "to-A", tagged=(10,)))
    b.add_link(link("A:to-B", "B:to-A"))
    b.add_port(access_port("B", "acc", 10))
    if extra_member:
        # a NEW stranded vlan-10 access port on an isolated switch: changes vlan
        # 10's own structure (so the row fires) WITHOUT removing carriage or an
        # exit l3intf (so causes_for_blackhole stays honest-empty)
        b.add_device(sw("C"))
        b.add_port(access_port("C", "acc", 10))
    b.with_capability(IRCapability.WIRED_L2).with_capability(IRCapability.L3_EXITS)
    return b.build()


def test_exit_unlocatable_honest_empty_when_exit_already_none():
    # vlan 10's exit is None in BOTH sides; the delta only ADDS a fresh stranded
    # vlan-10 member (no carriage cut, no removed exit). The exit_unlocatable row
    # IS emitted (vlan 10's structure changed) and must carry no cause.
    base = _never_had_exit_ir(extra_member=False)
    prop = _never_had_exit_ir(extra_member=True)
    result = L2BlackholeCheck().run(_ctx(base, prop))
    # non-vacuous: this MUST find an emitted exit_unlocatable row for vlan 10 —
    # a StopIteration here means the fixture is wrong, not a silent pass
    row = next(
        f
        for f in result.findings
        if f.code == "wired.l2.blackhole.exit_unlocatable"
        and f.subject == ObjectRef("vlan", "10")
    )
    assert row.caused_by == ()


# --- INFERRED_UPLINK downstream cases -----------------------------------------------


def _uplink(did, name, vid):  # an is_uplink trunk toward an UNMODELED core
    return replace(trunk_port(did, name, tagged=(vid,)), is_uplink=True)


def _ab(*, extra_member=False, uplink_disabled=False):
    # A(member access) -- B(is_uplink toward unmodeled core); no IRB, no gateway
    b = IRBuilder()
    b.add_device(sw("A")).add_device(sw("B"))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    b.add_port(access_port("A", "acc", 10))
    if extra_member:
        b.add_port(access_port("A", "acc2", 10))  # the delta: a new vlan-10 member
    b.add_port(trunk_port("A", "up", tagged=(10,)))
    b.add_port(trunk_port("B", "down", tagged=(10,)))
    core = _uplink("B", "core", 10)
    if uplink_disabled:
        core = replace(core, disabled=True)
    b.add_port(core)
    b.add_link(link("A:up", "B:down"))
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def _abc(*, sever=False):
    # A(member) -- B -- C(is_uplink toward unmodeled core); delta severs A's uplink
    b = IRBuilder()
    for d in ("A", "B", "C"):
        b.add_device(sw(d))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    b.add_port(access_port("A", "acc", 10))
    a_up = trunk_port("A", "up", tagged=(10,))
    if sever:
        a_up = replace(a_up, disabled=True)  # the delta: A loses its path upstream
    b.add_port(a_up)
    b.add_port(trunk_port("B", "da", tagged=(10,)))
    b.add_port(trunk_port("B", "uc", tagged=(10,)))
    b.add_port(trunk_port("C", "dc", tagged=(10,)))
    b.add_port(_uplink("C", "core", 10))  # untouched -> exit survives in proposed
    b.add_link(link("A:up", "B:da"))
    b.add_link(link("B:uc", "C:dc"))
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def test_inferred_uplink_intact_is_review_not_safe():
    # case 1: a changed vlan still reaches its inferred uplink -> structural PASS,
    # but result confidence LOW -> decision floors REVIEW (never SAFE), and NO
    # exit_unlocatable noise is emitted.
    result = L2BlackholeCheck().run(_ctx(_ab(), _ab(extra_member=True)))
    assert result.status is Status.PASS
    assert result.confidence is not None and result.confidence.level is ConfidenceLevel.LOW
    assert not any("unlocatable" in f.code for f in result.findings)
    decision, _ = decide(
        DecisionInputs(rejections=(), l0_fatal=False, baseline_unavailable=False,
                       check_results=(result,))
    )
    assert decision is Decision.REVIEW  # the LOW result confidence floors it; never SAFE


def test_inferred_uplink_severed_is_exit_lost_warning():
    # case 2: the delta cuts A off from the SURVIVING inferred uplink at C ->
    # sharper exit_lost (WARNING at LOW exit confidence), not vague unlocatable.
    result = L2BlackholeCheck().run(_ctx(_abc(sever=False), _abc(sever=True)))
    f = next(f for f in result.findings if f.code == "wired.l2.blackhole.exit_lost")
    assert f.severity is Severity.WARNING
    assert not any("unlocatable" in x.code for x in result.findings)


def test_inferred_uplink_last_uplink_removed_stays_unlocatable():
    # case 3: disabling the sole qualifying uplink removes the inferred exit in
    # proposed -> NONE -> exit_unlocatable (unchanged; the exit genuinely vanished)
    result = L2BlackholeCheck().run(_ctx(_ab(), _ab(uplink_disabled=True)))
    assert any(f.code == "wired.l2.blackhole.exit_unlocatable" for f in result.findings)
