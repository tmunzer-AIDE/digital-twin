from digital_twin.adapters.mist.ingest.base import IngestContext
from digital_twin.adapters.mist.ingest.switch import SwitchIngester
from digital_twin.ir import DeviceRole, IRBuilder, IRCapability, L3Role, PortMode
from tests.adapters.mist.fixtures import ALL_FETCHED, SITE_EFFECTIVE, SWITCH_A, raw_site


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


_GATEWAY = {
    "type": "gateway",
    "mac": "cc0000000001",
    "id": "00000000-0000-0000-2000-cc0000000001",
    "port_config": {
        "ge-0/0/3": {
            "usage": "lan",
            "networks": ["corp", "iot"],
            "port_network": "mgmt",
        },
        "ge-0/0/0": {"usage": "wan"},
    },
    "ip_configs": {"corp": {"type": "static", "ip": "198.51.100.1"}},
}

# the GATEWAY namespace is the ORG networks list (real orgs use different
# names there than in the switch-side site networks — found live 2026-06-11)
_ORG_NETWORKS = (
    {"name": "corp", "vlan_id": 10, "subnet": "198.51.100.0/24"},
    {"name": "iot", "vlan_id": "20"},
    {"name": "mgmt", "vlan_id": 1},
)


def _gateway_ir(org_networks=_ORG_NETWORKS):
    from digital_twin.adapters.mist.ingest.base import IngestContext
    from digital_twin.ir import IRBuilder

    ctx = IngestContext(
        raw=raw_site(devices=(_GATEWAY,), org_networks=org_networks),
        site_effective={"networks": {}},
        device_effective={},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    return ctx.builder.build()


def test_gateway_lan_port_carriage_resolves_via_org_networks():
    # GS22: the gateway's LAN trunk declares its carriage — that end of the
    # core<->gateway link must stop being vlan-blind (assumed/MEDIUM)
    ir = _gateway_ir()
    p = ir.ports["cc0000000001:ge-0/0/3"]
    assert p.native_vlan == 1 and p.tagged_vlans == (10, 20)
    assert p.meta.provenance.value == "config"


def test_gateway_unresolvable_network_names_make_the_port_blind_not_empty():
    # a config-empty trunk claims "carries NOTHING" — false if the names just
    # don't resolve (e.g. org networks not fetched). Carriage UNKNOWN -> the
    # L2 graph's assumed-carriage rule takes over (unknown != empty)
    ir = _gateway_ir(org_networks=())
    p = ir.ports["cc0000000001:ge-0/0/3"]
    assert p.native_vlan is None and p.tagged_vlans == ()
    assert p.meta.provenance.value == "inferred"


def test_gateway_wan_port_carries_no_site_vlans():
    ir = _gateway_ir()
    p = ir.ports["cc0000000001:ge-0/0/0"]
    assert p.native_vlan is None and p.tagged_vlans == ()


def test_gateway_l3_interfaces_from_ip_configs_and_attached_routed_networks():
    from digital_twin.ir.entities import L3Role

    ir = _gateway_ir()
    gw_intfs = {i.vlan_id: i for i in ir.l3intfs if i.device_id == "cc0000000001"}
    # ip_configs entry: an explicit config statement -> HIGH
    assert gw_intfs[10].role is L3Role.GATEWAY and gw_intfs[10].ip == "198.51.100.1"
    assert gw_intfs[10].meta.confidence.level.name == "HIGH"
    # 'corp' is ALSO routed (subnet) + attached to the LAN port — same intf;
    # 'iot' has no subnet and no ip_config -> no L3 claim for vlan 20
    assert 20 not in gw_intfs


def test_gateway_attached_routed_network_without_ip_config_is_an_inferred_exit():
    from digital_twin.ir.entities import L3Role

    org_nets = ({"name": "corp", "vlan_id": 10, "subnet": "198.51.100.0/24"},
                {"name": "iot", "vlan_id": 20}, {"name": "mgmt", "vlan_id": 1})
    gw = {**_GATEWAY, "ip_configs": {}}
    from digital_twin.adapters.mist.ingest.base import IngestContext
    from digital_twin.ir import IRBuilder

    ctx = IngestContext(
        raw=raw_site(devices=(gw,), org_networks=org_nets),
        site_effective={"networks": {}},
        device_effective={},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    ir = ctx.builder.build()
    intfs = {i.vlan_id: i for i in ir.l3intfs if i.device_id == "cc0000000001"}
    # routed + attached to a LAN port: the Mist gateway model terminates it
    # there — INFERRED (the claim rides the model, not an explicit statement)
    assert intfs[10].role is L3Role.GATEWAY
    assert intfs[10].meta.provenance.value == "inferred"


def test_templated_org_network_values_never_crash_and_stay_unresolved():
    # real orgs carry org-level template vars in networks (vlan_id
    # '{{guest_vlan}}', subnet '{{guest_net}}/{{cidr}}') — found live
    # 2026-06-11, crashed the whole ingester. Unresolvable = UNKNOWN: the
    # referencing port stays blind, no routed intent is invented.
    org_nets = (
        {"name": "guest", "vlan_id": "{{guest_vlan}}", "subnet": "{{guest_net}}/{{cidr}}"},
        {"name": "mgmt", "vlan_id": 1},
        {"name": "corp", "vlan_id": 10},
        {"name": "iot", "vlan_id": 20},
    )
    gw = {**_GATEWAY, "port_config": {"ge-0/0/3": {"usage": "lan",
                                                   "networks": ["corp", "guest"],
                                                   "port_network": "mgmt"}}}
    from digital_twin.adapters.mist.ingest.base import IngestContext
    from digital_twin.ir import IRBuilder

    ctx = IngestContext(
        raw=raw_site(devices=(gw,), org_networks=org_nets),
        site_effective={"networks": {}},
        device_effective={},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    ir = ctx.builder.build()
    p = ir.ports["cc0000000001:ge-0/0/3"]
    assert p.tagged_vlans == () and p.meta.provenance.value == "inferred"  # blind
    assert all("{{" not in str(v.subnet) for v in ir.vlans.values() if v.subnet)


def test_gateway_is_marked_l3_unmodeled_when_org_networks_unfetched():
    # review on 9b4dbe7: a failed/absent org_networks fetch silently degraded
    # gateway facts while L3_EXITS was still earned — the blind spot must be
    # IN the IR (checks never see raw fetch meta)
    from digital_twin.adapters.mist.ingest.base import IngestContext
    from digital_twin.ir import IRBuilder

    fetched = tuple(f for f in ALL_FETCHED if f != "org_networks")
    ctx = IngestContext(
        raw=raw_site(devices=(_GATEWAY, SWITCH_A), fetched=fetched),
        site_effective={"networks": {}},
        device_effective={},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    ir = ctx.builder.build()
    assert ir.devices["cc0000000001"].l3_unmodeled is True
    assert ir.devices["aa0000000001"].l3_unmodeled is False  # switches: IRBs modeled


def test_gateway_is_not_l3_unmodeled_when_org_networks_fetched():
    ir = _gateway_ir()  # raw_site defaults: org_networks in ALL_FETCHED
    assert ir.devices["cc0000000001"].l3_unmodeled is False


def test_vlan_dhcp_sources_from_site_and_gateway():
    # GS24: a vlan's modeled DHCP providers — site-level dhcpd_config (the
    # switch-hosted server/relay) and the gateway's own dhcpd_config (resolved
    # via org networks). type 'none' is an explicit NO-path statement; a relay
    # without servers forwards nowhere.
    from digital_twin.adapters.mist.ingest.base import IngestContext
    from digital_twin.ir import IRBuilder

    eff = {
        "networks": {
            "corp_sw": {"vlan_id": 10},
            "iot_sw": {"vlan_id": 20},
            "lab": {"vlan_id": 30},
            "stale": {"vlan_id": 40},
        },
        "dhcpd_config": {
            "iot_sw": {"type": "local", "ip_start": "10.0.0.10"},
            "lab": {"type": "relay", "servers": ["10.9.9.9"]},
            "stale": {"type": "none"},
        },
    }
    gw = {**_GATEWAY, "dhcpd_config": {"corp": {"type": "local"}}, "ip_configs": {}}
    ctx = IngestContext(
        raw=raw_site(devices=(SWITCH_A, gw), org_networks=_ORG_NETWORKS),
        site_effective=eff,
        device_effective={"aa0000000001": eff},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    ir = ctx.builder.build()
    assert ir.vlans[10].dhcp_sources == ("cc0000000001",)  # gateway serves corp
    assert ir.vlans[20].dhcp_sources == ("site",)  # switch-hosted local server
    assert ir.vlans[30].dhcp_sources == ("site",)  # relay WITH servers = a path
    assert ir.vlans[40].dhcp_sources == ()  # type none = explicitly no path


def test_unresolvable_gateway_dhcp_reference_marks_the_gateway_unresolved():
    # review on 80c4c48 (P1): org networks FETCHED but the gateway's dhcpd
    # entry references a name that is not there (or has a templated vlan_id).
    # GS22 rule: unresolved = UNKNOWN, not absent — the gateway may be serving
    # DHCP on a vlan we cannot identify, and the check must know.
    from digital_twin.adapters.mist.ingest.base import IngestContext
    from digital_twin.ir import IRBuilder

    gw = {**_GATEWAY, "dhcpd_config": {"mystery_net": {"type": "local"}}, "ip_configs": {}}
    ctx = IngestContext(
        raw=raw_site(devices=(gw,), org_networks=_ORG_NETWORKS),
        site_effective={"networks": {}},
        device_effective={},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    dev = ctx.builder.build().devices["cc0000000001"]
    assert dev.dhcp_unresolved is True


def test_templated_org_vlan_must_not_be_guessed_from_the_site_namespace():
    # review on d90fa15 (P1): org 'corp' has vlan_id '{{corp_vlan}}' and a
    # SITE network happens to share the name with vlan 10. The gateway's
    # reference is to the ORG network — present-but-unparseable SHADOWS the
    # site fallback (same contract as the dynamic-usage sources: key present
    # with None = known-but-unresolvable, key missing = fall through).
    from digital_twin.adapters.mist.ingest.base import IngestContext
    from digital_twin.ir import IRBuilder

    org_nets = ({"name": "corp", "vlan_id": "{{corp_vlan}}"},)
    eff = {"networks": {"corp": {"vlan_id": 10}}}
    gw = {**_GATEWAY, "port_config": {}, "ip_configs": {},
          "dhcpd_config": {"corp": {"type": "local"}}}
    ctx = IngestContext(
        raw=raw_site(devices=(gw,), org_networks=org_nets),
        site_effective=eff,
        device_effective={},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    ir = ctx.builder.build()
    assert ir.devices["cc0000000001"].dhcp_unresolved is True
    assert "cc0000000001" not in ir.vlans[10].dhcp_sources


def test_resolvable_gateway_dhcp_reference_is_not_flagged():
    from digital_twin.adapters.mist.ingest.base import IngestContext
    from digital_twin.ir import IRBuilder

    gw = {**_GATEWAY, "dhcpd_config": {"corp": {"type": "local"}}, "ip_configs": {}}
    ctx = IngestContext(
        raw=raw_site(devices=(gw,), org_networks=_ORG_NETWORKS),
        site_effective={"networks": {}},
        device_effective={},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    assert ctx.builder.build().devices["cc0000000001"].dhcp_unresolved is False


def test_relay_without_servers_is_not_a_dhcp_path():
    from digital_twin.adapters.mist.ingest.base import IngestContext
    from digital_twin.ir import IRBuilder

    eff = {
        "networks": {"corp_sw": {"vlan_id": 10}},
        "dhcpd_config": {"corp_sw": {"type": "relay", "servers": []}},
    }
    ctx = IngestContext(
        raw=raw_site(devices=(SWITCH_A,)),
        site_effective=eff,
        device_effective={"aa0000000001": eff},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    assert ctx.builder.build().vlans[10].dhcp_sources == ()


def test_org_network_subnet_marks_the_vlan_routed():
    eff = {"networks": {"corp_sw": {"vlan_id": 10}}}
    from digital_twin.adapters.mist.ingest.base import IngestContext
    from digital_twin.ir import IRBuilder

    ctx = IngestContext(
        raw=raw_site(devices=(SWITCH_A,), org_networks=_ORG_NETWORKS),
        site_effective=eff,
        device_effective={"aa0000000001": eff},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    ir = ctx.builder.build()
    assert ir.vlans[10].subnet == "198.51.100.0/24"  # org net 'corp' overlay


def test_vlan_carries_routed_intent_subnet():
    from digital_twin.adapters.mist.ingest.base import IngestContext
    from digital_twin.ir import IRBuilder

    eff = {"networks": {"corp": {"vlan_id": 10, "subnet": "198.51.100.0/24"}}}
    ctx = IngestContext(
        raw=raw_site(devices=(SWITCH_A,)),
        site_effective=eff,
        device_effective={"aa0000000001": eff},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    ir = ctx.builder.build()
    assert ir.vlans[10].subnet == "198.51.100.0/24"


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


def test_invalid_bridge_priority_is_modeled_distinctly_from_absent():
    # absent = platform default (assumed); INVALID = uninterpretable — the IR
    # must distinguish them or the root check would simulate banana as 32768
    from digital_twin.adapters.mist.ingest.base import IngestContext
    from digital_twin.ir import IRBuilder

    eff = {"stp_config": {"bridge_priority": "banana"}, "port_config": {}}
    ctx = IngestContext(
        raw=raw_site(devices=(SWITCH_A,)),
        site_effective=eff,
        device_effective={"aa0000000001": eff},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    dev = ctx.builder.build().devices["aa0000000001"]
    assert dev.stp_priority is None and dev.stp_priority_invalid is True


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
