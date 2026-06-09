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


def test_plan2_public_api():
    from digital_twin.adapters.mist import (
        ClientsIngester,
        IngesterRegistry,
        IngestReport,
        LldpIngester,
        SwitchIngester,
        compile_device,
        compile_site,
        merge_only,
    )
    from digital_twin.engine import validate_supply
    from digital_twin.providers import (
        FetchError,
        MistApiProvider,
        RawSiteState,
        SiteScope,
        StateProvider,
    )

    assert all(callable(f) for f in (compile_site, compile_device, merge_only, validate_supply))
    assert IngestReport is not None and FetchError is not None
    assert all(
        x is not None
        for x in (
            SwitchIngester,
            LldpIngester,
            ClientsIngester,
            IngesterRegistry,
            MistApiProvider,
            RawSiteState,
            SiteScope,
            StateProvider,
        )
    )
