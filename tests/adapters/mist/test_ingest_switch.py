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


def test_disabled_usage_marks_port_disabled():
    # an admin-disabled port (usage disabled:true — incl. the system-defined
    # 'disabled' usage) forwards NOTHING: the IR must carry the fact so the
    # L2 graph can drop its links (physical severance, 2026-06-10 real case)
    from digital_twin.adapters.mist.ingest.base import IngestContext
    from digital_twin.ir import IRBuilder

    eff = {
        "networks": {"corp": {"vlan_id": 10}},
        "port_usages": {"off": {"mode": "access", "port_network": "corp", "disabled": True}},
        "port_config": {"ge-0/0/1": {"usage": "off"}, "ge-0/0/2": {"usage": "disabled"}},
    }
    ctx = IngestContext(
        raw=raw_site(devices=({**SWITCH_A, "port_config": eff["port_config"]},)),
        site_effective=eff,
        device_effective={"aa0000000001": eff},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    ir = ctx.builder.build()
    assert ir.ports["aa0000000001:ge-0/0/1"].disabled is True
    assert ir.ports["aa0000000001:ge-0/0/2"].disabled is True  # system 'disabled'


# -- dynamic port profiles: runtime usage from rules + observed LLDP ------------

_DYN_EFF = {
    "networks": {"corp": {"vlan_id": 10}, "iot": {"vlan_id": 30}},
    "port_usages": {
        "aps": {"mode": "trunk", "all_networks": True, "port_network": "corp"},
        "plain": {"mode": "access", "port_network": "corp"},
        "dynamic": {
            "mode": "dynamic",
            "rules": [
                {"src": "lldp_system_name", "expression": "[0:3]", "equals": "AP_", "usage": "aps"}
            ],
        },
    },
    "port_config": {
        "ge-0/0/1": {"usage": "plain", "dynamic_usage": "dynamic"},
        "ge-0/0/2": {"usage": "plain", "dynamic_usage": "dynamic"},
        "ge-0/0/3": {"usage": "plain", "dynamic_usage": "dynamic"},
        "ge-0/0/4": {"usage": "plain", "dynamic_usage": "dynamic"},
    },
}


def _dyn_ir(port_stats):
    from digital_twin.adapters.mist.ingest.base import IngestContext
    from digital_twin.ir import IRBuilder

    ctx = IngestContext(
        raw=raw_site(
            devices=({**SWITCH_A, "port_config": _DYN_EFF["port_config"]},),
            port_stats=tuple(port_stats),
        ),
        site_effective=_DYN_EFF,
        device_effective={"aa0000000001": _DYN_EFF},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    return ctx.builder.build()


def test_dynamic_port_with_matching_neighbor_gets_the_runtime_usage():
    ir = _dyn_ir(
        [{"mac": "aa0000000001", "port_id": "ge-0/0/1", "up": True, "neighbor_system_name": "AP_7"}]
    )
    p = ir.ports["aa0000000001:ge-0/0/1"]
    assert p.mode is PortMode.TRUNK and p.tagged_vlans == (30,) and p.native_vlan == 10
    assert p.profile == "aps"
    assert "dynamic rule" in " ".join(p.meta.confidence.reasons)


def test_dynamic_port_with_unmatched_lldp_neighbor_is_blind_not_static():
    # name rules miss -> Mist would keep static, BUT our rule list is fully
    # name-sourced so a miss IS conclusive -> static usage stands
    ir = _dyn_ir(
        [{"mac": "aa0000000001", "port_id": "ge-0/0/2", "up": True, "neighbor_system_name": "PC-9"}]
    )
    p = ir.ports["aa0000000001:ge-0/0/2"]
    assert p.mode is PortMode.ACCESS and p.native_vlan == 10  # static 'plain'


def test_dynamic_port_down_keeps_static_usage():
    ir = _dyn_ir([{"mac": "aa0000000001", "port_id": "ge-0/0/3", "up": False}])
    p = ir.ports["aa0000000001:ge-0/0/3"]
    assert p.mode is PortMode.ACCESS and p.native_vlan == 10  # static 'plain'


def test_dynamic_port_without_stats_row_is_vlan_blind():
    # no port-stats row: connected-or-not is unknowable -> carriage unknown
    ir = _dyn_ir([])
    p = ir.ports["aa0000000001:ge-0/0/4"]
    assert p.native_vlan is None and p.tagged_vlans == ()
    assert p.meta.provenance.value == "inferred"


# -- PoE: config intent (poe_disabled) + observed draw (poe_on) -----------------

_POE_EFF = {
    "networks": {"corp": {"vlan_id": 10}},
    "port_usages": {
        "ap": {"mode": "trunk", "all_networks": True, "poe_disabled": False},
        "nopoe": {"mode": "access", "port_network": "corp", "poe_disabled": True},
    },
    "port_config": {
        "ge-0/0/1": {"usage": "ap"},
        "ge-0/0/2": {"usage": "nopoe"},
        "ge-0/0/3": {"usage": "ap", "poe_disabled": True},  # inline override
    },
}


def _poe_ir(port_stats=()):
    from digital_twin.adapters.mist.ingest.base import IngestContext
    from digital_twin.ir import IRBuilder

    ctx = IngestContext(
        raw=raw_site(
            devices=({**SWITCH_A, "port_config": _POE_EFF["port_config"]},),
            port_stats=tuple(port_stats),
        ),
        site_effective=_POE_EFF,
        device_effective={"aa0000000001": _POE_EFF},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    return ctx.builder.build()


def test_poe_disabled_usage_sets_port_poe_false():
    ir = _poe_ir()
    assert ir.ports["aa0000000001:ge-0/0/1"].poe is True   # enabled
    assert ir.ports["aa0000000001:ge-0/0/2"].poe is False  # usage disabled
    assert ir.ports["aa0000000001:ge-0/0/3"].poe is False  # inline override


def test_observed_poe_on_sets_poe_draw():
    ir = _poe_ir(
        [{"mac": "aa0000000001", "port_id": "ge-0/0/1", "up": True, "poe_on": True,
          "power_draw": 6.6}]
    )
    assert ir.ports["aa0000000001:ge-0/0/1"].poe_draw is True
    assert ir.ports["aa0000000001:ge-0/0/2"].poe_draw is None  # no stat row: UNKNOWN


def test_mtu_from_usage_and_inline_override():
    eff = {
        "networks": {"corp": {"vlan_id": 10}},
        "port_usages": {
            "jumbo": {"mode": "trunk", "networks": ["corp"], "mtu": 9200},
            "plain": {"mode": "access", "port_network": "corp", "mtu": None},
        },
        "port_config": {
            "ge-0/0/1": {"usage": "jumbo"},
            "ge-0/0/2": {"usage": "plain"},
            "ge-0/0/3": {"usage": "plain", "mtu": 9000},  # inline override
        },
    }
    from digital_twin.adapters.mist.ingest.base import IngestContext
    from digital_twin.ir import IRBuilder

    ctx = IngestContext(
        raw=raw_site(devices=({**SWITCH_A, "port_config": eff["port_config"]},)),
        site_effective=eff,
        device_effective={"aa0000000001": eff},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    ir = ctx.builder.build()
    assert ir.ports["aa0000000001:ge-0/0/1"].mtu == 9200
    assert ir.ports["aa0000000001:ge-0/0/2"].mtu is None  # null == absent (default)
    assert ir.ports["aa0000000001:ge-0/0/3"].mtu == 9000


def test_device_stp_config_survives_compile():
    # found via GS21: compile_device only carries listed device fields — a
    # device-level stp_config was silently dropped from the effective config
    # (raw gate passes, effective never changes -> a false-SAFE shape)
    from digital_twin.adapters.mist.compile.switch import compile_device

    eff = compile_device(None, {}, {"stp_config": {"bridge_priority": "4096"}})
    assert eff["stp_config"] == {"bridge_priority": "4096"}


def test_bridge_priority_parser_validates_the_junos_range():
    # valid = {0, 4096 .. 61440 step 4096} (or the "4k" form); anything else —
    # malformed OR out-of-step — must NOT silently simulate as some priority
    from digital_twin.adapters.mist.ingest.switch import _bridge_priority

    assert _bridge_priority({"bridge_priority": "0"}) == 0
    assert _bridge_priority({"bridge_priority": 4096}) == 4096
    assert _bridge_priority({"bridge_priority": "4k"}) == 4096
    assert _bridge_priority({"bridge_priority": "60k"}) == 61440
    assert _bridge_priority({"bridge_priority": "banana"}) is None
    assert _bridge_priority({"bridge_priority": "5000"}) is None  # not a 4k step
    assert _bridge_priority(None) is None


def test_invalid_bridge_priority_raises_an_adapter_finding():
    # an IN-SCOPE field whose value the model cannot interpret must never be
    # silently simulated as the default — that would be a quiet false state
    from digital_twin.adapters.mist.ingest.switch import invalid_bridge_priority_findings

    good = {"stp_config": {"bridge_priority": "4096"}}
    bad = {"stp_config": {"bridge_priority": "banana"}}
    assert invalid_bridge_priority_findings({"d1": good}, {"d1": good}) == []
    findings = invalid_bridge_priority_findings({"d1": good}, {"d1": bad})
    assert len(findings) == 1
    f = findings[0]
    assert f.code == "scope.stp.bridge_priority_invalid"
    assert f.severity.value == "warning"
    assert f.evidence == {"device": "d1", "baseline": "4096", "proposed": "banana"}
    # a malformed BASELINE poisons the prediction too — both sides checked
    assert invalid_bridge_priority_findings({"d1": bad}, {"d1": good}) != []


def test_stp_config_flags_and_bridge_priority():
    eff = {
        "networks": {"corp": {"vlan_id": 10}},
        "stp_config": {"bridge_priority": "4096"},
        "port_usages": {
            "nostp": {"mode": "trunk", "networks": ["corp"], "stp_disable": True},
            "edge": {"mode": "access", "port_network": "corp", "stp_edge": True},
            "plain": {"mode": "access", "port_network": "corp"},
        },
        "port_config": {
            "ge-0/0/1": {"usage": "nostp"},
            "ge-0/0/2": {"usage": "edge"},
            "ge-0/0/3": {"usage": "plain"},
        },
        "local_port_config": {"ge-0/0/3": {"usage": "plain", "stp_edge": True}},
    }
    from digital_twin.adapters.mist.ingest.base import IngestContext
    from digital_twin.ir import IRBuilder

    ctx = IngestContext(
        raw=raw_site(devices=({**SWITCH_A, "port_config": eff["port_config"]},)),
        site_effective=eff,
        device_effective={"aa0000000001": eff},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    ir = ctx.builder.build()
    assert ir.ports["aa0000000001:ge-0/0/1"].bpdu_filter is True  # stp_disable drops BPDUs
    assert ir.ports["aa0000000001:ge-0/0/1"].stp_edge is False
    assert ir.ports["aa0000000001:ge-0/0/2"].stp_edge is True
    assert ir.ports["aa0000000001:ge-0/0/3"].stp_edge is True  # local inline override
    assert ir.devices["aa0000000001"].stp_priority == 4096


def test_poe_draw_unknown_is_not_observed_off():
    # real rows (live fixture): `poe_on` is absent on some ports. Absent + port
    # UP -> powered state unknowable (None); absent + port DOWN -> a down port
    # powers nothing (False); present -> the observed value.
    ir = _poe_ir(
        [
            {"mac": "aa0000000001", "port_id": "ge-0/0/1", "up": True},
            {"mac": "aa0000000001", "port_id": "ge-0/0/2", "up": False},
            {"mac": "aa0000000001", "port_id": "ge-0/0/3", "up": True, "poe_on": False},
        ]
    )
    assert ir.ports["aa0000000001:ge-0/0/1"].poe_draw is None
    assert ir.ports["aa0000000001:ge-0/0/2"].poe_draw is False
    assert ir.ports["aa0000000001:ge-0/0/3"].poe_draw is False
