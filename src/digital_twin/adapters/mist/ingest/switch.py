"""Switch-domain ingester: effective config -> Device/Port/Vlan/L3Intf entities.

Reads device_effective (per-device compiled config) for ports/L3, and the raw
device list for identity (mac/model/role). APs become leaf Device entities here
(their links/clients come from the lldp/clients ingesters).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from digital_twin.contracts import Finding, FindingCategory, FindingSource, Severity
from digital_twin.ir import (
    Confidence,
    ConfidenceLevel,
    Device,
    DeviceRole,
    IRCapability,
    L3Intf,
    L3Role,
    Port,
    PortMode,
    Vlan,
    device_id,
    port_id,
)
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


def _literal_subnet(value: Any) -> str | None:
    """A subnet still carrying '{{vars}}' is unresolved intent — not a fact."""
    if not value or "{{" in str(value):
        return None
    return str(value)


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
        findings.append(
            Finding(
                source=FindingSource.ADAPTER,
                category=FindingCategory.OPERATIONAL,
                code="scope.stp.bridge_priority_invalid",
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
            )
        )
    return findings


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
            elif dev.get("type") == "gateway":
                self._gateway_ports_and_l3(ctx, dev)
        return frozenset({IRCapability.WIRED_L2, IRCapability.L3_EXITS})

    def _devices(self, ctx: IngestContext) -> None:
        for dev in ctx.raw.devices:
            role = _ROLE.get(str(dev.get("type")))
            if role is None or not dev.get("mac"):
                continue
            did = device_id(str(dev["mac"]))
            stp_priority: int | None = None
            stp_invalid = False
            if role is DeviceRole.SWITCH:
                eff = ctx.device_effective.get(did) or ctx.site_effective
                cfg = eff.get("stp_config")
                stp_priority = _bridge_priority(cfg)
                # present but uninterpretable != absent: never read as default
                stp_invalid = (
                    stp_priority is None and (cfg or {}).get("bridge_priority") is not None
                )
            ctx.builder.add_device(
                Device(
                    id=did,
                    role=role,
                    site=ctx.raw.scope.site_id,
                    model=dev.get("model"),
                    stp_priority=stp_priority,
                    stp_priority_invalid=stp_invalid,
                )
            )

    def _vlans(self, ctx: IngestContext) -> None:
        # VLANs come from the site effective AND every device effective — a
        # device-local network must still yield a Vlan entity (per-VLAN graphs
        # enumerate ir.vlans; a missing entity would hide it from analysis).
        seen: set[int] = set()
        # ORG networks carry the routed intent (subnet) the gateway serves —
        # overlay it onto vlans the switch side knows only by id
        org_subnets: dict[int, str] = {}
        for net in ctx.raw.org_networks:
            vid = _vlan_int(net.get("vlan_id"))
            subnet = _literal_subnet(net.get("subnet"))
            if vid is not None and subnet:
                org_subnets.setdefault(vid, subnet)
        sources: list[dict[str, Any]] = [ctx.site_effective, *ctx.device_effective.values()]
        for eff in sources:
            for name, net in (eff.get("networks") or {}).items():
                vid = net.get("vlan_id")
                if vid is not None and int(vid) not in seen:
                    seen.add(int(vid))
                    ctx.builder.add_vlan(
                        Vlan(
                            vlan_id=int(vid),
                            name=name,
                            scope=ctx.raw.scope.site_id,
                            subnet=net.get("subnet") or org_subnets.get(int(vid)),
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
        org_nets = {str(n.get("name")): n for n in ctx.raw.org_networks if n.get("name")}
        site_nets: dict[str, Any] = ctx.site_effective.get("networks") or {}

        def net_of(name: Any) -> Mapping[str, Any] | None:
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
            ctx.builder.add_port(
                Port(
                    id=port_id(did, member),
                    device_id=did,
                    name=member,
                    mode=mode,
                    native_vlan=native,
                    tagged_vlans=tagged,
                    profile=usage_name,
                    # explicit MTU only; null == absent (PUT semantics) == platform default
                    mtu=int(usage["mtu"]) if usage.get("mtu") else None,
                    # config PoE intent: None when the usage is blind/unresolved
                    poe=None if not usage else not bool(usage.get("poe_disabled")),
                    poe_draw=_poe_draw(row),
                    disabled=bool(usage.get("disabled")),
                    stp_edge=bool(usage.get("stp_edge")),
                    bpdu_filter=bool(usage.get("stp_disable")),
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
