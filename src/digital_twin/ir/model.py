"""IR: the immutable, validated, vendor-neutral container, plus an IRBuilder.

build() rejects duplicate ids (every entity type), dangling references,
non-canonical ids, bundle/kind mismatches, and ambiguous VC folds — so bad
ingester output cannot silently become misleading graph structure. Mappings
are read-only proxies; never mutate after build().
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .capabilities import Capability
from .entities import (
    AttachKind,
    Client,
    ClientEnrichment,
    ClientKind,
    Device,
    DeviceRole,
    DhcpScope,
    L3Intf,
    Link,
    LinkKind,
    NacRule,
    NacTag,
    OspfIntf,
    OspfNeighbor,
    Port,
    Vlan,
    Wlan,
    client_id,
    link_id,
    port_id,
)

IR_VERSION = "1.0"


class IRValidationError(ValueError):
    """Raised when an IR would be internally inconsistent (dup ids / dangling refs)."""


_EMPTY_MAP: Mapping[str, object] = MappingProxyType({})


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
    wlans: tuple[Wlan, ...] = ()
    dhcp_scopes: tuple[DhcpScope, ...] = ()
    # config-derived AP VLAN requirements (WlanIngester): ap device id -> the
    # VLANs its enabled WLANs need delivered on its uplink; and ap device id ->
    # reasons a WLAN's requirement could not be resolved (coverage gaps).
    ap_wlan_vlans: Mapping[str, frozenset[int]] = _EMPTY_MAP  # type: ignore[assignment]
    ap_wlan_unresolved: Mapping[str, tuple[str, ...]] = _EMPTY_MAP  # type: ignore[assignment]
    ospf_intfs: tuple[OspfIntf, ...] = ()
    # OBSERVATIONAL per-client identity for the client.impact report (mac ->
    # ClientEnrichment). Evidence only: NOT walked by diff_ir, earns no
    # capability, never read by verdict logic. Defaulted: absence = no enrichment.
    client_enrichment: Mapping[str, ClientEnrichment] = _EMPTY_MAP  # type: ignore[assignment]
    nacrules: tuple[NacRule, ...] = ()
    nactags: tuple[NacTag, ...] = ()
    # OBSERVATIONAL live OSPF adjacencies (site_ospf stats). Evidence/escalation
    # input only: NOT in diff_ir, no strict IR validation. Defaulted: absence = no telemetry.
    ospf_neighbors: tuple[OspfNeighbor, ...] = ()
    ospf_telemetry_unparsed_count: int = 0

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
        self._ospf_intfs: list[OspfIntf] = []
        self._ospf_intf_ids: set[str] = set()
        self._clients: list[Client] = []
        self._client_ids: set[str] = set()
        self._dhcp_scopes: dict[str, DhcpScope] = {}
        self._capabilities: set[Capability] = set()
        self._ap_wlan_vlans: dict[str, set[int]] = {}
        self._ap_wlan_unresolved: dict[str, list[str]] = {}
        self._client_enrichment: dict[str, ClientEnrichment] = {}
        self._wlans: list[Wlan] = []
        self._nacrules: list[NacRule] = []
        self._nacrule_ids: set[str] = set()
        self._nactags: list[NacTag] = []
        self._nactag_ids: set[str] = set()
        self._ospf_neighbors: list[OspfNeighbor] = []
        self._ospf_unparsed = 0

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

    def add_ospf_intf(self, intf: OspfIntf) -> IRBuilder:
        if intf.id in self._ospf_intf_ids:
            raise IRValidationError(f"duplicate ospf intf id {intf.id}")
        self._ospf_intf_ids.add(intf.id)
        self._ospf_intfs.append(intf)
        return self

    def add_client(self, client: Client) -> IRBuilder:
        if client.id in self._client_ids:
            raise IRValidationError(f"duplicate client id {client.id}")
        self._client_ids.add(client.id)
        self._clients.append(client)
        return self

    def add_wlan(self, wlan: Wlan) -> IRBuilder:
        # no duplicate-id guard (unlike add_client etc.): WLANs are observational
        # lint inputs — a bad/duplicate WLAN must never fail the build.
        self._wlans.append(wlan)
        return self

    def add_nacrule(self, rule: NacRule) -> IRBuilder:
        if rule.id in self._nacrule_ids:
            raise IRValidationError(f"duplicate nacrule id {rule.id}")
        self._nacrule_ids.add(rule.id)
        self._nacrules.append(rule)
        return self

    def add_nactag(self, tag: NacTag) -> IRBuilder:
        if tag.id in self._nactag_ids:
            raise IRValidationError(f"duplicate nactag id {tag.id}")
        self._nactag_ids.add(tag.id)
        self._nactags.append(tag)
        return self

    def add_dhcp_scope(self, scope: DhcpScope) -> IRBuilder:
        if scope.id in self._dhcp_scopes:
            raise IRValidationError(f"duplicate dhcp scope id {scope.id}")
        self._dhcp_scopes[scope.id] = scope
        return self

    def with_capability(self, cap: Capability) -> IRBuilder:
        self._capabilities.add(cap)
        return self

    def require_ap_vlans(self, ap_id: str, vlans: frozenset[int]) -> IRBuilder:
        """Record that an AP's enabled WLANs need these VLANs on its uplink."""
        self._ap_wlan_vlans.setdefault(ap_id, set()).update(vlans)
        return self

    def mark_ap_wlan_unresolved(self, ap_id: str, reasons: tuple[str, ...]) -> IRBuilder:
        self._ap_wlan_unresolved.setdefault(ap_id, []).extend(reasons)
        return self

    def set_client_enrichment(self, enrichment: Mapping[str, ClientEnrichment]) -> IRBuilder:
        """Publish the COMPLETE observational enrichment map atomically. NOT validated
        in build() — a bad entry must never fail the IR (non-load-bearing). Replacing
        (not merging) keeps 'broken enrichment == no enrichment': a partial map is never
        observed."""
        self._client_enrichment = dict(enrichment)
        return self

    def set_ospf_neighbors(
        self, neighbors: Iterable[OspfNeighbor], unparsed_count: int = 0
    ) -> IRBuilder:
        """Publish OBSERVATIONAL live OSPF adjacencies atomically. NOT validated in
        build() — a bad neighbor must never fail the IR (non-load-bearing)."""
        self._ospf_neighbors = list(neighbors)
        self._ospf_unparsed = unparsed_count
        return self

    # -- lookups / mutation used by ingesters (pre-build) ----------------------
    def has_device(self, did: str) -> bool:
        return did in self._devices

    def has_vlan(self, vid: int) -> bool:
        return vid in self._vlans

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
        errors += self._validate_ospf_intfs()
        errors += self._validate_clients()
        errors += self._validate_vc()
        errors += self._validate_wlan_reqs()
        errors += self._validate_dhcp_scopes()
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

    def _validate_ospf_intfs(self) -> list[str]:
        # the ospf_withdrawal check trusts these fields for collapse/clients/
        # affected-segment computation — mirror the role-aware dhcp_scope rule
        errors: list[str] = []
        for o in self._ospf_intfs:
            dev = self._devices.get(o.device_id)
            if dev is None:
                errors.append(f"ospf intf {o.id} references unknown device {o.device_id}")
            elif dev.role is not DeviceRole.SWITCH:
                errors.append(f"ospf intf {o.id} device {o.device_id} is not a switch")
            if o.unresolved and o.vlan_id is not None:
                errors.append(f"ospf intf {o.id} is unresolved but carries vlan_id {o.vlan_id}")
            if not o.unresolved and o.vlan_id is None:
                errors.append(f"ospf intf {o.id} is resolved but has no vlan_id")
            if not o.network_name:
                errors.append(f"ospf intf {o.id} has empty network_name")
            if o.vlan_id is not None and o.vlan_id not in self._vlans:
                errors.append(f"ospf intf {o.id} references unknown vlan {o.vlan_id}")
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

    def _validate_wlan_reqs(self) -> list[str]:
        # config-derived requirements must reference real AP devices, else an
        # ingester bug would silently misattribute VLAN needs.
        errors: list[str] = []
        for ap_id in set(self._ap_wlan_vlans) | set(self._ap_wlan_unresolved):
            dev = self._devices.get(ap_id)
            if dev is None:
                errors.append(f"wlan requirement references unknown device {ap_id}")
            elif dev.role is not DeviceRole.AP:
                errors.append(f"wlan requirement on {ap_id} which is not an AP")
        return errors

    def _validate_dhcp_scopes(self) -> list[str]:
        # a gateway-provided scope must reference a real GATEWAY device, else
        # an ingester bug would silently misattribute the scope
        errors: list[str] = []
        for s in self._dhcp_scopes.values():
            if s.provider == "site":
                continue
            dev = self._devices.get(s.provider)
            if dev is None:
                errors.append(f"dhcp scope {s.id} references unknown provider {s.provider}")
            elif dev.role is not DeviceRole.GATEWAY:
                errors.append(f"dhcp scope {s.id} provider {s.provider} is not a gateway")
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
            ospf_intfs=tuple(self._ospf_intfs),
            clients=tuple(self._clients),
            wlans=tuple(self._wlans),
            dhcp_scopes=tuple(sorted(self._dhcp_scopes.values(), key=lambda s: s.id)),
            ap_wlan_vlans=MappingProxyType(
                {ap: frozenset(v) for ap, v in self._ap_wlan_vlans.items()}
            ),
            ap_wlan_unresolved=MappingProxyType(
                {ap: tuple(r) for ap, r in self._ap_wlan_unresolved.items()}
            ),
            client_enrichment=MappingProxyType(dict(self._client_enrichment)),
            nacrules=tuple(self._nacrules),
            nactags=tuple(self._nactags),
            ospf_neighbors=tuple(self._ospf_neighbors),
            ospf_telemetry_unparsed_count=self._ospf_unparsed,
        )
