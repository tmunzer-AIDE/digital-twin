"""NAC rule shadowing — the conservative provable-superset core (GS34 spec §6)."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState, Status
from digital_twin.contracts import (
    Cause,
    Finding,
    FindingCategory,
    FindingSource,
    ObjectRef,
    Severity,
)
from digital_twin.ir import Confidence, ConfidenceLevel, IRDiff, NacRule


def is_provable(r: NacRule) -> bool:
    """Eligible for a shadowing proof: cleanly parsed, ordered, and constraining ONLY
    on {auth_types, port_types, match_tags}. One chokepoint — no dimension forgotten."""
    return (
        r.opaque_digest is None
        and r.order is not None
        and not r.not_matching
        and not (r.site_ids or r.sitegroup_ids or r.family or r.mfg
                 or r.model or r.os_type or r.vendor)
    )


def covers_choice(a: frozenset[str], b: frozenset[str]) -> bool:
    """auth_types / port_types — ∅ = any; A covers B iff A is unconstrained or B ⊆ A."""
    if not a:
        return True
    if not b:
        return False
    return b <= a


def covers_tags(a: frozenset[str], b: frozenset[str]) -> bool:
    """match_tags — CONSERVATIVE: A has no tag filter OR identical sets. NOT `a <= b`
    (that assumes tags AND, which is unconfirmed; a strict subset would false-positive)."""
    return (not a) or (a == b)


def A_covers_B(a: NacRule, b: NacRule) -> bool:  # noqa: N802 — matches spec name
    return (
        covers_choice(a.auth_types, b.auth_types)
        and covers_choice(a.port_types, b.port_types)
        and covers_tags(a.match_tags, b.match_tags)
    )


class ShadowStatus(StrEnum):
    TRUE = "true"
    FALSE = "false"
    INDETERMINATE = "indeterminate"


def shadow_status(a_id: str, b_id: str, state: Mapping[str, NacRule]) -> ShadowStatus:
    """Does A shadow B in `state`? The single definition attribution derives from.
    Ordering matters: disabled/order/absence short-circuit to FALSE even when the cover
    test is unevaluable."""
    a, b = state.get(a_id), state.get(b_id)
    if a is None or b is None:
        return ShadowStatus.FALSE          # absent (e.g. newly created)
    if not a.enabled or not b.enabled:
        return ShadowStatus.FALSE          # disabled never participates
    if a.order is not None and b.order is not None and a.order >= b.order:
        return ShadowStatus.FALSE          # A not strictly earlier than B
    if not is_provable(a) or not is_provable(b):
        return ShadowStatus.INDETERMINATE  # opaque / orderless / unmodeled criteria
    return ShadowStatus.TRUE if A_covers_B(a, b) else ShadowStatus.FALSE


def _changed_ids(diff: IRDiff) -> frozenset[str]:
    ids = {e.id for e in diff.added if e.kind == "nacrule"}
    ids |= {m.ref.id for m in diff.modified if m.ref.kind == "nacrule"}
    return frozenset(ids)


def _shadows(state: Mapping[str, NacRule]) -> list[tuple[str, str]]:
    """(shadowed_b, shadower_a) for every enabled provable B with the FIRST earlier
    enabled provable A that covers it. Deterministic (sorted by B.order then id)."""
    rules = sorted((r for r in state.values()),
                   key=lambda r: (r.order if r.order is not None else 1 << 30, r.id))
    out: list[tuple[str, str]] = []
    for i, b in enumerate(rules):
        if not (b.enabled and is_provable(b)):
            continue
        for a in rules[:i]:
            if shadow_status(a.id, b.id, state) is ShadowStatus.TRUE:
                out.append((b.id, a.id))
                break
    return out


class NacShadowingCheck:
    id = "nac.rule.shadowed"
    title = "NAC rule shadowed by an earlier rule"
    domain = "nac"
    default_severity = Severity.WARNING

    def requires(self) -> frozenset[object]:
        return frozenset()

    def applies_to(self, diff: IRDiff) -> bool:
        return diff.touches("nacrule")

    def run(self, ctx: CheckContext) -> CheckResult:
        base = {r.id: r for r in ctx.baseline.ir.nacrules}
        prop = {r.id: r for r in ctx.proposed.ir.nacrules}
        changed = _changed_ids(ctx.diff)
        changed_fields = {m.ref.id: m.changed_fields
                          for m in ctx.diff.modified if m.ref.kind == "nacrule"}
        findings: list[Finding] = []
        introduced = 0
        for b_id, a_id in _shadows(prop):
            baseline = shadow_status(a_id, b_id, base)
            if baseline is ShadowStatus.INDETERMINATE:
                continue  # cannot prove it is new — REVIEW comes from delta/operational
            new = baseline is ShadowStatus.FALSE
            a, b = prop[a_id], prop[b_id]
            code = "introduced" if new else "preexisting"
            # name the changed field(s) that introduced it — order/enabled/tags/etc.
            # (added rules carry () — they are wholly new, not a field change).
            causes = tuple(
                Cause(ref=ObjectRef("nacrule", r.id, r.name),
                      fields=changed_fields.get(r.id, ()))
                for r in (a, b) if r.id in changed
            ) if new else ()
            findings.append(Finding(
                source=FindingSource.CHECK,
                category=FindingCategory.NETWORK,
                code=f"{self.id}.{code}",
                severity=Severity.WARNING if new else Severity.INFO,
                confidence=Confidence(level=ConfidenceLevel.HIGH),
                message=(f"NAC rule {b.name or b.id!r} (order {b.order}) is unreachable — "
                         f"shadowed by earlier rule {a.name or a.id!r} (order {a.order})"
                         + ("" if new else " (pre-existing, unchanged by the delta — context)")),
                affected_entities=(b_id,),
                subject=ObjectRef("nacrule", b_id, b.name),
                evidence={"shadower": {"id": a_id, "name": a.name, "action": a.action},
                          "shadowed_action": b.action},
                caused_by=causes,
            ))
            introduced += 1 if new else 0
        return CheckResult(
            check_id=self.id,
            status=Status.WARN if introduced else Status.PASS,
            findings=tuple(findings),
            coverage=Coverage(state=CoverageState.COMPLETE),
            confidence=Confidence(level=ConfidenceLevel.HIGH),
            reasoning=f"{len(findings)} shadow(s); {introduced} introduced",
        )
