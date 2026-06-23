import pytest

from digital_twin.ir import BgpPeer, Device, DeviceRole, IRCapability
from digital_twin.ir.diff import diff_ir
from digital_twin.ir.model import IRBuilder, IRValidationError


def _peer(nip="10.0.0.2", session_name="s1", **kw):
    return BgpPeer(
        device_id="d1", role=DeviceRole.SWITCH, session_name=session_name, neighbor_ip=nip, **kw
    )


def _ir(peers):
    b = (IRBuilder().with_capability(IRCapability.WIRED_L2)
         .add_device(Device(id="d1", role=DeviceRole.SWITCH, site="x")))
    for p in peers:
        b.add_bgp_peer(p)
    return b.build()


def test_id_is_device_and_neighbor_ip():
    assert _peer().id == "d1:bgp:10.0.0.2"


def test_session_name_is_diff_ignored():
    base = _ir([_peer(session_name="underlay")])
    prop = _ir([_peer(session_name="renamed")])  # same (device, ip), only session_name differs
    assert diff_ir(base, prop).is_empty()


def test_neighbor_as_change_is_diff_bearing():
    base = _ir([_peer(neighbor_as=65001)])
    prop = _ir([_peer(neighbor_as=65002)])
    assert not diff_ir(base, prop).is_empty()
    assert diff_ir(base, prop).touches("bgp_peer")


def test_duplicate_id_raises():
    with pytest.raises(IRValidationError):
        _ir([_peer(), _peer()])  # same (device, ip) added twice -> caller must dedup
