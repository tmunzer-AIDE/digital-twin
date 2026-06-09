from digital_twin.adapters.mist.ingest.base import IngestContext
from digital_twin.adapters.mist.ingest.lldp import LldpIngester
from digital_twin.adapters.mist.ingest.switch import SwitchIngester
from digital_twin.ir import ConfidenceLevel, IRBuilder, LinkKind, Port, PortMode, port_id
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


def test_two_sided_lldp_creates_one_high_confidence_link():
    stats = [
        {
            "mac": "aa0000000001",
            "port_id": "ge-0/0/47",
            "up": True,
            "neighbor_mac": "bb0000000002",
            "neighbor_port_id": "ge-0/0/47",
        },
        {
            "mac": "bb0000000002",
            "port_id": "ge-0/0/47",
            "up": True,
            "neighbor_mac": "aa0000000001",
            "neighbor_port_id": "ge-0/0/47",
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
            "neighbor_port_id": "ge-0/0/47",
        }
    ]
    ir = _ctx(stats).builder.build()
    assert len(ir.links) == 1
    assert ir.links[0].meta.confidence.level is ConfidenceLevel.LOW


def test_lag_members_get_bundle_id():
    stats = [
        {
            "mac": "aa0000000001",
            "port_id": "ge-0/0/47",
            "up": True,
            "aggregated": True,
            "lag_name": "ae0",
            "neighbor_mac": "bb0000000002",
            "neighbor_port_id": "ge-0/0/47",
        },
        {
            "mac": "bb0000000002",
            "port_id": "ge-0/0/47",
            "up": True,
            "aggregated": True,
            "lag_name": "ae0",
            "neighbor_mac": "aa0000000001",
            "neighbor_port_id": "ge-0/0/47",
        },
    ]
    ir = _ctx(stats).builder.build()
    assert ir.links[0].kind is LinkKind.LAG and ir.links[0].bundle_id == "ae0"


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
            "neighbor_port_id": "p1",
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
            "neighbor_port_id": "ge-0/0/47",
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
            "neighbor_port_id": "eth0",
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
