from digital_twin.adapters.mist.ingest.base import IngestContext
from digital_twin.adapters.mist.ingest.switch import SwitchIngester
from digital_twin.ir import DeviceRole, IRBuilder, IRCapability, L3Role, PortMode
from tests.adapters.mist.fixtures import SITE_EFFECTIVE, SWITCH_A, raw_site


def _ingest() -> IngestContext:
    ctx = IngestContext(
        raw=raw_site(),
        site_effective=dict(SITE_EFFECTIVE),
        device_effective={"aa0000000001": {**SITE_EFFECTIVE, **SWITCH_A}},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    return ctx


def test_devices_created_for_switches_and_aps():
    ir = _ingest().builder.build()
    assert ir.device("aa0000000001").role is DeviceRole.SWITCH
    assert ir.device("cc0000000001").role is DeviceRole.AP


def test_ports_expanded_with_modes_and_vlans():
    ir = _ingest().builder.build()
    p0 = ir.port("aa0000000001:ge-0/0/0")
    assert p0.mode is PortMode.ACCESS and p0.native_vlan == 10
    p47 = ir.port("aa0000000001:ge-0/0/47")
    assert p47.mode is PortMode.TRUNK and p47.native_vlan == 10 and p47.tagged_vlans == (30,)
    assert "aa0000000001:ge-0/0/1" in ir.ports  # range expanded


def test_vlans_and_irb_exits_created():
    ir = _ingest().builder.build()
    assert ir.vlans[10].name == "corp" and ir.vlans[30].name == "voice"
    irbs = [i for i in ir.l3intfs if i.role is L3Role.IRB]
    assert len(irbs) == 1 and irbs[0].vlan_id == 10 and irbs[0].device_id == "aa0000000001"


def test_device_local_network_also_creates_vlan_entity():
    dev_eff = {
        **SITE_EFFECTIVE,
        **SWITCH_A,
        "networks": {**SITE_EFFECTIVE["networks"], "lab": {"vlan_id": 99}},
    }
    ctx = IngestContext(
        raw=raw_site(),
        site_effective=dict(SITE_EFFECTIVE),
        device_effective={"aa0000000001": dev_eff},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    assert ctx.builder.build().vlans[99].name == "lab"


def test_produces_declares_potential_capabilities():
    caps = SwitchIngester().produces()
    assert IRCapability.WIRED_L2 in caps and IRCapability.L3_EXITS in caps


def test_capabilities_earned_only_when_devices_fetched():
    ok = IngestContext(
        raw=raw_site(),
        site_effective=dict(SITE_EFFECTIVE),
        device_effective={},
        builder=IRBuilder(),
    )
    assert IRCapability.WIRED_L2 in SwitchIngester().ingest(ok)

    failed = IngestContext(
        raw=raw_site(fetched=("site", "setting")),
        site_effective=dict(SITE_EFFECTIVE),
        device_effective={},
        builder=IRBuilder(),
    )
    assert SwitchIngester().ingest(failed) == frozenset()
