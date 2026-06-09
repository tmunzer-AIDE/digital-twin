import pytest

from digital_twin.ir.capabilities import IRCapability
from digital_twin.ir.entities import (
    AttachKind,
    Client,
    ClientKind,
    DeviceRole,
    L3Intf,
    L3Role,
    Link,
    LinkKind,
    Port,
    PortMode,
    Vlan,
)
from digital_twin.ir.model import IR_VERSION, IRBuilder, IRValidationError
from tests.factories import ap, link, sw, trunk_port


def test_empty_ir_has_version_and_no_capabilities():
    ir = IRBuilder().build()
    assert ir.ir_version == IR_VERSION
    assert ir.capabilities == frozenset()
    assert ir.links == ()


def test_builder_collects_and_lookups_work():
    p = trunk_port("d1", "ge-0/0/1")
    ir = (
        IRBuilder()
        .add_device(sw("d1"))
        .add_port(p)
        .add_vlan(Vlan(vlan_id=30))
        .with_capability(IRCapability.WIRED_L2)
        .build()
    )
    assert ir.device("d1").role is DeviceRole.SWITCH
    assert ir.port("d1:ge-0/0/1") is p
    assert ir.vlans[30].vlan_id == 30
    assert ir.has(IRCapability.WIRED_L2) is True


def test_mappings_are_read_only():
    ir = IRBuilder().add_device(sw("d1")).build()
    with pytest.raises(TypeError):
        ir.devices["d2"] = sw("d2")  # type: ignore[index]


def test_duplicate_device_id_rejected():
    b = IRBuilder().add_device(sw("d1"))
    with pytest.raises(IRValidationError):
        b.add_device(sw("d1"))


def test_duplicate_link_id_rejected():
    b = (
        IRBuilder()
        .add_device(sw("d1"))
        .add_device(sw("d2"))
        .add_port(trunk_port("d1", "a"))
        .add_port(trunk_port("d2", "a"))
    )
    b.add_link(link("d1:a", "d2:a"))
    with pytest.raises(IRValidationError):
        b.add_link(link("d1:a", "d2:a"))


def test_reversed_duplicate_link_rejected():
    # canonical ids sort the endpoints, so a reversed duplicate collides on id
    b = (
        IRBuilder()
        .add_device(sw("d1"))
        .add_device(sw("d2"))
        .add_port(trunk_port("d1", "a"))
        .add_port(trunk_port("d2", "a"))
    )
    b.add_link(link("d1:a", "d2:a"))
    with pytest.raises(IRValidationError):
        b.add_link(link("d2:a", "d1:a"))


def test_duplicate_l3intf_id_rejected():
    b = (
        IRBuilder()
        .add_device(sw("d1"))
        .add_l3intf(L3Intf(device_id="d1", role=L3Role.IRB, vlan_id=30))
    )
    with pytest.raises(IRValidationError):
        b.add_l3intf(L3Intf(device_id="d1", role=L3Role.IRB, vlan_id=30))


def test_duplicate_client_id_rejected():
    b = (
        IRBuilder()
        .add_device(sw("d1"))
        .add_port(trunk_port("d1", "a"))
        .add_client(
            Client(
                mac="aa:bb", kind=ClientKind.WIRED, attach_kind=AttachKind.PORT, attach_id="d1:a"
            )
        )
    )
    with pytest.raises(IRValidationError):
        b.add_client(
            Client(
                mac="AA:BB", kind=ClientKind.WIRED, attach_kind=AttachKind.PORT, attach_id="d1:a"
            )
        )


def test_port_with_unknown_device_rejected_at_build():
    with pytest.raises(IRValidationError) as e:
        IRBuilder().add_port(trunk_port("ghost", "ge-0/0/1")).build()
    assert "unknown device" in str(e.value)


def test_link_with_dangling_endpoint_rejected_at_build():
    b = (
        IRBuilder()
        .add_device(sw("d1"))
        .add_port(trunk_port("d1", "ge-0/0/1"))
        .add_link(link("d1:ge-0/0/1", "d2:missing"))
    )
    with pytest.raises(IRValidationError) as e:
        b.build()
    assert "d2:missing" in str(e.value)


def test_non_canonical_port_id_rejected_at_build():
    bad = Port(id="whatever", device_id="d1", name="ge-0/0/1", mode=PortMode.TRUNK)
    with pytest.raises(IRValidationError) as e:
        IRBuilder().add_device(sw("d1")).add_port(bad).build()
    assert "not canonical" in str(e.value)


def test_non_canonical_link_id_rejected_at_build():
    # e.g. a vendor object id instead of the canonical sorted-endpoint id
    b = (
        IRBuilder()
        .add_device(sw("d1"))
        .add_device(sw("d2"))
        .add_port(trunk_port("d1", "a"))
        .add_port(trunk_port("d2", "a"))
        .add_link(Link(id="mist-obj-123", a_port="d1:a", b_port="d2:a", kind=LinkKind.PHYSICAL))
    )
    with pytest.raises(IRValidationError) as e:
        b.build()
    assert "not canonical" in str(e.value)


def test_lag_without_bundle_id_rejected_at_build():
    # an un-bundled LAG member would otherwise read as false parallel redundancy
    b = (
        IRBuilder()
        .add_device(sw("d1"))
        .add_device(sw("d2"))
        .add_port(trunk_port("d1", "a"))
        .add_port(trunk_port("d2", "a"))
        .add_link(link("d1:a", "d2:a", LinkKind.LAG))  # bundle=None
    )
    with pytest.raises(IRValidationError) as e:
        b.build()
    assert "requires a bundle_id" in str(e.value)


def test_physical_with_bundle_id_rejected_at_build():
    b = (
        IRBuilder()
        .add_device(sw("d1"))
        .add_device(sw("d2"))
        .add_port(trunk_port("d1", "a"))
        .add_port(trunk_port("d2", "a"))
        .add_link(link("d1:a", "d2:a", LinkKind.PHYSICAL, bundle="ae0"))
    )
    with pytest.raises(IRValidationError) as e:
        b.build()
    assert "must not have a bundle_id" in str(e.value)


def test_vc_self_membership_rejected_at_build():
    with pytest.raises(IRValidationError) as e:
        IRBuilder().add_device(sw("d1", vc_members=("d1",))).build()
    assert "itself" in str(e.value)


def test_vc_duplicate_membership_rejected_at_build():
    b = (
        IRBuilder()
        .add_device(sw("d1", vc_members=("d3",)))
        .add_device(sw("d2", vc_members=("d3",)))
        .add_device(sw("d3"))
    )
    with pytest.raises(IRValidationError) as e:
        b.build()
    assert "member of both" in str(e.value)


def test_nested_vc_membership_rejected_at_build():
    # d2 is both a VC root (owns d3) and a member of d1 -> ambiguous fold
    b = (
        IRBuilder()
        .add_device(sw("d1", vc_members=("d2",)))
        .add_device(sw("d2", vc_members=("d3",)))
        .add_device(sw("d3"))
    )
    with pytest.raises(IRValidationError) as e:
        b.build()
    assert "nested" in str(e.value)


def test_wired_client_with_unknown_port_rejected_at_build():
    b = (
        IRBuilder()
        .add_device(sw("d1"))
        .add_client(
            Client(
                mac="aa", kind=ClientKind.WIRED, attach_kind=AttachKind.PORT, attach_id="d1:ghost"
            )
        )
    )
    with pytest.raises(IRValidationError):
        b.build()


def test_wireless_client_must_attach_to_an_ap_role_device():
    b = (
        IRBuilder()
        .add_device(sw("d1"))
        .add_client(
            Client(mac="bb", kind=ClientKind.WIRELESS, attach_kind=AttachKind.AP, attach_id="d1")
        )
    )
    with pytest.raises(IRValidationError) as e:
        b.build()
    assert "not an AP" in str(e.value)


def test_wireless_client_to_real_ap_builds():
    ir = (
        IRBuilder()
        .add_device(ap("ap1"))
        .add_client(
            Client(mac="cc", kind=ClientKind.WIRELESS, attach_kind=AttachKind.AP, attach_id="ap1")
        )
        .build()
    )
    assert len(ir.clients) == 1


def test_kind_attachment_mismatch_rejected():
    b = (
        IRBuilder()
        .add_device(sw("d1"))
        .add_port(trunk_port("d1", "a"))
        .add_client(
            Client(
                mac="dd", kind=ClientKind.WIRELESS, attach_kind=AttachKind.PORT, attach_id="d1:a"
            )
        )
    )
    with pytest.raises(IRValidationError):
        b.build()


def test_valid_ir_with_full_references_builds():
    ir = (
        IRBuilder()
        .add_device(sw("d1"))
        .add_device(sw("d2"))
        .add_port(trunk_port("d1", "ge-0/0/1"))
        .add_port(trunk_port("d2", "ge-0/0/5"))
        .add_link(link("d1:ge-0/0/1", "d2:ge-0/0/5"))
        .build()
    )
    assert len(ir.links) == 1
