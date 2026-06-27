from dataclasses import replace

from digital_twin.analysis.context import AnalysisContext
from digital_twin.analysis.exits import ExitKind, exit_anchor_nodes
from digital_twin.ir import ConfidenceLevel, IRBuilder, Vlan
from digital_twin.ir.entities import Device, DeviceRole, L3Intf, L3Role
from digital_twin.ir.provenance import Provenance
from tests.factories import access_port, irb, link, sw, trunk_port


def _base(
    with_irb: bool,
    with_gateway: bool = False,
    gw_one_sided: bool = False,
    *,
    with_uplink: bool = False,
    uplink_flag: bool | None = True,
    uplink_disabled: bool = False,
    uplink_carries: bool = True,
):
    b = IRBuilder()
    b.add_device(sw("A"))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    b.add_port(trunk_port("A", "down", tagged=(10,)))
    if with_irb:
        b.add_l3intf(irb("A", 10))
    if with_gateway:
        b.add_device(Device(id="GW", role=DeviceRole.GATEWAY, site="s1"))
        b.add_port(trunk_port("A", "up", tagged=(10,)))
        b.add_port(trunk_port("GW", "down", tagged=(10,)))
        prov = Provenance.LLDP_ONE_SIDED if gw_one_sided else Provenance.LLDP_TWO_SIDED
        b.add_link(link("A:up", "GW:down", prov=prov))
    if with_uplink:
        b.add_port(access_port("A", "acc", 10))  # member -> A is a vlan-10 graph node
        b.add_port(
            replace(
                trunk_port("A", "up2", tagged=(10,) if uplink_carries else ()),
                is_uplink=uplink_flag,
                disabled=uplink_disabled,
            )
        )
    return b.build()


def test_rule1_irb_is_high_confidence_exit():
    res = AnalysisContext(_base(with_irb=True)).exit_for(10)
    assert res.kind is ExitKind.IRB
    assert res.nodes == ("A",)
    assert res.confidence is not None and res.confidence.level is ConfidenceLevel.HIGH


def test_rule2_boundary_uplink_two_sided_is_high():
    res = AnalysisContext(_base(with_irb=False, with_gateway=True)).exit_for(10)
    assert res.kind is ExitKind.BOUNDARY_UPLINK
    assert res.nodes == ("GW",)
    assert res.confidence is not None and res.confidence.level is ConfidenceLevel.HIGH


def test_rule2_one_sided_uplink_is_low():
    res = AnalysisContext(_base(with_irb=False, with_gateway=True, gw_one_sided=True)).exit_for(10)
    assert res.kind is ExitKind.BOUNDARY_UPLINK
    assert res.confidence is not None and res.confidence.level is ConfidenceLevel.LOW


def test_rule1_wins_over_rule2():
    res = AnalysisContext(_base(with_irb=True, with_gateway=True)).exit_for(10)
    assert res.kind is ExitKind.IRB


def test_rule3_no_exit_found():
    res = AnalysisContext(_base(with_irb=False)).exit_for(10)
    assert res.kind is ExitKind.NONE
    assert res.confidence is None  # absent -> INSUFFICIENT_DATA at the check


def test_exit_anchor_nodes_collects_gateway_and_irb_svi():
    b = IRBuilder()
    b.add_device(Device(id="gw", role=DeviceRole.GATEWAY, site="s1"))
    b.add_device(Device(id="core", role=DeviceRole.SWITCH, site="s1"))
    b.add_device(Device(id="acc", role=DeviceRole.SWITCH, site="s1"))
    b.add_vlan(Vlan(vlan_id=10, name="a", scope="s1"))
    b.add_vlan(Vlan(vlan_id=20, name="b", scope="s1"))
    b.add_l3intf(L3Intf(device_id="core", role=L3Role.IRB, vlan_id=10))
    b.add_l3intf(L3Intf(device_id="acc", role=L3Role.SVI, vlan_id=20))
    assert exit_anchor_nodes(b.build()) == {"gw", "core", "acc"}


def test_exit_anchor_nodes_excludes_wan_loopback_and_plain_switch():
    b = IRBuilder()
    b.add_device(Device(id="sw1", role=DeviceRole.SWITCH, site="s1"))
    b.add_device(Device(id="gwdev", role=DeviceRole.GATEWAY, site="s1"))
    # WAN / LOOPBACK L3 interfaces are NOT exits; gwdev is an anchor by ROLE only
    b.add_l3intf(L3Intf(device_id="gwdev", role=L3Role.WAN, port="ge-0/0/0"))
    b.add_l3intf(L3Intf(device_id="sw1", role=L3Role.LOOPBACK, port="lo0"))
    assert exit_anchor_nodes(b.build()) == {"gwdev"}


def test_exit_anchor_nodes_ignores_unresolved_irb_without_vlan():
    # an IRB/SVI not tied to a concrete VLAN is unresolved/malformed -> not an exit
    b = IRBuilder()
    b.add_device(Device(id="sw", role=DeviceRole.SWITCH, site="s1"))
    b.add_l3intf(L3Intf(device_id="sw", role=L3Role.IRB, vlan_id=None, port="irb"))
    assert exit_anchor_nodes(b.build()) == set()


def test_exit_anchor_nodes_folds_vc_members_to_root():
    b = IRBuilder()
    # member1 must exist as a device (IRBuilder._validate_l3intfs rejects unknown
    # devices); it is also declared a VC member of vcroot, so it folds to the root.
    b.add_device(Device(id="vcroot", role=DeviceRole.SWITCH, site="s1", vc_members=("member1",)))
    b.add_device(Device(id="member1", role=DeviceRole.SWITCH, site="s1"))
    b.add_vlan(Vlan(vlan_id=10, name="a", scope="s1"))
    b.add_l3intf(L3Intf(device_id="member1", role=L3Role.IRB, vlan_id=10))
    # the IRB lives on a VC member -> its anchor node is the VC root
    assert exit_anchor_nodes(b.build()) == {"vcroot"}


def test_rule3_inferred_uplink_is_low_confidence_exit():
    res = AnalysisContext(_base(with_irb=False, with_uplink=True)).exit_for(10)
    assert res.kind is ExitKind.INFERRED_UPLINK
    assert res.nodes == ("A",)
    assert res.confidence is not None and res.confidence.level is ConfidenceLevel.LOW
    assert res.confidence.reasons == (
        "exit inferred from Mist uplink flag; upstream gateway unmodeled",
    )


def test_rule3_disqualifies_non_true_disabled_or_vlan_blind():
    # is_uplink None / False, a disabled uplink, and a VLAN-blind uplink each
    # leave the exit unlocatable (NONE) when nothing else locates it.
    def kind(**kw: bool | None) -> ExitKind:
        return AnalysisContext(_base(False, with_uplink=True, **kw)).exit_for(10).kind  # type: ignore[arg-type]

    assert kind(uplink_flag=None) is ExitKind.NONE
    assert kind(uplink_flag=False) is ExitKind.NONE
    assert kind(uplink_disabled=True) is ExitKind.NONE
    assert kind(uplink_carries=False) is ExitKind.NONE


def test_rule3_yields_to_irb_and_gateway():
    # precedence: a stronger exit always wins over the inferred uplink
    assert AnalysisContext(_base(with_irb=True, with_uplink=True)).exit_for(10).kind is ExitKind.IRB
    assert (
        AnalysisContext(_base(False, with_gateway=True, with_uplink=True)).exit_for(10).kind
        is ExitKind.BOUNDARY_UPLINK
    )
