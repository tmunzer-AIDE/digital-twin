"""Switch-domain ingester: effective config -> Device/Port/Vlan/L3Intf entities.

Reads device_effective (per-device compiled config) for ports/L3, and the raw
device list for identity (mac/model/role). APs become leaf Device entities here
(their links/clients come from the lldp/clients ingesters).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from digital_twin.ir import (
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
from .ports import resolve_effective_ports, resolve_port_bases, usage_definition, usage_vlans

_Json = Mapping[str, Any]

_ROLE = {"switch": DeviceRole.SWITCH, "ap": DeviceRole.AP, "gateway": DeviceRole.GATEWAY}


def _bridge_priority(stp_config: Any) -> int | None:
    """`stp_config.bridge_priority` — OAS says string, '4096' or '4k' shaped;
    unparseable/absent -> None (the platform default, treated as ASSUMED)."""
    raw = (stp_config or {}).get("bridge_priority")
    if raw is None:
        return None
    text = str(raw).strip().lower()
    try:
        return int(text[:-1]) * 1024 if text.endswith("k") else int(text)
    except ValueError:
        return None


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
        return frozenset({IRCapability.WIRED_L2, IRCapability.L3_EXITS})

    def _devices(self, ctx: IngestContext) -> None:
        for dev in ctx.raw.devices:
            role = _ROLE.get(str(dev.get("type")))
            if role is None or not dev.get("mac"):
                continue
            did = device_id(str(dev["mac"]))
            stp_priority: int | None = None
            if role is DeviceRole.SWITCH:
                eff = ctx.device_effective.get(did) or ctx.site_effective
                stp_priority = _bridge_priority(eff.get("stp_config"))
            ctx.builder.add_device(
                Device(
                    id=did,
                    role=role,
                    site=ctx.raw.scope.site_id,
                    model=dev.get("model"),
                    stp_priority=stp_priority,
                )
            )

    def _vlans(self, ctx: IngestContext) -> None:
        # VLANs come from the site effective AND every device effective — a
        # device-local network must still yield a Vlan entity (per-VLAN graphs
        # enumerate ir.vlans; a missing entity would hide it from analysis).
        seen: set[int] = set()
        sources: list[dict[str, Any]] = [ctx.site_effective, *ctx.device_effective.values()]
        for eff in sources:
            for name, net in (eff.get("networks") or {}).items():
                vid = net.get("vlan_id")
                if vid is not None and int(vid) not in seen:
                    seen.add(int(vid))
                    ctx.builder.add_vlan(
                        Vlan(vlan_id=int(vid), name=name, scope=ctx.raw.scope.site_id)
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
