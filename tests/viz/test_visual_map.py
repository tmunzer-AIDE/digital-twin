from digital_twin.contracts import (
    Cause,
    Finding,
    FindingCategory,
    FindingSource,
    ObjectRef,
    Severity,
    VisualTier,
)
from digital_twin.ir import Confidence, ConfidenceLevel, IRBuilder
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

_HIGH = Confidence(level=ConfidenceLevel.HIGH)


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


def _f(**kw):
    base = dict(source=FindingSource.CHECK, category=FindingCategory.NETWORK,
               code="t.x", severity=Severity.WARNING, confidence=_HIGH, message="m")
    return Finding(**{**base, **kw})


def _views(contribs, view):
    return {(c.kind, c.id) for c in contribs if c.view == view}


def test_affected_vlan_scoped_finding_does_not_touch_other_vlans():
    ir = _two_switch_vlan_ir()
    idx = vm._build_view_index(ir)
    # blackhole on vlan 10, component nodes s1,s2
    f = _f(subject=ObjectRef("vlan", "10"),
           evidence={"vlan": 10, "component_nodes": ["s1", "s2"]})
    cs = vm._affected_contributions(f, 0, ir, idx)
    assert ("device", "s1") in _views(cs, "vlan:10")
    assert ("vlan", "10") in _views(cs, "vlan:10")
    assert _views(cs, "vlan:20") == set()  # never touches vlan 20
    assert ("device", "s1") in _views(cs, "l2")  # l2 carries the nodes


def test_affected_no_vlan_finding_is_l2_only():
    ir = _two_switch_vlan_ir()
    idx = vm._build_view_index(ir)
    # isolation.severed: device subject, fragment nodes, NO vlan
    f = _f(subject=ObjectRef("device", "s1"),
           evidence={"fragment_nodes": ["s1", "s2"]}, affected_entities=("s1", "s2"))
    cs = vm._affected_contributions(f, 0, ir, idx)
    assert _views(cs, "l2") == {("device", "s1"), ("device", "s2")}
    assert all(not c.view.startswith("vlan:") for c in cs)
    assert all(c.view != "l3_exits" for c in cs)


def _dual_vlan_ir():
    """s1 and s2 BOTH carry vlan 10 AND 20 over a shared trunk, so both nodes are
    in both vlan graphs. This is what makes the pairing test meaningful: a
    finding-wide cross-product bug is NOT masked by the node_in_vlan() filter."""
    b = IRBuilder()
    b.add_device(Device(id="s1", role=DeviceRole.SWITCH, site="site1"))
    b.add_device(Device(id="s2", role=DeviceRole.SWITCH, site="site1"))
    b.add_port(Port(id="s1:ge-0/0/0", device_id="s1", name="ge-0/0/0",
                    mode=PortMode.TRUNK, tagged_vlans=(10, 20)))
    b.add_port(Port(id="s2:ge-0/0/0", device_id="s2", name="ge-0/0/0",
                    mode=PortMode.TRUNK, tagged_vlans=(10, 20)))
    b.add_link(Link(
        id="s1:ge-0/0/0__s2:ge-0/0/0",
        a_port="s1:ge-0/0/0",
        b_port="s2:ge-0/0/0",
        kind=LinkKind.PHYSICAL,
    ))
    b.add_vlan(Vlan(vlan_id=10, name="data"))
    b.add_vlan(Vlan(vlan_id=20, name="voice"))
    return b.build()


def test_affected_paired_impacts_do_not_cross_product():
    ir = _dual_vlan_ir()
    idx = vm._build_view_index(ir)
    # PRECONDITION: both nodes are in both vlan graphs, so a cross-product bug
    # would NOT be masked by the node_in_vlan() membership filter.
    assert idx.node_in_vlan("s1", 20) and idx.node_in_vlan("s2", 10)
    # client impact: vlan 10 client on s1, vlan 20 client on s2 (distinct nodes).
    f = _f(code="wired.client.impact.active_clients",
           affected_entities=("aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"),
           evidence={"impacts": [
               {"mac": "aa:bb:cc:dd:ee:01", "vlan": 10, "attachment": "s1:ge-0/0/0"},
               {"mac": "aa:bb:cc:dd:ee:02", "vlan": 20, "attachment": "s2:ge-0/0/0"},
           ]})
    cs = vm._affected_contributions(f, 0, ir, idx)
    assert ("device", "s1") in _views(cs, "vlan:10")
    assert ("device", "s2") in _views(cs, "vlan:20")
    assert ("device", "s2") not in _views(cs, "vlan:10")  # pairing, not cross-product
    assert ("device", "s1") not in _views(cs, "vlan:20")
    # the EXACT impacted port is in the map (not just its device)
    assert ("port", "s1:ge-0/0/0") in _views(cs, "l2")
    assert ("port", "s2:ge-0/0/0") in _views(cs, "l2")
    # the client MAC must NOT have resolved to any entity
    assert all(c.kind != "port" or c.id != "aa:bb:cc:dd:ee:01" for c in cs)


def test_affected_snooping_and_loop_evidence_keys():
    # snooping names blocked ports via untrusted_egress; loop names links via
    # link_ids and nodes via cycle_nodes — all must reach the map.
    ir = _dual_vlan_ir()
    idx = vm._build_view_index(ir)
    snoop = _f(code="wired.l2.snooping.blocks_dhcp", subject=ObjectRef("vlan", "10"),
               affected_entities=("s1", "10"),
               evidence={"device": "s1", "vlan": 10, "untrusted_egress": ["s1:ge-0/0/0"]})
    scs = vm._affected_contributions(snoop, 0, ir, idx)
    assert ("port", "s1:ge-0/0/0") in _views(scs, "l2")
    loop = _f(code="wired.l2.loop.unprotected", subject=ObjectRef("vlan", "10"),
              affected_entities=("s1:ge-0/0/0", "s2:ge-0/0/0"),
              evidence={"vlan": 10, "cycle_nodes": ["s1", "s2"],
                        "link_ids": ["s1:ge-0/0/0__s2:ge-0/0/0"]})
    lcs = vm._affected_contributions(loop, 0, ir, idx)
    assert ("link", "s1:ge-0/0/0__s2:ge-0/0/0") in _views(lcs, "l2")
    assert ("device", "s1") in _views(lcs, "l2")  # from cycle_nodes


def test_affected_l3_exits_only_serving_interfaces():
    ir = _two_switch_vlan_ir()
    idx = vm._build_view_index(ir)
    f = _f(subject=ObjectRef("vlan", "10"), evidence={"vlan": 10, "component_nodes": ["s1"]})
    cs = vm._affected_contributions(f, 0, ir, idx)
    l3 = _views(cs, "l3_exits")
    assert ("vlan", "10") in l3
    assert ("intf", "s1:l3:irb:10") in l3  # serves vlan 10


def test_affected_l3_exits_excludes_non_hit_node_interface():
    # IRB for vlan 10 lives on s1, but the finding hits s2 only -> s1's IRB must
    # NOT be highlighted (interfaces are scoped to HIT nodes, not just the vlan).
    ir = _two_switch_vlan_ir()
    idx = vm._build_view_index(ir)
    f = _f(subject=ObjectRef("vlan", "10"), evidence={"vlan": 10, "component_nodes": ["s2"]})
    cs = vm._affected_contributions(f, 0, ir, idx)
    assert ("intf", "s1:l3:irb:10") not in _views(cs, "l3_exits")


def test_affected_non_proposed_vlan_makes_no_phantom_view():
    # a finding referencing a vlan absent from proposed IR yields no vlan: view
    ir = _two_switch_vlan_ir()
    idx = vm._build_view_index(ir)
    f = _f(subject=ObjectRef("vlan", "999"), evidence={"vlan": 999, "component_nodes": ["s1"]})
    cs = vm._affected_contributions(f, 0, ir, idx)
    assert not any(c.view == "vlan:999" for c in cs)


def test_affected_consumes_evidence_device_key_ospf_style():
    # OSPF withdrawal names its device ONLY via evidence["device"]; the affected
    # projection must paint that device, not just the vlan box.
    ir = _two_switch_vlan_ir()
    idx = vm._build_view_index(ir)
    f = _f(code="wired.l3.ospf.withdrawn", subject=ObjectRef("vlan", "10"),
           affected_entities=("10",), evidence={"device": "s1", "vlan": 10})
    cs = vm._affected_contributions(f, 0, ir, idx)
    assert ("device", "s1") in _views(cs, "l2")
    assert ("device", "s1") in _views(cs, "vlan:10")


def test_affected_emits_exact_port_and_link_entries():
    # the map must carry exact port/link keys (not collapse to device), so the UI
    # never has to re-infer the precise port/link from the finding.
    ir = _dual_vlan_ir()
    idx = vm._build_view_index(ir)
    pf = _f(subject=ObjectRef("port", "s1:ge-0/0/0"))
    pcs = vm._affected_contributions(pf, 0, ir, idx)
    assert ("port", "s1:ge-0/0/0") in _views(pcs, "l2")
    assert ("device", "s1") in _views(pcs, "l2")  # owner device too
    lf = _f(affected_entities=("s1:ge-0/0/0__s2:ge-0/0/0",))
    lcs = vm._affected_contributions(lf, 0, ir, idx)
    assert ("link", "s1:ge-0/0/0__s2:ge-0/0/0") in _views(lcs, "l2")
    assert ("device", "s1") in _views(lcs, "l2") and ("device", "s2") in _views(lcs, "l2")


def test_origin_port_cause_surfaces_owner_device_on_l2_and_vlan():
    ir = _two_switch_vlan_ir()
    idx = vm._build_view_index(ir)
    f = _f(subject=ObjectRef("vlan", "10"), evidence={"vlan": 10, "component_nodes": ["s2"]},
           caused_by=(Cause(ref=ObjectRef("port", "s1:ge-0/0/0"), fields=("disabled",)),))
    cs = vm._origin_contributions(f, 0, ir, ir, idx)
    assert any(c.view == "l2" and c.kind == "device" and c.id == "s1"
               and c.tier is vm.VisualTier.ORIGIN for c in cs)
    # the port itself ALSO gets a self-entry on l2 (it resolves in proposed IR)
    assert any(c.view == "l2" and c.kind == "port" and c.id == "s1:ge-0/0/0"
               and c.tier is vm.VisualTier.ORIGIN for c in cs)
    # s1 participates in vlan 10 -> origin shows on vlan:10 too
    assert any(c.view == "vlan:10" and c.id == "s1" and c.tier is vm.VisualTier.ORIGIN
               for c in cs)


def test_origin_removed_device_makes_no_phantom_l2_entry():
    base = _two_switch_vlan_ir()
    # proposed: s1 removed entirely
    pb = IRBuilder()
    pb.add_device(Device(id="s2", role=DeviceRole.SWITCH, site="site1"))
    pb.add_device(Device(id="s3", role=DeviceRole.SWITCH, site="site1"))
    pb.add_vlan(Vlan(vlan_id=10, name="data", subnet="10.0.10.0/24"))
    pb.add_vlan(Vlan(vlan_id=20, name="voice"))
    proposed = pb.build()
    idx = vm._build_view_index(proposed)
    f = _f(subject=ObjectRef("vlan", "10"), evidence={"vlan": 10},
           caused_by=(Cause(ref=ObjectRef("device", "s1")),))
    cs = vm._origin_contributions(f, 0, base, proposed, idx)
    assert not any(c.id == "s1" for c in cs)  # removed device -> no phantom entry


def test_origin_removed_l3intf_falls_back_to_owner_on_l2_only():
    base = _two_switch_vlan_ir()
    # proposed: the IRB on s1 for vlan 10 is REMOVED, and s1 no longer carries vlan 10
    pb = IRBuilder()
    pb.add_device(Device(id="s1", role=DeviceRole.SWITCH, site="site1"))
    pb.add_device(Device(id="s2", role=DeviceRole.SWITCH, site="site1"))
    pb.add_device(Device(id="s3", role=DeviceRole.SWITCH, site="site1"))
    pb.add_vlan(Vlan(vlan_id=10, name="data", subnet="10.0.10.0/24"))
    pb.add_vlan(Vlan(vlan_id=20, name="voice"))
    proposed = pb.build()
    idx = vm._build_view_index(proposed)
    f = _f(subject=ObjectRef("vlan", "10"), evidence={"vlan": 10},
           caused_by=(Cause(ref=ObjectRef("l3intf", "s1:l3:irb:10"), fields=()),))
    cs = vm._origin_contributions(f, 0, base, proposed, idx)
    assert any(c.view == "l2" and c.id == "s1" and c.tier is vm.VisualTier.ORIGIN for c in cs)
    # s1 no longer participates in vlan 10's proposed graph -> no forced vlan origin
    assert not any(c.view == "vlan:10" and c.kind == "device" for c in cs)
    # and no dangling intf self-entry (the interface is gone from proposed)
    assert not any(c.kind == "intf" for c in cs)


def test_origin_per_impact_cause_pairs_with_its_vlan():
    ir = _dual_vlan_ir()  # both s1,s2 participate in BOTH vlans -> bug not masked
    idx = vm._build_view_index(ir)
    assert idx.node_in_vlan("s2", 10)  # precondition: s2 IS in vlan 10's graph
    f = _f(code="wired.client.impact.active_clients",
           caused_by=(Cause(ref=ObjectRef("port", "s1:ge-0/0/0")),
                      Cause(ref=ObjectRef("port", "s2:ge-0/0/0"))),
           evidence={"impacts": [
               {"mac": "m1", "vlan": 10, "attachment": "s1:ge-0/0/0",
                "caused_by": [Cause(ref=ObjectRef("port", "s1:ge-0/0/0"))]},
               {"mac": "m2", "vlan": 20, "attachment": "s2:ge-0/0/0",
                "caused_by": [Cause(ref=ObjectRef("port", "s2:ge-0/0/0"))]},
           ]})
    cs = vm._origin_contributions(f, 0, ir, ir, idx)
    # s2's cause is paired to vlan 20; despite s2 participating in vlan 10's graph
    # it must NOT appear as an origin on vlan:10 (pairing, not finding-wide union)
    assert not any(c.view == "vlan:10" and c.id == "s2" for c in cs)
    assert any(c.view == "vlan:20" and c.id == "s2" and c.tier is vm.VisualTier.ORIGIN
               for c in cs)


def test_build_map_origin_beats_affected_severity_orthogonal():
    ir = _two_switch_vlan_ir()
    # one finding makes s1 affected (warning); a second makes s1 origin (info-severity)
    affected = _f(severity=Severity.WARNING, affected_entities=("s1",))
    origin = _f(severity=Severity.INFO, subject=ObjectRef("vlan", "10"),
                evidence={"vlan": 10},
                caused_by=(Cause(ref=ObjectRef("device", "s1")),))
    m = vm.build_visual_map(ir, ir, (affected, origin))
    e = m["l2"]["device:s1"]
    assert e.tier is VisualTier.ORIGIN          # origin wins
    assert e.severity is Severity.WARNING  # severity worst-wins, independent of tier
    assert {r.index for r in e.findings} == {0, 1}


def test_build_map_headline_bleed_regression():
    ir = _two_switch_vlan_ir()
    # blackhole on vlan 10 hitting s1; vlan 20 and untouched vlans must stay clean
    f = _f(subject=ObjectRef("vlan", "10"),
           evidence={"vlan": 10, "component_nodes": ["s1", "s2"]})
    m = vm.build_visual_map(ir, ir, (f,))
    assert "device:s1" in m.get("vlan:10", {})
    assert "device:s1" not in m.get("vlan:20", {})  # THE FIX
    # vlan:20 carries no paint at all from a vlan-10-scoped finding
    assert m.get("vlan:20", {}) == {}


def test_build_map_serializable_entry_shape():
    ir = _two_switch_vlan_ir()
    f = _f(affected_entities=("s1",))
    m = vm.build_visual_map(ir, ir, (f,))
    e = m["l2"]["device:s1"]
    assert (e.kind, e.id) == ("device", "s1")


def test_gateway_effective_coverage_gap_uses_device_visual_entry():
    b = IRBuilder()
    b.add_device(Device(id="gw1", role=DeviceRole.GATEWAY, site="site1"))
    ir = b.build()
    gap = Finding(
        source=FindingSource.ADAPTER,
        category=FindingCategory.OPERATIONAL,
        code="coverage.gap",
        severity=Severity.WARNING,
        confidence=_HIGH,
        message="Coverage gap in gateway gw1: netmask changed",
        subject=ObjectRef("device", "gw1"),
        affected_entities=("gw1",),
        evidence={
            "stage": "derived_gate",
            "artifact": "gateway gw1",
            "paths": ["ip_configs.corp.netmask"],
        },
    )

    m = vm.build_visual_map(ir, ir, (gap,))

    assert "device:gw1" in m["l2"]
    assert "gateway:gw1" not in m["l2"]
    assert m["l2"]["device:gw1"].findings[0].subject == ObjectRef("device", "gw1")


def test_safe_build_visual_map_swallows_errors(monkeypatch):
    # the map is presentational: a builder bug must yield {}, never crash the verdict
    monkeypatch.setattr(vm, "build_visual_map",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert vm.safe_build_visual_map(_two_switch_vlan_ir(), _two_switch_vlan_ir(), ()) == {}
