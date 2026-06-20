"""wireless.wlan.duplicate_ssid — same SSID on 2+ enabled WLANs with overlapping
AP scope. Provable overlap only: both site; site + explicit-AP; or a shared
explicit ap_id. wxtag/mixed/unknown scope -> unverifiable -> PARTIAL note, not a
finding. Key = the overlapping WLAN-id pair (a pre-existing dup on A/B must not
mask a new dup on C/D). Delta-conditioned via run_delta_lint."""

from __future__ import annotations

from itertools import combinations

from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState
from digital_twin.checks.wired.config_lint import Violation, run_delta_lint, touched_ids
from digital_twin.contracts import ObjectRef, Severity
from digital_twin.ir import Capability, IRCapability, IRDiff, Wlan
from digital_twin.ir.model import IR


def _overlap(a: Wlan, b: Wlan) -> str:
    """'yes' | 'no' | 'unknown' — do these two WLANs cover a common AP?"""
    sa, sb = a.apply_to, b.apply_to
    if "wxtags" in (sa, sb) or None in (sa, sb):
        return "unknown"
    if sa == "site" and sb == "site":
        return "yes"
    if sa == "site" and sb == "aps":
        return "yes" if b.ap_ids else "no"
    if sb == "site" and sa == "aps":
        return "yes" if a.ap_ids else "no"
    if sa == "aps" and sb == "aps":
        return "yes" if set(a.ap_ids) & set(b.ap_ids) else "no"
    return "unknown"


class WlanDuplicateSsidCheck:
    id = "wireless.wlan.duplicate_ssid"
    title = "duplicate SSID on overlapping APs"
    domain = "wireless.wlan"
    default_severity = Severity.WARNING

    def requires(self) -> frozenset[Capability]:
        # applies_to ("wlan" touched) implies WLAN_CONFIG was earned — Wlan entities
        # exist only when the wlan fetch succeeded — so the no-capability path is
        # correct-by-construction unreachable (never a spurious INSUFFICIENT_DATA floor).
        return frozenset({IRCapability.WLAN_CONFIG})

    def applies_to(self, diff: IRDiff) -> bool:
        return diff.touches("wlan")

    def _groups(self, ir: IR) -> dict[str, list[Wlan]]:
        by_ssid: dict[str, list[Wlan]] = {}
        for w in ir.wlans:
            if w.enabled and w.ssid:
                by_ssid.setdefault(w.ssid, []).append(w)
        return {s: g for s, g in by_ssid.items() if len(g) >= 2}

    def _violations(self, ctx: CheckContext, ir: IR) -> list[Violation]:
        viols: list[Violation] = []
        for ssid, group in self._groups(ir).items():
            for a, b in combinations(sorted(group, key=lambda w: w.id), 2):
                if _overlap(a, b) == "yes":
                    pair = (a.id, b.id)
                    causes = tuple(
                        c for c in (ctx.delta_index.cause("wlan", a.id),
                                    ctx.delta_index.cause("wlan", b.id)) if c is not None
                    )
                    viols.append(
                        Violation(
                            key=pair,
                            subject=ObjectRef("wlan", a.id, ssid),
                            affected=pair,
                            summary=(
                                f"SSID '{ssid}' is broadcast by two overlapping WLANs "
                                f"({a.id}, {b.id})"
                            ),
                            evidence={"ssid": ssid, "wlans": list(pair)},
                            caused_by=causes,
                        )
                    )
        return viols

    def _unverifiable(self, ir: IR) -> list[tuple[str, str, str]]:
        """(ssid, a.id, b.id) for each pair whose overlap can't be verified."""
        out: list[tuple[str, str, str]] = []
        for ssid, group in self._groups(ir).items():
            for a, b in combinations(sorted(group, key=lambda w: w.id), 2):
                if _overlap(a, b) == "unknown":
                    out.append((ssid, a.id, b.id))
        return out

    def run(self, ctx: CheckContext) -> CheckResult:
        base = self._violations(ctx, ctx.baseline.ir)
        prop = self._violations(ctx, ctx.proposed.ir)
        # RELEVANCE-SCOPED: note an unverifiable duplicate only when one of its WLANs is
        # delta-touched, so a pre-existing wxtag duplicate never floors an unrelated change.
        touched = touched_ids(ctx.diff, "wlan")
        notes = tuple(dict.fromkeys(
            f"SSID '{ssid}' duplicated across WLANs with wxtag/unknown scope — overlap unverifiable"
            for ssid, a_id, b_id in self._unverifiable(ctx.proposed.ir)
            if a_id in touched or b_id in touched
        ))
        coverage = Coverage(
            state=CoverageState.PARTIAL if notes else CoverageState.COMPLETE, notes=notes,
        )
        return run_delta_lint(check_id=self.id, base=base, proposed=prop, coverage=coverage)
