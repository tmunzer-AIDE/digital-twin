"""Shared test builders for constructing IR fixtures concisely (DRY across test files)."""

from __future__ import annotations

from digital_twin.ir.entities import (
    AttachKind,
    Client,
    ClientKind,
    Device,
    DeviceRole,
    L3Intf,
    L3Role,
    Link,
    LinkKind,
    OspfIntf,
    Port,
    PortMode,
    link_id,
)
from digital_twin.ir.provenance import Provenance, fact_meta


def sw(
    did: str = "S", *, vc_members: tuple[str, ...] = (), stp_priority: int | None = None
) -> Device:
    return Device(
        id=did, role=DeviceRole.SWITCH, site="s1", vc_members=vc_members, stp_priority=stp_priority
    )


def ap(did: str) -> Device:
    return Device(id=did, role=DeviceRole.AP, site="s1")


def trunk_port(
    did: str,
    name: str,
    tagged: tuple[int, ...] = (),
    native: int | None = None,
    mtu: int | None = None,
) -> Port:
    return Port(
        id=f"{did}:{name}",
        device_id=did,
        name=name,
        mode=PortMode.TRUNK,
        native_vlan=native,
        tagged_vlans=tagged,
        mtu=mtu,
    )


def access_port(did: str, name: str, vlan: int) -> Port:
    return Port(
        id=f"{did}:{name}", device_id=did, name=name, mode=PortMode.ACCESS, native_vlan=vlan
    )


def link(
    pa: str,
    pb: str,
    kind: LinkKind = LinkKind.PHYSICAL,
    bundle: str | None = None,
    prov: Provenance = Provenance.LLDP_TWO_SIDED,
) -> Link:
    return Link(
        id=link_id(pa, pb), a_port=pa, b_port=pb, kind=kind, bundle_id=bundle, meta=fact_meta(prov)
    )


def irb(did: str, vlan: int, subnet: str | None = None) -> L3Intf:
    return L3Intf(device_id=did, role=L3Role.IRB, vlan_id=vlan, subnet=subnet)


def ospf(did: str, vlan: int | None, area: str = "0", *, passive: bool = False,
         name: str | None = None, unresolved: bool = False,
         metric: int | None = None, metric_unresolved: str | None = None) -> OspfIntf:
    return OspfIntf(
        device_id=did, vlan_id=vlan, area=area,
        network_name=name if name is not None else (f"net{vlan}" if vlan is not None else "ghost"),
        passive=passive, unresolved=unresolved, metric=metric,
        metric_unresolved=metric_unresolved,
    )


def wired_client(mac: str, port_id: str, vlan: int | None = None) -> Client:
    return Client(
        mac=mac, kind=ClientKind.WIRED, attach_kind=AttachKind.PORT, attach_id=port_id, vlan=vlan
    )


def wireless_client(mac: str, ap_id: str, vlan: int | None = None) -> Client:
    return Client(
        mac=mac, kind=ClientKind.WIRELESS, attach_kind=AttachKind.AP, attach_id=ap_id, vlan=vlan
    )
