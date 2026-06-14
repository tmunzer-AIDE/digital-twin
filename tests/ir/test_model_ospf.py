"""OspfIntf entity + IRBuilder wiring + role-aware validation (GS26)."""

import pytest

from digital_twin.ir import IRBuilder, IRValidationError
from digital_twin.ir.entities import Device, DeviceRole, OspfIntf, Vlan
from tests.factories import sw


def test_id_is_derived_from_device_area_and_name():
    o = OspfIntf(device_id="S", vlan_id=10, area="0", network_name="corp")
    assert o.id == "S:ospf:0:corp"
    assert o.passive is False and o.unresolved is False


def test_builder_adds_and_build_exposes_ospf_intfs():
    ir = (
        IRBuilder()
        .add_device(sw())
        .add_vlan(Vlan(vlan_id=10, name="corp"))
        .add_ospf_intf(OspfIntf(device_id="S", vlan_id=10, area="0", network_name="corp"))
        .build()
    )
    assert len(ir.ospf_intfs) == 1
    assert ir.ospf_intfs[0].id == "S:ospf:0:corp"


def test_passive_ospf_intf_builds_and_is_preserved():
    ir = (
        IRBuilder()
        .add_device(sw("S"))
        .add_vlan(Vlan(vlan_id=10))
        .add_ospf_intf(
            OspfIntf(device_id="S", vlan_id=10, area="0", network_name="corp", passive=True)
        )
        .build()
    )
    assert ir.ospf_intfs[0].passive is True


def test_duplicate_ospf_intf_id_rejected():
    b = IRBuilder().add_device(sw()).add_vlan(Vlan(vlan_id=10))
    b.add_ospf_intf(OspfIntf(device_id="S", vlan_id=10, area="0", network_name="corp"))
    with pytest.raises(IRValidationError, match="duplicate ospf"):
        b.add_ospf_intf(OspfIntf(device_id="S", vlan_id=10, area="0", network_name="corp"))


def test_validation_rejects_unknown_device():
    b = IRBuilder().add_vlan(Vlan(vlan_id=10))
    b.add_ospf_intf(OspfIntf(device_id="GHOST", vlan_id=10, area="0", network_name="corp"))
    with pytest.raises(IRValidationError, match="unknown device"):
        b.build()


def test_validation_rejects_non_switch_device():
    b = IRBuilder().add_device(Device(id="GW", role=DeviceRole.GATEWAY, site="s1"))
    b.add_vlan(Vlan(vlan_id=10))
    b.add_ospf_intf(OspfIntf(device_id="GW", vlan_id=10, area="0", network_name="corp"))
    with pytest.raises(IRValidationError, match="is not a switch"):
        b.build()


def test_validation_rejects_resolved_vlan_not_minted():
    b = IRBuilder().add_device(sw())
    b.add_ospf_intf(OspfIntf(device_id="S", vlan_id=10, area="0", network_name="corp"))
    with pytest.raises(IRValidationError, match="unknown vlan"):
        b.build()


def test_validation_rejects_unresolved_invariant_violation():
    b = IRBuilder().add_device(sw()).add_vlan(Vlan(vlan_id=10))
    # unresolved must carry NO vlan_id
    b.add_ospf_intf(
        OspfIntf(device_id="S", vlan_id=10, area="0", network_name="corp", unresolved=True)
    )
    with pytest.raises(IRValidationError, match="unresolved"):
        b.build()


def test_unresolved_row_with_none_vlan_is_valid():
    ir = (
        IRBuilder()
        .add_device(sw())
        .add_ospf_intf(
            OspfIntf(device_id="S", vlan_id=None, area="0", network_name="ghost", unresolved=True)
        )
        .build()
    )
    assert ir.ospf_intfs[0].unresolved is True


def test_validation_rejects_empty_network_name():
    b = IRBuilder().add_device(sw()).add_vlan(Vlan(vlan_id=10))
    b.add_ospf_intf(OspfIntf(device_id="S", vlan_id=10, area="0", network_name=""))
    with pytest.raises(IRValidationError, match="empty network_name"):
        b.build()
