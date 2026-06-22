from digital_twin.analysis.ospf_reachability import (
    blind_peers,
    broken_peers,
    covered,
    is_established,
    unevaluable_peers,
)
from digital_twin.ir import IRCapability, OspfIntf, OspfNeighbor
from digital_twin.ir.entities import Vlan
from digital_twin.ir.model import IRBuilder
from tests.factories import sw


def _switch_ir(*, intfs, vlans, neighbors):
    b = IRBuilder().add_device(sw("d1"))      # build() validates ospf_intfs -> switch must exist
    b.with_capability(IRCapability.WIRED_L2).with_capability(IRCapability.OSPF_TELEMETRY)
    for v in vlans:
        b.add_vlan(v)
    for i in intfs:
        b.add_ospf_intf(i)
    b.set_ospf_neighbors(neighbors, 0)
    return b.build()


def test_is_established_normalizes():
    assert is_established("Full") and is_established(" full ")
    assert not is_established("Init") and not is_established("") and not is_established("2-Way")


def test_covered_in_subnet_area_match():
    ir = _switch_ir(
        vlans=[Vlan(vlan_id=10, name="c", subnet="10.0.0.0/24")],
        intfs=[OspfIntf(device_id="d1", vlan_id=10, area="0", network_name="c")],
        neighbors=[OspfNeighbor(device_id="d1", peer_ip="10.0.0.5", area="0", state="Full")])
    assert covered(ir.ospf_neighbors[0], ir) is True


def test_peer_not_in_subnet_is_blind_not_broken():
    ir = _switch_ir(
        vlans=[Vlan(vlan_id=10, name="c", subnet="10.0.0.0/24")],
        intfs=[OspfIntf(device_id="d1", vlan_id=10, area="0", network_name="c")],
        neighbors=[OspfNeighbor(device_id="d1", peer_ip="192.168.9.9", area="0", state="Full")])
    assert covered(ir.ospf_neighbors[0], ir) is False
    assert ir.ospf_neighbors[0] in blind_peers(ir)


def test_broken_when_interface_goes_passive():
    nb = [OspfNeighbor(device_id="d1", peer_ip="10.0.0.5", area="0", state="Full")]
    base = _switch_ir(
        vlans=[Vlan(vlan_id=10, name="c", subnet="10.0.0.0/24")],
        intfs=[OspfIntf(device_id="d1", vlan_id=10, area="0", network_name="c", passive=False)],
        neighbors=nb)
    prop = _switch_ir(
        vlans=[Vlan(vlan_id=10, name="c", subnet="10.0.0.0/24")],
        intfs=[OspfIntf(device_id="d1", vlan_id=10, area="0", network_name="c", passive=True)],
        neighbors=nb)
    assert [n.peer_ip for n in broken_peers(base, prop)] == ["10.0.0.5"]


def test_subnet_exclude_breaks_peer():
    nb = [OspfNeighbor(device_id="d1", peer_ip="10.0.0.5", area="0", state="Full")]
    base = _switch_ir(vlans=[Vlan(vlan_id=10, name="c", subnet="10.0.0.0/24")],
                      intfs=[OspfIntf(device_id="d1", vlan_id=10, area="0", network_name="c")],
                      neighbors=nb)
    prop = _switch_ir(vlans=[Vlan(vlan_id=10, name="c", subnet="10.0.1.0/24")],   # excludes .0.5
                      intfs=[OspfIntf(device_id="d1", vlan_id=10, area="0", network_name="c")],
                      neighbors=nb)
    assert [n.peer_ip for n in broken_peers(base, prop)] == ["10.0.0.5"]


def test_proposed_unresolved_is_unevaluable_not_broken():
    nb = [OspfNeighbor(device_id="d1", peer_ip="10.0.0.5", area="0", state="Full")]
    base = _switch_ir(vlans=[Vlan(vlan_id=10, name="c", subnet="10.0.0.0/24")],
                      intfs=[OspfIntf(device_id="d1", vlan_id=10, area="0", network_name="c")],
                      neighbors=nb)
    prop = _switch_ir(vlans=[Vlan(vlan_id=10, name="c", subnet=None, subnet_unresolved=True)],
                      intfs=[OspfIntf(device_id="d1", vlan_id=10, area="0", network_name="c")],
                      neighbors=nb)
    assert broken_peers(base, prop) == []
    assert [n.peer_ip for n in unevaluable_peers(base, prop)] == ["10.0.0.5"]


def test_area_move_with_subnet_unresolved_is_broken_not_unevaluable():
    # peer area 0; proposed interface moved to area 1 AND subnet went unresolved -> the area
    # mismatch is a CONFIRMED break (no area-0 cover candidate), not blind.
    nb = [OspfNeighbor(device_id="d1", peer_ip="10.0.0.5", area="0", state="Full")]
    base = _switch_ir(vlans=[Vlan(vlan_id=10, name="c", subnet="10.0.0.0/24")],
                      intfs=[OspfIntf(device_id="d1", vlan_id=10, area="0", network_name="c")],
                      neighbors=nb)
    prop = _switch_ir(vlans=[Vlan(vlan_id=10, name="c", subnet=None, subnet_unresolved=True)],
                      intfs=[OspfIntf(device_id="d1", vlan_id=10, area="1", network_name="c")],
                      neighbors=nb)
    assert [n.peer_ip for n in broken_peers(base, prop)] == ["10.0.0.5"]
    assert unevaluable_peers(base, prop) == []


def test_non_established_never_broken():
    nb = [OspfNeighbor(device_id="d1", peer_ip="10.0.0.5", area="0", state="Init")]
    base = _switch_ir(
        vlans=[Vlan(vlan_id=10, name="c", subnet="10.0.0.0/24")],
        intfs=[OspfIntf(device_id="d1", vlan_id=10, area="0", network_name="c", passive=False)],
        neighbors=nb)
    prop = _switch_ir(
        vlans=[Vlan(vlan_id=10, name="c", subnet="10.0.0.0/24")],
        intfs=[OspfIntf(device_id="d1", vlan_id=10, area="0", network_name="c", passive=True)],
        neighbors=nb)
    assert broken_peers(base, prop) == []
