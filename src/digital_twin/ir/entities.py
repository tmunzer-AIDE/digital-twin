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
    GATEWAY = "gateway"  # gateway/SRX-side L3 interface (device ip_configs)


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
    # CONFIG intent: STP bridge priority (root election); None = the platform
    # default (32768) — consumers must treat the default as ASSUMED, not known.
    # stp_priority_invalid marks a PRESENT but uninterpretable value: distinct
    # from absent — an election over it cannot be predicted at all (the
    # adapter finding scope.stp.bridge_priority_invalid carries the REVIEW)
    stp_priority: int | None = None
    stp_priority_invalid: bool = False
    # GATEWAY whose network namespace (org networks) was NOT fetched: its
    # carriage and L3 interfaces are UNKNOWN, not absent — checks making
    # negative-existence L3 claims must degrade coverage over it
    l3_unmodeled: bool = False
    # GATEWAY with an ACTIVE dhcpd_config entry whose network name did not
    # resolve to a vlan (namespace fetched, name missing/templated): it may
    # serve DHCP on a vlan we cannot identify — UNKNOWN, not absent
    dhcp_unresolved: bool = False
    # SWITCH dhcp_snooping intent (GS25): None = disabled, ("*",) =
    # all_networks, else the enabled network names (site-network namespace)
    dhcp_snooping: tuple[str, ...] | None = None
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
    # CONFIG intent: explicit interface MTU; None = no explicit MTU (platform
    # default) OR the usage is blind — consumers disambiguate via `meta`
    mtu: int | None = None
    poe: bool | None = None  # CONFIG intent: PoE enabled (True) / `poe_disabled` (False) / unknown
    # OBSERVED: port currently delivering power (stats `poe_on`); None = the
    # powered state is UNKNOWABLE (no stat row, or an UP port without the stat)
    poe_draw: bool | None = None
    profile: str | None = None
    disabled: bool = False  # admin-down (usage `disabled` attr): forwards NOTHING
    # CONFIG intent (usage stp_edge / stp_disable): an edge port does not
    # expect BPDUs (self-heals on receipt); bpdu_filter DROPS them — the port
    # stops participating in loop protection entirely
    stp_edge: bool = False
    bpdu_filter: bool = False
    # CONFIG intent, tri-state (GS25): DHCP-offer trust under snooping.
    # True = allow_dhcpd=true OR (allow_dhcpd absent and trunk);
    # False = allow_dhcpd=false (even on a trunk) OR (absent and access);
    # None = effective usage unknown (unresolved usage / unresolved dynamic) —
    # unknown trust must never collapse to untrusted (and a RESOLVED dynamic
    # uses its runtime usage like any other port).
    dhcp_trusted: bool | None = None
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
    # ROUTED intent: the network declares a subnet — someone must provide its
    # L3 interface (the wired.l3.gateway_gap check consumes this)
    subnet: str | None = None
    # True iff subnet INTENT exists but is unreadable (templated) or AMBIGUOUS
    # (a non-winning same-vlan row disagrees — conflict is unresolvable intent,
    # never a silent winner). Absent/empty subnet = no intent = stays False
    # (a blanket flag would PARTIAL-floor every ordinary subnet-less vlan).
    subnet_unresolved: bool = False
    # Declared default-gateway IP (networks.*.gateway), minted from the SAME
    # effective network row that wins this Vlan (org overlay only when no
    # row declares one). None = no declared intent OR unresolved (flag).
    gateway: str | None = None
    # True iff gateway INTENT exists but is unreadable (templated) or
    # AMBIGUOUS (a non-winning same-vlan row disagrees — conflict is
    # unresolvable intent, never a silent winner). Absent intent stays False.
    gateway_unresolved: bool = False
    # modeled DHCP providers for this vlan: "site" (switch-hosted server/relay
    # from the site dhcpd_config) and/or gateway device ids (their own
    # dhcpd_config). Empty = NO modeled path (which is normal — external
    # servers are invisible); the wired.dhcp.path check reasons about REMOVAL.
    dhcp_sources: tuple[str, ...] = ()
    meta: FactMeta = CONFIG_META

    @property
    def id(self) -> str:
        return str(self.vlan_id)


@dataclass(frozen=True)
class DhcpScope:
    """A SERVING dhcpd_config entry's range facts (GS25 lint surface).

    provider: "site" (switch-hosted site dhcpd_config) or a gateway device id
    (its OWN dhcpd_config). Identity is provider:network — exactly how
    dhcpd_config is keyed. vlan/subnet are resolution RESULTS (org namespace
    for gateway scopes, site networks for site scopes) and may be None when
    the namespace is blind; range fields are LITERAL config, None when absent
    or templated ({{var}}). Relay/none entries never become scopes (they are
    dhcp_sources material only — _dhcp_serves_scope, NOT _dhcp_active).
    """

    provider: str
    network: str
    vlan: int | None = None
    ip_start: str | None = None
    ip_end: str | None = None
    gateway: str | None = None
    subnet: str | None = None
    # True iff subnet INTENT exists but is unreadable (templated value) or
    # unknowable (unfetched org namespace). False when the namespace is
    # fetched and simply declares no subnet — no intent is NOT a blind spot
    # (a blanket note would PARTIAL-floor ordinary subnet-less networks).
    subnet_unresolved: bool = False
    # The OWNING network's declared gateway, resolved in the PROVIDER's
    # namespace (org for gateway scopes, site for site scopes — exactly
    # like subnet). Feeds wired.dhcp.scope_lint.gateway_mismatch.
    network_gateway: str | None = None
    # Mirrors subnet_unresolved: declared-but-unreadable, or unknowable
    # (unfetched org namespace / name missing from a fetched one).
    network_gateway_unresolved: bool = False
    meta: FactMeta = CONFIG_META

    @property
    def id(self) -> str:
        return f"{self.provider}:{self.network}"


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
