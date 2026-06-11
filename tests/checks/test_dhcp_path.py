"""wired.dhcp.path (GS24): removing a vlan's only modeled DHCP path strands
its clients at lease renewal. Removal with observed clients -> ERROR (UNSAFE
at HIGH); without -> WARNING. A vlan that never had a modeled path is silent
(external servers are invisible — no intent marker exists for DHCP). The
review-series lessons apply from birth: a blind gateway may hold the invisible
replacement server (caps the claim at MEDIUM); unknown client data must not
silently downgrade severity (GS6 doctrine)."""

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, CoverageState, Status
from digital_twin.checks.wired.dhcp_path import DhcpPathCheck
from digital_twin.contracts import Severity
from digital_twin.ir import ConfidenceLevel, IRBuilder, IRCapability, Vlan, diff_ir
from tests.factories import access_port, sw, wired_client


def _ir(*, served=True, with_client=True, blind_gateway=False, clients_fetched=True):
    b = IRBuilder().add_device(sw("S"))
    b.add_port(access_port("S", "ge-0/0/1", vlan=10))
    if blind_gateway:
        from digital_twin.ir.entities import Device, DeviceRole

        b.add_device(Device(id="GW", role=DeviceRole.GATEWAY, site="s1", l3_unmodeled=True))
    b.add_vlan(Vlan(vlan_id=10, name="corp", dhcp_sources=("site",) if served else ()))
    if with_client:
        b.add_client(wired_client("aa:00", "S:ge-0/0/1", vlan=10))
    b.with_capability(IRCapability.WIRED_L2)
    if clients_fetched:
        b.with_capability(IRCapability.CLIENTS_ACTIVE)
    return b.build()


def _run(base, prop):
    return DhcpPathCheck().run(
        CheckContext(
            baseline=AnalysisContext(base), proposed=AnalysisContext(prop), diff=diff_ir(base, prop)
        )
    )


def test_removing_the_path_with_observed_clients_is_unsafe():
    result = _run(_ir(served=True), _ir(served=False))
    assert result.status is Status.FAIL
    f = result.findings[0]
    assert f.code == "wired.dhcp.path.removed"
    assert f.severity is Severity.ERROR and f.confidence.level is ConfidenceLevel.HIGH
    assert f.evidence["vlan"] == 10 and f.evidence["observed_clients"] == 1


def test_removing_the_path_without_clients_is_a_warning():
    result = _run(_ir(served=True, with_client=False), _ir(served=False, with_client=False))
    assert result.status is Status.WARN
    f = result.findings[0]
    assert f.code == "wired.dhcp.path.removed" and f.severity is Severity.WARNING


def test_never_served_vlan_is_silent():
    assert _run(_ir(served=False), _ir(served=False)).findings == ()


def test_kept_path_is_silent():
    assert _run(_ir(served=True), _ir(served=True)).findings == ()


def test_blind_gateway_caps_the_removal_at_review():
    # the invisible replacement DHCP server may live on the unmodeled gateway
    result = _run(
        _ir(served=True, blind_gateway=True), _ir(served=False, blind_gateway=True)
    )
    assert result.status is Status.WARN
    f = result.findings[0]
    assert f.severity is Severity.WARNING and f.confidence.level is ConfidenceLevel.MEDIUM
    assert result.coverage.state is CoverageState.PARTIAL


def test_unknown_client_data_does_not_silently_downgrade():
    # GS6 doctrine: clients unfetched means the blast radius is UNKNOWN — the
    # severity stays WARNING but coverage degrades (REVIEW floor), never a
    # quiet "no clients, just a warning" claim
    result = _run(
        _ir(served=True, with_client=False, clients_fetched=False),
        _ir(served=False, with_client=False, clients_fetched=False),
    )
    assert result.status is Status.WARN
    assert result.coverage.state is CoverageState.PARTIAL
    assert any("client" in n for n in result.coverage.notes)
