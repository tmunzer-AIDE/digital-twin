"""wired.l2.vlan_segmentation — structural broadcast-domain change, no intent.

Per VLAN, compares the per-VLAN graph partition between IR and IR':
- SPLIT (a baseline component fragments into >=2 proposed components) ->
  PASS + INFO finding, HIGH confidence. A split is reported as INFO context —
  the real harm (a populated piece losing its exit) is carried by
  `blackhole`/`isolation`.
- expansion/contraction (node set grows/shrinks without a split) -> PASS +
  INFO finding, HIGH confidence.
Deliberately does NOT judge whether the change is allowed (that needs intent).
Distinct from blackhole: segmentation = shape changed; blackhole = lost exit.
"""

from __future__ import annotations

from digital_twin.analysis.delta_cause import causes_for_vlan_split
from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState, Status
from digital_twin.contracts import (
    Cause,
    Finding,
    FindingCategory,
    FindingSource,
    ObjectRef,
    Severity,
)
from digital_twin.ir import Capability, Confidence, ConfidenceLevel, IRCapability, IRDiff

_HIGH = Confidence(level=ConfidenceLevel.HIGH)


class L2VlanSegmentationCheck:
    id = "wired.l2.vlan_segmentation"
    title = "Broadcast-domain shape change"
    domain = "wired.l2"
    default_severity = Severity.INFO

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2})

    def applies_to(self, diff: IRDiff) -> bool:
        return any(diff.touches(k) for k in ("link", "port", "vlan", "device"))

    def run(self, ctx: CheckContext) -> CheckResult:
        findings: list[Finding] = []
        status = Status.PASS
        for vid in sorted(set(ctx.baseline.ir.vlans) | set(ctx.proposed.ir.vlans)):
            base_comps = [set(c.nodes) for c in ctx.baseline.vlan_components(vid)]
            prop_comps = [set(c.nodes) for c in ctx.proposed.vlan_components(vid)]
            split = any(len([p for p in prop_comps if p & b]) >= 2 for b in base_comps)
            if split:
                findings.append(
                    self._finding(
                        code="wired.l2.vlan_segmentation.split",
                        severity=Severity.INFO,
                        message=f"vlan {vid}: broadcast domain partitioned by the delta",
                        vid=vid,
                        base=base_comps,
                        prop=prop_comps,
                        caused_by=causes_for_vlan_split(ctx, vid),
                    )
                )
                continue
            base_nodes = set().union(*base_comps) if base_comps else set()
            prop_nodes = set().union(*prop_comps) if prop_comps else set()
            if base_nodes != prop_nodes:
                grew, shrank = prop_nodes - base_nodes, base_nodes - prop_nodes
                findings.append(
                    self._finding(
                        code="wired.l2.vlan_segmentation.reshape",
                        severity=Severity.INFO,
                        message=(
                            f"vlan {vid}: domain "
                            f"{'expands to ' + str(sorted(grew)) if grew else ''}"
                            f"{' and ' if grew and shrank else ''}"
                            f"{'stops reaching ' + str(sorted(shrank)) if shrank else ''}"
                        ),
                        vid=vid,
                        base=base_comps,
                        prop=prop_comps,
                    )
                )
        return CheckResult(
            check_id=self.id,
            status=status,
            findings=tuple(findings),
            coverage=Coverage(state=CoverageState.COMPLETE),
            confidence=_HIGH,
            reasoning="compared per-vlan graph partitions baseline vs proposed",
        )

    def _finding(
        self,
        *,
        code: str,
        severity: Severity,
        message: str,
        vid: int,
        base: list[set[str]],
        prop: list[set[str]],
        caused_by: tuple[Cause, ...] = (),
    ) -> Finding:
        return Finding(
            source=FindingSource.CHECK,
            category=FindingCategory.NETWORK,
            code=code,
            severity=severity,
            confidence=_HIGH,
            message=message,
            subject=ObjectRef("vlan", str(vid)),
            evidence={
                "vlan": vid,
                "baseline_components": [sorted(c) for c in base],
                "proposed_components": [sorted(c) for c in prop],
            },
            caused_by=caused_by,
        )
