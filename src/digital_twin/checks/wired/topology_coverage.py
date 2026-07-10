"""Relevance-scoped guard for unavailable physical-topology observations.

Most wired checks can still produce useful findings from configuration when
Mist's port/device statistics are unavailable. This guard prevents the subset
of topology-dependent deltas from being certified SAFE against an empty graph,
without flooring independent auth or MAC-limit changes to REVIEW.
"""

from __future__ import annotations

from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState, Status
from digital_twin.contracts import Severity
from digital_twin.ir import Capability, Confidence, ConfidenceLevel, IRCapability, IRDiff

_TOPOLOGY_PORT_FIELDS = frozenset(
    {
        "mode",
        "native_vlan",
        "tagged_vlans",
        "voice_vlan",
        "speed",
        "duplex",
        "autoneg_disabled",
        "mtu",
        "poe",
        "disabled",
        "stp_edge",
        "bpdu_filter",
        "dhcp_trusted",
    }
)


class TopologyCoverageCheck:
    id = "wired.l2.topology_coverage"
    title = "Physical topology observations available"
    domain = "wired.l2"
    default_severity = Severity.WARNING

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.L2_TOPOLOGY})

    def applies_to(self, diff: IRDiff) -> bool:
        topology_kinds = {"device", "link", "l3intf"}
        if any(ref.kind in topology_kinds | {"port"} for ref in (*diff.added, *diff.removed)):
            return True
        return any(
            modified.ref.kind in topology_kinds
            or (
                modified.ref.kind == "port"
                and bool(_TOPOLOGY_PORT_FIELDS & set(modified.changed_fields))
            )
            for modified in diff.modified
        )

    def run(self, ctx: CheckContext) -> CheckResult:
        return CheckResult(
            check_id=self.id,
            status=Status.PASS,
            findings=(),
            coverage=Coverage(state=CoverageState.COMPLETE),
            confidence=Confidence(level=ConfidenceLevel.HIGH),
            reasoning="port and device topology observations were fetched successfully",
        )


__all__ = ["TopologyCoverageCheck"]
