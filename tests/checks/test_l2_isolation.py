"""l2.isolation: a member/client-bearing fragment physically severed from the
rest of its baseline L2 domain -> ERROR at the severed LINKS' confidence (their
existence proves the lost reach — NOT the edges' carriage confidence, which may
be capped by blind-peer assumptions); pre-existing islands = no finding."""

from dataclasses import replace

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.wired.l2_isolation import L2IsolationCheck
from digital_twin.contracts import Severity
from digital_twin.ir import ConfidenceLevel, IRBuilder, IRCapability, Port, PortMode, Vlan, diff_ir
from digital_twin.ir.provenance import Provenance, fact_meta
from tests.factories import access_port, irb, link, sw, trunk_port, wired_client


def _ir(
    *,
    uplink_disabled: bool,
    blind_peer: bool = False,
    b_has_irb: bool = False,
    link_prov: Provenance = Provenance.LLDP_TWO_SIDED,
):
    """A(member+client) --up/down-- B. The delta disables A's uplink."""
    b = IRBuilder()
    b.add_device(sw("A")).add_device(sw("B"))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    if b_has_irb:
        b.add_l3intf(irb("B", 10))  # B owns a routed IRB -> B is an exit anchor
    acc = access_port("A", "acc", 10)
    b.add_port(acc)
    b.add_client(wired_client("cc:01", acc.id, vlan=10))
    up = trunk_port("A", "up", tagged=(10,))
    if uplink_disabled:
        up = replace(up, disabled=True)
    b.add_port(up)
    down = trunk_port("B", "down", tagged=(10,))
    if blind_peer:  # stat-ensured peer: no vlan facts, but the LINK is two-sided HIGH
        down = Port(
            id="B:down",
            device_id="B",
            name="down",
            mode=PortMode.TRUNK,
            meta=fact_meta(Provenance.OBSERVED),
        )
    b.add_port(down)
    b.add_link(link("A:up", "B:down", prov=link_prov))
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def _run(baseline, proposed):
    return L2IsolationCheck().run(
        CheckContext(
            baseline=AnalysisContext(baseline),
            proposed=AnalysisContext(proposed),
            diff=diff_ir(baseline, proposed),
        )
    )


def test_disabling_the_only_uplink_severs_the_member_fragment():
    result = _run(_ir(uplink_disabled=False), _ir(uplink_disabled=True))
    assert result.status is Status.FAIL
    f = next(f for f in result.findings if "A" in f.affected_entities)
    assert f.severity is Severity.ERROR
    assert f.confidence.level is ConfidenceLevel.HIGH
    assert f.evidence["lost_anchor_nodes"] == []  # exit-less home -> no anchor lost -> ERROR
    assert f.evidence["exit_anchor_nodes"] == []  # no exit anywhere in this domain
    assert f.evidence["severity_reason"] == "physical severance, no surviving exit anchor"


def test_severed_from_a_surviving_exit_anchor_is_critical():
    # A is cut from B, and B owns a routed IRB (a surviving exit anchor on the
    # far side) -> the severance is the top-severity "lost the gateway" event.
    result = _run(
        _ir(uplink_disabled=False, b_has_irb=True),
        _ir(uplink_disabled=True, b_has_irb=True),
    )
    assert result.status is Status.FAIL
    f = next(f for f in result.findings if "A" in f.affected_entities)
    assert f.severity is Severity.CRITICAL
    assert "B" in f.evidence["lost_anchor_nodes"]
    assert "B" in f.evidence["exit_anchor_nodes"]
    assert f.evidence["severity_reason"] == "severed from a surviving exit anchor"


def test_below_high_severance_with_anchor_is_warning():
    # same severed-from-anchor topology, but the severed link is one-sided LLDP
    # (LOW) -> severance confidence is below HIGH, so it stays WARNING even though
    # an exit anchor exists (the `high` gate dominates).
    result = _run(
        _ir(uplink_disabled=False, b_has_irb=True, link_prov=Provenance.LLDP_ONE_SIDED),
        _ir(uplink_disabled=True, b_has_irb=True, link_prov=Provenance.LLDP_ONE_SIDED),
    )
    f = next(f for f in result.findings if "A" in f.affected_entities)
    assert f.severity is Severity.WARNING
    assert "B" in f.evidence["lost_anchor_nodes"]  # anchor present, but high gate dominates
    assert "B" in f.evidence["exit_anchor_nodes"]
    assert f.evidence["severity_reason"] == "physical severance, severance confidence below HIGH"


def test_severed_with_a_newly_added_far_side_anchor_is_not_critical():
    # the delta severs A from B AND adds a brand-new IRB on B. A never had reach
    # to a gateway in baseline (B's anchor did not exist then), so it did not LOSE
    # one -> ERROR, not CRITICAL. Mirrors blackhole.exit_lost, which fires only on
    # a BASELINE exit that is no longer reached, not on a proposed-only exit.
    result = _run(
        _ir(uplink_disabled=False, b_has_irb=False),  # baseline: B is NOT an anchor
        _ir(uplink_disabled=True, b_has_irb=True),  # proposed: B gains an IRB, A severed
    )
    f = next(f for f in result.findings if "A" in f.affected_entities)
    assert f.severity is Severity.ERROR
    assert f.evidence["lost_anchor_nodes"] == []  # no baseline anchor to lose
    assert "B" in f.evidence["exit_anchor_nodes"]  # B is a proposed anchor, but new


def test_severance_confidence_comes_from_the_link_not_assumed_carriage():
    # the blind-peer rule caps the EDGE's carriage confidence at MEDIUM, but the
    # LINK's existence is two-sided HIGH — severing it is a HIGH conclusion
    result = _run(
        _ir(uplink_disabled=False, blind_peer=True), _ir(uplink_disabled=True, blind_peer=True)
    )
    assert result.status is Status.FAIL
    f = next(f for f in result.findings if "A" in f.affected_entities)
    assert f.confidence.level is ConfidenceLevel.HIGH, f.confidence


def test_preexisting_island_is_not_a_finding():
    ir = _ir(uplink_disabled=True)  # already severed in baseline AND proposed
    result = _run(ir, ir)
    assert result.status is Status.PASS and result.findings == ()


# --- CA-T13: cause attribution on isolation.severed ---------------------------------


def _ids(causes):
    return sorted((c.ref.kind, c.ref.id) for c in causes)


def test_isolation_finding_names_the_disabled_uplink_port():
    # disabling A's only uplink severs {A}; the cause is the changed boundary
    # port A:up (its `disabled` field flipped, dropping the L2 edge)
    result = _run(_ir(uplink_disabled=False), _ir(uplink_disabled=True))
    f = next(f for f in result.findings if "A" in f.affected_entities)
    assert ("port", "A:up") in _ids(f.caused_by)


def test_preexisting_island_has_no_cause():
    # already severed in baseline AND proposed -> no finding emitted at all, so
    # there is nothing carrying a (spurious) cause
    ir = _ir(uplink_disabled=True)
    result = _run(ir, ir)
    assert result.findings == ()


# --- Exit anchor guard: suppress survivors that retain L3 exits ----------------------


def _anchored_ir(*, link_disabled: bool):
    """core(IRB vlan10, member+client) --trunk link-- leaf(member+client).
    Both sides are OCCUPIED; only `core` holds an exit anchor (its IRB)."""
    b = IRBuilder()
    b.add_device(sw("core")).add_device(sw("leaf"))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    b.add_l3intf(irb("core", 10, subnet="10.0.10.0/24"))  # core is an exit anchor
    c_acc = access_port("core", "cacc", 10)
    l_acc = access_port("leaf", "lacc", 10)
    b.add_port(c_acc).add_port(l_acc)
    b.add_client(wired_client("cc:core", c_acc.id, vlan=10))
    b.add_client(wired_client("cc:leaf", l_acc.id, vlan=10))
    c_up = trunk_port("core", "up", tagged=(10,))
    if link_disabled:
        c_up = replace(c_up, disabled=True)
    b.add_port(c_up)
    b.add_port(trunk_port("leaf", "down", tagged=(10,)))
    b.add_link(link("core:up", "leaf:down"))
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def test_exit_anchored_survivor_not_flagged_only_the_leaf():
    # disabling the link severs leaf from core. core is occupied AND a strict
    # subset of the baseline domain, but it keeps its IRB -> NOT flagged.
    result = _run(_anchored_ir(link_disabled=False), _anchored_ir(link_disabled=True))
    flagged = {n for f in result.findings for n in f.affected_entities}
    assert "leaf" in flagged       # the cut-off, anchor-less side IS flagged
    assert "core" not in flagged   # the survivor keeps an exit -> NOT flagged


def test_both_sides_keep_an_exit_neither_flagged():
    def _ir(*, link_disabled: bool):
        b = IRBuilder()
        b.add_device(sw("a")).add_device(sw("b"))
        b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
        b.add_l3intf(irb("a", 10, subnet="10.0.10.0/24"))
        b.add_l3intf(irb("b", 10, subnet="10.0.11.0/24"))
        a_acc, b_acc = access_port("a", "aacc", 10), access_port("b", "bacc", 10)
        b.add_port(a_acc).add_port(b_acc)
        b.add_client(wired_client("cc:a", a_acc.id, vlan=10))
        b.add_client(wired_client("cc:b", b_acc.id, vlan=10))
        a_up = trunk_port("a", "up", tagged=(10,))
        if link_disabled:
            a_up = replace(a_up, disabled=True)
        b.add_port(a_up).add_port(trunk_port("b", "down", tagged=(10,)))
        b.add_link(link("a:up", "b:down"))
        b.with_capability(IRCapability.WIRED_L2)
        return b.build()

    result = _run(_ir(link_disabled=False), _ir(link_disabled=True))
    assert result.status is Status.PASS
    assert result.findings == ()


def test_exit_removed_by_delta_flags_the_fragment():
    # baseline: core keeps leaf reachable AND core has an IRB. proposed: the link
    # is cut AND core's IRB is removed -> core retains NO proposed anchor ->
    # core's occupied fragment is flagged (proposed-state anchors only).
    baseline = _anchored_ir(link_disabled=False)
    # rebuild `proposed` WITHOUT core's IRB
    pb = IRBuilder()
    pb.add_device(sw("core")).add_device(sw("leaf"))
    pb.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    c_acc, l_acc = access_port("core", "cacc", 10), access_port("leaf", "lacc", 10)
    pb.add_port(c_acc).add_port(l_acc)
    pb.add_client(wired_client("cc:core", c_acc.id, vlan=10))
    pb.add_client(wired_client("cc:leaf", l_acc.id, vlan=10))
    pb.add_port(replace(trunk_port("core", "up", tagged=(10,)), disabled=True))
    pb.add_port(trunk_port("leaf", "down", tagged=(10,)))
    pb.add_link(link("core:up", "leaf:down"))
    pb.with_capability(IRCapability.WIRED_L2)
    result = _run(baseline, pb.build())
    flagged = {n for f in result.findings for n in f.affected_entities}
    assert "core" in flagged   # IRB gone in proposed -> no anchor -> flagged
    assert "leaf" in flagged


def test_exitless_only_uplink_still_severs_member_side():
    # P1 guard / regression: NO exits modeled anywhere; the stranded member side
    # (with all the occupants) is still flagged even though the upstream stub is
    # empty. A size/majority heuristic would have false-SAFE'd this.
    result = _run(_ir(uplink_disabled=False), _ir(uplink_disabled=True))
    flagged = {n for f in result.findings for n in f.affected_entities}
    assert "A" in flagged
