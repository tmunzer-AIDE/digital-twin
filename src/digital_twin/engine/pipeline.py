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
from digital_twin.adapters.mist.ingest.switch import invalid_bridge_priority_findings
from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext
from digital_twin.checks.registry import CheckRegistry
from digital_twin.checks.wired import ALL_WIRED_CHECKS
from digital_twin.contracts import Finding, Rejection
from digital_twin.engine.run_context import RunContext
from digital_twin.ir import IRDiff, diff_ir
from digital_twin.providers.base import RawSiteState, SiteScope, StateMeta, StateProvider
from digital_twin.scope.derived_gate import check_derived
from digital_twin.scope.envelope import parse_change_plan
from digital_twin.scope.field_gate import screen_op
from digital_twin.scope.object_gate import check_objects
from digital_twin.verdict.decision import DecisionInputs
from digital_twin.verdict.state_meta import StateMetaView, build_state_meta
from digital_twin.verdict.verdict import Verdict, assemble

_EMPTY_DIFF = IRDiff((), (), ())


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

    def unknown(
        rejection: Rejection | None,
        *,
        l0_fatal: bool = False,
        baseline_unavailable: bool = False,
        state_meta: StateMetaView | None = None,
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

    # 1 — pre-fetch gates
    with trace.stage("scope.pre"):
        plan = parse_change_plan(plan_data)
        if isinstance(plan, Rejection):
            return unknown(plan)
        rejection = check_objects(plan)
        if rejection:
            return unknown(rejection)

    # 2 — (L0 moved into the per-op loop: Mist update semantics are root-level
    # merge, so `required`/conditional validation is only meaningful against
    # the EFFECTIVE object, which needs the fetched current state)

    # 3 — fetch
    with trace.stage("fetch"):
        assert plan.scope.site_id is not None  # object gate guaranteed it
        raw = provider.fetch_site(SiteScope(org_id=plan.scope.org_id, site_id=plan.scope.site_id))
        if not isinstance(raw, RawSiteState):
            # the FetchError still carries host/acquired_at/failures — agents
            # must see WHAT failed even when no baseline is usable
            return unknown(
                None,
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
                return unknown(
                    Rejection(
                        stage="apply",
                        reasons=(
                            f"ops[order={op.order}]: no {op.object_type} with id "
                            f"{op.object_id!r} in fetched state",
                        ),
                    ),
                    state_meta=state_meta,
                )
            conflicts = update_conflicts(op.payload)
            if conflicts:
                return unknown(
                    Rejection(
                        stage="apply",
                        reasons=tuple(
                            f"ops[order={op.order}]: conflicting set AND '-{c}' delete "
                            "marker for the same attribute"
                            for c in conflicts
                        ),
                    ),
                    state_meta=state_meta,
                )
            effective = effective_update(current, op.payload)
            result = adapter.validate(replace(op, payload=effective))
            adapter_findings += result.findings
            if result.fatal:
                return unknown(None, l0_fatal=True, state_meta=state_meta)
            rejection = screen_op(op.object_type, current, effective)
            if rejection:
                return unknown(rejection, state_meta=state_meta)
            applied = adapter.apply(proposed_raw, (op,))  # apply owns the semantics
            if isinstance(applied, Rejection):
                return unknown(applied, state_meta=state_meta)
            proposed_raw = applied

    # 5 — baseline ingest
    with trace.stage("ingest.baseline"):
        baseline = adapter.ingest(raw)
        if baseline.ir is None:
            return unknown(None, baseline_unavailable=True, state_meta=state_meta)

    # 7 — proposed ingest
    with trace.stage("ingest.proposed"):
        proposed = adapter.ingest(proposed_raw)
        if proposed.ir is None:
            return unknown(
                Rejection(
                    stage="ingest",
                    reasons=tuple(
                        f"proposed-state ingest failed: {f.ingester}: {f.error}"
                        for f in proposed.report.failures
                    ),
                ),
                state_meta=state_meta,
            )

    # 7b — dynamic-port honesty: vlan-defining definitions changed on a device
    # with dynamically-profiled ports whose RUNTIME usage could not be resolved
    # from observed LLDP -> WARNING (-> REVIEW). Resolved dynamic ports need no
    # gate: their impact is in the IR diff and the checks reason about it.
    with trace.stage("dynamic_gate"):
        adapter_findings += unresolved_dynamic_findings(
            baseline.device_effective, proposed.device_effective, raw.port_stats
        )
        # in-scope but uninterpretable bridge_priority (either side) — the
        # root election cannot be predicted -> WARNING, never silently
        # simulated as the platform default
        adapter_findings += tuple(
            invalid_bridge_priority_findings(
                baseline.device_effective, proposed.device_effective
            )
        )

    # 8 — derived-impact gate (site + every device effective)
    with trace.stage("derived_gate"):
        rejection = check_derived(baseline.site_effective, proposed.site_effective)
        if rejection:
            return unknown(rejection, state_meta=state_meta)
        for did in sorted(set(baseline.device_effective) | set(proposed.device_effective)):
            rejection = check_derived(
                baseline.device_effective.get(did, {}),
                proposed.device_effective.get(did, {}),
                artifact=f"device {did}",
            )
            if rejection:
                return unknown(rejection, state_meta=state_meta)

    # 9 — diff + checks
    with trace.stage("checks"):
        diff = diff_ir(baseline.ir, proposed.ir)
        results = registry.run_all(
            CheckContext(
                baseline=AnalysisContext(baseline.ir),
                proposed=AnalysisContext(proposed.ir),
                diff=diff,
            )
        )

    # 10 — verdict
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
