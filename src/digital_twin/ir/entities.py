"""Vendor-neutral IR entities (frozen) with per-fact provenance/confidence.

Ids derive from stable keys — never a vendor object_id — so baseline/proposed IRs
line up for diffing and future cross-vendor reconciliation. Every entity exposes a
stable `.id` and carries a FactMeta; a field from a different source than the entity's
config gets its own `*_meta` (M1's one case: Port.stp_meta, a live fact).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .provenance import CONFIG_META, OBSERVED_META, FactMeta


class DeviceRole(StrEnum):
    SWITCH = "switch"
    GATEWAY = "gateway"
    AP = "ap"
    MISTEDGE = "mistedge"


class PortMode(StrEnum):
    ACCESS = "access"
    TRUNK = "trunk"


class LinkKind(StrEnum):
    PHYSICAL = "physical"
    LAG = "lag"
    MCLAG = "mclag"
    VC = "vc"


class StpMode(StrEnum):
    RSTP = "rstp"
    MSTP = "mstp"
    VSTP = "vstp"
    NONE = "none"


class L3Role(StrEnum):
    IRB = "irb"
    SVI = "svi"
    WAN = "wan"
    LOOPBACK = "loopback"


class ClientKind(StrEnum):
    WIRED = "wired"
    WIRELESS = "wireless"


class AttachKind(StrEnum):
    PORT = "port"
    AP = "ap"


def _norm_mac(mac: str) -> str:
    return mac.lower().replace(":", "").replace("-", "")


def device_id(mac: str) -> str:
    return _norm_mac(mac)


def port_id(dev_id: str, name: str) -> str:
    return f"{dev_id}:{name}"


def link_id(port_a_id: str, port_b_id: str) -> str:
    a, b = sorted((port_a_id, port_b_id))
    return f"{a}__{b}"


def client_id(mac: str) -> str:
    return _norm_mac(mac)


@dataclass(frozen=True)
class Device:
    id: str
    role: DeviceRole
    site: str
    model: str | None = None
    vc_members: tuple[str, ...] = ()
    meta: FactMeta = CONFIG_META


@dataclass(frozen=True)
class Port:
    id: str
    device_id: str
    name: str
    mode: PortMode
    native_vlan: int | None = None
    tagged_vlans: tuple[int, ...] = ()
    speed: int | None = None
    poe: bool | None = None  # CONFIG intent: PoE enabled (True) / `poe_disabled` (False) / unknown
    # OBSERVED: port currently delivering power (stats `poe_on`); None = the
    # powered state is UNKNOWABLE (no stat row, or an UP port without the stat)
    poe_draw: bool | None = None
    profile: str | None = None
    disabled: bool = False  # admin-down (usage `disabled` attr): forwards NOTHING
    stp_enabled: bool | None = None
    stp_mode: StpMode = StpMode.NONE
    stp_state: str | None = None
    # STP is a LIVE fact with its own provenance, distinct from the port's config `meta`.
    # None = STP state unknown (drives the loop check to INSUFFICIENT_DATA / LOW confidence).
    stp_meta: FactMeta | None = None
    meta: FactMeta = CONFIG_META


@dataclass(frozen=True)
class Link:
    id: str
    a_port: str
    b_port: str
    kind: LinkKind
    bundle_id: str | None = None  # LAG/MCLAG bundle identity; None for standalone links
    meta: FactMeta = CONFIG_META


@dataclass(frozen=True)
class Vlan:
    vlan_id: int
    name: str | None = None
    scope: str = "site"
    meta: FactMeta = CONFIG_META

    @property
    def id(self) -> str:
        return str(self.vlan_id)


@dataclass(frozen=True)
class L3Intf:
    device_id: str
    role: L3Role
    vlan_id: int | None = None
    port: str | None = None
    subnet: str | None = None
    ip: str | None = None
    meta: FactMeta = CONFIG_META
    id: str = ""  # auto-derived in __post_init__ if empty

    def __post_init__(self) -> None:
        if not self.id:
            key = str(self.vlan_id) if self.vlan_id is not None else (self.port or "?")
            object.__setattr__(self, "id", f"{self.device_id}:l3:{self.role.value}:{key}")


@dataclass(frozen=True)
class Client:
    mac: str
    kind: ClientKind
    attach_kind: AttachKind
    attach_id: str  # a port id (wired) or an ap device id (wireless)
    vlan: int | None = None
    ip: str | None = None
    active: bool = True
    meta: FactMeta = OBSERVED_META

    @property
    def id(self) -> str:
        return client_id(self.mac)
