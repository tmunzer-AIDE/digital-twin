from digital_twin.ir import IRCapability, OspfNeighbor
from digital_twin.ir.diff import diff_ir
from digital_twin.ir.model import IRBuilder


def _ir(neighbors, unparsed=0):
    return (IRBuilder().with_capability(IRCapability.WIRED_L2)
            .set_ospf_neighbors(neighbors, unparsed).build())


def test_ospf_neighbor_id_and_absent_area():
    n = OspfNeighbor(device_id="d1", peer_ip="10.0.0.5", state="Full")
    assert n.area is None and n.id == "d1:ospfnbr:*:10.0.0.5"
    n2 = OspfNeighbor(device_id="d1", peer_ip="10.0.0.6", area="0")
    assert n2.id == "d1:ospfnbr:0:10.0.0.6"


def test_ospf_neighbor_is_not_diff_bearing():
    base = _ir([OspfNeighbor(device_id="d1", peer_ip="10.0.0.5", state="Full")])
    prop = _ir([])  # neighbor vanished
    assert diff_ir(base, prop).is_empty()       # telemetry change != config change


def test_unparsed_count_and_capability_carried():
    ir = _ir([OspfNeighbor(device_id="d1", peer_ip="10.0.0.5")], unparsed=3)
    assert ir.ospf_telemetry_unparsed_count == 3
