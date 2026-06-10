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
from tests.factories import access_port, link, sw, trunk_port, wired_client


def _ir(*, uplink_disabled: bool, blind_peer: bool = False):
    """A(member+client) --up/down-- B. The delta disables A's uplink."""
    b = IRBuilder()
    b.add_device(sw("A")).add_device(sw("B"))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
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
    b.add_link(link("A:up", "B:down"))  # link() default meta is HIGH (two-sided)
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
