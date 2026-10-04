from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.wired.control_plane_reachability import (
    ControlPlaneReachabilityCheck,
)
from digital_twin.checks.wired.dhcp_capacity import DhcpCapacityCheck
from digital_twin.checks.wired.lag_redundancy import LagRedundancyCheck
from digital_twin.checks.wired.static_route_reachability import (
    StaticRouteReachabilityCheck,
)
from digital_twin.checks.wired.storm_control_policy import StormControlPolicyCheck
from digital_twin.checks.wired.vrf_leak import VrfLeakCheck
from digital_twin.contracts import Severity
from digital_twin.ir import (
    AttachKind,
    Client,
    ClientKind,
    Device,
    DeviceRole,
    DhcpScope,
    IRBuilder,
    IRCapability,
    L3Intf,
    L3Role,
    Link,
    LinkKind,
    Port,
    PortMode,
    StaticRoute,
    VrfInstance,
    diff_ir,
)
from digital_twin.ir.entities import PortMisc


def _run(check, base, prop):
    diff = diff_ir(base, prop)
    return check.run(CheckContext(AnalysisContext(base), AnalysisContext(prop), diff))


def _device(builder: IRBuilder, did: str = "d1") -> IRBuilder:
    return builder.add_device(Device(id=did, role=DeviceRole.SWITCH, site="s1"))


def test_dhcp_pool_below_observed_demand_is_unsafe():
    def build(end: str):
        b = _device(IRBuilder()).with_capability(IRCapability.CLIENTS_ACTIVE)
        b.add_port(Port(id="d1:p1", device_id="d1", name="p1", mode=PortMode.ACCESS))
        b.add_dhcp_scope(DhcpScope(
            provider="site", network="corp", vlan=10,
            ip_start="10.0.0.1", ip_end=end,
        ))
        for i in range(1, 4):
            b.add_client(Client(
                mac=f"00000000000{i}", kind=ClientKind.WIRED,
                attach_kind=AttachKind.PORT, attach_id="d1:p1", vlan=10,
            ))
        return b.build()

    result = _run(DhcpCapacityCheck(), build("10.0.0.20"), build("10.0.0.2"))
    assert result.status is Status.FAIL
    assert result.findings[0].code.endswith("below_demand")


def test_unreachable_static_next_hop_requires_review():
    def build(hop: str):
        b = _device(IRBuilder())
        b.add_l3intf(L3Intf(
            device_id="d1", role=L3Role.SVI, subnet="192.0.2.0/24", vlan_id=None,
        ))
        b.add_static_route(StaticRoute(
            device_id="d1", destination="10.0.0.0/8", next_hops=(hop,),
        ))
        return b.build()

    result = _run(
        StaticRouteReachabilityCheck(), build("192.0.2.1"), build("198.51.100.1")
    )
    assert result.status is Status.WARN
    assert result.findings[0].code.endswith("recursive_resolution")


def test_recursive_static_routes_require_review():
    def build(second_hop: str):
        b = _device(IRBuilder())
        b.add_static_route(StaticRoute(
            device_id="d1", destination="10.0.0.0/24", next_hops=("10.0.1.1",)
        ))
        b.add_static_route(StaticRoute(
            device_id="d1", destination="10.0.1.0/24", next_hops=(second_hop,)
        ))
        return b.build()

    result = _run(
        StaticRouteReachabilityCheck(), build("192.0.2.1"), build("10.0.0.1")
    )
    assert any(f.code.endswith("recursive_loop") for f in result.findings)


def test_duplicate_vrf_membership_is_unsafe():
    base = _device(IRBuilder()).add_vrf_instance(
        VrfInstance(device_id="d1", name="corp", networks=("corp",))
    ).build()
    prop = _device(IRBuilder())
    prop.add_vrf_instance(VrfInstance(device_id="d1", name="corp", networks=("corp",)))
    prop.add_vrf_instance(VrfInstance(device_id="d1", name="guest", networks=("corp",)))
    result = _run(VrfLeakCheck(), base, prop.build())
    assert result.status is Status.FAIL
    assert result.findings[0].severity is Severity.ERROR


def _lag_ir(member_names: tuple[str, ...]):
    b = _device(IRBuilder(), "a")
    _device(b, "b")
    for name in ("p1", "p2"):
        b.add_port(Port(id=f"a:{name}", device_id="a", name=name, mode=PortMode.TRUNK))
        b.add_port(Port(id=f"b:{name}", device_id="b", name=name, mode=PortMode.TRUNK))
    for name in member_names:
        b.add_link(Link(
            id=f"a:{name}__b:{name}", a_port=f"a:{name}", b_port=f"b:{name}",
            kind=LinkKind.LAG, bundle_id="ae0",
        ))
    return b.with_capability(IRCapability.L2_TOPOLOGY).build()


def test_lag_final_member_loss_is_unsafe():
    result = _run(LagRedundancyCheck(), _lag_ir(("p1",)), _lag_ir(()))
    assert result.status is Status.FAIL
    assert result.findings[0].code.endswith("last_member_removed")


def test_configured_lag_change_wakes_with_unchanged_lldp_telemetry():
    def build(configured: bool):
        b = _device(IRBuilder(), "a")
        _device(b, "b")
        b.add_port(Port(
            id="a:p1", device_id="a", name="p1", mode=PortMode.TRUNK,
            lag_bundle="ae0" if configured else None,
            lacp_mode="active" if configured else None,
        ))
        b.add_port(Port(
            id="b:p1", device_id="b", name="p1", mode=PortMode.TRUNK,
            lag_bundle="ae0", lacp_mode="active",
        ))
        b.add_link(Link(
            id="a:p1__b:p1", a_port="a:p1", b_port="b:p1",
            kind=LinkKind.LAG, bundle_id="ae0",
        ))
        return b.with_capability(IRCapability.L2_TOPOLOGY).build()

    base, prop = build(True), build(False)
    diff = diff_ir(base, prop)
    check = LagRedundancyCheck()
    assert check.applies_to(diff)
    result = check.run(CheckContext(AnalysisContext(base), AnalysisContext(prop), diff))
    assert result.status is Status.FAIL
    assert any(f.code.endswith("configured_last_member_removed") for f in result.findings)


def test_default_route_loss_floors_control_plane_review():
    base = _device(IRBuilder()).add_static_route(StaticRoute(
        device_id="d1", destination="0.0.0.0/0", next_hops=("192.0.2.1",)
    )).build()
    prop = _device(IRBuilder()).build()
    result = _run(ControlPlaneReachabilityCheck(), base, prop)
    assert result.status is Status.WARN
    assert result.findings[0].code.endswith("default_route_lost")


def test_storm_shutdown_on_uplink_requires_review():
    def build(storm: str | None):
        b = _device(IRBuilder())
        b.add_port(Port(
            id="d1:p1", device_id="d1", name="p1", mode=PortMode.TRUNK,
            is_uplink=True, misc=PortMisc(storm_control=storm) if storm else None,
        ))
        return b.with_capability(IRCapability.WIRED_L2).build()

    result = _run(
        StormControlPolicyCheck(), build(None), build("disable_port=True;percentage=50")
    )
    assert result.status is Status.WARN
    assert result.findings[0].code.endswith("uplink_shutdown")
