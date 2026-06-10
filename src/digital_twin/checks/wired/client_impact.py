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

from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState, Status
from digital_twin.contracts import Finding, FindingCategory, FindingSource, Severity
from digital_twin.ir import Capability, Confidence, ConfidenceLevel, IRCapability, IRDiff
from digital_twin.ir.entities import AttachKind, Client
from digital_twin.ir.indexes import node_for, vc_root_map

_HIGH = Confidence(level=ConfidenceLevel.HIGH)
_CAVEAT = "currently-connected clients only (not-yet-connected clients are unobservable)"


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
                return self._entry(client, "disconnect", "attach port removed")
            if base_port.native_vlan is not None and prop_port.native_vlan != base_port.native_vlan:
                return self._entry(
                    client,
                    "vlan_move",
                    f"access vlan {base_port.native_vlan} -> {prop_port.native_vlan}",
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
                                    client, "blackhole", f"vlan {vlan} segment loses its exit"
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

    def _entry(self, client: Client, impact: str, detail: str) -> dict[str, Any]:
        return {
            "mac": client.mac,
            "vlan": client.vlan,
            "attachment": client.attach_id,
            "impact": impact,
            "detail": detail,
        }
