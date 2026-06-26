from digital_twin.ir import IRBuilder
from digital_twin.ir.entities import (
    Device,
    DeviceRole,
    L3Intf,
    L3Role,
    Link,
    LinkKind,
    Port,
    PortMode,
    Vlan,
)
from digital_twin.viz import visual_map as vm


def _baseline():
    b = IRBuilder()
    b.add_device(Device(id="s1", role=DeviceRole.SWITCH, site="site1"))
    b.add_device(Device(id="s2", role=DeviceRole.SWITCH, site="site1"))
    b.add_port(Port(id="s1:ge-0/0/1", device_id="s1", name="ge-0/0/1", mode=PortMode.ACCESS))
    b.add_vlan(Vlan(vlan_id=10, name="data"))
    b.add_l3intf(L3Intf(device_id="s1", role=L3Role.IRB, vlan_id=10))
    return b.build()


def test_mac_normalizes_mist_device_id():
    assert vm._mac("00000000-0000-0000-1000-aabb01") == "aabb01"
    assert vm._mac("s1") == "s1"


def test_node_resolves_only_real_devices():
    ir = _baseline()
    assert vm._node(ir, "s1") == "s1"
    assert vm._node(ir, "00000000-0000-0000-2000-s1") == "s1"  # gateway 2000 tag
    assert vm._node(ir, "not-a-device") is None


def test_resolve_affected_rejects_client_mac():
    ir = _baseline()
    # a colon-bearing MAC must NOT become a port-ish entity
    assert vm._resolve_affected("aa:bb:cc:dd:ee:ff", ir) is None
    assert vm._resolve_affected("s1", ir) == ("device", "s1")
    assert vm._resolve_affected("10", ir) == ("vlan", "10")
    assert vm._resolve_affected("s1:ge-0/0/1", ir) == ("port", "s1:ge-0/0/1")


def test_owner_device_nodes_for_port_link_l3intf():
    base = _baseline()
    prop = _baseline()
    assert vm.owner_device_nodes("port", "s1:ge-0/0/1", base, prop) == ["s1"]
    link_nodes = vm.owner_device_nodes(
        "link", "s1:ge-0/0/1__s2:ge-0/0/2", base, prop
    )
    assert sorted(link_nodes) == ["s1", "s2"]
    # l3intf owner resolves via BASELINE (works even if removed in proposed)
    iid = "s1:l3:irb:10"
    assert vm.owner_device_nodes("l3intf", iid, base, prop) == ["s1"]
    assert vm.owner_device_nodes("vlan", "10", base, prop) == []
    # an ADDED (proposed-only) l3intf resolves its owner via proposed IR
    pb = IRBuilder()
    pb.add_device(Device(id="s9", role=DeviceRole.SWITCH, site="site1"))
    pb.add_vlan(Vlan(vlan_id=77, name="new"))
    pb.add_l3intf(L3Intf(device_id="s9", role=L3Role.IRB, vlan_id=77))
    added = pb.build()
    assert vm.owner_device_nodes("l3intf", "s9:l3:irb:77", _baseline(), added) == ["s9"]


def _two_switch_vlan_ir():
    b = IRBuilder()
    b.add_device(Device(id="s1", role=DeviceRole.SWITCH, site="site1"))
    b.add_device(Device(id="s2", role=DeviceRole.SWITCH, site="site1"))
    # trunk between s1 and s2 carrying vlan 10; s3 isolated, only vlan 20
    b.add_device(Device(id="s3", role=DeviceRole.SWITCH, site="site1"))
    b.add_port(Port(id="s1:ge-0/0/0", device_id="s1", name="ge-0/0/0",
                    mode=PortMode.TRUNK, tagged_vlans=(10,)))
    b.add_port(Port(id="s2:ge-0/0/0", device_id="s2", name="ge-0/0/0",
                    mode=PortMode.TRUNK, tagged_vlans=(10,)))
    b.add_port(Port(id="s3:ge-0/0/1", device_id="s3", name="ge-0/0/1",
                    mode=PortMode.ACCESS, native_vlan=20))
    b.add_link(Link(
        id="s1:ge-0/0/0__s2:ge-0/0/0",
        a_port="s1:ge-0/0/0",
        b_port="s2:ge-0/0/0",
        kind=LinkKind.PHYSICAL,
    ))
    b.add_vlan(Vlan(vlan_id=10, name="data", subnet="10.0.10.0/24"))
    b.add_vlan(Vlan(vlan_id=20, name="voice"))
    b.add_l3intf(L3Intf(device_id="s1", role=L3Role.IRB, vlan_id=10))
    return b.build()


def test_view_index_vlan_membership_is_scoped():
    ir = _two_switch_vlan_ir()
    idx = vm._build_view_index(ir)
    assert idx.node_in_vlan("s1", 10) and idx.node_in_vlan("s2", 10)
    assert not idx.node_in_vlan("s3", 10)  # s3 is not in vlan 10's graph
    assert idx.node_in_vlan("s3", 20)


def test_view_index_routed_and_interfaces():
    ir = _two_switch_vlan_ir()
    idx = vm._build_view_index(ir)
    assert 10 in idx.routed_vlans  # has a subnet / IRB
    assert [i.vlan_id for i in idx.intfs_for_vlan(10)] == [10]
    assert idx.intfs_for_vlan(20) == []
