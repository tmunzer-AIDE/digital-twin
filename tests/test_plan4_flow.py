"""Plan-4 slice: two IRs -> diff -> registry (all four checks, gating order) ->
verdict/decision. The full ChangePlan->verdict pipeline is Plan 5; this proves
the reasoning half composes: a cut uplink with an active client yields UNSAFE
with blackhole + segmentation + client findings; a cosmetic no-op yields SAFE."""

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.registry import CheckRegistry
from digital_twin.checks.wired import ALL_WIRED_CHECKS
from digital_twin.ir import IRBuilder, IRCapability, Vlan, diff_ir
from digital_twin.verdict.decision import Decision, DecisionInputs
from digital_twin.verdict.verdict import assemble
from tests.factories import access_port, irb, link, sw, trunk_port, wired_client

ALL_CAPS = (
    IRCapability.WIRED_L2,
    IRCapability.L3_EXITS,
    IRCapability.CLIENTS_ACTIVE,
    IRCapability.STP_STATE,
)


def _site(*, connected: bool):
    b = IRBuilder()
    b.add_device(sw("ACCESS")).add_device(sw("CORE"))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    b.add_port(access_port("ACCESS", "acc", 10))
    b.add_port(trunk_port("ACCESS", "up", tagged=(10,)))
    b.add_port(trunk_port("CORE", "down", tagged=(10,)))
    if connected:
        b.add_link(link("ACCESS:up", "CORE:down"))
    b.add_l3intf(irb("CORE", 10))
    b.add_client(wired_client("aa:bb", "ACCESS:acc", vlan=10))
    for cap in ALL_CAPS:
        b.with_capability(cap)
    return b.build()


def _verdict(baseline, proposed):
    diff = diff_ir(baseline, proposed)
    ctx = CheckContext(
        baseline=AnalysisContext(baseline), proposed=AnalysisContext(proposed), diff=diff
    )
    results = CheckRegistry(ALL_WIRED_CHECKS).run_all(ctx)
    return assemble(
        inputs=DecisionInputs(
            rejections=(), l0_fatal=False, baseline_unavailable=False, check_results=results
        ),
        ir_diff=diff,
    )


def test_cutting_the_uplink_is_unsafe_with_three_findings():
    verdict = _verdict(_site(connected=True), _site(connected=False))
    assert verdict.decision is Decision.UNSAFE
    codes = {f.code for f in verdict.findings}
    assert "wired.l2.blackhole.exit_lost" in codes
    assert "wired.l2.vlan_segmentation.split" in codes
    assert "wired.client.impact.active_clients" in codes
    by_id = {r.check_id: r for r in verdict.check_results}
    assert by_id["wired.l2.blackhole"].status is Status.FAIL


def test_identical_irs_yield_safe_via_not_applicable():
    verdict = _verdict(_site(connected=True), _site(connected=True))
    assert verdict.decision is Decision.SAFE
    assert all(r.status is Status.NOT_APPLICABLE for r in verdict.check_results)


def _site_no_clients(connected: bool):
    b = IRBuilder()
    b.add_device(sw("ACCESS")).add_device(sw("CORE"))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    b.add_port(access_port("ACCESS", "acc", 10))
    b.add_port(trunk_port("ACCESS", "up", tagged=(10,)))
    b.add_port(trunk_port("CORE", "down", tagged=(10,)))
    if connected:
        b.add_link(link("ACCESS:up", "CORE:down"))
    b.add_l3intf(irb("CORE", 10))
    b.with_capability(IRCapability.WIRED_L2).with_capability(IRCapability.L3_EXITS)
    return b.build()


def test_missing_client_capability_floors_review_not_safe():
    # an in-domain change with client data missing: client.impact INSUFFICIENT_DATA
    verdict = _verdict(_site_no_clients(True), _site_no_clients(False))
    by_id = {r.check_id: r for r in verdict.check_results}
    assert by_id["wired.client.impact"].status is Status.INSUFFICIENT_DATA
    assert verdict.decision is not Decision.SAFE  # blind spot can never be SAFE
