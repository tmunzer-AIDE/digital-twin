from dataclasses import FrozenInstanceError

import pytest

from digital_twin.ir.confidence import ConfidenceLevel
from digital_twin.ir.entities import (
    AttachKind,
    Client,
    ClientKind,
    Device,
    DeviceRole,
    L3Intf,
    L3Role,
    Link,
    LinkKind,
    Port,
    PortMode,
    StpMode,
    Vlan,
    client_id,
    device_id,
    link_id,
    port_id,
)
from digital_twin.ir.provenance import CONFIG_META, OBSERVED_META, Provenance, fact_meta


def test_id_helpers():
    assert device_id("AA:BB:CC:00:11:22") == "aabbcc001122"
    assert port_id("aabbcc001122", "ge-0/0/1") == "aabbcc001122:ge-0/0/1"
    assert link_id("d2:p", "d1:p") == link_id("d1:p", "d2:p")
    assert client_id("DE:AD:BE:EF:00:01") == "deadbeef0001"


def test_entities_default_to_config_meta():
    assert Device(id="d1", role=DeviceRole.SWITCH, site="s1").meta is CONFIG_META


def test_link_has_bundle_id_and_meta_not_separate_source():
    link = Link(
        id="l1",
        a_port="d1:p",
        b_port="d2:p",
        kind=LinkKind.LAG,
        bundle_id="ae0",
        meta=fact_meta(Provenance.LLDP_TWO_SIDED),
    )
    assert link.bundle_id == "ae0"
    assert link.meta.provenance is Provenance.LLDP_TWO_SIDED
    assert not hasattr(link, "source")


def test_port_stp_is_a_field_specific_fact():
    port = Port(
        id="d1:ge-0/0/1",
        device_id="d1",
        name="ge-0/0/1",
        mode=PortMode.TRUNK,
        tagged_vlans=(10, 30),
        stp_enabled=True,
        stp_mode=StpMode.RSTP,
        stp_meta=fact_meta(Provenance.OBSERVED),
    )
    assert port.tagged_vlans == (10, 30)
    assert port.meta is CONFIG_META
    assert port.stp_meta is not None
    assert port.stp_meta.confidence.level is ConfidenceLevel.HIGH


def test_port_stp_unknown_by_default():
    port = Port(id="d1:p", device_id="d1", name="p", mode=PortMode.TRUNK)
    assert port.stp_meta is None


def test_l3intf_auto_derives_stable_id():
    intf = L3Intf(device_id="d1", role=L3Role.IRB, vlan_id=30, subnet="10.0.30.0/24")
    assert intf.id == "d1:l3:irb:30"


def test_client_defaults_to_observed_meta_and_has_id():
    c = Client(
        mac="deadbeef0001",
        kind=ClientKind.WIRELESS,
        attach_kind=AttachKind.AP,
        attach_id="ap1",
        vlan=30,
    )
    assert c.meta is OBSERVED_META
    assert c.id == "deadbeef0001"
    assert c.active is True


def test_vlan_has_scope_and_id():
    v = Vlan(vlan_id=30, name="voice", scope="s1")
    assert v.scope == "s1"
    assert v.id == "30"


def test_entities_are_frozen():
    dev = Device(id="d1", role=DeviceRole.SWITCH, site="s1")
    with pytest.raises(FrozenInstanceError):
        dev.site = "s2"  # type: ignore[misc]
