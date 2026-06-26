from digital_twin.adapters.mist.ingest.base import IngestContext
from digital_twin.adapters.mist.ingest.switch import SwitchIngester
from digital_twin.ir import DeviceRole, IRBuilder, IRCapability, L3Role, PortMode
from tests.adapters.mist.fixtures import ALL_FETCHED, SITE_EFFECTIVE, SWITCH_A, raw_site


def test_mac_limit_normalizer():
    from digital_twin.adapters.mist.ingest.switch import _mac_limit
    assert _mac_limit(5) == 5 and _mac_limit("5") == 5
    assert _mac_limit(0) is None and _mac_limit("") is None and _mac_limit(None) is None
    assert _mac_limit(True) is None
    assert isinstance(_mac_limit("{{var}}"), str) and _mac_limit("{{var}}").startswith("unresolved")
    assert isinstance(_mac_limit({"x": 1}), str)  # object -> token, not None


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


def test_inline_disabled_via_overwrite_marks_port_disabled():
    # overwrite-only port with disabled:true -> Port.disabled True (the bug shape)
    from digital_twin.adapters.mist.ingest.base import IngestContext
    from digital_twin.ir import IRBuilder

    eff = {
        "networks": {"corp": {"vlan_id": 10}},
        "port_usages": {"office": {"mode": "access", "port_network": "corp"}},
        "port_config": {"ge-0/0/1": {"usage": "office"}},
        "port_config_overwrite": {"mge-0/0/0": {"disabled": True}},
    }
    device = {
        **SWITCH_A,
        "port_config": eff["port_config"],
        "port_config_overwrite": eff["port_config_overwrite"],
    }
    ctx = IngestContext(
        raw=raw_site(devices=(device,)),
        site_effective=eff,
        device_effective={"aa0000000001": eff},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    ir = ctx.builder.build()
    assert ir.ports["aa0000000001:mge-0/0/0"].disabled is True
    assert ir.ports["aa0000000001:ge-0/0/1"].disabled is False


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


def test_dhcp_type_server_is_a_serving_path_like_local():
    # GS25 review (P2): the checked-in OAS enums dhcpd type as
    # {none, relay, server} — 'local' is the LIVE-fixture shape, 'server' the
    # OAS-canonical one. Both exist in the wild; a 'server' entry invisible
    # as a DHCP source is a GS24 false-SAFE (its removal would never flag).
    from digital_twin.adapters.mist.ingest.base import IngestContext
    from digital_twin.ir import IRBuilder

    eff = {
        "networks": {"corp_sw": {"vlan_id": 10}, "iot_sw": {"vlan_id": 20}},
        "dhcpd_config": {"iot_sw": {"type": "server", "ip_start": "10.0.0.10"}},
    }
    gw = {**_GATEWAY, "dhcpd_config": {"corp": {"type": "server"}}, "ip_configs": {}}
    ctx = IngestContext(
        raw=raw_site(devices=(SWITCH_A, gw), org_networks=_ORG_NETWORKS),
        site_effective=eff,
        device_effective={"aa0000000001": eff},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    ir = ctx.builder.build()
    assert ir.vlans[10].dhcp_sources == ("cc0000000001",)  # OAS 'server' shape
    assert ir.vlans[20].dhcp_sources == ("site",)


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


def test_unfetched_namespace_never_credits_the_gateway_as_a_dhcp_source():
    # review on fa9c0a9: org_networks UNFETCHED -> the org map is empty, every
    # name looks "missing", and the site fallback guessed a vlan — crediting
    # the blind gateway as a DHCP source SUPPRESSES removal findings before
    # the l3_unmodeled cap can run. Unfetched namespace = no gateway facts.
    from digital_twin.adapters.mist.ingest.base import IngestContext
    from digital_twin.ir import IRBuilder

    fetched = tuple(f for f in ALL_FETCHED if f != "org_networks")
    eff = {"networks": {"corp": {"vlan_id": 10}},
           "dhcpd_config": {"corp": {"type": "local"}}}
    gw = {**_GATEWAY, "port_config": {}, "ip_configs": {},
          "dhcpd_config": {"corp": {"type": "local"}}}
    ctx = IngestContext(
        raw=raw_site(devices=(gw,), fetched=fetched),
        site_effective=eff,
        device_effective={},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    ir = ctx.builder.build()
    assert ir.vlans[10].dhcp_sources == ("site",)  # the gateway is NOT credited
    assert ir.devices["cc0000000001"].l3_unmodeled is True


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


def test_compile_device_carries_dhcp_snooping():
    # GS21 lesson: a device field the gate allows but the compiler drops is a
    # false-SAFE shape (the simulation silently ignores the change)
    from digital_twin.adapters.mist.compile.switch import compile_device

    site = {"dhcp_snooping": {"enabled": True, "all_networks": True}}
    dev = {"dhcp_snooping": {"enabled": True, "networks": ["corp"]}}
    eff = compile_device(None, site, dev)
    # device value REPLACES the site value wholesale (merge.py REPLACE policy)
    assert eff["dhcp_snooping"] == {"enabled": True, "networks": ["corp"]}


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
    assert f.subject is not None and f.subject.kind == "device" and f.subject.id == "d1"
    # a malformed BASELINE poisons the prediction too — both sides checked
    assert invalid_bridge_priority_findings({"d1": bad}, {"d1": good}) != []


def test_unresolved_dhcp_range_finding_is_delta_gated():
    # spec r1: a PRE-EXISTING unchanged template must not floor unrelated
    # plans to REVIEW (adapter findings are baseline-blind); only a delta
    # that INTRODUCES or CHANGES the unresolved value fires.
    from digital_twin.adapters.mist.ingest.switch import unresolved_dhcp_range_findings

    tpl = {"dhcpd_config": {"corp": {"type": "local", "ip_start": "{{a}}"}}}
    clean = {"dhcpd_config": {"corp": {"type": "local", "ip_start": "10.0.0.1"}}}
    other = {"dhcpd_config": {"corp": {"type": "local", "ip_start": "{{b}}"}}}

    assert unresolved_dhcp_range_findings(tpl, tpl) == []          # pre-existing
    assert unresolved_dhcp_range_findings(clean, clean) == []      # nothing wrong
    intro = unresolved_dhcp_range_findings({}, tpl)                # introduced
    assert [f.code for f in intro] == ["scope.dhcp.range_unresolved"]
    assert intro[0].evidence["before"] is None                     # None -> template
    assert intro[0].subject is not None
    assert intro[0].subject.kind == "dhcp_scope" and intro[0].subject.id == "corp"
    changed = unresolved_dhcp_range_findings(tpl, other)           # changed
    assert [f.code for f in changed] == ["scope.dhcp.range_unresolved"]
    assert changed[0].evidence["before"] == "{{a}}"                # template -> template
    assert unresolved_dhcp_range_findings(clean, tpl) != []        # literal -> template
    assert unresolved_dhcp_range_findings(tpl, clean) == []        # resolved -> fine


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
            "ge-0/0/3": {"usage": "plain", "no_local_overwrite": False},
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


def _ir_for(eff):
    ctx = IngestContext(
        raw=raw_site(devices=(SWITCH_A,)),
        site_effective=eff,
        device_effective={"aa0000000001": eff},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    return ctx.builder.build()


def test_port_dhcp_trust_tristate():
    # OAS: allow_dhcpd is tri-state — only the UNDEFINED value defers to the
    # mode default (trunk trusted / access untrusted); explicit false wins
    # even on a trunk. Unresolved usage = trust UNKNOWN, never untrusted.
    eff = {
        "networks": {"corp": {"vlan_id": 10}},
        "port_usages": {
            "up": {"mode": "trunk", "networks": ["corp"]},
            "up_untrusted": {"mode": "trunk", "networks": ["corp"], "allow_dhcpd": False},
            "edge": {"mode": "access", "port_network": "corp"},
            "edge_trusted": {"mode": "access", "port_network": "corp", "allow_dhcpd": True},
        },
        "port_config": {
            "ge-0/0/0": {"usage": "up"},
            "ge-0/0/1": {"usage": "up_untrusted"},
            "ge-0/0/2": {"usage": "edge"},
            "ge-0/0/3": {"usage": "edge_trusted"},
            "ge-0/0/4": {"usage": "missing_usage"},
            "ge-0/0/5": {"usage": "edge", "allow_dhcpd": True},  # inline override
        },
    }
    ir = _ir_for(eff)
    t = {p.name: p.dhcp_trusted for p in ir.ports.values()}
    assert t["ge-0/0/0"] is True       # trunk, absent -> trusted
    assert t["ge-0/0/1"] is False      # explicit false beats trunk
    assert t["ge-0/0/2"] is False      # access, absent -> untrusted
    assert t["ge-0/0/3"] is True       # explicit true beats access
    assert t["ge-0/0/4"] is None       # unresolved usage -> UNKNOWN
    assert t["ge-0/0/5"] is True       # inline port_config override honored


def test_device_dhcp_snooping_fact():
    def ir_with(snoop):
        eff = {"networks": {"corp": {"vlan_id": 10}}}
        if snoop is not None:
            eff["dhcp_snooping"] = snoop
        return _ir_for(eff)

    assert ir_with(None).devices["aa0000000001"].dhcp_snooping is None
    assert ir_with({"enabled": False, "networks": ["corp"]}).devices[
        "aa0000000001"
    ].dhcp_snooping is None
    assert ir_with({"enabled": True, "networks": ["corp"]}).devices[
        "aa0000000001"
    ].dhcp_snooping == ("corp",)
    assert ir_with({"enabled": True, "all_networks": True}).devices[
        "aa0000000001"
    ].dhcp_snooping == ("*",)


def test_dhcp_scopes_minted_for_serving_entries_only():
    # _dhcp_serves_scope truth table (spec): serving (local/server/absent) ->
    # scope row; relay (a valid GS24 PATH) and none -> sources-only, NEVER a
    # scope (a range-less relay row would drag PARTIAL noise onto every
    # normal relay config).
    eff = {
        "networks": {
            "corp": {"vlan_id": 10, "subnet": "10.0.0.0/24"},
            "lab": {"vlan_id": 30},
            "old": {"vlan_id": 40},
        },
        "dhcpd_config": {
            "corp": {"type": "local", "ip_start": "10.0.0.10", "ip_end": "10.0.0.99",
                     "gateway": "10.0.0.1"},
            "lab": {"type": "relay", "servers": ["10.9.9.9"]},
            "old": {"type": "none"},
        },
    }
    ir = _ir_for(eff)
    assert [s.id for s in ir.dhcp_scopes] == ["site:corp"]
    s = ir.dhcp_scopes[0]
    assert (s.vlan, s.ip_start, s.ip_end, s.gateway, s.subnet) == (
        10, "10.0.0.10", "10.0.0.99", "10.0.0.1", "10.0.0.0/24"
    )


def test_gateway_scope_resolves_via_org_namespace():
    gw = {**_GATEWAY, "ip_configs": {}, "dhcpd_config": {
        "corp": {"type": "server", "ip_start": "198.51.100.10", "ip_end": "198.51.100.99"}
    }}
    ctx = IngestContext(
        raw=raw_site(devices=(SWITCH_A, gw), org_networks=_ORG_NETWORKS),
        site_effective={},
        device_effective={},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    ir = ctx.builder.build()
    s = next(x for x in ir.dhcp_scopes if x.provider == "cc0000000001")
    # org net corp: vlan 10, subnet 198.51.100.0/24 (_ORG_NETWORKS fixture)
    assert (s.vlan, s.subnet) == (10, "198.51.100.0/24")


def test_unfetched_org_namespace_still_mints_gateway_scope_ranges():
    # GS24 rule untouched: NO dhcp_sources credit from a blind namespace.
    # But ranges are LITERAL device config — without the scope row a new
    # site scope overlapping the gateway range would falsely PASS (review r2).
    gw = {**_GATEWAY, "ip_configs": {}, "dhcpd_config": {
        "corp": {"type": "local", "ip_start": "198.51.100.10", "ip_end": "198.51.100.99"}
    }}
    fetched = tuple(f for f in ALL_FETCHED if f != "org_networks")
    ctx = IngestContext(
        raw=raw_site(devices=(SWITCH_A, gw), fetched=fetched),
        site_effective={},
        device_effective={},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    ir = ctx.builder.build()
    s = next(x for x in ir.dhcp_scopes if x.provider == "cc0000000001")
    assert (s.vlan, s.subnet) == (None, None)
    assert (s.ip_start, s.ip_end) == ("198.51.100.10", "198.51.100.99")
    assert s.subnet_unresolved is True  # intent UNKNOWABLE, not "no intent"


def test_gateway_scope_with_unknown_network_name_is_subnet_unresolved():
    # org namespace FETCHED but the dhcpd entry names a network that is not
    # there: subnet intent is UNKNOWABLE (the network is missing entirely),
    # which is not the same as "fetched and declares no subnet"
    gw = {**_GATEWAY, "ip_configs": {}, "dhcpd_config": {
        "mystery_net": {"type": "local", "ip_start": "10.7.0.10", "ip_end": "10.7.0.99"}
    }}
    ctx = IngestContext(
        raw=raw_site(devices=(SWITCH_A, gw), org_networks=_ORG_NETWORKS),
        site_effective={}, device_effective={}, builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    ir = ctx.builder.build()
    s = next(x for x in ir.dhcp_scopes if x.provider == "cc0000000001")
    assert (s.ip_start, s.ip_end) == ("10.7.0.10", "10.7.0.99")  # ranges literal
    assert s.subnet is None
    assert s.subnet_unresolved is True  # name missing from a FETCHED namespace


def test_templated_range_fields_mint_none_never_crash():
    eff = {
        "networks": {"corp": {"vlan_id": 10}},
        "dhcpd_config": {"corp": {"type": "local", "ip_start": "{{dhcp_start}}",
                                  "ip_end": "10.0.0.99"}},
    }
    s = _ir_for(eff).dhcp_scopes[0]
    assert s.ip_start is None and s.ip_end == "10.0.0.99"


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


def test_vlan_gateway_from_winning_row_with_org_overlay():
    eff = {"networks": {
        "corp": {"vlan_id": 10, "gateway": "10.0.0.1"},
        "lab": {"vlan_id": 30},
    }}
    ctx = IngestContext(
        raw=raw_site(org_networks=({"name": "labnet", "vlan_id": 30,
                                    "gateway": "10.0.30.1"},)),
        site_effective=eff,
        device_effective={"aa0000000001": eff},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    ir = ctx.builder.build()
    assert ir.vlans[10].gateway == "10.0.0.1"      # site row wins
    assert ir.vlans[30].gateway == "10.0.30.1"     # org overlay fills absence
    assert ir.vlans[10].gateway_unresolved is False


def test_vlan_gateway_templated_winner_shadows_org():
    # present-shadows contract: an unreadable declared value NEVER falls
    # through to another namespace
    eff = {"networks": {"corp": {"vlan_id": 10, "gateway": "{{gw}}"}}}
    ctx = IngestContext(
        raw=raw_site(org_networks=({"name": "corpnet", "vlan_id": 10,
                                    "gateway": "10.0.0.1"},)),
        site_effective=eff,
        device_effective={"aa0000000001": eff},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    v = ctx.builder.build().vlans[10]
    assert v.gateway is None and v.gateway_unresolved is True


def test_conflicting_nonwinning_device_row_makes_gateway_unresolved():
    # singleton-Vlan limitation (spec r3): a device row for an already-seen
    # vlan id that DISAGREES on the gateway = ambiguous intent, never a
    # silent winner — and the Vlan CHANGES (diff fires) when a device op
    # introduces the conflict, closing the false-SAFE shape
    site = {"networks": {"corp": {"vlan_id": 10, "gateway": "10.0.0.1"}}}
    dev = {"networks": {"corp_local": {"vlan_id": 10, "gateway": "10.0.0.9"}}}
    ctx = IngestContext(
        raw=raw_site(),
        site_effective=site,
        device_effective={"aa0000000001": dev},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    v = ctx.builder.build().vlans[10]
    assert v.gateway is None and v.gateway_unresolved is True


def test_agreeing_rows_and_gatewayless_rows_do_not_conflict():
    site = {"networks": {"corp": {"vlan_id": 10, "gateway": "10.0.0.1"}}}
    dev = {"networks": {
        "corp_local": {"vlan_id": 10, "gateway": "10.0.0.1/24"},  # same_ip True
        "corp_plain": {"vlan_id": 10},                            # no gateway key
    }}
    ctx = IngestContext(
        raw=raw_site(),
        site_effective=site,
        device_effective={"aa0000000001": dev},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    v = ctx.builder.build().vlans[10]
    assert v.gateway == "10.0.0.1" and v.gateway_unresolved is False


def test_silent_winner_with_declaring_nonwinning_row_is_unresolved():
    # review r1: the WINNING row has no gateway but a later device row
    # declares one — never silently promote it, never fall through to org
    site = {"networks": {"corp": {"vlan_id": 10}}}
    dev = {"networks": {"corp_local": {"vlan_id": 10, "gateway": "10.0.0.9"}}}
    ctx = IngestContext(
        raw=raw_site(org_networks=({"name": "corpnet", "vlan_id": 10,
                                    "gateway": "10.0.0.1"},)),
        site_effective=site,
        device_effective={"aa0000000001": dev},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    v = ctx.builder.build().vlans[10]
    assert v.gateway is None and v.gateway_unresolved is True


def test_explicit_null_gateway_is_no_intent():
    # null==absent canon: an explicit "gateway": null neither conflicts nor
    # blocks the org fallback
    site = {"networks": {"corp": {"vlan_id": 10, "gateway": None}}}
    ctx = IngestContext(
        raw=raw_site(org_networks=({"name": "corpnet", "vlan_id": 10,
                                    "gateway": "10.0.0.1"},)),
        site_effective=site,
        device_effective={"aa0000000001": site},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    v = ctx.builder.build().vlans[10]
    assert v.gateway == "10.0.0.1" and v.gateway_unresolved is False


def test_scope_network_gateway_site_namespace():
    eff = {
        "networks": {"corp": {"vlan_id": 10, "gateway": "10.0.0.1"},
                     "iot": {"vlan_id": 20, "gateway": "{{gw}}"}},
        "dhcpd_config": {
            "corp": {"type": "local", "gateway": "10.0.0.254"},
            "iot": {"type": "local"},
        },
    }
    scopes = {s.id: s for s in _ir_for(eff).dhcp_scopes}
    s = scopes["site:corp"]
    assert s.network_gateway == "10.0.0.1"
    assert s.network_gateway_unresolved is False
    t = scopes["site:iot"]
    assert t.network_gateway is None and t.network_gateway_unresolved is True


def test_gateway_scope_network_gateway_org_namespace():
    gw = {**_GATEWAY, "ip_configs": {}, "dhcpd_config": {
        "corp": {"type": "local", "gateway": "198.51.100.254"}
    }}
    org = ({"name": "corp", "vlan_id": 10, "gateway": "198.51.100.1"},)
    ctx = IngestContext(
        raw=raw_site(devices=(SWITCH_A, gw), org_networks=org),
        site_effective={}, device_effective={}, builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    s = next(x for x in ctx.builder.build().dhcp_scopes if x.provider == "cc0000000001")
    assert s.network_gateway == "198.51.100.1"
    assert s.network_gateway_unresolved is False


def test_gateway_scope_network_gateway_blind_namespace_unresolved():
    gw = {**_GATEWAY, "ip_configs": {}, "dhcpd_config": {"corp": {"type": "local"}}}
    fetched = tuple(f for f in ALL_FETCHED if f != "org_networks")
    ctx = IngestContext(
        raw=raw_site(devices=(SWITCH_A, gw), fetched=fetched),
        site_effective={}, device_effective={}, builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    s = next(x for x in ctx.builder.build().dhcp_scopes if x.provider == "cc0000000001")
    assert s.network_gateway is None and s.network_gateway_unresolved is True


def test_templated_vlan_id_row_is_skipped_never_a_crash():
    # _vlan_int contract: unparseable = UNKNOWN, never a crash — the mint
    # loop must guard exactly like the rows_by_vid collection does
    eff = {"networks": {
        "corp": {"vlan_id": 10, "gateway": "10.0.0.1"},
        "tpl": {"vlan_id": "{{vlan}}", "gateway": "10.0.99.1"},
    }}
    ir = _ir_for(eff)
    assert 10 in ir.vlans
    assert ir.vlans[10].gateway == "10.0.0.1"
    assert len(ir.vlans) >= 1  # the templated row minted nothing and crashed nothing


def test_gateway_scope_org_templated_gateway_is_unresolved():
    gw = {**_GATEWAY, "ip_configs": {}, "dhcpd_config": {"corp": {"type": "local"}}}
    org = ({"name": "corp", "vlan_id": 10, "gateway": "{{gw}}"},)
    ctx = IngestContext(
        raw=raw_site(devices=(SWITCH_A, gw), org_networks=org),
        site_effective={}, device_effective={}, builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    s = next(x for x in ctx.builder.build().dhcp_scopes if x.provider == "cc0000000001")
    assert s.network_gateway is None and s.network_gateway_unresolved is True


def test_org_only_templated_gateway_sets_unresolved():
    eff = {"networks": {"corp": {"vlan_id": 10}}}
    ctx = IngestContext(
        raw=raw_site(org_networks=({"name": "corpnet", "vlan_id": 10,
                                    "gateway": "{{gw}}"},)),
        site_effective=eff,
        device_effective={"aa0000000001": eff},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    v = ctx.builder.build().vlans[10]
    assert v.gateway is None and v.gateway_unresolved is True


def test_vlan_templated_subnet_is_unresolved_not_routed():
    # the false-SAFE: a templated subnet must NOT read as a literal nor as
    # "not routed" — it is declared-but-unreadable
    eff = {"networks": {"corp": {"vlan_id": 10, "subnet": "{{vlan10_subnet}}"}}}
    ctx = IngestContext(
        raw=raw_site(),
        site_effective=eff,
        device_effective={"aa0000000001": eff},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    v = ctx.builder.build().vlans[10]
    assert v.subnet is None and v.subnet_unresolved is True


def test_vlan_empty_subnet_is_absent_not_blind():
    # present-but-empty "" = no routed intent, NOT a blind spot (no flag)
    eff = {"networks": {"corp": {"vlan_id": 10, "subnet": ""}}}
    ctx = IngestContext(
        raw=raw_site(),
        site_effective=eff,
        device_effective={"aa0000000001": eff},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    v = ctx.builder.build().vlans[10]
    assert v.subnet is None and v.subnet_unresolved is False


def test_org_overlay_empty_subnet_is_absent_not_blind():
    # the truthy guard treats an org-network subnet "" as absent (no overlay),
    # same as a missing key — never a blind spot
    eff = {"networks": {"corp": {"vlan_id": 10}}}
    ctx = IngestContext(
        raw=raw_site(org_networks=({"name": "corpnet", "vlan_id": 10, "subnet": ""},)),
        site_effective=eff,
        device_effective={"aa0000000001": eff},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    v = ctx.builder.build().vlans[10]
    assert v.subnet is None and v.subnet_unresolved is False


def test_vlan_no_subnet_anywhere_is_absent_not_unresolved():
    # leg 1: a vlan known only by id, no subnet in any effective row and no
    # matching org network -> not routed, NOT a blind spot (flag stays False)
    eff = {"networks": {"corp": {"vlan_id": 10}}}
    ctx = IngestContext(
        raw=raw_site(),
        site_effective=eff,
        device_effective={"aa0000000001": eff},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    v = ctx.builder.build().vlans[10]
    assert v.subnet is None and v.subnet_unresolved is False


def test_vlan_subnet_org_overlay_literal_still_routed():
    # regression: switch knows the vlan by id, org networks carry the subnet
    eff = {"networks": {"corp": {"vlan_id": 10}}}
    ctx = IngestContext(
        raw=raw_site(org_networks=({"name": "corpnet", "vlan_id": 10,
                                    "subnet": "10.0.10.0/24"},)),
        site_effective=eff,
        device_effective={"aa0000000001": eff},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    v = ctx.builder.build().vlans[10]
    assert v.subnet == "10.0.10.0/24" and v.subnet_unresolved is False


def test_vlan_org_only_templated_subnet_sets_unresolved():
    # no effective row declares subnet; org overlay value is templated
    eff = {"networks": {"corp": {"vlan_id": 10}}}
    ctx = IngestContext(
        raw=raw_site(org_networks=({"name": "corpnet", "vlan_id": 10,
                                    "subnet": "{{sub}}"},)),
        site_effective=eff,
        device_effective={"aa0000000001": eff},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    v = ctx.builder.build().vlans[10]
    assert v.subnet is None and v.subnet_unresolved is True


def test_conflicting_nonwinning_device_row_makes_subnet_unresolved():
    # literal-disagreement leg: a device row for an already-seen vlan id
    # declares a DIFFERENT subnet than the winner -> ambiguous, never silent
    site = {"networks": {"corp": {"vlan_id": 10, "subnet": "10.0.10.0/24"}}}
    dev = {"networks": {"corp_local": {"vlan_id": 10, "subnet": "10.0.99.0/24"}}}
    ctx = IngestContext(
        raw=raw_site(),
        site_effective=site,
        device_effective={"aa0000000001": dev},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    v = ctx.builder.build().vlans[10]
    assert v.subnet is None and v.subnet_unresolved is True


def test_silent_winner_with_declaring_nonwinning_subnet_row_is_unresolved():
    # review P2: the WINNING row has no subnet but a later device row declares
    # one -> never silently promote it, never fall through to org (the
    # distinct false-SAFE leg, twin of the gateway suite)
    site = {"networks": {"corp": {"vlan_id": 10}}}
    dev = {"networks": {"corp_local": {"vlan_id": 10, "subnet": "10.0.99.0/24"}}}
    ctx = IngestContext(
        raw=raw_site(org_networks=({"name": "corpnet", "vlan_id": 10,
                                    "subnet": "10.0.10.0/24"},)),
        site_effective=site,
        device_effective={"aa0000000001": dev},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    v = ctx.builder.build().vlans[10]
    assert v.subnet is None and v.subnet_unresolved is True


def test_agreeing_subnet_rows_do_not_conflict():
    # host-bits-set sibling that normalizes equal -> still the winner literal
    site = {"networks": {"corp": {"vlan_id": 10, "subnet": "10.0.10.0/24"}}}
    dev = {"networks": {
        "corp_local": {"vlan_id": 10, "subnet": "10.0.10.5/24"},  # same_subnet True
        "corp_plain": {"vlan_id": 10},                            # no subnet key
    }}
    ctx = IngestContext(
        raw=raw_site(),
        site_effective=site,
        device_effective={"aa0000000001": dev},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    v = ctx.builder.build().vlans[10]
    assert v.subnet == "10.0.10.0/24" and v.subnet_unresolved is False


# -- GS26: OSPF participation ingest -------------------------------------------

_OSPF_EFF_BASE: dict = {
    "networks": {"corp": {"vlan_id": 10}, "guest": {"vlan_id": 20}},
    "port_usages": {"office": {"mode": "access", "port_network": "corp"}},
    "port_config": {},
    "ospf_config": {"enabled": True},
    "ospf_areas": {"0": {"networks": {"corp": {}, "guest": {"passive": True}}}},
}

_OSPF_SWITCH: dict = {
    "mac": "aa0000000001",
    "id": "dev-a",
    "type": "switch",
    "model": "EX4100-48P",
    "name": "sw-ospf",
}


def _ospf_ir(eff: dict):
    ctx = IngestContext(
        raw=raw_site(devices=(_OSPF_SWITCH,)),
        site_effective=eff,
        device_effective={"aa0000000001": eff},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    return ctx.builder.build()


def test_ospf_ingest_mints_participation_for_enabled_switch():
    ir = _ospf_ir(_OSPF_EFF_BASE)
    by_name = {o.network_name: o for o in ir.ospf_intfs}
    assert by_name["corp"].vlan_id == 10 and by_name["corp"].passive is False
    assert by_name["corp"].area == "0"  # the area key round-trips as a string
    assert by_name["guest"].vlan_id == 20 and by_name["guest"].passive is True
    assert all(o.unresolved is False for o in ir.ospf_intfs)


def test_ospf_ingest_silent_when_disabled():
    eff = {**_OSPF_EFF_BASE, "ospf_config": {"enabled": False}}
    ir = _ospf_ir(eff)
    assert ir.ospf_intfs == ()


def test_ospf_ingest_silent_when_config_absent():
    # the gate uses truthiness, so an absent ospf_config mints nothing (guards
    # against a future refactor to `== True` that would crash on the missing key)
    eff = {k: v for k, v in _OSPF_EFF_BASE.items() if k != "ospf_config"}
    ir = _ospf_ir(eff)
    assert ir.ospf_intfs == ()


def test_ospf_ingest_unresolved_name():
    eff = {
        **_OSPF_EFF_BASE,
        "networks": {"corp": {"vlan_id": 10}},
        "ospf_areas": {"0": {"networks": {"ghost": {}}}},
    }
    ir = _ospf_ir(eff)
    o = next(o for o in ir.ospf_intfs if o.network_name == "ghost")
    assert o.vlan_id is None and o.unresolved is True


def test_dhcp_predicates_skip_non_dict_enabled_flag():
    # Mist stores dhcpd_config.enabled as a BOOLEAN alongside per-network scopes;
    # the predicates must treat it as not-a-scope (regression: gatewaytemplate
    # dhcpd_config materialized onto a gateway device crashed the ingest live)
    from digital_twin.adapters.mist.ingest.switch import _dhcp_active, _dhcp_serves_scope
    assert _dhcp_serves_scope(True) is False
    assert _dhcp_active(True) is False
    assert _dhcp_serves_scope({"type": "local"}) is True


# -- caused_by parity tests: adapter findings -----------------------------------


def test_bridge_priority_caused_by_empty_when_unchanged():
    # A malformed bridge_priority that was ALREADY malformed in baseline
    # (unchanged both sides) must not attribute a cause — the plan didn't
    # introduce the problem.
    from digital_twin.adapters.mist.ingest.switch import invalid_bridge_priority_findings

    bad = {"stp_config": {"bridge_priority": "banana"}}
    findings = invalid_bridge_priority_findings({"d1": bad}, {"d1": bad})
    assert len(findings) == 1
    assert findings[0].caused_by == ()


def test_bridge_priority_caused_by_names_device_when_changed():
    # A malformed value that CHANGED baseline→proposed (good→bad) attributes the
    # device as the cause.
    from digital_twin.adapters.mist.ingest.switch import invalid_bridge_priority_findings
    from digital_twin.contracts import Cause, ObjectRef

    good = {"stp_config": {"bridge_priority": "4096"}}
    bad = {"stp_config": {"bridge_priority": "banana"}}
    findings = invalid_bridge_priority_findings({"d1": good}, {"d1": bad})
    assert len(findings) == 1
    assert findings[0].caused_by == (Cause(ref=ObjectRef("device", "d1")),)


def test_dhcp_range_caused_by_names_scope_when_changed():
    # A dhcp range value INTRODUCED (changed from absent/different) attributes
    # the dhcp_scope as the cause.
    from digital_twin.adapters.mist.ingest.switch import unresolved_dhcp_range_findings
    from digital_twin.contracts import Cause, ObjectRef

    tpl = {"dhcpd_config": {"corp": {"type": "local", "ip_start": "{{a}}"}}}
    findings = unresolved_dhcp_range_findings({}, tpl)
    assert len(findings) == 1
    assert findings[0].caused_by == (Cause(ref=ObjectRef("dhcp_scope", "corp")),)


def test_dhcp_range_caused_by_always_set_because_builder_skips_unchanged():
    # The builder already skips unchanged templates (the `str(before) == str(value)`
    # guard). Every finding that fires is a change, so caused_by is always non-empty.
    # Verify a "changed template" case also names the scope.
    from digital_twin.adapters.mist.ingest.switch import unresolved_dhcp_range_findings
    from digital_twin.contracts import Cause, ObjectRef

    tpl_a = {"dhcpd_config": {"corp": {"type": "local", "ip_start": "{{a}}"}}}
    tpl_b = {"dhcpd_config": {"corp": {"type": "local", "ip_start": "{{b}}"}}}
    findings = unresolved_dhcp_range_findings(tpl_a, tpl_b)
    assert len(findings) == 1
    assert findings[0].caused_by == (Cause(ref=ObjectRef("dhcp_scope", "corp")),)


def test_device_name_populated_from_raw():
    # device display name flows from the raw device into the IR
    from digital_twin.adapters.mist.ingest.base import IngestContext
    from digital_twin.ir import IRBuilder

    ctx = IngestContext(
        raw=raw_site(devices=({**SWITCH_A, "name": "core-sw-1"},)),
        site_effective=dict(SITE_EFFECTIVE),
        device_effective={"aa0000000001": {**SITE_EFFECTIVE, **SWITCH_A}},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    assert ctx.builder.build().device("aa0000000001").name == "core-sw-1"


def test_l1_config_sets_port_fields_and_normalizes_auto():
    eff = {
        "networks": {"corp": {"vlan_id": 10}},
        "port_usages": {
            "forced": {"mode": "access", "port_network": "corp", "speed": "1g",
                       "duplex": "full", "disable_autoneg": True},
            "autoport": {"mode": "access", "port_network": "corp", "speed": "auto",
                         "duplex": "auto"},
        },
        "port_config": {"ge-0/0/1": {"usage": "forced"}, "ge-0/0/2": {"usage": "autoport"}},
    }
    from digital_twin.adapters.mist.ingest.base import IngestContext
    from digital_twin.ir import IRBuilder
    ctx = IngestContext(
        raw=raw_site(devices=({**SWITCH_A, "port_config": eff["port_config"]},)),
        site_effective=eff, device_effective={"aa0000000001": eff}, builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    ir = ctx.builder.build()
    forced = ir.ports["aa0000000001:ge-0/0/1"]
    assert (forced.speed, forced.duplex, forced.autoneg_disabled) == ("1g", "full", True)
    auto = ir.ports["aa0000000001:ge-0/0/2"]
    # "auto" is NEVER stored — normalized to None
    assert (auto.speed, auto.duplex, auto.autoneg_disabled) == (None, None, False)


def test_observed_l1_canonicalized_and_up_gated():
    eff = {
        "networks": {"corp": {"vlan_id": 10}},
        "port_usages": {"u": {"mode": "access", "port_network": "corp"}},
        "port_config": {"ge-0/0/1": {"usage": "u"}, "ge-0/0/2": {"usage": "u"},
                        "ge-0/0/3": {"usage": "u"}},
    }
    stats = [
        {"mac": "aa0000000001", "port_id": "ge-0/0/1", "up": True, "speed": 1000,
         "full_duplex": True},
        {"mac": "aa0000000001", "port_id": "ge-0/0/2", "up": True, "speed": 100,
         "full_duplex": False},
        {"mac": "aa0000000001", "port_id": "ge-0/0/3", "up": False, "speed": 0,
         "full_duplex": False},
    ]
    from digital_twin.adapters.mist.ingest.base import IngestContext
    from digital_twin.ir import IRBuilder
    ctx = IngestContext(
        raw=raw_site(devices=({**SWITCH_A, "port_config": eff["port_config"]},),
                     port_stats=tuple(stats)),
        site_effective=eff, device_effective={"aa0000000001": eff}, builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    ir = ctx.builder.build()
    p1 = ir.ports["aa0000000001:ge-0/0/1"]
    assert (p1.observed_speed, p1.observed_duplex) == ("1g", "full")
    p2 = ir.ports["aa0000000001:ge-0/0/2"]
    assert (p2.observed_speed, p2.observed_duplex) == ("100m", "half")
    p3 = ir.ports["aa0000000001:ge-0/0/3"]  # down port: never spurious half
    assert (p3.observed_speed, p3.observed_duplex) == (None, None)


def test_port_auth_normalization_and_none_when_default():
    from digital_twin.adapters.mist.ingest.switch import _port_auth, _reauth
    from digital_twin.ir.entities import PortAuth
    # all-default surface -> None
    assert _port_auth({"mode": "access", "port_network": "corp"}) is None
    # persist_mac-only -> non-None (false-SAFE guard)
    assert _port_auth({"persist_mac": True}) == PortAuth(persist_mac=True)
    # reauth: 36000 (int) and "36000" (numeric str) canonicalize equal; "" -> None;
    # object -> stable token (never silently None)
    assert _reauth(36000) == _reauth("36000") == "36000"
    assert _reauth("") is None and _reauth(None) is None
    assert _reauth({"x": 1}) is not None  # stable token, NOT collapsed to None


def test_reauth_65000_int_equals_str():
    from digital_twin.adapters.mist.ingest.switch import _reauth
    assert _reauth(65000) == _reauth("65000") == "65000"


def test_voip_sets_voice_vlan_and_access_membership():
    eff = {
        "networks": {"corp": {"vlan_id": 10}, "voice": {"vlan_id": 30}},
        "port_usages": {
            "phone": {"mode": "access", "port_network": "corp", "voip_network": "voice"},
            "up": {"mode": "trunk", "all_networks": True, "voip_network": "voice"},
        },
        "port_config": {"ge-0/0/1": {"usage": "phone"}, "ge-0/0/2": {"usage": "up"}},
    }
    from digital_twin.adapters.mist.ingest.base import IngestContext
    from digital_twin.ir import IRBuilder
    from digital_twin.ir.indexes import access_ports_by_vlan
    ctx = IngestContext(
        raw=raw_site(devices=({**SWITCH_A, "port_config": eff["port_config"]},)),
        site_effective=eff, device_effective={"aa0000000001": eff}, builder=IRBuilder())
    SwitchIngester().ingest(ctx)
    ir = ctx.builder.build()
    acc = ir.ports["aa0000000001:ge-0/0/1"]
    assert acc.voice_vlan == 30 and 30 in acc.tagged_vlans   # access: voice folded + member
    members30 = {p.id for p in access_ports_by_vlan(ir).get(30, [])}
    assert "aa0000000001:ge-0/0/1" in members30              # access port is a MEMBER of voice vlan
    trunk = ir.ports["aa0000000001:ge-0/0/2"]
    assert trunk.voice_vlan == 30                            # trunk resolves voice...
    assert "aa0000000001:ge-0/0/2" not in members30          # ...but is NOT an endpoint member
