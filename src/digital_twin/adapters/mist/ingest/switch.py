"""Switch-domain ingester: effective config -> Device/Port/Vlan/L3Intf entities.

Reads device_effective (per-device compiled config) for ports/L3, and the raw
device list for identity (mac/model/role). APs become leaf Device entities here
(their links/clients come from the lldp/clients ingesters).
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from digital_twin.contracts import (
    Cause,
    Finding,
    FindingCategory,
    FindingSource,
    ObjectRef,
    Severity,
)
from digital_twin.ir import (
    BgpPeer,
    Confidence,
    ConfidenceLevel,
    Device,
    DeviceRole,
    DhcpScope,
    IRCapability,
    L3Intf,
    L3Role,
    OspfIntf,
    Port,
    PortMode,
    Vlan,
    device_id,
    port_id,
    same_ip,
    same_subnet,
)
from digital_twin.ir.entities import PortAuth
from digital_twin.ir.provenance import CONFIG_META, FactMeta, Provenance, fact_meta

from .base import IngestContext
from .dynamic_usage import classify_dynamic_port
from .ports import (
    expand_port_members,
    resolve_effective_ports,
    resolve_port_bases,
    usage_definition,
    usage_vlans,
)

_Json = Mapping[str, Any]

_ROLE = {"switch": DeviceRole.SWITCH, "ap": DeviceRole.AP, "gateway": DeviceRole.GATEWAY}


def _vlan_int(value: Any) -> int | None:
    """Org-level objects can carry UNRESOLVED template vars ('{{guest_vlan}}')
    — found live 2026-06-11; unparseable = UNKNOWN, never a crash or a guess."""
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _metric_int(value: Any) -> int | None:
    """OSPF metric -> int or None (templated/unparseable/absent -> None)."""
    if isinstance(value, bool) or value is None:
        return None  # bool is an int subclass; reject before int() coerces True->1

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _literal_subnet(value: Any) -> str | None:
    """A subnet still carrying '{{vars}}' is unresolved intent — not a fact."""
    if not value or "{{" in str(value):
        return None
    return str(value)


# ---------------------------------------------------------------------------
# GS28: BGP peer helpers
# ---------------------------------------------------------------------------

_BGP_TYPES = frozenset({"external", "internal"})
_BGP_VIAS = frozenset({"lan", "tunnel", "vpn", "wan"})


def _bgp_as_int(v: Any) -> tuple[int | None, str | None]:
    """(parsed, unresolved_token). absent -> (None, None); present-unparseable -> (None, raw)."""
    if v is None:
        return None, None
    try:
        return int(v), None
    except (TypeError, ValueError):
        return None, str(v)


def _bgp_enum(v: Any, allowed: frozenset[str]) -> tuple[str | None, str | None]:
    if v is None:
        return None, None
    s = str(v)
    return (s, None) if s in allowed else (None, s)


def _bgp_disabled(v: Any) -> tuple[bool, str | None]:
    if v is None:
        return False, None                # schema default False
    if isinstance(v, bool):
        return v, None
    return False, str(v)                  # templated/non-bool -> unresolved token


def _is_literal_ip(s: str) -> bool:
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


def _bgp_attrs(p: BgpPeer) -> tuple[Any, ...]:
    """Attr-key for ambiguity comparison: spans ALL modeled attrs including
    admin-state and every *_unresolved token (diff-bearing)."""
    return (
        p.local_as, p.local_as_unresolved,
        p.neighbor_as, p.neighbor_as_unresolved,
        p.session_type, p.session_type_unresolved,
        p.via, p.via_unresolved,
        p.disabled, p.disabled_unresolved,
    )


def _org_vlan_map(ctx: IngestContext) -> dict[str, int | None]:
    """name -> vlan_id over the ORG networks (the gateway namespace). The
    dynamic-usage sources contract applies: a name PRESENT with None means
    known-but-unresolvable (templated/unparseable vlan_id) and SHADOWS any
    same-named site network — falling back there would be a cross-namespace
    guess; a name MISSING falls through."""
    out: dict[str, int | None] = {}
    for net in ctx.raw.org_networks:
        if net.get("name"):
            out[str(net["name"])] = _vlan_int(net.get("vlan_id"))
    return out


def _gw_net_vlan(
    name: Any, org_map: Mapping[str, int | None], site_nets: Mapping[str, Any]
) -> int | None:
    if str(name) in org_map:
        return org_map[str(name)]  # None = unresolvable, NOT a site fallback
    return _vlan_int((site_nets.get(str(name)) or {}).get("vlan_id"))


# Serving dhcpd types: 'local' is what live Mist emits, 'server' is the
# OAS-canonical enum value ({none, relay, server}) — both exist in the wild.
_DHCP_SERVING_TYPES = frozenset({"local", "server"})


def _dhcp_active(entry: Any) -> bool:
    """A dhcpd_config entry IS a DHCP path when it serves (type local/server,
    absent defaults to serving) or forwards somewhere (relay WITH servers);
    'none' is an explicit no-path statement. A NON-DICT value (e.g. the
    `dhcpd_config.enabled` BOOLEAN flag Mist stores alongside the per-network
    scope dicts — surfaces on gatewaytemplate dhcpd_config) is NOT a path."""
    if not isinstance(entry, dict):
        return False
    kind = str(entry.get("type") or "local")
    if kind in _DHCP_SERVING_TYPES:
        return True
    return kind == "relay" and bool(entry.get("servers"))


def _dhcp_serves_scope(entry: Any) -> bool:
    """Does this dhcpd entry OWN a scope (GS25)? Distinct from _dhcp_active
    ("is this a PATH"): relay-with-servers is an active path but owns no
    ip_start..ip_end — minting a range-less row would drag PARTIAL noise
    onto every normal relay config. A non-dict value (the `enabled` flag) owns
    no scope."""
    if not isinstance(entry, dict):
        return False
    kind = str(entry.get("type") or "local")
    return kind in _DHCP_SERVING_TYPES


def _literal_ip(value: Any) -> str | None:
    """Range/gateway fields are literal config; a templated {{var}} (or empty)
    is None — unknown, never parsed or guessed (doctrine f)."""
    if not value or "{{" in str(value):
        return None
    return str(value)


def _winning_literal(
    vid: int,
    rows_by_vid: Mapping[int, list[Any]],
    org_raw: Mapping[int, Any],
    *,
    parse: Callable[[Any], str | None],
    same: Callable[[str | None, str | None], bool | None],
) -> tuple[str | None, bool]:
    """(value, unresolved) for a vlan id under the five-leg precedence shared
    by the gateway and subnet fields. `rows_by_vid` values are ALL same-vid
    network rows' raw field values in mint order — [0] is the true Vlan
    winner; None = no intent (null==absent canon, normalized by the caller).

    - nobody declares -> org overlay (unreadable org value -> (None, True))
    - winner silent but a NON-WINNING row declares -> (None, True): the
      singleton Vlan cannot represent a per-device value, never silently
      promote it nor fall to org over it
    - winner declares but unreadable -> (None, True), shadows org
    - winner declares, any declaring sibling disagrees -> (None, True)
    - winner declares, readable, every other declaring row agrees -> value
    """
    rows = rows_by_vid.get(vid, [])
    declaring = [r for r in rows if r is not None]
    if not declaring:
        if vid not in org_raw:
            return None, False  # no intent anywhere — not a blind spot
        lit = parse(org_raw[vid])
        return lit, lit is None
    if rows[0] is None:
        return None, True  # winner silent, a non-winning row declares
    winner = parse(rows[0])
    if winner is None:
        return None, True  # declared-but-unreadable (still shadows org)
    for other in declaring[1:]:
        if same(parse(other), winner) is not True:
            return None, True  # conflict = unresolvable intent
    return winner, False


def _vlan_gateway(
    vid: int, rows_by_vid: Mapping[int, list[Any]], org_raw: Mapping[int, Any]
) -> tuple[str | None, bool]:
    """(gateway, unresolved) for a vlan id — the gateway specialization of
    `_winning_literal` (GS22-GW). rows are same-vid `networks.*.gateway`
    values in mint order; None = no intent (null==absent canon)."""
    return _winning_literal(vid, rows_by_vid, org_raw, parse=_literal_ip, same=same_ip)


def _snooping(eff: Mapping[str, Any]) -> tuple[str, ...] | None:
    """Device.dhcp_snooping: None = disabled; ("*",) = all_networks; else the
    enabled network names (site-network namespace)."""
    cfg = eff.get("dhcp_snooping") or {}
    if not cfg.get("enabled"):
        return None
    if cfg.get("all_networks"):
        return ("*",)
    return tuple(sorted(str(n) for n in (cfg.get("networks") or ())))


def _dhcp_trust(usage: Mapping[str, Any]) -> bool | None:
    """Tri-state DHCP-offer trust (GS25). The OAS marks allow_dhcpd itself
    tri-state: only the UNDEFINED value defers to the mode default. An empty
    usage (unresolved name / unresolved dynamic) is UNKNOWN — never untrusted."""
    if not usage:
        return None
    explicit = usage.get("allow_dhcpd")
    if explicit is not None:
        return bool(explicit)
    return usage.get("mode") == "trunk"


# Junos bridge priorities: 0..61440 in 4096 steps ("Range [0, 4k, 8k.. 60k]")
_VALID_PRIORITIES = frozenset(range(0, 61441, 4096))


def _bridge_priority(stp_config: Any) -> int | None:
    """`stp_config.bridge_priority` — OAS types it as a bare string; accepts
    '4096'/4096/'4k' shapes but ONLY values in the Junos range {0, 4k..60k}.
    None = absent OR uninterpretable — callers must distinguish via the raw
    value (invalid_bridge_priority_findings), never silently simulate."""
    raw = (stp_config or {}).get("bridge_priority")
    if raw is None:
        return None
    text = str(raw).strip().lower()
    try:
        value = int(text[:-1]) * 1024 if text.endswith("k") else int(text)
    except ValueError:
        return None
    return value if value in _VALID_PRIORITIES else None


def invalid_bridge_priority_findings(
    baseline_effective: Mapping[str, _Json], proposed_effective: Mapping[str, _Json]
) -> list[Finding]:
    """An IN-SCOPE `bridge_priority` whose value the model cannot interpret
    (malformed, or outside the Junos 4k-step range) must never be silently
    simulated as the platform default — a malformed BASELINE poisons the root
    prediction just as much as a malformed proposal -> WARNING (-> REVIEW)."""
    findings: list[Finding] = []
    for did in sorted(set(baseline_effective) | set(proposed_effective)):
        sides: dict[str, Any] = {}
        invalid = False
        for side, effs in (("baseline", baseline_effective), ("proposed", proposed_effective)):
            cfg = (effs.get(did) or {}).get("stp_config") or {}
            raw = cfg.get("bridge_priority")
            sides[side] = raw
            if raw is not None and _bridge_priority(cfg) is None:
                invalid = True
        if not invalid:
            continue
        changed = sides["baseline"] != sides["proposed"]
        caused_by = (Cause(ref=ObjectRef("device", did)),) if changed else ()
        findings.append(
            Finding(
                source=FindingSource.ADAPTER,
                category=FindingCategory.OPERATIONAL,
                code="scope.stp.bridge_priority_invalid",
                subject=ObjectRef(
                    "device", did,
                    name=(proposed_effective.get(did) or {}).get("name")
                    or (baseline_effective.get(did) or {}).get("name"),
                ),
                severity=Severity.WARNING,
                confidence=Confidence(level=ConfidenceLevel.HIGH),
                message=(
                    f"device {did}: stp_config.bridge_priority "
                    f"{sides['baseline']!r} -> {sides['proposed']!r} is not a valid "
                    "Junos priority (0..61440 in 4096 steps) — the root election "
                    "cannot be predicted"
                ),
                affected_entities=(did,),
                evidence={"device": did, **sides},
                caused_by=caused_by,
            )
        )
    return findings


_DHCP_RANGE_FIELDS = ("ip_start", "ip_end", "gateway")


def unresolved_dhcp_range_findings(
    baseline_site_eff: _Json, proposed_site_eff: _Json
) -> list[Finding]:
    """A dhcpd range/gateway value the model cannot read ({{var}}) that the
    DELTA introduces or changes -> WARNING. Unlike bridge_priority (a GLOBAL
    election poisoned by either side), a templated range only poisons
    conclusions about that one scope — pre-existing unchanged templates stay
    silent here and degrade scope_lint coverage instead (GS25 spec)."""
    findings: list[Finding] = []
    base_cfg: _Json = baseline_site_eff.get("dhcpd_config") or {}
    prop_cfg: _Json = proposed_site_eff.get("dhcpd_config") or {}
    for name, entry in sorted(prop_cfg.items()):
        if not _dhcp_serves_scope(entry):
            continue
        for field in _DHCP_RANGE_FIELDS:
            value = (entry or {}).get(field)
            if value is None or "{{" not in str(value):
                continue
            before = (base_cfg.get(name) or {}).get(field)
            if str(before) == str(value):
                continue  # pre-existing and unchanged
            caused_by = (Cause(ref=ObjectRef("dhcp_scope", str(name))),)
            findings.append(
                Finding(
                    source=FindingSource.ADAPTER,
                    category=FindingCategory.OPERATIONAL,
                    code="scope.dhcp.range_unresolved",
                    subject=ObjectRef("dhcp_scope", str(name)),
                    severity=Severity.WARNING,
                    confidence=Confidence(level=ConfidenceLevel.HIGH),
                    message=(
                        f"dhcpd scope {name!r}: {field} {value!r} is templated and "
                        "cannot be evaluated — range/subnet lint is blind to it"
                    ),
                    affected_entities=(str(name),),
                    evidence={
                        "network": str(name),
                        "field": field,
                        "value": str(value),
                        "before": None if before is None else str(before),
                    },
                    caused_by=caused_by,
                )
            )
    return findings


def _l1_config(usage: dict[str, Any]) -> tuple[str | None, str | None, bool]:
    """(speed, duplex, autoneg_disabled) from effective usage attrs. "auto" and
    absent normalize to None for speed & duplex — the IR never stores "auto"."""
    def _norm(v: Any) -> str | None:
        # string-gated: keeps mypy-strict happy (no Any return) and drops
        # malformed non-string values; "auto"/"" -> None ("auto" never stored)
        if isinstance(v, str) and v not in ("", "auto"):
            return v
        return None
    return _norm(usage.get("speed")), _norm(usage.get("duplex")), bool(usage.get("disable_autoneg"))


def _reauth(v: Any) -> str | None:
    """Canonical reauth_interval: None for null/empty; the decimal string for an
    int or numeric string (so 65000 == "65000"); a stable token otherwise — never
    silently collapse an unparseable non-empty value to None (it must stay
    change-detecting). bool is not a valid interval."""
    if v is None or v == "" or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        s = v.strip()
        return s if s.isdigit() else (f"raw:{s}" if s else None)
    return f"raw:{v!r}"


def _port_auth(usage: dict[str, Any]) -> PortAuth | None:
    """Effective wired-auth surface from the resolved usage attrs. None iff the
    whole surface is default/absent (== PortAuth())."""
    a = PortAuth(
        port_auth=usage.get("port_auth") or None,
        mac_auth=bool(usage.get("enable_mac_auth")),
        mac_auth_only=bool(usage.get("mac_auth_only")),
        mac_auth_preferred=bool(usage.get("mac_auth_preferred")),
        mac_auth_protocol=usage.get("mac_auth_protocol") or "eap-md5",
        allow_multiple_supplicants=bool(usage.get("allow_multiple_supplicants")),
        dynamic_vlan_networks=tuple(usage.get("dynamic_vlan_networks") or ()),
        server_fail_network=usage.get("server_fail_network") or None,
        server_reject_network=usage.get("server_reject_network") or None,
        guest_network=usage.get("guest_network") or None,
        bypass_auth_when_server_down=bool(usage.get("bypass_auth_when_server_down")),
        bypass_auth_when_server_down_for_unknown_client=bool(
            usage.get("bypass_auth_when_server_down_for_unknown_client")
        ),
        persist_mac=bool(usage.get("persist_mac")),
        reauth_interval=_reauth(usage.get("reauth_interval")),
    )
    return a if a != PortAuth() else None


# Observed negotiated speed (port_stats numeric Mbps) -> config speed enum.
_SPEED_MBPS: dict[int, str] = {
    10: "10m", 100: "100m", 1000: "1g", 2500: "2.5g", 5000: "5g",
    10000: "10g", 25000: "25g", 40000: "40g", 100000: "100g",
}


def _l1_observed(row: _Json | None) -> tuple[str | None, str | None]:
    """(observed_speed, observed_duplex) from a port_stats row, UP ports only.
    Numeric Mbps -> config enum (unknown/0 -> None); duplex from full_duplex.
    A down port (or no row) yields (None, None) — never a spurious 'half'."""
    if row is None or not row.get("up"):
        return None, None
    spd = row.get("speed")
    speed = _SPEED_MBPS.get(spd) if isinstance(spd, int) else None
    fd = row.get("full_duplex")
    duplex = ("full" if fd else "half") if isinstance(fd, bool) else None
    return speed, duplex


def _poe_draw(row: _Json | None) -> bool | None:
    """Observed power delivery — honest about missing telemetry (real rows lack
    `poe_on` on some ports): no stat row -> UNKNOWN; `poe_on` present -> the
    observed value; absent on a DOWN port -> False (a down port powers
    nothing); absent on an UP port -> UNKNOWN, never 'not drawing'."""
    if row is None:
        return None
    if row.get("poe_on") is not None:
        return bool(row["poe_on"])
    return False if not row.get("up") else None


class SwitchIngester:
    name = "switch"

    def produces(self) -> frozenset[str]:  # potential supply
        return frozenset({IRCapability.WIRED_L2, IRCapability.L3_EXITS})

    def ingest(self, ctx: IngestContext) -> frozenset[str]:
        if "devices" not in ctx.raw.meta.fetched:
            return frozenset()  # no device data -> nothing earned, nothing claimed
        self._devices(ctx)
        self._vlans(ctx)
        for dev in ctx.raw.devices:
            if dev.get("type") == "switch":
                self._switch_ports_and_l3(ctx, dev)
                self._ospf(ctx, dev)
                self._bgp(ctx, dev, DeviceRole.SWITCH)
            elif dev.get("type") == "gateway":
                self._gateway_ports_and_l3(ctx, dev)
                self._bgp(ctx, dev, DeviceRole.GATEWAY)
        return frozenset({IRCapability.WIRED_L2, IRCapability.L3_EXITS})

    def _devices(self, ctx: IngestContext) -> None:
        org_map = _org_vlan_map(ctx)
        site_nets: dict[str, Any] = ctx.site_effective.get("networks") or {}
        for dev in ctx.raw.devices:
            role = _ROLE.get(str(dev.get("type")))
            if role is None or not dev.get("mac"):
                continue
            did = device_id(str(dev["mac"]))
            stp_priority: int | None = None
            stp_invalid = False
            dhcp_snooping: tuple[str, ...] | None = None
            if role is DeviceRole.SWITCH:
                eff = ctx.device_effective.get(did) or ctx.site_effective
                cfg = eff.get("stp_config")
                stp_priority = _bridge_priority(cfg)
                # present but uninterpretable != absent: never read as default
                stp_invalid = (
                    stp_priority is None and (cfg or {}).get("bridge_priority") is not None
                )
                dhcp_snooping = _snooping(eff)
            ctx.builder.add_device(
                Device(
                    id=did,
                    role=role,
                    site=ctx.raw.scope.site_id,
                    model=dev.get("model"),
                    name=dev.get("name"),
                    stp_priority=stp_priority,
                    stp_priority_invalid=stp_invalid,
                    dhcp_snooping=dhcp_snooping,
                    # gateway namespace unfetched -> its L3 model is UNKNOWN
                    l3_unmodeled=(
                        role is DeviceRole.GATEWAY
                        and "org_networks" not in ctx.raw.meta.fetched
                    ),
                    # an ACTIVE gateway dhcpd entry whose name does not resolve:
                    # it may serve DHCP on a vlan we cannot identify
                    dhcp_unresolved=(
                        role is DeviceRole.GATEWAY
                        and any(
                            _dhcp_active(entry)
                            and _gw_net_vlan(name, org_map, site_nets) is None
                            for name, entry in (dev.get("dhcpd_config") or {}).items()
                        )
                    ),
                )
            )

    def _vlans(self, ctx: IngestContext) -> None:
        # VLANs come from the site effective AND every device effective — a
        # device-local network must still yield a Vlan entity (per-VLAN graphs
        # enumerate ir.vlans; a missing entity would hide it from analysis).
        seen: set[int] = set()
        sources: list[dict[str, Any]] = [ctx.site_effective, *ctx.device_effective.values()]
        # org subnet overlay, RAW values (templated must be distinguishable
        # from absent — _literal_subnet only inside the winning-literal core)
        org_subnet_raw: dict[int, Any] = {}
        for net in ctx.raw.org_networks:
            vid = _vlan_int(net.get("vlan_id"))
            # truthy guard (not `is not None`): "" subnet = absent here too,
            # mirroring the per-row `or None` normalization below
            if vid is not None and net.get("subnet"):
                org_subnet_raw.setdefault(vid, net.get("subnet"))
        # org gateway overlay, RAW values (templated must be distinguishable
        # from absent — _literal_ip only at decision time)
        org_gw_raw: dict[int, Any] = {}
        for net in ctx.raw.org_networks:
            vid = _vlan_int(net.get("vlan_id"))
            if vid is not None and net.get("gateway") is not None:
                org_gw_raw.setdefault(vid, net.get("gateway"))
        # EVERY row per vlan id across ALL effective sources, in source
        # order — [0] is the exact row that wins the Vlan mint (gateway or
        # not; tracking only gateway-bearing rows would silently promote a
        # non-winning device row to winner). Declared = value is not None:
        # explicit "gateway": null is ABSENT (null==absent canon).
        gw_rows_by_vid: dict[int, list[Any]] = {}
        subnet_rows_by_vid: dict[int, list[Any]] = {}
        # every network NAME per vid (GS30 collision evidence) — same single pass
        names_by_vid: dict[int, list[str]] = {}
        for eff in sources:
            for name, net in (eff.get("networks") or {}).items():
                vid = _vlan_int(net.get("vlan_id"))
                if vid is not None:
                    gw_rows_by_vid.setdefault(vid, []).append(net.get("gateway"))
                    # "" subnet = absent (no routed intent); only {{templated}}
                    # is declared-unreadable — normalize empty to None here so
                    # the shared core's "declared = not None" stays uniform
                    subnet_rows_by_vid.setdefault(vid, []).append(net.get("subnet") or None)
                    names_by_vid.setdefault(vid, []).append(name)
        org_vlan_by_name = _org_vlan_map(ctx)
        dhcp_sources = self._dhcp_sources(ctx, org_vlan_by_name)
        # same org map as _dhcp_sources — the two layers must not disagree
        self._mint_dhcp_scopes(ctx, org_vlan_by_name)
        for eff in sources:
            for name, net in (eff.get("networks") or {}).items():
                vid = _vlan_int(net.get("vlan_id"))
                if vid is not None and vid not in seen:
                    seen.add(vid)
                    gw, gw_unresolved = _vlan_gateway(vid, gw_rows_by_vid, org_gw_raw)
                    subnet, subnet_unresolved = _winning_literal(
                        vid, subnet_rows_by_vid, org_subnet_raw,
                        parse=_literal_subnet, same=same_subnet,
                    )
                    collisions = tuple(sorted(set(names_by_vid.get(vid, [])) - {name}))
                    ctx.builder.add_vlan(
                        Vlan(
                            vlan_id=vid,
                            name=name,
                            scope=ctx.raw.scope.site_id,
                            subnet=subnet,
                            subnet_unresolved=subnet_unresolved,
                            gateway=gw,
                            gateway_unresolved=gw_unresolved,
                            dhcp_sources=tuple(sorted(dhcp_sources.get(vid, ()))),
                            collisions=collisions,
                        )
                    )

    @staticmethod
    def _dhcp_sources(
        ctx: IngestContext, org_vlan_by_name: Mapping[str, int | None]
    ) -> dict[int, set[str]]:
        """vlan -> modeled DHCP providers. 'site' = the switch-hosted
        server/relay (site dhcpd_config, names in the SWITCH namespace);
        gateway device ids = their OWN dhcpd_config (names in the ORG
        namespace). Device-level SWITCH dhcpd_config is intentionally
        unmodeled (the compiler does not carry it; see ROADMAP)."""
        out: dict[int, set[str]] = {}
        site_nets: dict[str, Any] = ctx.site_effective.get("networks") or {}
        for name, entry in (ctx.site_effective.get("dhcpd_config") or {}).items():
            vid = _vlan_int((site_nets.get(str(name)) or {}).get("vlan_id"))
            if vid is not None and _dhcp_active(entry):
                out.setdefault(vid, set()).add("site")
        if "org_networks" not in ctx.raw.meta.fetched:
            # the gateway namespace is UNKNOWN: crediting a gateway as a DHCP
            # source via a same-named site network would be a guess — worse, a
            # POSITIVE one that suppresses removal findings before the
            # l3_unmodeled cap can run. No facts from blind gateways.
            return out
        for dev in ctx.raw.devices:
            if dev.get("type") != "gateway" or not dev.get("mac"):
                continue
            did = device_id(str(dev["mac"]))
            for name, entry in (dev.get("dhcpd_config") or {}).items():
                vid = _gw_net_vlan(name, org_vlan_by_name, site_nets)
                if vid is not None and _dhcp_active(entry):
                    out.setdefault(vid, set()).add(did)
                # unresolvable ACTIVE entries are NOT dropped silently: the
                # device carries dhcp_unresolved (set in _devices) for them
        return out

    @staticmethod
    def _mint_dhcp_scopes(
        ctx: IngestContext, org_vlan_by_name: Mapping[str, int | None]
    ) -> None:
        """DhcpScope rows for SERVING dhcpd entries (_dhcp_serves_scope, not
        _dhcp_active — relay/none never own a scope). Site entries resolve in
        the SITE namespace (always fetched: unresolved only when a DECLARED
        subnet is unreadable); gateway entries resolve in the ORG namespace —
        when it is unfetched the vlan/subnet stay None (no cross-namespace
        guess) but the RANGES still mint: they are literal device config, and
        dropping them would let a new overlapping site scope falsely PASS."""
        site_nets: dict[str, Any] = ctx.site_effective.get("networks") or {}
        for name, entry in (ctx.site_effective.get("dhcpd_config") or {}).items():
            if not _dhcp_serves_scope(entry):
                continue
            entry = entry or {}
            net = site_nets.get(str(name)) or {}
            declared = net.get("subnet")
            ctx.builder.add_dhcp_scope(
                DhcpScope(
                    provider="site",
                    network=str(name),
                    vlan=_vlan_int(net.get("vlan_id")),
                    ip_start=_literal_ip(entry.get("ip_start")),
                    ip_end=_literal_ip(entry.get("ip_end")),
                    gateway=_literal_ip(entry.get("gateway")),
                    subnet=_literal_subnet(declared),
                    subnet_unresolved=(
                        declared is not None and _literal_subnet(declared) is None
                    ),
                    network_gateway=_literal_ip(net.get("gateway")),
                    # site namespace is always fetched: unresolved only when
                    # a DECLARED gateway is unreadable (templated); explicit
                    # null = absent (null==absent canon, same as subnet)
                    network_gateway_unresolved=(
                        net.get("gateway") is not None
                        and _literal_ip(net.get("gateway")) is None
                    ),
                )
            )
        org_fetched = "org_networks" in ctx.raw.meta.fetched
        org_nets = {str(n.get("name")): n for n in ctx.raw.org_networks if n.get("name")}
        for dev in ctx.raw.devices:
            if dev.get("type") != "gateway" or not dev.get("mac"):
                continue
            did = device_id(str(dev["mac"]))
            for name, entry in (dev.get("dhcpd_config") or {}).items():
                if not _dhcp_serves_scope(entry):
                    continue
                entry = entry or {}
                net_entry = org_nets.get(str(name)) if org_fetched else None
                declared = (net_entry or {}).get("subnet")
                declared_gw = (net_entry or {}).get("gateway")
                ctx.builder.add_dhcp_scope(
                    DhcpScope(
                        provider=did,
                        network=str(name),
                        vlan=(
                            _gw_net_vlan(name, org_vlan_by_name, site_nets)
                            if org_fetched
                            else None
                        ),
                        ip_start=_literal_ip(entry.get("ip_start")),
                        ip_end=_literal_ip(entry.get("ip_end")),
                        gateway=_literal_ip(entry.get("gateway")),
                        subnet=_literal_subnet(declared),
                        # blind namespace OR name missing from the fetched one:
                        # intent UNKNOWABLE; present-but-templated: unreadable;
                        # present with no subnet declared: no intent, not blind
                        subnet_unresolved=(
                            not org_fetched
                            or net_entry is None
                            or (declared is not None and _literal_subnet(declared) is None)
                        ),
                        network_gateway=_literal_ip(declared_gw),
                        # same three-way rule as subnet_unresolved: blind
                        # namespace OR name missing -> unknowable; declared
                        # but templated -> unreadable; absent/null -> no intent
                        network_gateway_unresolved=(
                            not org_fetched
                            or net_entry is None
                            or (declared_gw is not None
                                and _literal_ip(declared_gw) is None)
                        ),
                    )
                )

    def _gateway_ports_and_l3(self, ctx: IngestContext, dev: Mapping[str, Any]) -> None:
        """GS22: the gateway's OWN config — its LAN trunk carriage (so the
        core<->gateway link end stops being vlan-blind) and its L3 interfaces
        (HIGH/MEDIUM exits instead of MEDIUM boundary assumptions).

        Gateways are not a compile target in M1: facts come from the RAW
        device object, and network names resolve via the ORG networks list
        (the gateway namespace — real orgs use DIFFERENT names there than in
        the switch-side site networks; found live 2026-06-11), with the site
        networks as fallback. A port referencing ANY unresolvable name stays
        VLAN-BLIND (carriage unknown, assumed-carriage applies) — claiming a
        config-empty trunk would be a false 'carries nothing'.

        L3 interfaces, two grades: an `ip_configs` entry is an explicit
        statement (CONFIG/HIGH); a ROUTED org network (subnet) attached to a
        LAN port is terminated by the gateway per the Mist gateway model —
        real but inferred (INFERRED/MEDIUM)."""
        did = device_id(str(dev["mac"]))
        org_fetched = "org_networks" in ctx.raw.meta.fetched
        org_nets = {str(n.get("name")): n for n in ctx.raw.org_networks if n.get("name")}
        site_nets: dict[str, Any] = ctx.site_effective.get("networks") or {}

        def net_of(name: Any) -> Mapping[str, Any] | None:
            # namespace UNFETCHED -> no resolution at all (a same-named site
            # network would be a cross-namespace guess); the device is marked
            # l3_unmodeled and its ports stay vlan-blind
            if not org_fetched:
                return None
            return org_nets.get(str(name)) or site_nets.get(str(name))

        def vlan_of(name: Any) -> int | None:
            return _vlan_int((net_of(name) or {}).get("vlan_id"))

        lan_networks: set[str] = set()
        for key, attrs in (dev.get("port_config") or {}).items():
            attrs = attrs or {}
            referenced = [str(n) for n in attrs.get("networks") or []]
            if attrs.get("port_network"):
                referenced.append(str(attrs["port_network"]))
            lan_networks.update(referenced)
            unresolved = [n for n in referenced if vlan_of(n) is None]
            if unresolved:
                native: int | None = None
                tagged: tuple[int, ...] = ()
                meta = fact_meta(
                    Provenance.INFERRED,
                    (f"gateway port references unresolvable network(s) {unresolved!r}"
                     " — carriage unknown",),
                )
            else:
                native = vlan_of(attrs.get("port_network"))
                tagged = tuple(
                    sorted(
                        v
                        for v in (vlan_of(n) for n in attrs.get("networks") or [])
                        if v is not None and v != native
                    )
                )
                meta = CONFIG_META
            for member in expand_port_members(str(key)):
                ctx.builder.add_port(
                    Port(
                        id=port_id(did, member),
                        device_id=did,
                        name=member,
                        mode=PortMode.TRUNK if tagged else PortMode.ACCESS,
                        native_vlan=native,
                        tagged_vlans=tagged,
                        profile=attrs.get("usage"),
                        disabled=bool(attrs.get("disabled")),
                        meta=meta,
                    )
                )
        l3_vlans: set[int] = set()
        for net_name, ipc in (dev.get("ip_configs") or {}).items():
            vid = vlan_of(net_name)
            if vid is not None:
                l3_vlans.add(vid)
                ctx.builder.add_l3intf(
                    L3Intf(
                        device_id=did,
                        role=L3Role.GATEWAY,
                        vlan_id=vid,
                        ip=(ipc or {}).get("ip"),
                        subnet=None,
                    )
                )
        for name in sorted(lan_networks):
            net = net_of(name)
            vid = vlan_of(name)
            subnet = _literal_subnet((net or {}).get("subnet"))
            if net is None or vid is None or vid in l3_vlans or subnet is None:
                continue
            l3_vlans.add(vid)
            ctx.builder.add_l3intf(
                L3Intf(
                    device_id=did,
                    role=L3Role.GATEWAY,
                    vlan_id=vid,
                    ip=net.get("gateway"),
                    subnet=subnet,
                    meta=fact_meta(
                        Provenance.INFERRED,
                        (f"routed network {name!r} attached to a gateway LAN port — "
                         "L3 termination per the Mist gateway model",),
                    ),
                )
            )

    def _switch_ports_and_l3(self, ctx: IngestContext, dev: Mapping[str, Any]) -> None:
        did = device_id(str(dev["mac"]))
        eff = ctx.device_effective.get(did) or ctx.site_effective
        networks: dict[str, Any] = eff.get("networks") or {}
        bases = resolve_port_bases(eff)
        stat_rows: dict[str, _Json] = {
            str(r["port_id"]): r
            for r in ctx.raw.port_stats
            if r.get("port_id") and r.get("mac") and device_id(str(r["mac"])) == did
        }
        for member, usage, usage_name, resolution in resolve_effective_ports(eff):
            dyn_profile = (bases.get(member) or {}).get("dynamic_usage")
            if dyn_profile:
                usage, usage_name, meta = self._runtime_usage(
                    eff, str(dyn_profile), member, stat_rows.get(member), usage, usage_name,
                    self._meta_for(resolution, usage_name),
                )
            else:
                meta = self._meta_for(resolution, usage_name)
            native, tagged = usage_vlans(usage, networks)
            mode = PortMode.TRUNK if usage.get("mode") == "trunk" else PortMode.ACCESS
            row = stat_rows.get(member)
            l1_speed, l1_duplex, l1_autoneg = _l1_config(usage)
            obs_speed, obs_duplex = _l1_observed(row)
            auth = _port_auth(usage)
            ctx.builder.add_port(
                Port(
                    id=port_id(did, member),
                    device_id=did,
                    name=member,
                    mode=mode,
                    native_vlan=native,
                    tagged_vlans=tagged,
                    profile=usage_name,
                    speed=l1_speed,
                    duplex=l1_duplex,
                    autoneg_disabled=l1_autoneg,
                    # explicit MTU only; null == absent (PUT semantics) == platform default
                    mtu=int(usage["mtu"]) if usage.get("mtu") else None,
                    # config PoE intent: None when the usage is blind/unresolved
                    poe=None if not usage else not bool(usage.get("poe_disabled")),
                    poe_draw=_poe_draw(row),
                    observed_speed=obs_speed,
                    observed_duplex=obs_duplex,
                    disabled=bool(usage.get("disabled")),
                    stp_edge=bool(usage.get("stp_edge")),
                    bpdu_filter=bool(usage.get("stp_disable")),
                    dhcp_trusted=_dhcp_trust(usage),
                    auth=auth,
                    meta=meta,
                )
            )
        for net_name, ipc in (eff.get("other_ip_configs") or {}).items():
            vid = (networks.get(net_name) or {}).get("vlan_id")
            if vid is not None:
                ctx.builder.add_l3intf(
                    L3Intf(
                        device_id=did,
                        role=L3Role.IRB,
                        vlan_id=int(vid),
                        ip=ipc.get("ip"),
                        subnet=None,
                    )
                )

    def _ospf(self, ctx: IngestContext, dev: Mapping[str, Any]) -> None:
        """GS26: switch OSPF participation. Gated by ospf_config.enabled; each
        ospf_areas.<area>.networks.<name> joins to the effective networks map.
        A name that does not resolve to a vlan_id mints an unresolved row (the
        only switch-side blindness — l3_unmodeled is gateway-only)."""
        did = device_id(str(dev["mac"]))
        eff = ctx.device_effective.get(did) or ctx.site_effective
        if not (eff.get("ospf_config") or {}).get("enabled"):
            return
        networks: dict[str, Any] = eff.get("networks") or {}
        for area, area_cfg in (eff.get("ospf_areas") or {}).items():
            for name, ncfg in ((area_cfg or {}).get("networks") or {}).items():
                ncfg = ncfg or {}
                vid = _vlan_int((networks.get(str(name)) or {}).get("vlan_id"))
                raw_metric = ncfg.get("metric")
                metric = _metric_int(raw_metric)
                # raw token ONLY when present-but-unparseable (so absent stays None);
                # diff-bearing -> an absent->templated metric edit is not an empty diff.
                metric_unresolved = (
                    str(raw_metric) if (raw_metric is not None and metric is None) else None
                )
                ctx.builder.add_ospf_intf(
                    OspfIntf(
                        device_id=did,
                        vlan_id=vid,
                        area=str(area),
                        network_name=str(name),
                        passive=bool(ncfg.get("passive", False)),
                        metric=metric,
                        metric_unresolved=metric_unresolved,
                        unresolved=(vid is None),
                    )
                )

    def _bgp(self, ctx: IngestContext, dev: Mapping[str, Any], role: DeviceRole) -> None:
        """GS28: switch/gateway BGP peerings. Switch reads device/site effective
        config; gateway reads its materialized bgp_config off the raw device. One
        BgpPeer per (device, neighbor_ip) — a neighbor IP claimed by 2+ sessions
        with differing modeled attrs is marked ambiguous (never last-win)."""
        did = device_id(str(dev["mac"]))
        if role is DeviceRole.GATEWAY:
            bgp = dev.get("bgp_config") or {}
        else:
            eff = ctx.device_effective.get(did) or ctx.site_effective
            bgp = eff.get("bgp_config") or {}
        if not bgp:
            return
        # accumulate per neighbor_ip; detect cross-session ambiguity
        chosen: dict[str, BgpPeer] = {}
        ambiguous: set[str] = set()
        for sname, scfg in bgp.items():
            scfg = scfg or {}
            local_as, local_as_unresolved = _bgp_as_int(scfg.get("local_as"))
            stype, stype_unresolved = _bgp_enum(scfg.get("type"), _BGP_TYPES)
            via, via_unresolved = _bgp_enum(scfg.get("via"), _BGP_VIAS)
            for nip, ncfg in (scfg.get("neighbors") or {}).items():
                ncfg = ncfg or {}
                nas, nas_unresolved = _bgp_as_int(ncfg.get("neighbor_as"))
                disabled, disabled_unresolved = _bgp_disabled(ncfg.get("disabled"))
                key = str(nip)
                peer = BgpPeer(
                    device_id=did, role=role, session_name=str(sname), neighbor_ip=key,
                    local_as=local_as, neighbor_as=nas, session_type=stype,
                    disabled=disabled, via=via,
                    local_as_unresolved=local_as_unresolved,
                    neighbor_as_unresolved=nas_unresolved,
                    session_type_unresolved=stype_unresolved,
                    via_unresolved=via_unresolved,
                    disabled_unresolved=disabled_unresolved,
                    unresolved=not _is_literal_ip(key),
                )
                if key in chosen and _bgp_attrs(chosen[key]) != _bgp_attrs(peer):
                    ambiguous.add(key)     # differing modeled attrs -> ambiguous
                else:
                    chosen[key] = peer
        for key, peer in chosen.items():
            if key in ambiguous:
                peer = replace(peer, ambiguous=True)
            ctx.builder.add_bgp_peer(peer)

    @staticmethod
    def _meta_for(resolution: str, usage_name: str | None) -> FactMeta:
        """Honesty: system-defined semantics are inferred from docs, and an
        unresolved usage name means the carriage is UNKNOWN — both get INFERRED
        (MEDIUM) so no conclusion rides them at config-HIGH; an unresolved
        no-vlan port is VLAN-BLIND in the L2 graph."""
        if resolution == "system":
            return fact_meta(
                Provenance.INFERRED,
                (f"usage {usage_name!r} resolved from Mist system-defined defaults",),
            )
        if resolution == "unresolved":
            return fact_meta(
                Provenance.INFERRED,
                (f"usage {usage_name!r} has no definition in the modeled config",),
            )
        return CONFIG_META

    def _runtime_usage(
        self,
        eff: dict[str, Any],
        profile: str,
        member: str,
        row: _Json | None,
        static_usage: dict[str, Any],
        static_name: str | None,
        static_meta: FactMeta,
    ) -> tuple[dict[str, Any], str | None, FactMeta]:
        """The RUNTIME usage of a dynamically-profiled port (see
        classify_dynamic_port — the shared source of truth): static stands when
        nothing is connected or the rules conclusively miss; a match yields the
        matched usage at OBSERVED confidence; anything unresolvable is
        VLAN-BLIND (carriage unknown, never guessed)."""
        kind, detail = classify_dynamic_port(eff, profile, row)
        if kind == "static":
            return static_usage, static_name, static_meta
        if kind == "matched" and detail is not None:
            definition, _ = usage_definition(eff, detail)
            neighbor = (row or {}).get("neighbor_system_name")
            return (
                definition,
                detail,
                fact_meta(
                    Provenance.OBSERVED,
                    (
                        f"runtime usage {detail!r} via dynamic rule on "
                        f"lldp_system_name {neighbor!r}",
                    ),
                ),
            )
        return {}, static_name, fact_meta(
            Provenance.INFERRED, (f"port {member}: {detail or 'runtime usage unknown'}",)
        )
