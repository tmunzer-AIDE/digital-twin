"""wired.client.impact — who is affected, RIGHT NOW (enrichment over IRDiff).

Enumerates currently-connected clients whose connectivity the delta changes:
- vlan_move:   the client's access port changes native VLAN (still up).
- disconnect:  the client's attach port disappears from IR'.
- blackhole:   the client's VLAN component loses exit reach in IR'.
WARN (WARNING, network) when >=1 client is affected; HIGH confidence (devices
report their own clients). The currently-connected-only caveat is a COVERAGE
note — not-yet-connected clients are out of observational reach (spec).
"""

from __future__ import annotations

from typing import Any

from digital_twin.analysis.delta_cause import causes_for_blackhole
from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState, Status
from digital_twin.checks.wired.snooping import snooped_vlans  # "vlans this device snoops"
from digital_twin.contracts import Cause, Finding, FindingCategory, FindingSource, Severity
from digital_twin.ir import Capability, Confidence, ConfidenceLevel, IRCapability, IRDiff
from digital_twin.ir.entities import AttachKind, Client, ClientEnrichment
from digital_twin.ir.indexes import node_for, vc_root_map

_HIGH = Confidence(level=ConfidenceLevel.HIGH)
_CAVEAT = "currently-connected clients only (not-yet-connected clients are unobservable)"
# the ONLY ClientEnrichment fields projected into evidence — excludes `meta` so
# observational provenance never leaks into the report.
_IDENTITY_FIELDS = (
    "hostname", "family", "mfg", "model", "os", "auth_type", "auth_method",
    "auth_state", "nacrule", "status", "assigned_vlan", "vlan_source", "username",
)


class ClientImpactCheck:
    id = "wired.client.impact"
    title = "Active-client impact"
    domain = "wired.client"
    default_severity = Severity.WARNING

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2, IRCapability.CLIENTS_ACTIVE})

    def applies_to(self, diff: IRDiff) -> bool:
        return any(diff.touches(k) for k in ("port", "link", "vlan", "client", "l3intf"))

    def run(self, ctx: CheckContext) -> CheckResult:
        impacts: list[dict[str, Any]] = []
        for client in ctx.baseline.ir.clients:
            impact = self._impact_of(ctx, client)
            if impact is not None:
                impacts.append(impact)
        findings: tuple[Finding, ...] = ()
        if impacts:
            # no single `subject`: this finding aggregates clients across many
            # attachments (ports/APs/vlans) — the macs are in affected_entities
            # and each impact carries its own attachment in evidence
            union = tuple(dict.fromkeys(c for i in impacts for c in i.get("caused_by", ())))
            findings = (
                Finding(
                    source=FindingSource.CHECK,
                    category=FindingCategory.NETWORK,
                    code="wired.client.impact.active_clients",
                    severity=Severity.WARNING,
                    confidence=_HIGH,
                    message=f"{len(impacts)} currently-connected client(s) affected by the delta",
                    affected_entities=tuple(i["mac"] for i in impacts),
                    evidence={"impacts": impacts},
                    caused_by=union,
                ),
            )
        return CheckResult(
            check_id=self.id,
            status=Status.WARN if impacts else Status.PASS,
            findings=findings,
            coverage=Coverage(state=CoverageState.COMPLETE, notes=(_CAVEAT,)),
            confidence=_HIGH,
            reasoning=f"evaluated {len(ctx.baseline.ir.clients)} observed clients",
        )

    def _impact_of(self, ctx: CheckContext, client: Client) -> dict[str, Any] | None:
        base_ir, prop_ir = ctx.baseline.ir, ctx.proposed.ir
        if client.attach_kind is AttachKind.PORT:
            base_port = base_ir.ports.get(client.attach_id)
            prop_port = prop_ir.ports.get(client.attach_id)
            if base_port is None:
                return None
            if prop_port is None:
                return self._entry(ctx, client, "disconnect", "attach port removed",
                                   caused_by=ctx.delta_index.causes("port", [client.attach_id]))
            if base_port.native_vlan is not None and prop_port.native_vlan != base_port.native_vlan:
                return self._entry(
                    ctx, client,
                    "vlan_move",
                    f"access vlan {base_port.native_vlan} -> {prop_port.native_vlan}",
                    caused_by=ctx.delta_index.causes("port", [client.attach_id]),
                )
            base_offered = {base_port.native_vlan, base_port.voice_vlan} - {None}
            prop_offered = {prop_port.native_vlan, prop_port.voice_vlan} - {None}
            if client.vlan in base_offered and client.vlan not in prop_offered:
                return self._entry(
                    ctx, client, "vlan_removed",
                    f"vlan {client.vlan} no longer offered on this port",
                    caused_by=ctx.delta_index.causes("port", [client.attach_id]),
                )
        vlan = client.vlan
        if vlan is not None and vlan in prop_ir.vlans:
            node = self._attach_node(ctx, client)
            if node is not None:
                for comp in ctx.proposed.vlan_components(vlan):
                    if node in comp.nodes and comp.has_members and not comp.reaches_exit:
                        for base_comp in ctx.baseline.vlan_components(vlan):
                            if node in base_comp.nodes and base_comp.reaches_exit:
                                return self._entry(
                                    ctx, client, "blackhole",
                                    f"vlan {vlan} segment loses its exit",
                                    caused_by=causes_for_blackhole(ctx, vlan, comp),
                                )
        return None

    def _attach_node(self, ctx: CheckContext, client: Client) -> str | None:
        ir = ctx.baseline.ir
        vc_root = vc_root_map(ir)
        if client.attach_kind is AttachKind.PORT:
            port = ir.ports.get(client.attach_id)
            return node_for(vc_root, port.device_id) if port else None
        if client.attach_kind is AttachKind.AP:
            return node_for(vc_root, client.attach_id)
        return None

    def _entry(
        self, ctx: CheckContext, client: Client, impact: str, detail: str,
        caused_by: tuple[Cause, ...] = (),
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "mac": client.mac,
            "vlan": client.vlan,
            "attachment": client.attach_id,
            "impact": impact,
            "detail": detail,
            "caused_by": caused_by,
            "subnet": self._subnet(ctx, client),
            "dhcp_vlan_touched": self._dhcp_vlan_touched(ctx, client),
        }
        identity = self._identity(ctx, client)
        if identity:
            entry["identity"] = identity
        return entry

    def _identity(self, ctx: CheckContext, client: Client) -> dict[str, str]:
        # BASELINE enrichment: the finding describes clients connected BEFORE the change.
        # Project an EXPLICIT allowlist (_IDENTITY_FIELDS) so the record's `meta` (and any
        # future non-identity field) never leaks into evidence["impacts"][i].identity.
        ce: ClientEnrichment | None = ctx.baseline.ir.client_enrichment.get(client.id)
        if ce is None:
            return {}
        return {
            name: getattr(ce, name)
            for name in _IDENTITY_FIELDS
            if getattr(ce, name) is not None
        }

    def _subnet(self, ctx: CheckContext, client: Client) -> str | None:
        if client.vlan is None:
            return None
        vlan = ctx.baseline.ir.vlans.get(client.vlan)
        return vlan.subnet if vlan is not None else None

    def _dhcp_vlan_touched(self, ctx: CheckContext, client: Client) -> bool:
        # Four triggers, grouped by what they key on (so the code runs a,b,d,c, NOT
        # a,b,c,d): a+b are vid-scoped (DHCP providers / serving scopes for the vlan);
        # d+c are attach-port-scoped (this port's trust / its switch's snooping).
        vid = client.vlan  # the client's BASELINE attach vlan (held fixed across both arms)
        base_ir, prop_ir = ctx.baseline.ir, ctx.proposed.ir
        # (a) the vlan's modeled DHCP providers changed
        if vid is not None:
            bv, pv = base_ir.vlans.get(vid), prop_ir.vlans.get(vid)
            if bv is not None and pv is not None and bv.dhcp_sources != pv.dhcp_sources:
                return True
            # (b) a DHCP scope SERVING this vlan was added/removed/changed
            # (DhcpScope exposes `vlan`, NOT `vlan_id` — entities.py:208)
            def serving(ir: Any) -> dict[str, Any]:
                return {s.id: s for s in ir.dhcp_scopes if s.vlan == vid}
            if serving(base_ir) != serving(prop_ir):
                return True
        # (d) the client's own attach port: dhcp_trusted flip
        if client.attach_kind is AttachKind.PORT:
            bp, pp = base_ir.ports.get(client.attach_id), prop_ir.ports.get(client.attach_id)
            if bp is not None and pp is not None and bp.dhcp_trusted != pp.dhcp_trusted:
                return True
            # (c) snooping on the client's switch — counts ONLY if it flips whether the
            # CLIENT's vlan is snooped (not any snooping change). Reuses snooped_vlans.
            if bp is not None and vid is not None and (
                (vid in snooped_vlans(base_ir, bp.device_id))
                != (vid in snooped_vlans(prop_ir, bp.device_id))
            ):
                return True
        return False
