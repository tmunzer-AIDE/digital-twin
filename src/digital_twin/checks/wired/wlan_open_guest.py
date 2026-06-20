"""wireless.wlan.open_guest — an enabled open-auth WLAN with no client isolation.

Open auth + no isolation => any joined client can reach any other on the segment
(lateral traffic). Delta-conditioned: introduced => WARNING, pre-existing => INFO.
Scope-aware and never-false-positive: an explicit EMPTY AP scope applies nowhere
(silent); a wxtag-only / unknown scope is 'potentially active but unresolved'
(PARTIAL note, not a finding); unknown auth is skipped."""

from __future__ import annotations

from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState
from digital_twin.checks.wired.config_lint import Violation, run_delta_lint, touched_ids
from digital_twin.contracts import ObjectRef, Severity
from digital_twin.ir import Capability, IRCapability, IRDiff, Wlan
from digital_twin.ir.model import IR


def _active_scope(w: Wlan) -> str:
    """'active' | 'nowhere' | 'unresolved' — does this WLAN apply to any AP?"""
    if w.apply_to == "site":
        return "active"
    if w.apply_to == "aps":
        return "active" if w.ap_ids else "nowhere"
    # wxtags / None / unknown -> can't resolve membership
    return "unresolved"


class WlanOpenGuestCheck:
    id = "wireless.wlan.open_guest"
    title = "open guest WLAN without client isolation"
    domain = "wireless.wlan"
    default_severity = Severity.WARNING

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WLAN_CONFIG})

    def applies_to(self, diff: IRDiff) -> bool:
        return diff.touches("wlan")

    def _violations(self, ir: IR) -> list[Violation]:
        viols: list[Violation] = []
        for w in ir.wlans:
            if not w.enabled or w.auth_type != "open" or w.isolation:
                continue
            if _active_scope(w) != "active":  # nowhere -> silent; unresolved -> note in run()
                continue
            viols.append(
                Violation(
                    key=w.id,
                    subject=ObjectRef("wlan", w.id, w.ssid or None),
                    affected=(w.id,),
                    summary=(
                        f"open guest WLAN '{w.ssid}' has no client isolation — "
                        "joined clients can reach each other (lateral traffic)"
                    ),
                    evidence={"ssid": w.ssid, "auth_type": w.auth_type, "isolation": w.isolation},
                )
            )
        return viols

    def _unresolved(self, ir: IR) -> list[Wlan]:
        return [
            w for w in ir.wlans
            if w.enabled and w.auth_type == "open" and not w.isolation
            and _active_scope(w) == "unresolved"
        ]

    def run(self, ctx: CheckContext) -> CheckResult:
        base = self._violations(ctx.baseline.ir)
        prop = self._violations(ctx.proposed.ir)
        # RELEVANCE-SCOPED: note an unresolved open WLAN only when it is delta-touched,
        # so an unrelated old wxtag WLAN never floors an unrelated change to REVIEW.
        touched = touched_ids(ctx.diff, "wlan")
        notes = tuple(
            f"WLAN '{w.ssid}' is open without isolation but its AP scope "
            f"({w.apply_to}) is unresolved — potentially active"
            for w in self._unresolved(ctx.proposed.ir) if w.id in touched
        )
        coverage = Coverage(
            state=CoverageState.PARTIAL if notes else CoverageState.COMPLETE, notes=notes,
        )
        return run_delta_lint(check_id=self.id, base=base, proposed=prop, coverage=coverage)
