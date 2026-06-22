"""Pure OSPF reachability join (GS27 telemetry layer). IR-only, no I/O. Predicts
each active OSPF interface's connected subnet from Vlan.subnet, then asks whether a
live established peer is covered; broken_peers = covered-in-baseline, uncovered-in-
proposed. Escalate-only: a wrong prediction can only over- or under-flag, never SAFE."""

from __future__ import annotations

import ipaddress

from digital_twin.ir import OspfNeighbor
from digital_twin.ir.model import IR

_Net = ipaddress.IPv4Network | ipaddress.IPv6Network
# Full = adjacency complete; 2-Way (non-DR/BDR) intentionally excluded — only a
# fully-established peer is a confirmed adjacency. Mist-OAS state assumption, pending
# live grounding (no OSPF in the reachable org); unknown states -> not established.
_ESTABLISHED = {"full"}


def is_established(state: str) -> bool:
    return state.strip().lower() in _ESTABLISHED


def _net(subnet: str | None) -> _Net | None:
    if not subnet:
        return None
    try:
        return ipaddress.ip_network(subnet, strict=False)
    except ValueError:
        return None


def _addr(ip: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(ip)
    except ValueError:
        return None


def _active_intf_subnets(ir: IR) -> list[tuple[str, int, str, _Net]]:
    """(device_id, vlan_id, area, subnet) for each ACTIVE (non-passive) resolved OSPF intf."""
    out: list[tuple[str, int, str, _Net]] = []
    for o in ir.ospf_intfs:
        if o.passive or o.vlan_id is None:
            continue
        vlan = ir.vlans.get(o.vlan_id)
        net = _net(vlan.subnet) if vlan is not None else None
        if net is not None:
            out.append((o.device_id, o.vlan_id, o.area, net))
    return out


def covering_dev_vlan(neighbor: OspfNeighbor, ir: IR) -> tuple[str, int] | None:
    addr = _addr(neighbor.peer_ip)
    if addr is None:
        return None
    for did, vid, area, net in _active_intf_subnets(ir):
        if did != neighbor.device_id:
            continue
        if neighbor.area is not None and neighbor.area != area:
            continue                      # area given -> must match; absent -> subnet-only
        if addr in net:
            return (did, vid)
    return None


def covered(neighbor: OspfNeighbor, ir: IR) -> bool:
    return covering_dev_vlan(neighbor, ir) is not None


def _proposed_unevaluable(neighbor: OspfNeighbor, base_ir: IR, prop_ir: IR) -> bool:
    """The peer's baseline-covering (device, vlan) is STILL active OSPF in proposed, in an
    AREA that would still cover this peer, but its subnet is now unresolved/None -> proposed
    coverage CANNOT be evaluated. This is 'unknown/blind', NOT a confirmed break.

    The area gate matters: if the interface ALSO moved area away from the peer (e.g. peer in
    area 0, proposed interface in area 1), that is a confirmed area-driven break (owned by
    .area_changed), not an unevaluable note — so an area-mismatched candidate does NOT count."""
    cv = covering_dev_vlan(neighbor, base_ir)
    if cv is None:
        return False
    did, vid = cv
    for o in prop_ir.ospf_intfs:
        if o.passive or o.device_id != did or o.vlan_id != vid:
            continue
        if neighbor.area is not None and neighbor.area != o.area:
            continue                      # area mismatch -> not a valid cover candidate
        vlan = prop_ir.vlans.get(vid)
        if vlan is None or _net(vlan.subnet) is None:
            return True                   # area-valid active OSPF here, but subnet unevaluable
    return False


def broken_peers(base_ir: IR, prop_ir: IR) -> list[OspfNeighbor]:
    """CONFIRMED breaks only: established, covered-in-baseline, uncovered-in-proposed,
    and proposed coverage was EVALUABLE (covering interface structurally gone, or still
    active with a RESOLVED subnet that excludes the peer). The unevaluable case is blind."""
    return [
        n for n in base_ir.ospf_neighbors
        if is_established(n.state) and covered(n, base_ir) and not covered(n, prop_ir)
        and not _proposed_unevaluable(n, base_ir, prop_ir)
    ]


def unevaluable_peers(base_ir: IR, prop_ir: IR) -> list[OspfNeighbor]:
    """Baseline-covered established peers whose PROPOSED coverage is unevaluable (covering
    interface still active OSPF but subnet unresolved) -> a REVIEW coverage note, not a break."""
    return [
        n for n in base_ir.ospf_neighbors
        if is_established(n.state) and covered(n, base_ir) and not covered(n, prop_ir)
        and _proposed_unevaluable(n, base_ir, prop_ir)
    ]


def blind_peers(ir: IR) -> list[OspfNeighbor]:
    """Established peers the model could not place in THIS ir (no covering active subnet)."""
    return [n for n in ir.ospf_neighbors if is_established(n.state) and not covered(n, ir)]
