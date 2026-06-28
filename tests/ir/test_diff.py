from digital_twin.ir.diff import diff_ir
from digital_twin.ir.entities import AttachKind, Client, ClientKind, Port, PortMode
from digital_twin.ir.model import IRBuilder
from digital_twin.ir.provenance import Provenance, fact_meta
from tests.factories import ap, sw, trunk_port


def test_no_change_is_empty_diff():
    ir = IRBuilder().add_device(sw("d1")).add_port(trunk_port("d1", "p", (30,))).build()
    assert diff_ir(ir, ir).is_empty()


def test_added_and_removed_detected():
    base = IRBuilder().add_device(sw("d1")).build()
    proposed = IRBuilder().add_device(sw("d1")).add_device(sw("d2")).build()
    assert ("device", "d2") in {(r.kind, r.id) for r in diff_ir(base, proposed).added}
    assert ("device", "d2") in {(r.kind, r.id) for r in diff_ir(proposed, base).removed}


def test_modified_port_reports_changed_fields():
    base = IRBuilder().add_device(sw("d1")).add_port(trunk_port("d1", "p", (10, 30))).build()
    proposed = IRBuilder().add_device(sw("d1")).add_port(trunk_port("d1", "p", (10,))).build()
    mods = {(m.ref.kind, m.ref.id): m.changed_fields for m in diff_ir(base, proposed).modified}
    assert "tagged_vlans" in mods[("port", "d1:p")]


def test_meta_only_change_is_not_a_modification():
    base = IRBuilder().add_device(sw("d1")).add_port(trunk_port("d1", "p", (30,))).build()
    p2 = Port(
        id="d1:p",
        device_id="d1",
        name="p",
        mode=PortMode.TRUNK,
        tagged_vlans=(30,),
        meta=fact_meta(Provenance.LLDP_ONE_SIDED),
    )
    proposed = IRBuilder().add_device(sw("d1")).add_port(p2).build()
    assert diff_ir(base, proposed).is_empty()


def test_vlan_modification_detected_by_id():
    from digital_twin.ir.entities import Vlan

    base = IRBuilder().add_vlan(Vlan(vlan_id=30, name="old")).build()
    proposed = IRBuilder().add_vlan(Vlan(vlan_id=30, name="new")).build()
    mods = {(m.ref.kind, m.ref.id): m.changed_fields for m in diff_ir(base, proposed).modified}
    assert ("vlan", "30") in mods
    assert "name" in mods[("vlan", "30")]


def test_touches_reports_kinds():
    base = IRBuilder().add_device(sw("d1")).build()
    proposed = IRBuilder().add_device(sw("d1")).add_device(sw("d2")).build()
    d = diff_ir(base, proposed)
    assert d.touches("device") is True
    assert d.touches("port") is False


def test_subnet_unresolved_flip_alone_marks_vlan_modified():
    from digital_twin.ir.entities import Vlan

    base = IRBuilder()
    base.add_vlan(Vlan(vlan_id=10, subnet=None, subnet_unresolved=False))
    prop = IRBuilder()
    prop.add_vlan(Vlan(vlan_id=10, subnet=None, subnet_unresolved=True))
    d = diff_ir(base.build(), prop.build())
    assert any(
        m.ref.kind == "vlan" and m.ref.id == "10" and "subnet_unresolved" in m.changed_fields
        for m in d.modified
    )


def test_ospf_intf_removal_and_passive_flip_are_diffed():
    from digital_twin.ir import IRBuilder, diff_ir
    from digital_twin.ir.entities import Device, DeviceRole, OspfIntf, Vlan

    def _ir(passive, present=True):
        b = IRBuilder().add_device(Device(id="S", role=DeviceRole.SWITCH, site="s1"))
        b.add_vlan(Vlan(vlan_id=10))
        if present:
            b.add_ospf_intf(
                OspfIntf(device_id="S", vlan_id=10, area="0", network_name="corp", passive=passive)
            )
        return b.build()

    # removal: present -> absent surfaces a removed ospf_intf ref
    d = diff_ir(_ir(False, present=True), _ir(False, present=False))
    assert d.touches("ospf_intf")
    assert any(r.kind == "ospf_intf" and r.id == "S:ospf:0:corp" for r in d.removed)

    # passive flip (retained id): a MODIFIED ospf_intf with changed_fields=("passive",)
    d2 = diff_ir(_ir(False), _ir(True))
    assert d2.touches("ospf_intf")
    mod = next(m for m in d2.modified if m.ref.id == "S:ospf:0:corp")
    assert mod.changed_fields == ("passive",)


def test_diff_output_order_is_deterministic():
    # set-based diffing must not leak nondeterministic ordering into verdicts/fixtures
    base = IRBuilder().add_device(sw("d1")).build()
    proposed = (
        IRBuilder()
        .add_device(sw("d1"))
        .add_device(sw("d9"))
        .add_device(sw("d2"))
        .add_device(sw("d5"))
        .add_port(trunk_port("d2", "a"))
        .build()
    )
    d = diff_ir(base, proposed)
    assert [(r.kind, r.id) for r in d.added] == [
        ("device", "d2"),
        ("device", "d5"),
        ("device", "d9"),
        ("port", "d2:a"),
    ]
    # and repeated runs agree
    assert d == diff_ir(base, proposed)


def test_device_name_change_is_not_a_diff():
    from digital_twin.ir import IRBuilder
    from digital_twin.ir.entities import Device, DeviceRole

    base = IRBuilder().add_device(
        Device(id="d1", role=DeviceRole.SWITCH, site="s1", name="old")
    ).build()
    prop = IRBuilder().add_device(
        Device(id="d1", role=DeviceRole.SWITCH, site="s1", name="new")
    ).build()
    assert diff_ir(base, prop).is_empty()  # rename is display-only, not a config change


def test_vlan_name_change_is_still_a_diff():
    # regression guard: name ignore is DEVICE-ONLY (Vlan.name stays a real field)
    from digital_twin.ir import IRBuilder
    from digital_twin.ir.entities import Vlan

    base = IRBuilder().add_vlan(Vlan(vlan_id=30, name="old")).build()
    prop = IRBuilder().add_vlan(Vlan(vlan_id=30, name="new")).build()
    mods = {(m.ref.kind, m.ref.id): m.changed_fields for m in diff_ir(base, prop).modified}
    assert "name" in mods[("vlan", "30")]


def test_is_uplink_only_change_is_not_a_modification():
    # is_uplink is observational evidence, NOT a config change -> empty diff,
    # so flipping the observed uplink bit never wakes a check.
    base = IRBuilder().add_device(sw("S")).add_port(
        Port(id="S:ge-0/0/1", device_id="S", name="ge-0/0/1", mode=PortMode.TRUNK,
             is_uplink=True)
    ).build()
    proposed = IRBuilder().add_device(sw("S")).add_port(
        Port(id="S:ge-0/0/1", device_id="S", name="ge-0/0/1", mode=PortMode.TRUNK,
             is_uplink=False)
    ).build()
    assert diff_ir(base, proposed).is_empty()


def test_client_ssid_only_change_is_not_a_modification():
    base = IRBuilder().add_device(ap("AP")).add_client(
        Client(
            mac="001122334455",
            kind=ClientKind.WIRELESS,
            attach_kind=AttachKind.AP,
            attach_id="AP",
            ssid="Corp",
        )
    ).build()
    proposed = IRBuilder().add_device(ap("AP")).add_client(
        Client(
            mac="001122334455",
            kind=ClientKind.WIRELESS,
            attach_kind=AttachKind.AP,
            attach_id="AP",
            ssid="Guest",
        )
    ).build()
    assert diff_ir(base, proposed).is_empty()
