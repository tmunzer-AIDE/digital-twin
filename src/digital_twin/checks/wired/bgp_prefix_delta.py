"""routing.bgp.prefix_delta — explicit BGP export prefix changes."""

from __future__ import annotations

import ipaddress

from digital_twin.checks.base import (
    CheckContext,
    CheckResult,
    Coverage,
    CoverageState,
    status_from_findings,
)
from digital_twin.checks.wired.bgp_adjacency import is_established
from digital_twin.contracts import Finding, FindingCategory, FindingSource, ObjectRef, Severity
from digital_twin.ir import Capability, Confidence, ConfidenceLevel, IRCapability, IRDiff

_HIGH = Confidence(level=ConfidenceLevel.HIGH)


class BgpPrefixDeltaCheck:
    id = "routing.bgp.prefix_delta"
    title = "BGP advertised-prefix delta"
    domain = "routing.bgp"
    default_severity = Severity.WARNING

    def requires(self) -> frozenset[Capability]:
        return frozenset()

    def applies_to(self, diff: IRDiff) -> bool:
        return any(
            modified.ref.kind == "bgp_peer"
            and bool({"advertised_prefixes", "export_unresolved"} & set(modified.changed_fields))
            for modified in diff.modified
        )

    def run(self, ctx: CheckContext) -> CheckResult:
        base = {peer.id: peer for peer in ctx.baseline.ir.bgp_peers}
        prop = {peer.id: peer for peer in ctx.proposed.ir.bgp_peers}
        established = {
            (neighbor.device_id, neighbor.peer_ip)
            for neighbor in ctx.baseline.ir.bgp_neighbors
            if is_established(neighbor)
        }
        telemetry = IRCapability.BGP_TELEMETRY in ctx.baseline.ir.capabilities
        proposed_owners: dict[str, set[str]] = {}
        for peer in prop.values():
            if peer.disabled:
                continue
            for prefix in peer.advertised_prefixes:
                proposed_owners.setdefault(prefix, set()).add(peer.id)

        findings: list[Finding] = []
        notes: list[str] = []
        for modified in ctx.diff.modified:
            if modified.ref.kind != "bgp_peer" or not (
                {"advertised_prefixes", "export_unresolved"} & set(modified.changed_fields)
            ):
                continue
            before = base[modified.ref.id]
            after = prop[modified.ref.id]
            causes = ctx.delta_index.causes("bgp_peer", [modified.ref.id])
            if before.export_unresolved != after.export_unresolved:
                findings.append(
                    Finding(
                        source=FindingSource.CHECK,
                        category=FindingCategory.NETWORK,
                        code=f"{self.id}.policy_changed",
                        severity=Severity.WARNING,
                        confidence=_HIGH,
                        message=(
                            f"BGP export selector changed for {after.neighbor_ip}; the named/"
                            "opaque policy cannot be expanded into prefixes"
                        ),
                        subject=ObjectRef("device", after.device_id),
                        affected_entities=(after.neighbor_ip,),
                        evidence={
                            "device": after.device_id,
                            "neighbor_ip": after.neighbor_ip,
                            "baseline_export": before.export_unresolved,
                            "proposed_export": after.export_unresolved,
                            "vrf": "unmodeled",
                        },
                        caused_by=causes,
                    )
                )
                notes.append(
                    f"BGP export policy for {after.id} is opaque; exact prefix impact is partial"
                )
            removed = sorted(set(before.advertised_prefixes) - set(after.advertised_prefixes))
            added = sorted(set(after.advertised_prefixes) - set(before.advertised_prefixes))
            for prefix in removed:
                sole = not proposed_owners.get(prefix)
                was_established = (before.device_id, before.neighbor_ip) in established
                unsafe = sole and telemetry and was_established
                findings.append(
                    Finding(
                        source=FindingSource.CHECK,
                        category=FindingCategory.NETWORK,
                        code=f"{self.id}.withdrawn{'_sole' if sole else ''}",
                        severity=Severity.ERROR if unsafe else Severity.WARNING,
                        confidence=_HIGH,
                        message=(
                            f"BGP prefix {prefix} is withdrawn from {before.neighbor_ip}"
                            + (" and has no remaining modeled advertisement" if sole else "")
                        ),
                        subject=ObjectRef("device", before.device_id),
                        affected_entities=(prefix, before.neighbor_ip),
                        evidence={
                            "prefix": prefix,
                            "neighbor_ip": before.neighbor_ip,
                            "device": before.device_id,
                            "sole_modeled_advertisement": sole,
                            "baseline_established": was_established if telemetry else None,
                            "vrf": "unmodeled",
                        },
                        caused_by=causes,
                    )
                )
            existing = {
                prefix
                for peer in base.values()
                if peer.id != before.id and not peer.disabled
                for prefix in peer.advertised_prefixes
            }
            for prefix in added:
                network = ipaddress.ip_network(prefix)
                overlaps = sorted(
                    candidate
                    for candidate in existing
                    if ipaddress.ip_network(candidate).version == network.version
                    and ipaddress.ip_network(candidate).overlaps(network)
                )
                findings.append(
                    Finding(
                        source=FindingSource.CHECK,
                        category=FindingCategory.NETWORK,
                        code=(
                            f"{self.id}.overlap_added"
                            if overlaps
                            else f"{self.id}.advertised_added"
                        ),
                        severity=Severity.WARNING,
                        confidence=_HIGH,
                        message=(
                            f"BGP prefix {prefix} is added to exports toward {after.neighbor_ip}"
                            + (f" and overlaps {', '.join(overlaps)}" if overlaps else "")
                        ),
                        subject=ObjectRef("device", after.device_id),
                        affected_entities=(prefix, after.neighbor_ip),
                        evidence={
                            "prefix": prefix,
                            "neighbor_ip": after.neighbor_ip,
                            "device": after.device_id,
                            "overlapping_prefixes": overlaps,
                            "vrf": "unmodeled",
                        },
                        caused_by=causes,
                    )
                )
        if findings and not telemetry:
            notes.append("BGP neighbor telemetry is unavailable; sole withdrawals cannot escalate")
        if findings:
            notes.append("BGP VRF placement is not modeled for export selectors")
        return CheckResult(
            check_id=self.id,
            status=status_from_findings(findings),
            findings=tuple(findings),
            coverage=Coverage(
                CoverageState.PARTIAL if notes else CoverageState.COMPLETE,
                tuple(dict.fromkeys(notes)),
            ),
            confidence=_HIGH,
            reasoning="compared explicit literal BGP export prefixes",
        )
