"""Pure index lookups over the IR — reused by representations and analysis.

No graph algorithms; just reorganizations of IR facts. `vc_root_map`/`node_for` live
here because they are pure device->device mappings (members fold into their VC root).
"""

from __future__ import annotations

from collections import defaultdict

from .entities import AttachKind, Client, L3Intf, L3Role, Port, PortMode
from .model import IR


def vc_root_map(ir: IR) -> dict[str, str]:
    """member device id -> containing VC device id (non-members are absent)."""
    root: dict[str, str] = {}
    for dev in ir.devices.values():
        for member in dev.vc_members:
            root[member] = dev.id
    return root


def node_for(vc_root: dict[str, str], dev_id: str) -> str:
    """The graph node a device folds into (its VC root, or itself)."""
    return vc_root.get(dev_id, dev_id)


def ports_by_device(ir: IR) -> dict[str, list[Port]]:
    out: dict[str, list[Port]] = defaultdict(list)
    for p in ir.ports.values():
        out[p.device_id].append(p)
    return dict(out)


def access_ports_by_vlan(ir: IR) -> dict[int, list[Port]]:
    """Access ports keyed by their native VLAN (their membership VLAN)."""
    out: dict[int, list[Port]] = defaultdict(list)
    for p in ir.ports.values():
        if p.mode is PortMode.ACCESS and p.native_vlan is not None:
            out[p.native_vlan].append(p)
    return dict(out)


def exits_by_vlan(ir: IR) -> dict[int, list[L3Intf]]:
    """IRB/SVI L3 interfaces keyed by VLAN (the VLAN's L3 exit candidates)."""
    out: dict[int, list[L3Intf]] = defaultdict(list)
    for intf in ir.l3intfs:
        if intf.role in (L3Role.IRB, L3Role.SVI) and intf.vlan_id is not None:
            out[intf.vlan_id].append(intf)
    return dict(out)


def clients_by_port(ir: IR) -> dict[str, list[Client]]:
    """Wired clients keyed by their attach port id."""
    out: dict[str, list[Client]] = defaultdict(list)
    for c in ir.clients:
        if c.attach_kind is AttachKind.PORT:
            out[c.attach_id].append(c)
    return dict(out)


def clients_by_ap(ir: IR) -> dict[str, list[Client]]:
    """Wireless clients keyed by their AP device id (for Wi-Fi-aware client impact)."""
    out: dict[str, list[Client]] = defaultdict(list)
    for c in ir.clients:
        if c.attach_kind is AttachKind.AP:
            out[c.attach_id].append(c)
    return dict(out)


def wlan_aps_by_vlan(ir: IR) -> dict[int, list[str]]:
    """Config WLAN-required AP graph-nodes keyed by VLAN (an AP whose enabled
    WLANs need the VLAN delivered on its uplink). Folds members into their VC
    root, like the other membership indexes."""
    vc_root = vc_root_map(ir)
    out: dict[int, list[str]] = defaultdict(list)
    for ap_id, vlans in ir.ap_wlan_vlans.items():
        node = node_for(vc_root, ap_id)
        for vid in vlans:
            out[vid].append(node)
    return dict(out)


def clients_by_vlan(ir: IR) -> dict[int, list[Client]]:
    out: dict[int, list[Client]] = defaultdict(list)
    for c in ir.clients:
        if c.vlan is not None:
            out[c.vlan].append(c)
    return dict(out)
