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
    name: str | None = None  # display name (from raw device `name`); DIFF-IGNORED (see ir/diff.py)
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
class PortAuth:
    """Effective wired-auth config for a switch port (SP3). Frozen + comparable:
    change-detection is plain inequality. Defaults are the OAS defaults, so
    PortAuth() is the canonical all-default surface — Port.auth is None ONLY when
    the whole surface is default/absent (a lone persist_mac/reauth change is a
    non-default PortAuth, never collapsed to None)."""

    port_auth: str | None = None          # "dot1x" | None
    mac_auth: bool = False                 # enable_mac_auth
    mac_auth_only: bool = False
    mac_auth_preferred: bool = False
    mac_auth_protocol: str = "eap-md5"     # OAS default
    allow_multiple_supplicants: bool = False
    dynamic_vlan_networks: tuple[str, ...] = ()
    server_fail_network: str | None = None
    server_reject_network: str | None = None
    guest_network: str | None = None
    bypass_auth_when_server_down: bool = False
    bypass_auth_when_server_down_for_unknown_client: bool = False
    persist_mac: bool = False
    reauth_interval: str | None = None     # canonical (see ingest _reauth)


def requires_auth(a: PortAuth | None) -> bool:
    """The port forces clients to authenticate (dot1x or MAC-auth)."""
    return a is not None and (a.port_auth == "dot1x" or a.mac_auth or a.mac_auth_only)


def admitted_methods(a: PortAuth | None) -> frozenset[str] | None:
    """The auth methods the port admits. None = no auth required (all clients
    admitted). Else a subset of {"dot1x", "mac"}."""
    if not requires_auth(a):
        return None
    assert a is not None
    m: set[str] = set()
    if a.mac_auth or a.mac_auth_only:
        m.add("mac")
    if a.port_auth == "dot1x" and not a.mac_auth_only:
        m.add("dot1x")
    return frozenset(m)


def _fallbacks(a: PortAuth | None) -> frozenset[str]:
    if a is None:
        return frozenset()
    return frozenset(
        n for n in (a.guest_network, a.server_fail_network, a.server_reject_network) if n
    )


def tightens(old: PortAuth | None, new: PortAuth | None) -> bool:
    """Admission became more restrictive in a way that could block currently-
    admitted clients: a previously-admitted auth method is no longer admitted,
    OR a fallback network (guest/server_fail/server_reject) was removed.

    `admitted_methods` returns None when no auth is required (the universe — all
    clients admitted). The method test is a set-DIFFERENCE, not strict-subset, so
    it covers auth newly required (universe -> a concrete set), MAC-auth-only
    (a swap {dot1x} -> {mac}: dot1x clients rejected even though mac is newly
    admitted), and a single method dropped ({dot1x,mac} -> {dot1x})."""
    old_m, new_m = admitted_methods(old), admitted_methods(new)
    if new_m is None:
        narrowed = False              # new admits everyone -> nothing removed
    elif old_m is None:
        narrowed = True               # old admitted everyone -> new restricts
    else:
        narrowed = bool(old_m - new_m)  # a previously-admitted method is gone
    return narrowed or bool(_fallbacks(old) - _fallbacks(new))


@dataclass(frozen=True)
class Port:
    id: str
    device_id: str
    name: str
    mode: PortMode
    native_vlan: int | None = None
    tagged_vlans: tuple[int, ...] = ()
    voice_vlan: int | None = None  # SP4: resolved voip_network (voice VLAN), tagged
    # CONFIG intent (L1, SP2): concrete speed enum / duplex. None = unset/auto —
    # the IR NEVER stores "auto" (ingest normalizes "auto"/absent to None), so
    # forced ⇔ autoneg_disabled and speed is not None and duplex is not None.
    speed: str | None = None
    duplex: str | None = None  # "full" | "half" | None
    autoneg_disabled: bool = False  # from disable_autoneg
    # CONFIG intent: explicit interface MTU; None = no explicit MTU (platform
    # default) OR the usage is blind — consumers disambiguate via `meta`
    mtu: int | None = None
    poe: bool | None = None  # CONFIG intent: PoE enabled (True) / `poe_disabled` (False) / unknown
    # OBSERVED: port currently delivering power (stats `poe_on`); None = the
    # powered state is UNKNOWABLE (no stat row, or an UP port without the stat)
    poe_draw: bool | None = None
    # OBSERVED (L1, SP2): negotiated speed/duplex from port_stats, UP ports only;
    # None = down / no telemetry. Speed canonicalized to the config enum (Task 2).
    observed_speed: str | None = None
    observed_duplex: str | None = None
    profile: str | None = None
    disabled: bool = False  # admin-down (usage `disabled` attr): forwards NOTHING
    auth: PortAuth | None = None  # SP3: effective wired-auth surface; None = all-default
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
    mac_limit: int | str | None = None  # SP4: concrete cap / None=unlimited / str=unresolved token
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
    # GS30: distinct OTHER network names that also claim this vlan_id (the dedup
    # keeps the first; this surfaces the shadowed claimants). () = no collision.
    collisions: tuple[str, ...] = ()
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
class OspfIntf:
    """A switch network's OSPF-area participation (GS26 withdrawal surface).

    Minted only when ospf_config.enabled is truthy. `passive=False` (the Mist
    default) means the interface forms adjacencies = adjacency-bearing
    (transit/uplink); `passive=True` is advertise-only (stub). `unresolved=True`
    is the OSPF analog of vlan-blind carriage — the network name did not resolve
    to a vlan (then vlan_id is None). Identity carries area+network_name for
    stability and for withdrawal-report messaging; the semantic match key is
    (device, vlan[, area]) rather than id, so a rename or area-move is not a
    false withdrawal.
    """

    device_id: str
    vlan_id: int | None = None
    area: str = "0"
    network_name: str = ""
    passive: bool = False
    metric: int | None = None        # OSPF cost; None = absent OR present-but-unparseable
    # raw metric token when present-but-unparseable (templated/garbage), else None. Carried
    # SEPARATELY from `metric` (and diff-bearing) so an absent->templated or templated->other
    # metric edit produces a diff — else it collapses to metric=None==None -> false-SAFE.
    metric_unresolved: str | None = None
    unresolved: bool = False
    meta: FactMeta = CONFIG_META
    id: str = ""  # auto-derived in __post_init__ if empty

    def __post_init__(self) -> None:
        if not self.id:
            object.__setattr__(
                self, "id", f"{self.device_id}:ospf:{self.area}:{self.network_name}"
            )


@dataclass(frozen=True)
class OspfNeighbor:
    """OBSERVATIONAL live OSPF adjacency (site_ospf stats). Evidence/escalation
    input only: NOT in diff_ir, no strict IR validation. `area=None` means the
    telemetry omitted the area -> the reachability join matches on subnet only."""

    device_id: str
    peer_ip: str
    area: str | None = None
    state: str = ""                       # raw Mist state, e.g. "Full"
    vrf: str | None = None
    neighbor_router_id: str | None = None
    meta: FactMeta = OBSERVED_META
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            area_key = self.area or "*"
            object.__setattr__(self, "id", f"{self.device_id}:ospfnbr:{area_key}:{self.peer_ip}")


@dataclass(frozen=True)
class BgpPeer:
    """A switch/gateway BGP peering (one per session-neighbor), modeled for the
    wired.l3.bgp_adjacency check (GS28). Identity is (device, neighbor_ip): a
    device peers with a given neighbor IP once; session_name is config grouping,
    DIFF-IGNORED (see ir/diff.py) so a session rename is not a false change. ASN /
    type / via / disabled that are PRESENT-but-unparseable (templated {{var}} /
    non-enum / non-bool) keep their parsed field None and carry the raw token in
    the matching *_unresolved field (diff-bearing) so absent->templated does not
    collapse to None==None (the GS27 metric false-SAFE scar tissue). auth_key is
    NEVER modeled (secret). `unresolved` = the neighbor-IP map key is not a literal
    IP. `ambiguous` = 2+ sessions defined this (device, neighbor_ip) with differing
    modeled attrs (set by ingest, never last-win)."""

    device_id: str
    role: DeviceRole
    session_name: str
    neighbor_ip: str
    local_as: int | None = None
    neighbor_as: int | None = None
    session_type: str | None = None   # "external" | "internal"; None if absent OR unparseable
    disabled: bool = False            # per-neighbor admin shutdown (schema default False)
    via: str | None = None            # transport lan|tunnel|vpn|wan; None if absent/unparseable
    local_as_unresolved: str | None = None
    neighbor_as_unresolved: str | None = None
    session_type_unresolved: str | None = None
    via_unresolved: str | None = None
    disabled_unresolved: str | None = None
    unresolved: bool = False
    ambiguous: bool = False
    meta: FactMeta = CONFIG_META
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            object.__setattr__(self, "id", f"{self.device_id}:bgp:{self.neighbor_ip}")


@dataclass(frozen=True)
class BgpNeighbor:
    """OBSERVATIONAL live BGP adjacency (org_bgp/site_bgp stats). Evidence/
    escalation input only: NOT in diff_ir, no IR validation. Both `state` and
    `up` are represented so liveness conveyed via the boolean (not the string)
    still escalates."""

    device_id: str
    peer_ip: str
    state: str = ""                   # raw BGP state, e.g. "Established"
    up: bool | None = None
    neighbor_as: int | None = None
    vrf: str | None = None
    meta: FactMeta = OBSERVED_META
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            object.__setattr__(self, "id", f"{self.device_id}:bgpnbr:{self.peer_ip}")


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


@dataclass(frozen=True)
class ClientEnrichment:
    """OBSERVATIONAL per-client identity. Best-effort, non-diff-bearing (never in
    diff_ir). MAY enrich or cap a finding (e.g. wired.auth.access_change naming
    at-risk clients), but never ORIGINATES or floors a verdict, and its absence
    must degrade gracefully."""

    hostname: str | None = None
    family: str | None = None
    mfg: str | None = None
    model: str | None = None
    os: str | None = None
    auth_type: str | None = None
    auth_method: str | None = None
    auth_state: str | None = None
    nacrule: str | None = None
    status: str | None = None
    assigned_vlan: str | None = None
    vlan_source: str | None = None
    username: str | None = None
    # OBSERVED provenance, mirroring every other IR entity. NOT part of the
    # identity projection — the check allowlists identity fields (Task 5), so meta
    # never leaks into evidence["impacts"][i].identity.
    meta: FactMeta = OBSERVED_META


@dataclass(frozen=True)
class Wlan:
    """A site's effective WLAN (from the derived WLAN list), modeled for the
    config-lint checks. Secret-free by construction. `inherited` = org-template
    owned (NOT site-writable); it is observational ownership, not a lint fact."""

    id: str            # provider WLAN id (pragmatic identity: rename => modify)
    ssid: str
    enabled: bool = False
    auth_type: str | None = None     # auth.type ("open"|"psk"|"eap"|…); None = unparsed
    isolation: bool = False          # isolation OR l2_isolation
    apply_to: str | None = None      # "site" | "aps" | "wxtags" | None
    ap_ids: tuple[str, ...] = ()     # sorted+deduped explicit AP scope
    wxtag_ids: tuple[str, ...] = ()  # sorted+deduped
    inherited: bool = False          # True = org-template-owned (fail-closed at ingest)
    meta: FactMeta = CONFIG_META


@dataclass(frozen=True)
class NacRule:
    id: str
    name: str | None = None
    order: int | None = None        # None = unparseable/absent → never ordered/proven
    enabled: bool = True            # absent ⇒ True (OAS default); non-bool ⇒ opaque_digest
    action: str | None = None       # "allow" | "block" | None
    auth_types: frozenset[str] = frozenset()   # ∅ = genuinely unconstrained (any)
    port_types: frozenset[str] = frozenset()
    match_tags: frozenset[str] = frozenset()   # matching.nactags ids
    site_ids: frozenset[str] = frozenset()
    sitegroup_ids: frozenset[str] = frozenset()
    family: frozenset[str] = frozenset()
    mfg: frozenset[str] = frozenset()
    model: frozenset[str] = frozenset()
    os_type: frozenset[str] = frozenset()
    vendor: frozenset[str] = frozenset()
    # the ENTIRE not_matching block normalized to (dimension, value) pairs — ONE field so
    # the diff sees any negative-criteria change and `not not_matching` is the whole
    # non-emptiness test. Any non-empty not_matching ⇒ non-provable for shadowing.
    not_matching: frozenset[tuple[str, str]] = frozenset()
    apply_tags: frozenset[str] = frozenset()
    # None = parsed cleanly. Non-None = stable digest of the raw row, set when a proof
    # field is unparseable or the row only partially parsed. Two roles: (1) provability
    # gate (opaque_digest is None); (2) diff-bearing so a change in unparseable content
    # still surfaces and cannot collapse-and-vanish.
    opaque_digest: str | None = None
    meta: FactMeta = CONFIG_META


@dataclass(frozen=True)
class NacTag:
    id: str
    name: str | None = None
    type: str | None = None
    match: str | None = None        # the match field, for type == "match"
    values: frozenset[str] = frozenset()
    match_all: bool = False
    meta: FactMeta = CONFIG_META
