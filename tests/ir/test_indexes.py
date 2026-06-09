from digital_twin.ir.indexes import (
    access_ports_by_vlan,
    clients_by_ap,
    clients_by_port,
    clients_by_vlan,
    exits_by_vlan,
    node_for,
    ports_by_device,
    vc_root_map,
)
from digital_twin.ir.model import IRBuilder
from tests.factories import access_port, ap, irb, sw, trunk_port, wired_client, wireless_client


def test_vc_root_map_and_node_for():
    ir = IRBuilder().add_device(sw("d1", vc_members=("d1b",))).add_device(sw("d1b")).build()
    vc_root = vc_root_map(ir)
    assert vc_root == {"d1b": "d1"}
    assert node_for(vc_root, "d1b") == "d1"
    assert node_for(vc_root, "d1") == "d1"  # non-member -> itself


def test_ports_by_device_groups():
    ir = (
        IRBuilder()
        .add_device(sw("d1"))
        .add_port(trunk_port("d1", "a"))
        .add_port(trunk_port("d1", "b"))
        .build()
    )
    assert {p.id for p in ports_by_device(ir)["d1"]} == {"d1:a", "d1:b"}


def test_access_ports_by_vlan_uses_native_of_access_ports_only():
    ir = (
        IRBuilder()
        .add_device(sw("d1"))
        .add_port(access_port("d1", "a", 30))
        .add_port(trunk_port("d1", "b", (30,), native=30))
        .build()
    )
    assert [p.id for p in access_ports_by_vlan(ir)[30]] == ["d1:a"]


def test_exits_by_vlan_indexes_irb_only():
    from digital_twin.ir.entities import L3Intf, L3Role

    wan = L3Intf(device_id="d1", role=L3Role.WAN, vlan_id=30)
    ir = IRBuilder().add_device(sw("d1")).add_l3intf(irb("d1", 30)).add_l3intf(wan).build()
    assert [i.vlan_id for i in exits_by_vlan(ir)[30]] == [30]


def test_clients_by_port_ap_and_vlan():
    ir = (
        IRBuilder()
        .add_device(sw("d1"))
        .add_device(ap("ap1"))
        .add_port(access_port("d1", "a", 30))
        .add_client(wired_client("aa", "d1:a", 30))
        .add_client(wireless_client("bb", "ap1", 30))
        .build()
    )
    assert [c.mac for c in clients_by_port(ir)["d1:a"]] == ["aa"]
    assert [c.mac for c in clients_by_ap(ir)["ap1"]] == ["bb"]
    assert {c.mac for c in clients_by_vlan(ir)[30]} == {"aa", "bb"}
