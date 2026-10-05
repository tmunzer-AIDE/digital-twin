"""wired.l3.control_plane_reachability: which port edits count as a path change.

Only the device's OWN routed traffic matters here (management, DNS, NTP, RADIUS,
TACACS, syslog). Adding tagged VLANs that carry no L3 interface of the device
leaves every path that traffic can use unchanged; loop risk on the new VLAN
belongs to wired.l2.loop (per-VLAN cycles with STP evidence).
"""

import pytest

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.wired.control_plane_reachability import ControlPlaneReachabilityCheck
from digital_twin.ir import (
    Device,
    DeviceRole,
    IRBuilder,
    L3Intf,
    L3Role,
    Port,
    PortMode,
    StaticRoute,
    diff_ir,
)

MGMT = 10  # the VLAN carrying the switch's SVI
TRUNK = "d1:xe-0/1/0"


def _ir(*, tagged=(MGMT, 20), native=None, mode=PortMode.TRUNK, disabled=False,
        svi_vlans=(MGMT,), extra_ports=()):
    b = IRBuilder().add_device(Device(id="d1", role=DeviceRole.SWITCH, site="s1"))
    b.add_port(Port(id=TRUNK, device_id="d1", name="xe-0/1/0", mode=mode,
                    native_vlan=native, tagged_vlans=tagged, disabled=disabled))
    for port in extra_ports:
        b.add_port(port)
    for vid in svi_vlans:
        b.add_l3intf(L3Intf(device_id="d1", role=L3Role.SVI, vlan_id=vid,
                            subnet=f"192.0.{vid}.0/24"))
    b.add_static_route(StaticRoute(device_id="d1", destination="0.0.0.0/0",
                                   next_hops=(f"192.0.{MGMT}.1",)))
    return b.build()


def _run(base, prop):
    diff = diff_ir(base, prop)
    return ControlPlaneReachabilityCheck().run(
        CheckContext(AnalysisContext(base), AnalysisContext(prop), diff)
    )


def test_adding_a_layer2_only_vlan_to_a_trunk_is_not_a_path_change():
    result = _run(_ir(tagged=(MGMT, 20)), _ir(tagged=(MGMT, 20, 199)))
    assert result.status is Status.PASS, result.findings
    assert not result.findings


def test_reordering_tagged_vlans_is_not_a_path_change():
    result = _run(_ir(tagged=(MGMT, 20)), _ir(tagged=(20, MGMT)))
    assert not result.findings


def test_removing_a_tagged_vlan_still_warns():
    result = _run(_ir(tagged=(MGMT, 20)), _ir(tagged=(MGMT,)))
    assert result.status is Status.WARN
    assert result.findings[0].code.endswith(".path_dependency_changed")
    assert result.findings[0].evidence["ports"] == [TRUNK]


def test_replacing_a_tagged_vlan_still_warns():
    result = _run(_ir(tagged=(MGMT, 20)), _ir(tagged=(MGMT, 199)))
    assert result.status is Status.WARN


@pytest.mark.parametrize("change", [
    {"native": 20},
    {"mode": PortMode.ACCESS},
    {"disabled": True},
])
def test_native_mode_or_disable_changes_still_warn(change):
    result = _run(_ir(), _ir(**change))
    assert result.status is Status.WARN


def test_adding_a_vlan_that_carries_the_switch_svi_still_warns():
    # e.g. the management VLAN put on another trunk: the switch's own traffic
    # can now take that port
    base = _ir(tagged=(20,), svi_vlans=(MGMT,))
    prop = _ir(tagged=(20, MGMT), svi_vlans=(MGMT,))
    result = _run(base, prop)
    assert result.status is Status.WARN
    assert result.findings[0].evidence["ports"] == [TRUNK]


def test_only_path_relevant_ports_are_reported():
    other = Port(id="d1:xe-0/1/1", device_id="d1", name="xe-0/1/1",
                 mode=PortMode.TRUNK, tagged_vlans=(20, 30))
    other_after = Port(id="d1:xe-0/1/1", device_id="d1", name="xe-0/1/1",
                       mode=PortMode.TRUNK, tagged_vlans=(20,))
    base = _ir(tagged=(MGMT, 20), extra_ports=(other,))
    prop = _ir(tagged=(MGMT, 20, 199), extra_ports=(other_after,))
    result = _run(base, prop)
    assert result.status is Status.WARN
    assert result.findings[0].evidence["ports"] == ["d1:xe-0/1/1"]
