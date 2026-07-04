from digital_twin.adapters.mist.ingest.base import IngestContext
from digital_twin.adapters.mist.ingest.lldp import LldpIngester
from digital_twin.adapters.mist.ingest.switch import SwitchIngester
from digital_twin.ir import (
    ConfidenceLevel,
    IRBuilder,
    IRCapability,
    LinkKind,
    Port,
    PortMode,
    port_id,
)
from tests.adapters.mist.fixtures import AP_1, SITE_EFFECTIVE, SWITCH_A, raw_site

SWITCH_B = {**SWITCH_A, "mac": "bb0000000002", "id": "dev-b", "name": "sw-b"}


def _ctx(port_stats, device_stats=()) -> IngestContext:
    ctx = IngestContext(
        raw=raw_site(
            devices=(SWITCH_A, SWITCH_B, AP_1),
            port_stats=tuple(port_stats),
            device_stats=tuple(device_stats),
        ),
        site_effective=dict(SITE_EFFECTIVE),
        device_effective={},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    # ensure stat-referenced ports exist even without port_config entries
    for did, name in (
        ("aa0000000001", "ge-0/0/47"),
        ("bb0000000002", "ge-0/0/47"),
        ("aa0000000001", "ge-0/0/10"),
    ):
        pid = port_id(did, name)
        if not ctx.builder.has_port(pid):
            ctx.builder.add_port(Port(id=pid, device_id=did, name=name, mode=PortMode.TRUNK))
    LldpIngester().ingest(ctx)
    return ctx


def _ctx_for_caps(port_stats, device_stats=()):
    ctx = IngestContext(
        raw=raw_site(devices=(SWITCH_A, SWITCH_B, AP_1),
                     port_stats=tuple(port_stats), device_stats=tuple(device_stats)),
        site_effective=dict(SITE_EFFECTIVE), device_effective={}, builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    for did, name in (("aa0000000001", "ge-0/0/47"), ("bb0000000002", "ge-0/0/47"),
                      ("aa0000000001", "ge-0/0/10")):
        pid = port_id(did, name)
        if not ctx.builder.has_port(pid):
            ctx.builder.add_port(Port(id=pid, device_id=did, name=name, mode=PortMode.TRUNK))
    return ctx


def _port(ir, did, name):
    return ir.ports[port_id(did, name)]


def test_neighbor_named_by_system_name_only_still_links():
    # real orgs exist whose switch port stats carry NO neighbor_mac — only
    # neighbor_system_name (found in real use, 2026-06-10). Without a name
    # fallback the whole site is EDGELESS and every strand looks pre-existing.
    stats = [
        {
            "mac": "aa0000000001",
            "port_id": "ge-0/0/47",
            "up": True,
            "neighbor_system_name": "sw-b",
            "neighbor_port_desc": "ge-0/0/47",
        },
        {
            "mac": "bb0000000002",
            "port_id": "ge-0/0/47",
            "up": True,
            "neighbor_system_name": "sw-a",
            "neighbor_port_desc": "ge-0/0/47",
        },
    ]
    ir = _ctx(stats).builder.build()
    assert len(ir.links) == 1
    assert ir.links[0].meta.confidence.level is ConfidenceLevel.HIGH  # two-sided


def test_unmatched_system_name_without_mac_is_skipped():
    # a non-Mist neighbor with no MAC cannot be identified -> no link, no
    # invented client (we have no stable id for it)
    stats = [
        {
            "mac": "aa0000000001",
            "port_id": "ge-0/0/47",
            "up": True,
            "neighbor_system_name": "some-printer",
            "neighbor_port_desc": "eth0",
        }
    ]
    ir = _ctx(stats).builder.build()
    assert ir.links == () and ir.clients == ()


def test_two_sided_lldp_creates_one_high_confidence_link():
    stats = [
        {
            "mac": "aa0000000001",
            "port_id": "ge-0/0/47",
            "up": True,
            "neighbor_mac": "bb0000000002",
            "neighbor_port_desc": "ge-0/0/47",
        },
        {
            "mac": "bb0000000002",
            "port_id": "ge-0/0/47",
            "up": True,
            "neighbor_mac": "aa0000000001",
            "neighbor_port_desc": "ge-0/0/47",
        },
    ]
    ir = _ctx(stats).builder.build()
    assert len(ir.links) == 1
    assert ir.links[0].meta.confidence.level is ConfidenceLevel.HIGH


def test_one_sided_lldp_creates_low_confidence_link():
    stats = [
        {
            "mac": "aa0000000001",
            "port_id": "ge-0/0/47",
            "up": True,
            "neighbor_mac": "bb0000000002",
            "neighbor_port_desc": "ge-0/0/47",
        }
    ]
    ir = _ctx(stats).builder.build()
    assert len(ir.links) == 1
    assert ir.links[0].meta.confidence.level is ConfidenceLevel.LOW


def test_lag_members_get_bundle_id():
    # a LAG member's port row carries port_parent = the bundle name (real Mist field)
    stats = [
        {
            "mac": "aa0000000001",
            "port_id": "ge-0/0/47",
            "up": True,
            "port_parent": "ae0",
            "neighbor_mac": "bb0000000002",
            "neighbor_port_desc": "ge-0/0/47",
        },
        {
            "mac": "bb0000000002",
            "port_id": "ge-0/0/47",
            "up": True,
            "port_parent": "ae0",
            "neighbor_mac": "aa0000000001",
            "neighbor_port_desc": "ge-0/0/47",
        },
    ]
    ir = _ctx(stats).builder.build()
    assert ir.links[0].kind is LinkKind.LAG and ir.links[0].bundle_id == "ae0"


def test_ap_uplink_matches_switch_by_chassis_id():
    # chassis_id (switch base MAC) is the robust match key; system_name is the fallback
    device_stats = [
        {
            "mac": "cc0000000001",
            "type": "ap",
            "lldp_stat": {"chassis_id": "aa:00:00:00:00:01", "port_id": "ge-0/0/10"},
        }
    ]
    ir = _ctx([], device_stats).builder.build()
    ap_links = [link for link in ir.links if "cc0000000001" in link.id]
    assert len(ap_links) == 1
    assert "aa0000000001:ge-0/0/10" in ap_links[0].id


def test_stp_state_attached_to_port_when_present():
    stats = [
        {
            "mac": "aa0000000001",
            "port_id": "ge-0/0/10",
            "up": True,
            "stp_state": "forwarding",
            "stp_role": "designated",
        }
    ]
    ir = _ctx(stats).builder.build()
    p = ir.port("aa0000000001:ge-0/0/10")
    assert p.stp_state == "forwarding"
    assert p.stp_meta is not None  # observed live fact


def test_empty_string_stp_state_is_treated_as_absent():
    # Real Mist payloads (found live 2026-07-04, EX4000) carry stp_state=""
    # on every NON-participating port (internal ifs, down ports) — present-
    # but-empty, not absent. Applying it would mark the port stp_enabled=True
    # on unconfirmed data, making l2_loop rank a cycle as STP-protected
    # (false-SAFE-adjacent) and earning STP_STATE off garbage rows.
    stats = [
        {"mac": "aa0000000001", "port_id": "ge-0/0/10", "up": True,
         "stp_state": "", "stp_role": ""},
    ]
    ctx = _ctx(stats)
    assert IRCapability.STP_STATE not in LldpIngester().ingest(ctx)
    p = ctx.builder.build().port("aa0000000001:ge-0/0/10")
    assert p.stp_state is None
    assert p.stp_enabled is None  # unknown, NOT True


def test_ap_uplink_link_from_ap_lldp_stat():
    device_stats = [
        {
            "mac": "cc0000000001",
            "type": "ap",
            "lldp_stat": {"system_name": "sw-a", "port_id": "ge-0/0/10", "mgmt_addr": "10.0.10.1"},
        }
    ]
    ir = _ctx([], device_stats).builder.build()
    ap_links = [link for link in ir.links if "cc0000000001" in link.id]
    assert len(ap_links) == 1


def test_unmanaged_lldp_neighbor_becomes_wired_edge_client_not_link():
    # a printer/unmanaged router reported by LLDP: no Link (device unknown),
    # but a wired Client on the local port — it stays in the impact surface
    stats = [
        {
            "mac": "aa0000000001",
            "port_id": "ge-0/0/10",
            "up": True,
            "neighbor_mac": "99eeddccbbaa",
            "neighbor_port_desc": "p1",
        }
    ]
    ir = _ctx(stats).builder.build()  # build() must NOT crash
    assert ir.links == ()
    edge = [c for c in ir.clients if c.mac == "99eeddccbbaa"]
    assert len(edge) == 1
    assert edge[0].attach_id == "aa0000000001:ge-0/0/10"
    assert "unmanaged LLDP neighbor" in edge[0].meta.confidence.reasons[0]


def test_ap_corroboration_requires_the_switch_to_name_that_ap():
    # the switch port reports SOME neighbor, but not the AP -> AP link stays LOW
    stats = [
        {
            "mac": "aa0000000001",
            "port_id": "ge-0/0/10",
            "up": True,
            "neighbor_mac": "bb0000000002",
            "neighbor_port_desc": "ge-0/0/47",
        }
    ]
    device_stats = [
        {
            "mac": "cc0000000001",
            "type": "ap",
            "lldp_stat": {"system_name": "sw-a", "port_id": "ge-0/0/10"},
        }
    ]
    ir = _ctx(stats, device_stats).builder.build()
    ap_link = next(link for link in ir.links if "cc0000000001" in link.id)
    assert ap_link.meta.confidence.level is ConfidenceLevel.LOW


def test_switch_reporting_ap_yields_one_link_not_duplicates():
    # both the switch port-stat claim AND the AP lldp_stat describe the same
    # physical link -> exactly ONE Link entity (no duplicate-id crash)
    stats = [
        {
            "mac": "aa0000000001",
            "port_id": "ge-0/0/10",
            "up": True,
            "neighbor_mac": "cc0000000001",
            "neighbor_port_desc": "eth0",
        }
    ]
    device_stats = [
        {
            "mac": "cc0000000001",
            "type": "ap",
            "lldp_stat": {"system_name": "sw-a", "port_id": "ge-0/0/10"},
        }
    ]
    ir = _ctx(stats, device_stats).builder.build()
    ap_links = [link for link in ir.links if "cc0000000001" in link.id]
    assert len(ap_links) == 1


def test_stp_capability_earned_only_when_stp_rows_seen():
    from digital_twin.ir import IRCapability

    # _ctx runs LldpIngester once internally; ingest() is re-run here purely to
    # capture the return value (idempotent on these inputs: stats unchanged).
    no_stp = _ctx([{"mac": "aa0000000001", "port_id": "ge-0/0/10", "up": True}])
    assert IRCapability.STP_STATE not in LldpIngester().ingest(no_stp)

    with_stp = _ctx(
        [{"mac": "aa0000000001", "port_id": "ge-0/0/10", "up": True, "stp_state": "forwarding"}]
    )
    assert IRCapability.STP_STATE in LldpIngester().ingest(with_stp)
    assert with_stp.builder.build().port("aa0000000001:ge-0/0/10").stp_state == "forwarding"


def test_uplink_bit_sets_is_uplink_independent_of_stp():
    # a row with uplink=True and NO stp_state still annotates the port
    stats = [{"mac": "aa0000000001", "port_id": "ge-0/0/47", "up": True, "uplink": True}]
    ir = _ctx(stats).builder.build()
    assert _port(ir, "aa0000000001", "ge-0/0/47").is_uplink is True


def test_uplink_false_is_recorded_as_false():
    stats = [{"mac": "aa0000000001", "port_id": "ge-0/0/47", "up": True, "uplink": False}]
    ir = _ctx(stats).builder.build()
    assert _port(ir, "aa0000000001", "ge-0/0/47").is_uplink is False


def test_non_bool_uplink_stays_unknown():
    # strict typing: a drifted/non-bool shape must read as None, never coerced
    for bad in ("false", 0, 1, "", "true"):
        stats = [{"mac": "aa0000000001", "port_id": "ge-0/0/47", "up": True, "uplink": bad}]
        ir = _ctx(stats).builder.build()
        assert _port(ir, "aa0000000001", "ge-0/0/47").is_uplink is None, bad


def test_uplink_only_row_earns_no_stp_capability():
    # an uplink-bearing row with no stp_state must NOT earn STP_STATE
    ctx = _ctx_for_caps([{"mac": "aa0000000001", "port_id": "ge-0/0/47",
                          "up": True, "uplink": True}])
    caps = LldpIngester().ingest(ctx)
    assert IRCapability.STP_STATE not in caps
    assert _port(ctx.builder.build(), "aa0000000001", "ge-0/0/47").is_uplink is True


def test_stp_role_read_beside_state_with_empty_string_absent():
    stats = [
        {"mac": "aa0000000001", "port_id": "ge-0/0/8", "up": True,
         "stp_state": "forwarding", "stp_role": "designated"},
        {"mac": "aa0000000001", "port_id": "ge-0/0/9", "up": True,
         "stp_state": "blocking", "stp_role": "backup"},
        {"mac": "aa0000000001", "port_id": "bme0", "up": True,
         "stp_state": "", "stp_role": ""},  # non-participant: both absent
    ]
    ir = _ctx(stats).builder.build()
    assert ir.port("aa0000000001:ge-0/0/8").stp_role == "designated"
    assert ir.port("aa0000000001:ge-0/0/9").stp_role == "backup"
    assert ir.port("aa0000000001:bme0").stp_role is None
    assert ir.port("aa0000000001:bme0").stp_state is None


def test_role_only_row_applies_and_earns_the_capability():
    # review P2: a row with non-empty stp_role but empty stp_state is still a
    # real STP observation — an implementation keeping the old
    # `if not stp_state: continue` gate would pass every other test here and
    # silently drop role-only rows
    stats = [
        {"mac": "aa0000000001", "port_id": "xe-0/1/3", "up": True,
         "stp_state": "", "stp_role": "root"},
    ]
    ctx = _ctx(stats)
    assert IRCapability.STP_STATE in LldpIngester().ingest(ctx)
    p = ctx.builder.build().port("aa0000000001:xe-0/1/3")
    assert p.stp_role == "root"
    assert p.stp_state is None
    assert p.stp_enabled is True


def test_reciprocal_self_loop_sets_peer_and_reciprocal_on_both_ports():
    # shaped like the live SWB-3 rows: neighbor_mac == the row's OWN mac
    stats = [
        {"mac": "aa0000000001", "port_id": "ge-0/0/8", "up": True,
         "neighbor_mac": "aa0000000001", "neighbor_port_desc": "ge-0/0/9"},
        {"mac": "aa0000000001", "port_id": "ge-0/0/9", "up": True,
         "neighbor_mac": "aa0000000001", "neighbor_port_desc": "ge-0/0/8"},
    ]
    ir = _ctx(stats).builder.build()
    a, b = ir.port("aa0000000001:ge-0/0/8"), ir.port("aa0000000001:ge-0/0/9")
    assert a.self_loop_peer == "aa0000000001:ge-0/0/9" and a.self_loop_reciprocal
    assert b.self_loop_peer == "aa0000000001:ge-0/0/8" and b.self_loop_reciprocal


def test_self_loop_matches_across_mac_formats():
    # same chassis, mixed formats: colon-separated uppercase vs bare lowercase —
    # the self-loop rule must compare canonical device ids, not raw strings
    stats = [
        {"mac": "AA:00:00:00:00:01", "port_id": "ge-0/0/8", "up": True,
         "neighbor_mac": "aa0000000001", "neighbor_port_desc": "ge-0/0/9"},
        {"mac": "aa0000000001", "port_id": "ge-0/0/9", "up": True,
         "neighbor_mac": "AA-00-00-00-00-01", "neighbor_port_desc": "ge-0/0/8"},
    ]
    ir = _ctx(stats).builder.build()
    a, b = ir.port("aa0000000001:ge-0/0/8"), ir.port("aa0000000001:ge-0/0/9")
    assert a.self_loop_peer == "aa0000000001:ge-0/0/9" and a.self_loop_reciprocal
    assert b.self_loop_peer == "aa0000000001:ge-0/0/8" and b.self_loop_reciprocal


def test_one_sided_self_claim_never_synthesizes_the_peer():
    stats = [
        {"mac": "aa0000000001", "port_id": "ge-0/0/8", "up": True,
         "neighbor_mac": "aa0000000001", "neighbor_port_desc": "ge-0/0/9"},
        {"mac": "aa0000000001", "port_id": "ge-0/0/9", "up": True},  # silent
    ]
    ir = _ctx(stats).builder.build()
    a = ir.port("aa0000000001:ge-0/0/8")
    assert a.self_loop_peer == "aa0000000001:ge-0/0/9"
    assert a.self_loop_reciprocal is False
    assert ir.port("aa0000000001:ge-0/0/9").self_loop_peer is None


def test_no_same_device_link_is_minted():
    # P2-3: current _emit_links mints these; this pins the NEW skip — and it
    # must also cover a name-fallback row resolving to the reporting device
    stats = [
        {"mac": "aa0000000001", "port_id": "ge-0/0/8", "up": True,
         "neighbor_mac": "aa0000000001", "neighbor_port_desc": "ge-0/0/9"},
        {"mac": "aa0000000001", "port_id": "ge-0/0/9", "up": True,
         # name-fallback path: MUST be the fixture device's EXACT name —
         # _claims matches case-sensitively; use the constant, don't hardcode
         "neighbor_system_name": SWITCH_A["name"], "neighbor_port_desc": "ge-0/0/8"},
    ]
    ir = _ctx(stats).builder.build()
    assert not [
        link for link in ir.links
        if link.a_port.split(":")[0] == link.b_port.split(":")[0]
    ]
