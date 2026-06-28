"""wireless.wlan.client_impact — active clients losing WLAN SSID coverage.

Delta-conditioned on WLAN config changes. Client telemetry is deliberately not a
hard registry requirement: if WLAN coverage is reduced but clients were not
fetched, the check emits a local unverified REVIEW-floor instead of becoming
INSUFFICIENT_DATA before run().
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState, Status
from digital_twin.contracts import (
    Finding,
    FindingCategory,
    FindingSource,
    ObjectRef,
    Severity,
)
from digital_twin.ir import (
    Capability,
    Client,
    ClientKind,
    Confidence,
    ConfidenceLevel,
    IRCapability,
    IRDiff,
    Wlan,
)
from digital_twin.ir.model import IR

_HIGH = Confidence(level=ConfidenceLevel.HIGH)
_CAVEAT = "client impact assessed from point-in-time wireless telemetry"
_AFFECTING_FIELDS = frozenset({"ssid", "enabled", "apply_to", "ap_ids", "wxtag_ids"})


def _active_wireless_clients(ir: IR) -> list[Client]:
    return [
        c for c in ir.clients
        if c.active is True and c.kind is ClientKind.WIRELESS
    ]


def _covers(wlan: Wlan, ap_id: str | None) -> Literal["yes", "unknown"]:
    if wlan.apply_to == "site":
        return "yes"
    if wlan.apply_to == "aps":
        if ap_id is None:
            return "unknown"
        return "yes" if ap_id in wlan.ap_ids else "unknown"
    return "unknown"


def _clients_known(ctx: CheckContext) -> bool:
    return (
        IRCapability.CLIENTS_ACTIVE in ctx.baseline.ir.capabilities
        and IRCapability.CLIENTS_ACTIVE in ctx.proposed.ir.capabilities
    )


class WlanClientImpactCheck:
    id = "wireless.wlan.client_impact"
    title = "active clients losing WLAN coverage"
    domain = "wireless.wlan"
    default_severity = Severity.ERROR

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WLAN_CONFIG})

    def applies_to(self, diff: IRDiff) -> bool:
        return diff.touches("wlan")

    def _affected_ssids(self, ctx: CheckContext) -> dict[str, tuple[str, ...]]:
        base = {w.id: w for w in ctx.baseline.ir.wlans}
        affected: dict[str, set[str]] = {}

        def add(wlan_id: str) -> None:
            w = base.get(wlan_id)
            if w is not None and w.enabled is True and w.ssid:
                affected.setdefault(w.ssid, set()).add(w.id)

        for ref in ctx.diff.removed:
            if ref.kind == "wlan":
                add(ref.id)
        for mod in ctx.diff.modified:
            if mod.ref.kind == "wlan" and _AFFECTING_FIELDS & set(mod.changed_fields):
                add(mod.ref.id)

        return {ssid: tuple(sorted(ids)) for ssid, ids in sorted(affected.items())}

    def _has_survivor(self, ir: IR, ssid: str, ap_id: str | None) -> bool:
        return any(
            w.enabled is True
            and w.ssid == ssid
            and _covers(w, ap_id) == "yes"
            for w in ir.wlans
        )

    def _coverage_lost(
        self, ctx: CheckContext, ssid: str, changed_ids: tuple[str, ...], clients: list[Client]
    ) -> Finding:
        headline = changed_ids[0]
        return Finding(
            source=FindingSource.CHECK,
            category=FindingCategory.NETWORK,
            code=f"{self.id}.coverage_lost",
            severity=Severity.ERROR,
            confidence=_HIGH,
            message=(
                f"{len(clients)} active wireless client(s) lose SSID '{ssid}' coverage"
            ),
            subject=ObjectRef("wlan", headline, ssid),
            affected_entities=tuple(c.id for c in clients),
            caused_by=ctx.delta_index.causes("wlan", changed_ids),
            evidence={
                "ssid": ssid,
                "clients": [
                    {"mac": c.mac, "ap": c.attach_id, "ssid": c.ssid}
                    for c in sorted(clients, key=lambda c: c.id)
                ],
            },
        )

    def _unverified(
        self,
        ctx: CheckContext,
        affected: dict[str, tuple[str, ...]],
        *,
        reason: str,
        clients: Iterable[Client] = (),
    ) -> Finding:
        affected_ssids = tuple(sorted(affected))
        changed_ids = tuple(dict.fromkeys(id_ for ids in affected.values() for id_ in ids))
        subject = ObjectRef("wlan", changed_ids[0], affected_ssids[0]) if changed_ids else None
        listed_clients = tuple(sorted(clients, key=lambda c: c.id))
        return Finding(
            source=FindingSource.CHECK,
            category=FindingCategory.OPERATIONAL,
            code=f"{self.id}.unverified",
            severity=Severity.WARNING,
            confidence=_HIGH,
            message="WLAN coverage changed, but active wireless client impact is unverified",
            subject=subject,
            affected_entities=tuple(c.id for c in listed_clients),
            caused_by=ctx.delta_index.causes("wlan", changed_ids),
            evidence={
                "reason": reason,
                "affected_ssids": affected_ssids,
                "clients": [
                    {"mac": c.mac, "ap": c.attach_id, "ssid": c.ssid}
                    for c in listed_clients
                ],
            },
        )

    def run(self, ctx: CheckContext) -> CheckResult:
        affected = self._affected_ssids(ctx)
        findings: list[Finding] = []
        unverified: list[Finding] = []

        if affected:
            if not _clients_known(ctx):
                unverified.append(
                    self._unverified(ctx, affected, reason="client_telemetry_unavailable")
                )
            else:
                unknown_ssid_clients: list[Client] = []
                impacted_by_ssid: dict[str, list[Client]] = {ssid: [] for ssid in affected}
                for client in _active_wireless_clients(ctx.baseline.ir):
                    if client.ssid is None:
                        unknown_ssid_clients.append(client)
                        continue
                    if (
                        client.ssid in affected
                        and not self._has_survivor(ctx.proposed.ir, client.ssid, client.attach_id)
                    ):
                        impacted_by_ssid[client.ssid].append(client)
                for ssid, clients in sorted(impacted_by_ssid.items()):
                    if clients:
                        findings.append(self._coverage_lost(ctx, ssid, affected[ssid], clients))
                if not findings and unknown_ssid_clients:
                    unverified.append(
                        self._unverified(
                            ctx,
                            affected,
                            reason="unknown_client_ssid",
                            clients=unknown_ssid_clients,
                        )
                    )

        coverage = Coverage(
            state=CoverageState.COMPLETE,
            notes=(_CAVEAT,) if affected and not findings and not unverified else (),
        )
        if findings:
            return CheckResult(
                self.id, Status.FAIL, tuple(findings), coverage, _HIGH, "coverage lost"
            )
        if unverified:
            return CheckResult(
                self.id, Status.WARN, tuple(unverified), coverage, _HIGH, "client impact unverified"
            )
        return CheckResult(self.id, Status.PASS, (), coverage, _HIGH, "no WLAN client impact")
