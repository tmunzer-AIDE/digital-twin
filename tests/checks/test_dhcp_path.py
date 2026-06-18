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


def _ir(
    *,
    served=True,
    with_client=True,
    blind_gateway=False,
    dhcp_unresolved_gateway=False,
    clients_fetched=True,
):
    b = IRBuilder().add_device(sw("S"))
    b.add_port(access_port("S", "ge-0/0/1", vlan=10))
    if blind_gateway or dhcp_unresolved_gateway:
        from digital_twin.ir.entities import Device, DeviceRole

        b.add_device(
            Device(
                id="GW",
                role=DeviceRole.GATEWAY,
                site="s1",
                l3_unmodeled=blind_gateway,
                dhcp_unresolved=dhcp_unresolved_gateway,
            )
        )
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


def test_unresolved_gateway_dhcp_reference_caps_the_removal_at_review():
    # review on 80c4c48 (P1): the namespace WAS fetched but a gateway dhcpd
    # entry's name did not resolve — the gateway may serve DHCP on a vlan we
    # cannot identify; the removal claim cannot be ERROR/HIGH/COMPLETE
    result = _run(
        _ir(served=True, dhcp_unresolved_gateway=True),
        _ir(served=False, dhcp_unresolved_gateway=True),
    )
    assert result.status is Status.WARN
    f = result.findings[0]
    assert f.severity is Severity.WARNING and f.confidence.level is ConfidenceLevel.MEDIUM
    assert result.coverage.state is CoverageState.PARTIAL


def test_clients_known_requires_the_capability_on_both_sides():
    # review on 80c4c48 (P2): the count is BASELINE-derived — a baseline
    # without CLIENTS_ACTIVE but carrying stale client rows must not yield a
    # confident ERROR from those rows
    result = _run(
        _ir(served=True, with_client=True, clients_fetched=False),
        _ir(served=False, with_client=False, clients_fetched=True),
    )
    assert result.status is Status.WARN
    assert result.findings[0].severity is Severity.WARNING
    assert result.coverage.state is CoverageState.PARTIAL
    assert any("client" in n for n in result.coverage.notes)


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


# --- caused_by attribution (CA Task 11) ---


def _gw_scope_ir(*, served, scope_present, gw_present=True):
    """vlan 10 served (in baseline) by gateway GW's own dhcpd_config; the
    serving DhcpScope (provider=GW) is present iff scope_present. When
    served=False the vlan loses dhcp_sources AND the scope is dropped. When
    gw_present=False the GW device itself is gone (an add/remove delta)."""
    from digital_twin.ir.entities import Device, DeviceRole, DhcpScope

    b = IRBuilder().add_device(sw("S"))
    if gw_present:
        b.add_device(Device(id="GW", role=DeviceRole.GATEWAY, site="s1"))
    b.add_port(access_port("S", "ge-0/0/1", vlan=10))
    b.add_vlan(Vlan(vlan_id=10, name="corp", dhcp_sources=("GW",) if served else ()))
    if scope_present:
        b.add_dhcp_scope(DhcpScope(provider="GW", network="corp", vlan=10))
    b.add_client(wired_client("aa:00", "S:ge-0/0/1", vlan=10))
    b.with_capability(IRCapability.WIRED_L2)
    b.with_capability(IRCapability.CLIENTS_ACTIVE)
    return b.build()


def test_removed_attributes_the_removed_gateway_source_and_scope():
    # baseline serves vlan 10 via gateway GW (+ its DhcpScope); proposed drops
    # the GW device itself (and with it the source + scope) -> the removed
    # finding names the gateway device AND the removed dhcp_scope as causes
    result = _run(
        _gw_scope_ir(served=True, scope_present=True, gw_present=True),
        _gw_scope_ir(served=False, scope_present=False, gw_present=False),
    )
    f = next(x for x in result.findings if x.code.endswith(".removed"))
    causes = {(c.ref.kind, c.ref.id) for c in f.caused_by}
    assert ("device", "GW") in causes
    assert ("dhcp_scope", "GW:corp") in causes


def test_removed_attributes_the_vlan_when_only_dhcp_sources_changed():
    # the scope stays put; only the vlan's dhcp_sources tuple emptied -> the
    # vlan itself is the changed entity and is named
    result = _run(
        _gw_scope_ir(served=True, scope_present=True),
        _gw_scope_ir(served=False, scope_present=True),
    )
    f = next(x for x in result.findings if x.code.endswith(".removed"))
    causes = {(c.ref.kind, c.ref.id) for c in f.caused_by}
    assert ("vlan", "10") in causes
