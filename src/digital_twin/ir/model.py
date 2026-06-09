"""IR: the immutable, validated, vendor-neutral container, plus an IRBuilder.

build() rejects duplicate ids (every entity type), dangling references,
non-canonical ids, bundle/kind mismatches, and ambiguous VC folds — so bad
ingester output cannot silently become misleading graph structure. Mappings
are read-only proxies; never mutate after build().
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .capabilities import Capability
from .entities import (
    AttachKind,
    Client,
    ClientKind,
    Device,
    DeviceRole,
    L3Intf,
    Link,
    LinkKind,
    Port,
    Vlan,
    client_id,
    link_id,
    port_id,
)

IR_VERSION = "1.0"


class IRValidationError(ValueError):
    """Raised when an IR would be internally inconsistent (dup ids / dangling refs)."""


@dataclass(frozen=True)
class IR:
    ir_version: str
    capabilities: frozenset[Capability]
    devices: Mapping[str, Device]
    ports: Mapping[str, Port]
    links: tuple[Link, ...]
    vlans: Mapping[int, Vlan]
    l3intfs: tuple[L3Intf, ...]
    clients: tuple[Client, ...]

    def device(self, did: str) -> Device:
        return self.devices[did]

    def port(self, pid: str) -> Port:
        return self.ports[pid]

    def has(self, cap: Capability) -> bool:
        return cap in self.capabilities


class IRBuilder:
    def __init__(self) -> None:
        self._devices: dict[str, Device] = {}
        self._ports: dict[str, Port] = {}
        self._links: list[Link] = []
        self._link_ids: set[str] = set()
        self._vlans: dict[int, Vlan] = {}
        self._l3intfs: list[L3Intf] = []
        self._l3intf_ids: set[str] = set()
        self._clients: list[Client] = []
        self._client_ids: set[str] = set()
        self._capabilities: set[Capability] = set()

    def add_device(self, device: Device) -> IRBuilder:
        if device.id in self._devices:
            raise IRValidationError(f"duplicate device id {device.id}")
        self._devices[device.id] = device
        return self

    def add_port(self, port: Port) -> IRBuilder:
        if port.id in self._ports:
            raise IRValidationError(f"duplicate port id {port.id}")
        self._ports[port.id] = port
        return self

    def add_link(self, link: Link) -> IRBuilder:
        if link.id in self._link_ids:
            raise IRValidationError(f"duplicate link id {link.id}")
        self._link_ids.add(link.id)
        self._links.append(link)
        return self

    def add_vlan(self, vlan: Vlan) -> IRBuilder:
        if vlan.vlan_id in self._vlans:
            raise IRValidationError(f"duplicate vlan id {vlan.vlan_id}")
        self._vlans[vlan.vlan_id] = vlan
        return self

    def add_l3intf(self, intf: L3Intf) -> IRBuilder:
        if intf.id in self._l3intf_ids:
            raise IRValidationError(f"duplicate l3intf id {intf.id}")
        self._l3intf_ids.add(intf.id)
        self._l3intfs.append(intf)
        return self

    def add_client(self, client: Client) -> IRBuilder:
        if client.id in self._client_ids:
            raise IRValidationError(f"duplicate client id {client.id}")
        self._client_ids.add(client.id)
        self._clients.append(client)
        return self

    def with_capability(self, cap: Capability) -> IRBuilder:
        self._capabilities.add(cap)
        return self

    # -- lookups / mutation used by ingesters (pre-build) ----------------------
    def has_device(self, did: str) -> bool:
        return did in self._devices

    def has_port(self, pid: str) -> bool:
        return pid in self._ports

    def has_client(self, mac: str) -> bool:
        return client_id(mac) in self._client_ids

    def get_port(self, pid: str) -> Port:
        return self._ports[pid]

    def replace_port(self, port: Port) -> IRBuilder:
        """Replace an already-added port (same id) — used by ingesters to enrich
        config-built ports with observed live facts (e.g. STP state)."""
        if port.id not in self._ports:
            raise IRValidationError(f"cannot replace unknown port {port.id}")
        self._ports[port.id] = port
        return self

    def _validate(self) -> None:
        errors: list[str] = []
        errors += self._validate_ports()
        errors += self._validate_links()
        errors += self._validate_l3intfs()
        errors += self._validate_clients()
        errors += self._validate_vc()
        if errors:
            raise IRValidationError("invalid IR:\n  " + "\n  ".join(errors))

    def _validate_ports(self) -> list[str]:
        errors: list[str] = []
        for p in self._ports.values():
            if p.device_id not in self._devices:
                errors.append(f"port {p.id} references unknown device {p.device_id}")
            expected = port_id(p.device_id, p.name)
            if p.id != expected:
                errors.append(f"port id {p.id} is not canonical (expected {expected})")
        return errors

    def _validate_links(self) -> list[str]:
        # Canonical ids sort the endpoints, so the duplicate-id check in add_link
        # also rejects reversed-duplicate endpoint pairs.
        errors: list[str] = []
        for link in self._links:
            for endpoint in (link.a_port, link.b_port):
                if endpoint not in self._ports:
                    errors.append(f"link {link.id} references unknown port {endpoint}")
            expected = link_id(link.a_port, link.b_port)
            if link.id != expected:
                errors.append(f"link id {link.id} is not canonical (expected {expected})")
            is_bundle_kind = link.kind in (LinkKind.LAG, LinkKind.MCLAG)
            if is_bundle_kind and link.bundle_id is None:
                errors.append(f"link {link.id} kind {link.kind.value} requires a bundle_id")
            if not is_bundle_kind and link.bundle_id is not None:
                errors.append(f"link {link.id} kind {link.kind.value} must not have a bundle_id")
        return errors

    def _validate_l3intfs(self) -> list[str]:
        errors: list[str] = []
        for intf in self._l3intfs:
            if intf.device_id not in self._devices:
                errors.append(f"l3intf {intf.id} references unknown device {intf.device_id}")
        return errors

    def _validate_clients(self) -> list[str]:
        errors: list[str] = []
        for c in self._clients:
            if c.kind is ClientKind.WIRELESS and c.attach_kind is not AttachKind.AP:
                errors.append(f"wireless client {c.mac} must attach to an AP")
            if c.kind is ClientKind.WIRED and c.attach_kind is not AttachKind.PORT:
                errors.append(f"wired client {c.mac} must attach to a port")
            if c.attach_kind is AttachKind.PORT and c.attach_id not in self._ports:
                errors.append(f"client {c.mac} references unknown port {c.attach_id}")
            if c.attach_kind is AttachKind.AP:
                ap = self._devices.get(c.attach_id)
                if ap is None:
                    errors.append(f"client {c.mac} references unknown ap {c.attach_id}")
                elif ap.role is not DeviceRole.AP:
                    errors.append(f"client {c.mac} attaches to {c.attach_id} which is not an AP")
        return errors

    def _validate_vc(self) -> list[str]:
        # An ambiguous fold (self/duplicate/nested membership) would silently drop
        # or misplace devices in the L2 graph — reject it here instead.
        errors: list[str] = []
        member_owner: dict[str, str] = {}
        for d in self._devices.values():
            for member in d.vc_members:
                if member == d.id:
                    errors.append(f"device {d.id} lists itself as a vc member")
                    continue
                if member not in self._devices:
                    errors.append(f"device {d.id} lists unknown vc member {member}")
                if member in member_owner:
                    errors.append(
                        f"device {member} is a vc member of both {member_owner[member]} and {d.id}"
                    )
                else:
                    member_owner[member] = d.id
        for d in self._devices.values():
            if d.vc_members and d.id in member_owner:
                errors.append(
                    f"device {d.id} is both a vc root and a member of "
                    f"{member_owner[d.id]} (nested VC)"
                )
        return errors

    def build(self) -> IR:
        self._validate()
        return IR(
            ir_version=IR_VERSION,
            capabilities=frozenset(self._capabilities),
            devices=MappingProxyType(dict(self._devices)),
            ports=MappingProxyType(dict(self._ports)),
            links=tuple(self._links),
            vlans=MappingProxyType(dict(self._vlans)),
            l3intfs=tuple(self._l3intfs),
            clients=tuple(self._clients),
        )
