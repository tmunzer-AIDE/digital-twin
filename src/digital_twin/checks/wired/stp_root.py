"""wired.stp.root_change — the delta moves a component's predicted root bridge.

A root re-election reconverges spanning tree across the whole L2 component:
blocked ports re-elect, paths flap, traffic pauses. Worth a REVIEW even when
intentional. Election model: single-instance RSTP — root = lowest
(bridge_priority, mac) among the component's SWITCHES; a device without
explicit `stp_config.bridge_priority` runs the platform default 32768, which
is an ASSUMPTION (caps the claim at MEDIUM). Per-vlan VSTP is out of scope.

Components come from the L2 graph (VC-folded, disabled links dropped). Each
PROPOSED component is compared against every baseline component it shares a
node with — a merge/split that re-roots part of the network is a root change
for that part.
"""

from __future__ import annotations

import networkx as nx

from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState, Status
from digital_twin.contracts import Finding, FindingCategory, FindingSource, Severity
from digital_twin.ir import (
    Capability,
    Confidence,
    ConfidenceLevel,
    IRCapability,
    IRDiff,
    min_confidence,
)
from digital_twin.ir.entities import DeviceRole
from digital_twin.ir.model import IR

_DEFAULT_PRIORITY = 32768
_HIGH = Confidence(level=ConfidenceLevel.HIGH)
_ASSUMED_DEFAULT = Confidence(
    level=ConfidenceLevel.MEDIUM,
    reasons=("bridge priority not explicit — platform default 32768 assumed",),
)


def _root_of(ir: IR, component: frozenset[str]) -> tuple[str, bool] | None:
    """(root device id, any-default-assumed) for the component's switches —
    None when fewer than two switches (no election to disturb)."""
    switches = [d for d in component if ir.devices[d].role is DeviceRole.SWITCH]
    if len(switches) < 2:
        return None
    if any(ir.devices[d].stp_priority_invalid for d in switches):
        # an uninterpretable priority in the component: the election cannot be
        # predicted — ABSTAIN rather than simulate the default; the adapter
        # finding scope.stp.bridge_priority_invalid owns the REVIEW
        return None
    assumed = any(ir.devices[d].stp_priority is None for d in switches)

    def election_key(d: str) -> tuple[int, str]:
        prio = ir.devices[d].stp_priority
        # explicit `is None`: 0 is a VALID priority — the strongest one
        return (_DEFAULT_PRIORITY if prio is None else prio, d)

    return min(switches, key=election_key), assumed


class StpRootChangeCheck:
    id = "wired.stp.root_change"
    title = "root bridge moves — spanning tree reconverges"
    domain = "wired.stp"
    default_severity = Severity.WARNING

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2})

    def applies_to(self, diff: IRDiff) -> bool:
        return any(diff.touches(k) for k in ("device", "link", "port"))

    def run(self, ctx: CheckContext) -> CheckResult:
        base_ir, prop_ir = ctx.baseline.ir, ctx.proposed.ir
        base_comps = [frozenset(c) for c in nx.connected_components(ctx.baseline.l2_graph())]
        prop_comps = [frozenset(c) for c in nx.connected_components(ctx.proposed.l2_graph())]
        findings: list[Finding] = []
        for comp in prop_comps:
            elected = _root_of(prop_ir, comp)
            if elected is None:
                continue
            prop_root, prop_assumed = elected
            for base_comp in base_comps:
                if not (comp & base_comp):
                    continue
                base_elected = _root_of(base_ir, base_comp)
                if base_elected is None:
                    continue
                base_root, base_assumed = base_elected
                if base_root == prop_root:
                    continue
                confidence = (
                    min_confidence(_HIGH, _ASSUMED_DEFAULT)
                    if (prop_assumed or base_assumed)
                    else _HIGH
                )
                findings.append(
                    Finding(
                        source=FindingSource.CHECK,
                        category=FindingCategory.NETWORK,
                        code=f"{self.id}.moved",
                        severity=Severity.WARNING,
                        confidence=confidence,
                        message=(
                            f"the predicted STP root bridge moves from {base_root} to "
                            f"{prop_root} — spanning tree reconverges across the "
                            f"component ({len(comp)} devices); paths re-form"
                        ),
                        affected_entities=(base_root, prop_root),
                        evidence={
                            "baseline_root": base_root,
                            "proposed_root": prop_root,
                            "component_devices": len(comp),
                        },
                    )
                )
        return CheckResult(
            check_id=self.id,
            status=Status.WARN if findings else Status.PASS,
            findings=tuple(findings),
            coverage=Coverage(state=CoverageState.COMPLETE),
            confidence=(
                min_confidence(*(f.confidence for f in findings)) if findings else _HIGH
            ),
            reasoning="elected the root bridge per L2 component, baseline vs proposed",
        )
