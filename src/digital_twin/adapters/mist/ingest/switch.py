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
from .dynamic_usage import evaluate_rules
from .ports import resolve_effective_ports, resolve_port_bases, usage_definition, usage_vlans

_Json = Mapping[str, Any]

_ROLE = {"switch": DeviceRole.SWITCH, "ap": DeviceRole.AP, "gateway": DeviceRole.GATEWAY}


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
            ctx.builder.add_device(
                Device(
                    id=device_id(str(dev["mac"])),
                    role=role,
                    site=ctx.raw.scope.site_id,
                    model=dev.get("model"),
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
            ctx.builder.add_port(
                Port(
                    id=port_id(did, member),
                    device_id=did,
                    name=member,
                    mode=mode,
                    native_vlan=native,
                    tagged_vlans=tagged,
                    profile=usage_name,
                    disabled=bool(usage.get("disabled")),
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
        """The RUNTIME usage of a dynamically-profiled port, from the profile's
        rules + the port's observed LLDP neighbor. Honesty per dynamic_usage.py:
        nothing connected (row down) or a conclusive rule miss -> the static
        usage stands; a match -> the matched usage at OBSERVED confidence; any
        inconclusive outcome -> VLAN-BLIND (carriage unknown, never guessed)."""

        def blind(reason: str) -> tuple[dict[str, Any], str | None, FactMeta]:
            return {}, static_name, fact_meta(Provenance.INFERRED, (reason,))

        rules = ((eff.get("port_usages") or {}).get(profile) or {}).get("rules")
        if not isinstance(rules, list):
            return blind(f"dynamic profile {profile!r} has no rules in the modeled config")
        if row is None:
            return blind(
                f"port {member} is dynamically profiled but has no port stats — "
                "runtime usage unknown"
            )
        if not row.get("up"):
            return static_usage, static_name, static_meta  # nothing connected
        outcome = evaluate_rules(
            rules, {"lldp_system_name": row.get("neighbor_system_name")}
        )
        if outcome.kind == "static":
            return static_usage, static_name, static_meta
        if outcome.kind == "matched" and outcome.usage is not None:
            definition, def_res = usage_definition(eff, outcome.usage)
            if def_res == "unresolved":
                return blind(
                    f"runtime usage {outcome.usage!r} (dynamic rule) has no definition "
                    "in the modeled config"
                )
            return (
                definition,
                outcome.usage,
                fact_meta(
                    Provenance.OBSERVED,
                    (
                        f"runtime usage {outcome.usage!r} via dynamic rule on "
                        f"lldp_system_name {row.get('neighbor_system_name')!r}",
                    ),
                ),
            )
        return blind("dynamic rules not evaluable from observed LLDP — runtime usage unknown")
