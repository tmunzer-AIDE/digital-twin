"""wired.l3.subnet_overlap — two different VLANs whose subnets overlap (address
ambiguity / routing confusion). Keys on the canonical parsed network (not the raw
string). Unresolved/unparseable subnets are skipped; their coverage note is
relevance-scoped (only when the skipped vlan is delta-touched). Delta-conditioned."""

from __future__ import annotations

import ipaddress
from itertools import combinations

from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState
from digital_twin.checks.wired.config_lint import Violation, run_delta_lint, touched_ids
from digital_twin.contracts import ObjectRef, Severity
from digital_twin.ir import Capability, IRCapability, IRDiff
from digital_twin.ir.entities import Vlan
from digital_twin.ir.model import IR

_Net = ipaddress.IPv4Network | ipaddress.IPv6Network


def _net(subnet: str | None) -> _Net | None:
    if not subnet:
        return None
    try:
        return ipaddress.ip_network(subnet, strict=False)
    except ValueError:
        return None


def _unusable(v: Vlan) -> bool:
    """The vlan declares a subnet we could NOT compare: templated/unresolved, OR a
    present-but-unparseable CIDR (both must be skipped AND can warrant a note)."""
    return v.subnet_unresolved or (bool(v.subnet) and _net(v.subnet) is None)


class SubnetOverlapCheck:
    id = "wired.l3.subnet_overlap"
    title = "overlapping subnets across VLANs"
    domain = "wired.l3"
    default_severity = Severity.WARNING

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2})

    def applies_to(self, diff: IRDiff) -> bool:
        return diff.touches("vlan")

    def _violations(self, ctx: CheckContext, ir: IR) -> list[Violation]:
        rows: list[tuple[int, _Net]] = []
        for v in ir.vlans.values():
            if v.subnet_unresolved:
                continue
            net = _net(v.subnet)
            if net is not None:
                rows.append((v.vlan_id, net))
        viols: list[Violation] = []
        for (va, na), (vb, nb) in combinations(rows, 2):
            if va == vb or na.version != nb.version:
                continue
            if na.overlaps(nb):
                key = frozenset({(va, str(na)), (vb, str(nb))})
                lo, hi = sorted((va, vb))
                causes = tuple(
                    c for c in (ctx.delta_index.cause("vlan", str(va)),
                                ctx.delta_index.cause("vlan", str(vb))) if c is not None
                )
                viols.append(
                    Violation(
                        key=key,
                        subject=ObjectRef("vlan", str(lo)),
                        affected=(str(lo), str(hi)),
                        summary=f"vlan {va} subnet {na} overlaps vlan {vb} subnet {nb}",
                        evidence={"a": [va, str(na)], "b": [vb, str(nb)]},
                        caused_by=causes,
                    )
                )
        return viols

    def run(self, ctx: CheckContext) -> CheckResult:
        base = self._violations(ctx, ctx.baseline.ir)
        prop = self._violations(ctx, ctx.proposed.ir)
        # RELEVANCE-SCOPED note: only when a DELTA-TOUCHED vlan has an unusable subnet
        # (unresolved OR unparseable) — an untouched bad subnet never floors to REVIEW.
        touched = touched_ids(ctx.diff, "vlan")
        notes = tuple(
            f"vlan {v.vlan_id} subnet {v.subnet!r} could not be compared (unresolved/unparseable)"
            for v in ctx.proposed.ir.vlans.values()
            if str(v.vlan_id) in touched and _unusable(v)
        )
        coverage = Coverage(
            state=CoverageState.PARTIAL if notes else CoverageState.COMPLETE, notes=notes
        )
        return run_delta_lint(check_id=self.id, base=base, proposed=prop, coverage=coverage)
