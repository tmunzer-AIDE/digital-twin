def test_ir_public_api():
    from digital_twin.ir import (
        IR,
        Capability,
        Client,
        Confidence,
        Device,
        FactMeta,
        IRBuilder,
        IRCapability,
        IRDiff,
        Link,
        Port,
        Provenance,
        Vlan,
        access_ports_by_vlan,
        clients_by_ap,
        diff_ir,
        exits_by_vlan,
        fact_meta,
        min_confidence,
        vc_root_map,
    )

    assert IRBuilder().build().ir_version
    assert all(
        callable(f)
        for f in (
            diff_ir,
            min_confidence,
            fact_meta,
            vc_root_map,
            access_ports_by_vlan,
            exits_by_vlan,
            clients_by_ap,
        )
    )
    assert all(
        x is not None
        for x in (
            IR,
            Client,
            Confidence,
            Device,
            FactMeta,
            IRCapability,
            IRDiff,
            Link,
            Port,
            Provenance,
            Vlan,
        )
    )
    cap: Capability = IRCapability.WIRED_L2
    assert cap == "wired.l2"


def test_representations_public_api():
    from digital_twin.representations import (
        L2Edge,
        VlanNode,
        build_l2_graph,
        build_vlan_graph,
        link_carried_vlans,
    )

    assert all(callable(f) for f in (build_l2_graph, build_vlan_graph, link_carried_vlans))
    assert L2Edge is not None and VlanNode is not None
