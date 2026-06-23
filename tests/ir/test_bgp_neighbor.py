from digital_twin.ir import BgpNeighbor, IRCapability
from digital_twin.ir.diff import diff_ir
from digital_twin.ir.model import IRBuilder


def _ir(neighbors, unparsed=0):
    return (IRBuilder().with_capability(IRCapability.WIRED_L2)
            .set_bgp_neighbors(neighbors, unparsed).build())


def test_bgp_neighbor_id():
    n = BgpNeighbor(device_id="d1", peer_ip="10.0.0.5", state="Established")
    assert n.id == "d1:bgpnbr:10.0.0.5"


def test_bgp_neighbor_is_not_diff_bearing():
    base = _ir([BgpNeighbor(device_id="d1", peer_ip="10.0.0.5", state="Established")])
    prop = _ir([])  # telemetry vanished -> NOT a config change
    assert diff_ir(base, prop).is_empty()


def test_unparsed_carried_and_no_capability_from_setter():
    ir = _ir([BgpNeighbor(device_id="d1", peer_ip="10.0.0.5")], unparsed=2)
    assert ir.bgp_telemetry_unparsed_count == 2
    # the setter earns NO capability — BGP_TELEMETRY is the fetch layer's job (Task 5)
    assert IRCapability.BGP_TELEMETRY not in ir.capabilities


def test_up_flag_represented():
    n = BgpNeighbor(device_id="d1", peer_ip="10.0.0.5", up=True)
    assert n.up is True and n.state == ""
