"""client.impact: enumerate CURRENTLY-CONNECTED clients whose connectivity the
delta changes (vlan_move / disconnect / blackhole), WARN when >=1 affected,
HIGH confidence (observed clients), currently-connected-only caveat in coverage."""

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.wired.client_impact import ClientImpactCheck
from digital_twin.ir import IRBuilder, IRCapability, Vlan, diff_ir
from tests.factories import access_port, irb, link, sw, trunk_port, wired_client


def _ir(*, acc_vlan: int = 10, with_client: bool = True, connected: bool = True):
    b = IRBuilder()
    b.add_device(sw("A")).add_device(sw("B"))
    for vid in (10, 20):
        b.add_vlan(Vlan(vlan_id=vid, name=f"v{vid}", scope="s1"))
    b.add_port(access_port("A", "acc", acc_vlan))
    b.add_port(trunk_port("A", "up", tagged=(10, 20)))
    b.add_port(trunk_port("B", "down", tagged=(10, 20)))
    if connected:
        b.add_link(link("A:up", "B:down"))
    b.add_l3intf(irb("B", 10))
    b.add_l3intf(irb("B", 20))
    if with_client:
        b.add_client(wired_client("aa:aa", "A:acc", vlan=acc_vlan))
    b.with_capability(IRCapability.WIRED_L2).with_capability(IRCapability.CLIENTS_ACTIVE)
    b.with_capability(IRCapability.L3_EXITS)
    return b.build()


def _ctx(baseline, proposed):
    return CheckContext(
        baseline=AnalysisContext(baseline),
        proposed=AnalysisContext(proposed),
        diff=diff_ir(baseline, proposed),
    )


def test_access_vlan_change_flags_vlan_move():
    result = ClientImpactCheck().run(_ctx(_ir(acc_vlan=10), _ir(acc_vlan=20)))
    assert result.status is Status.WARN
    f = result.findings[0]
    assert f.evidence["impacts"][0]["impact"] == "vlan_move"
    assert f.evidence["impacts"][0]["mac"] == "aa:aa"


def test_client_in_blackholed_segment_flags_blackhole():
    result = ClientImpactCheck().run(_ctx(_ir(connected=True), _ir(connected=False)))
    assert result.status is Status.WARN
    impacts = result.findings[0].evidence["impacts"]
    assert any(i["impact"] == "blackhole" and i["mac"] == "aa:aa" for i in impacts)


def test_no_clients_affected_passes_with_caveat():
    result = ClientImpactCheck().run(_ctx(_ir(with_client=False), _ir(with_client=False)))
    assert result.status is Status.PASS
    assert any("currently-connected" in n for n in result.coverage.notes)
