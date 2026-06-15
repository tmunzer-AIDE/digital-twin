"""The 10-stage simulation pipeline — ORCHESTRATION ONLY (spec diagram).

 1 ScopeResolver.pre   envelope + object gate (pre-fetch)        -> UNKNOWN
 3 StateProvider       fetch raw                                  -> UNKNOWN on total failure
 2+4 per op, vs the ROLLING pre-op state:
     effective object   Mist root-level update semantics (present roots replace,
                        omitted roots persist, "-attr" markers delete; conflicts
                        rejected)                                  -> UNKNOWN
     Adapter.validate   L0 on the EFFECTIVE object (fatal -> stop) -> UNKNOWN
     field gate         changed leaves vs allowlist (incl. role)   -> UNKNOWN
 5 Adapter.ingest      baseline (effective + IR)                  -> UNKNOWN if not ok
 6 Adapter.apply       per-op update on the rolling state          -> UNKNOWN on bad target
 7 Adapter.ingest      proposed                                   -> UNKNOWN if not ok
 8 derived gate        full effective config, site + per device   -> UNKNOWN
 9 diff + checks       registry (gating order, isolation)
10 verdict             DecisionInputs -> decision + assembly

L0 runs inside the loop (not pre-fetch as originally specced) because Mist
update semantics are a root-level merge: `required`/conditional validation is
only meaningful against the EFFECTIVE object, which needs the fetched state.
Every failure is a VALUE produced by the owning module; this file only maps
them into DecisionInputs and stops at the right stage. No business logic.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from digital_twin.adapters.mist.adapter import MistAdapter
from digital_twin.adapters.mist.apply import get_object
from digital_twin.adapters.mist.apply.objects import effective_update, update_conflicts
from digital_twin.adapters.mist.ingest.dynamic_usage import unresolved_dynamic_findings
from digital_twin.adapters.mist.ingest.switch import (
    invalid_bridge_priority_findings,
    unresolved_dhcp_range_findings,
)
from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext
from digital_twin.checks.registry import CheckRegistry
from digital_twin.checks.wired import ALL_WIRED_CHECKS
from digital_twin.contracts import Finding, Rejection
from digital_twin.engine.org_template import apply_template, override_template
from digital_twin.engine.run_context import RunContext
from digital_twin.ir import IRDiff, diff_ir
from digital_twin.providers.base import (
    OrgScope,
    OrgTemplateContext,
    RawSiteState,
    SiteScope,
    StateMeta,
    StateProvider,
)
from digital_twin.scope.allowlist import ORG_OBJECT_TYPES
from digital_twin.scope.derived_gate import check_derived
from digital_twin.scope.envelope import parse_change_plan
from digital_twin.scope.field_gate import screen_op
from digital_twin.scope.object_gate import check_objects
from digital_twin.verdict.decision import Decision, DecisionInputs
from digital_twin.verdict.org_verdict import OrgVerdict, decide_org
from digital_twin.verdict.state_meta import StateMetaView, build_state_meta
from digital_twin.verdict.verdict import Verdict, assemble

_EMPTY_DIFF = IRDiff((), (), ())


def _unknown(
    rejection: Rejection | None,
    *,
    adapter_findings: tuple[Finding, ...],
    run: RunContext,
    state_meta: StateMetaView | None = None,
    l0_fatal: bool = False,
    baseline_unavailable: bool = False,
) -> Verdict:
    return assemble(
        inputs=DecisionInputs(
            rejections=(rejection,) if rejection else (),
            l0_fatal=l0_fatal,
            baseline_unavailable=baseline_unavailable,
            check_results=(),
            adapter_findings=adapter_findings,
        ),
        ir_diff=_EMPTY_DIFF,
        state_meta=state_meta,
        trace_ref=run.run_id,
    )


def _simulate_site_state(
    baseline_raw: RawSiteState,
    proposed_raw: RawSiteState,
    *,
    adapter: MistAdapter,
    registry: CheckRegistry,
    run: RunContext,
    state_meta: StateMetaView | None,
    adapter_findings: tuple[Finding, ...] = (),
) -> Verdict:
    """Stages 5-10 for ONE site: ingest baseline + proposed, dynamic gate,
    derived gate, diff + checks, verdict. Both `simulate` (single-site) and
    `simulate_org_template` (per assigned site) call this with pre-built
    baseline/proposed raw states — no fetch, no apply here."""
    trace = run.trace
    assert trace is not None  # RunContext.__post_init__ guarantees it

    with trace.stage("ingest.baseline"):
        baseline = adapter.ingest(baseline_raw)
        if baseline.ir is None:
            return _unknown(
                None, adapter_findings=adapter_findings, run=run,
                state_meta=state_meta, baseline_unavailable=True,
            )
    with trace.stage("ingest.proposed"):
        proposed = adapter.ingest(proposed_raw)
        if proposed.ir is None:
            return _unknown(
                Rejection(
                    stage="ingest",
                    reasons=tuple(
                        f"proposed-state ingest failed: {f.ingester}: {f.error}"
                        for f in proposed.report.failures
                    ),
                ),
                adapter_findings=adapter_findings, run=run, state_meta=state_meta,
            )
    with trace.stage("dynamic_gate"):
        adapter_findings += unresolved_dynamic_findings(
            baseline.device_effective, proposed.device_effective, proposed_raw.port_stats
        )
        adapter_findings += tuple(
            invalid_bridge_priority_findings(baseline.device_effective, proposed.device_effective)
        )
        adapter_findings += tuple(
            unresolved_dhcp_range_findings(baseline.site_effective, proposed.site_effective)
        )
    with trace.stage("derived_gate"):
        rejection = check_derived(baseline.site_effective, proposed.site_effective)
        if rejection:
            return _unknown(
                rejection, adapter_findings=adapter_findings, run=run, state_meta=state_meta
            )
        for did in sorted(set(baseline.device_effective) | set(proposed.device_effective)):
            rejection = check_derived(
                baseline.device_effective.get(did, {}),
                proposed.device_effective.get(did, {}),
                artifact=f"device {did}",
            )
            if rejection:
                return _unknown(
                    rejection, adapter_findings=adapter_findings, run=run, state_meta=state_meta
                )
    with trace.stage("checks"):
        diff = diff_ir(baseline.ir, proposed.ir)
        results = registry.run_all(
            CheckContext(
                baseline=AnalysisContext(baseline.ir),
                proposed=AnalysisContext(proposed.ir),
                diff=diff,
            )
        )
    with trace.stage("verdict"):
        return assemble(
            inputs=DecisionInputs(
                rejections=(),
                l0_fatal=False,
                baseline_unavailable=False,
                check_results=results,
                adapter_findings=adapter_findings,
            ),
            ir_diff=diff,
            state_meta=state_meta,
            trace_ref=run.run_id,
        )


def simulate(
    plan_data: Mapping[str, Any],
    *,
    provider: StateProvider,
    adapter: MistAdapter | None = None,
    registry: CheckRegistry | None = None,
    run: RunContext | None = None,
) -> Verdict:
    run = run or RunContext()
    trace = run.trace
    assert trace is not None  # RunContext.__post_init__ guarantees it
    adapter = adapter or MistAdapter()
    registry = registry or CheckRegistry(ALL_WIRED_CHECKS)
    adapter_findings: tuple[Finding, ...] = ()

    # 1 — pre-fetch gates
    with trace.stage("scope.pre"):
        plan = parse_change_plan(plan_data)
        if isinstance(plan, Rejection):
            return _unknown(plan, adapter_findings=adapter_findings, run=run)
        rejection = check_objects(plan)
        if rejection:
            return _unknown(rejection, adapter_findings=adapter_findings, run=run)

    # 2 — (L0 moved into the per-op loop: Mist update semantics are root-level
    # merge, so `required`/conditional validation is only meaningful against
    # the EFFECTIVE object, which needs the fetched current state)

    # 3 — fetch
    with trace.stage("fetch"):
        if plan.scope.site_id is None:  # an ORG (template) plan reached single-site simulate
            return _unknown(
                Rejection(
                    stage="scope.pre",
                    reasons=(
                        "org/template plan has no site_id"
                        " — call simulate_org_template, not simulate",
                    ),
                ),
                adapter_findings=adapter_findings, run=run,
            )
        raw = provider.fetch_site(SiteScope(org_id=plan.scope.org_id, site_id=plan.scope.site_id))
        if not isinstance(raw, RawSiteState):
            # the FetchError still carries host/acquired_at/failures — agents
            # must see WHAT failed even when no baseline is usable
            return _unknown(
                None,
                adapter_findings=adapter_findings,
                run=run,
                baseline_unavailable=True,
                state_meta=build_state_meta(
                    StateMeta(
                        acquired_at=raw.acquired_at,
                        host=raw.host,
                        fetched=(),
                        failures=raw.failures,
                    ),
                    now=datetime.now(UTC),
                ),
            )
    state_meta = build_state_meta(raw.meta, now=datetime.now(UTC))

    # 2+4+6 — per op against the ROLLING pre-op state: compute the EFFECTIVE
    # object (Mist root-level update semantics: present roots replace, omitted
    # roots persist, "-attr" deletes), L0-validate it, field-gate it, apply.
    proposed_raw = raw
    with trace.stage("l0+scope.post+apply", note=f"{len(plan.ops)} op(s)"):
        for op in sorted(plan.ops, key=lambda o: o.order):
            current = get_object(proposed_raw, op.object_type, op.object_id)
            if current is None:
                return _unknown(
                    Rejection(
                        stage="apply",
                        reasons=(
                            f"ops[order={op.order}]: no {op.object_type} with id "
                            f"{op.object_id!r} in fetched state",
                        ),
                    ),
                    adapter_findings=adapter_findings,
                    run=run,
                    state_meta=state_meta,
                )
            conflicts = update_conflicts(op.payload)
            if conflicts:
                return _unknown(
                    Rejection(
                        stage="apply",
                        reasons=tuple(
                            f"ops[order={op.order}]: conflicting set AND '-{c}' delete "
                            "marker for the same attribute"
                            for c in conflicts
                        ),
                    ),
                    adapter_findings=adapter_findings,
                    run=run,
                    state_meta=state_meta,
                )
            effective = effective_update(current, op.payload)
            result = adapter.validate(replace(op, payload=effective))
            adapter_findings += result.findings
            if result.fatal:
                return _unknown(
                    None,
                    adapter_findings=adapter_findings,
                    run=run,
                    l0_fatal=True,
                    state_meta=state_meta,
                )
            rejection = screen_op(op.object_type, current, effective)
            if rejection:
                return _unknown(
                    rejection,
                    adapter_findings=adapter_findings,
                    run=run,
                    state_meta=state_meta,
                )
            applied = adapter.apply(proposed_raw, (op,))  # apply owns the semantics
            if isinstance(applied, Rejection):
                return _unknown(
                    applied,
                    adapter_findings=adapter_findings,
                    run=run,
                    state_meta=state_meta,
                )
            proposed_raw = applied

    return _simulate_site_state(
        raw, proposed_raw,
        adapter=adapter, registry=registry, run=run,
        state_meta=state_meta, adapter_findings=adapter_findings,
    )


def simulate_org_template(
    plan_data: Mapping[str, Any],
    *,
    provider: StateProvider,
    adapter: MistAdapter | None = None,
    registry: CheckRegistry | None = None,
    run: RunContext | None = None,
) -> OrgVerdict:
    run = run or RunContext()
    adapter = adapter or MistAdapter()
    registry = registry or CheckRegistry(ALL_WIRED_CHECKS)

    def org_unknown(
        rejections: tuple[Rejection, ...], *, template_findings: tuple[Finding, ...] = ()
    ) -> OrgVerdict:
        return OrgVerdict(
            decision=Decision.UNKNOWN,
            decision_reasons=tuple(f"[{r.stage}] {x}" for r in rejections for x in r.reasons),
            template_id=template_id, per_site={}, driving_sites=(), site_failures={},
            template_findings=tuple(template_findings), org_rejections=tuple(rejections),
        )

    plan = parse_change_plan(plan_data)
    # resolved after the ORG guard below; org_unknown() calls before that point
    # correctly surface "" (the template id is genuinely unknown that early)
    template_id = ""
    if isinstance(plan, Rejection):
        return org_unknown((plan,))
    rejection = check_objects(plan)
    if rejection:
        return org_unknown((rejection,))
    # ORG-mode guard (symmetric with simulate's SITE guard): a valid SITE plan
    # also passes check_objects — it must NOT be fanned out as a template.
    is_org = (
        bool(plan.ops)
        and all(op.object_type in ORG_OBJECT_TYPES for op in plan.ops)
        and not plan.scope.site_id
    )
    if not is_org:
        return org_unknown((Rejection(
            stage="scope.pre",
            reasons=("site-scoped plan: call simulate, not simulate_org_template",),
        ),))
    op = plan.ops[0]  # ORG mode guarantees exactly one networktemplate op
    template_id = op.object_id

    resolved = provider.resolve_org_template(OrgScope(org_id=plan.scope.org_id), template_id)
    if not isinstance(resolved, OrgTemplateContext):
        return org_unknown((Rejection(
            stage="fetch",
            reasons=tuple(
                f"org-template lookup failed: {f.object}: {f.error}" for f in resolved.failures
            )
            or ("org-template lookup failed",),
        ),))

    snapshot = dict(resolved.template)
    proposed_template = apply_template(snapshot, op.payload)
    if isinstance(proposed_template, Rejection):
        return org_unknown((proposed_template,))

    # org-level L0 — a FATAL violation short-circuits to org_rejections ONLY
    # (template_findings holds NON-fatal L0 only, per the spec's fatal-L0 rule)
    l0 = adapter.validate(replace(op, payload=proposed_template))
    if l0.fatal:
        return org_unknown(
            (Rejection(stage="l0", reasons=("structurally-fatal L0 on the proposed template",)),)
        )
    template_findings = tuple(l0.findings)
    # org-level field gate (no role check — networktemplate branch)
    fg = screen_op("networktemplate", snapshot, proposed_template)
    if fg:
        return org_unknown((fg,), template_findings=template_findings)

    if not resolved.assigned_site_ids:  # valid template, 0 sites
        decision, reasons, driving = decide_org(
            {}, template_findings=template_findings, org_rejections=()
        )
        return OrgVerdict(
            decision=decision, decision_reasons=reasons, template_id=template_id,
            per_site={}, driving_sites=driving, site_failures={},
            template_findings=template_findings, org_rejections=(),
        )

    raw_map = provider.fetch_sites(
        OrgScope(org_id=plan.scope.org_id), site_ids=resolved.assigned_site_ids
    )
    per_site: dict[str, Verdict] = {}
    site_failures: dict[str, str] = {}
    for sid in resolved.assigned_site_ids:
        fetched = raw_map.get(sid)
        if not isinstance(fetched, RawSiteState):
            failures = fetched.failures if fetched is not None else ()
            site_failures[sid] = "; ".join(
                f"{f.object}: {f.error}" for f in failures
            ) or "fetch failed"
            per_site[sid] = _unknown(
                None, adapter_findings=(), run=run, baseline_unavailable=True,
                state_meta=build_state_meta(
                    StateMeta(acquired_at=datetime.now(UTC), host=fetched.host if fetched else "",
                              fetched=(), failures=failures),
                    now=datetime.now(UTC),
                ),
            )
            continue
        base_raw, prop_raw = override_template(fetched, snapshot, proposed_template)
        sm = build_state_meta(fetched.meta, now=datetime.now(UTC))
        # adapter_findings=() — template L0 findings live ONLY on OrgVerdict
        # .template_findings (the rollup floors REVIEW on them via decide_org);
        # do NOT echo them into every per-site Verdict.
        per_site[sid] = _simulate_site_state(
            base_raw, prop_raw, adapter=adapter, registry=registry, run=run,
            state_meta=sm, adapter_findings=(),
        )

    decision, reasons, driving = decide_org(
        per_site, template_findings=template_findings, org_rejections=()
    )
    return OrgVerdict(
        decision=decision, decision_reasons=reasons, template_id=template_id,
        per_site=per_site, driving_sites=driving, site_failures=site_failures,
        template_findings=template_findings, org_rejections=(),
    )
