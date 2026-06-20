"""wired.l2.vlan_collision — one VLAN id claimed by 2+ distinct network names
(ambiguous L2 naming). Reads Vlan.collisions (the distinct OTHER names minted at
switch-ingest dedup). Delta-conditioned; the key carries the collision facts so a
changed set reads as introduced. Fully config-observable -> coverage COMPLETE."""

from __future__ import annotations

from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState
from digital_twin.checks.wired.config_lint import Violation, run_delta_lint
from digital_twin.contracts import ObjectRef, Severity
from digital_twin.ir import Capability, IRCapability, IRDiff
from digital_twin.ir.model import IR


class VlanCollisionCheck:
    id = "wired.l2.vlan_collision"
    title = "VLAN id with colliding names"
    domain = "wired.l2"
    default_severity = Severity.WARNING

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2})

    def applies_to(self, diff: IRDiff) -> bool:
        return diff.touches("vlan")

    def _violations(self, ctx: CheckContext, ir: IR) -> list[Violation]:
        viols: list[Violation] = []
        for v in ir.vlans.values():
            if not v.collisions:
                continue
            cause = ctx.delta_index.cause("vlan", str(v.vlan_id))
            viols.append(
                Violation(
                    key=(v.vlan_id, frozenset(v.collisions)),
                    subject=ObjectRef("vlan", str(v.vlan_id)),
                    affected=(str(v.vlan_id),),
                    summary=(
                        f"vlan {v.vlan_id} is named by multiple networks "
                        f"(also: {', '.join(v.collisions)})"
                    ),
                    evidence={"vlan_id": v.vlan_id, "collisions": list(v.collisions)},
                    caused_by=(cause,) if cause is not None else (),
                )
            )
        return viols

    def run(self, ctx: CheckContext) -> CheckResult:
        base = self._violations(ctx, ctx.baseline.ir)
        prop = self._violations(ctx, ctx.proposed.ir)
        return run_delta_lint(
            check_id=self.id, base=base, proposed=prop,
            coverage=Coverage(state=CoverageState.COMPLETE),
        )
