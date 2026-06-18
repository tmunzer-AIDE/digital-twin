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

from digital_twin.adapters.mist.adapter import IngestOutcome, MistAdapter
from digital_twin.adapters.mist.apply import get_object
from digital_twin.adapters.mist.apply.objects import effective_update, update_conflicts
from digital_twin.adapters.mist.ingest.dynamic_usage import unresolved_dynamic_findings
from digital_twin.adapters.mist.ingest.switch import (
    invalid_bridge_priority_findings,
    unresolved_dhcp_range_findings,
)
from digital_twin.analysis.context import AnalysisContext
from digital_twin.analysis.delta_cause import delta_index
from digital_twin.checks.base import CheckContext
from digital_twin.checks.registry import CheckRegistry
from digital_twin.checks.wired import ALL_WIRED_CHECKS
from digital_twin.contracts import Finding, ObjectRef, Rejection
from digital_twin.engine.org_overlay import OrgOverlay, affected_sites, apply_overlays
from digital_twin.engine.org_template import apply_template
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
from digital_twin.scope.allowlist import GATEWAY_EFFECTIVE_ALLOWLIST, ORG_OBJECT_TYPES
from digital_twin.scope.derived_gate import check_derived
from digital_twin.scope.device_profile_gate import device_profile_rejection
from digital_twin.scope.envelope import parse_change_plan
from digital_twin.scope.field_gate import screen_op
from digital_twin.scope.object_gate import check_objects
from digital_twin.verdict.decision import Decision, DecisionInputs
from digital_twin.verdict.org_verdict import OrgChange, OrgVerdict, decide_org
from digital_twin.verdict.state_meta import StateMetaView, build_state_meta
from digital_twin.verdict.verdict import Verdict, assemble

_EMPTY_DIFF = IRDiff((), (), ())

# Gateway roots screened by the derived gate when the edit is NOT a
# gatewaytemplate op (i.e. for sitetemplate/site_setting edits the switch
# derived gate already owns `networks` via site_effective, so projecting it
# here would false-UNKNOWN those edits). For gatewaytemplate edits the FULL
# effective is screened (full=True) to catch networks changes owned by the
# gateway namespace that never appear in site_effective.
GATEWAY_SCREENED_ROOTS: tuple[str, ...] = ("port_config", "ip_configs", "dhcpd_config", "vars")


def _gw_screen_view(eff: dict[str, Any], *, full: bool) -> dict[str, Any]:
    # SOURCE-AWARE. For a gatewaytemplate edit, screen the FULL effective:
    # gatewaytemplate's OWN networks (or a vars edit rippling into networks) is NOT
    # in site_effective, so the switch derived gate never sees it -> dropping it
    # here would resolve a gatewaytemplate networks change SAFE (false-SAFE). For a
    # sitetemplate/site_setting edit, project to the gateway-consumed roots: a
    # networks change there IS in site_effective and the switch gate owns it (the
    # gateway namespace is org_networks), so screening it here would false-UNKNOWN.
    return eff if full else {k: eff[k] for k in GATEWAY_SCREENED_ROOTS if k in eff}


# Gateway-NAMESPACE roots the sitetemplate fold leaks into site_effective
# (merge_site_effective folds the FULL sitetemplate, and fold_layers preserves
# unknown roots). They are NOT switch/site roots — switch L3 is `other_ip_configs`
# and switch ports are device-level `port_config` (screened in the device_effective
# gate, not the site one) — so the switch/site derived gate must screen them OUT, or
# a gateway-only sitetemplate edit (e.g. ip_configs.*.ip) false-UNKNOWNs against the
# switch EFFECTIVE_ALLOWLIST. They ARE screened by the gateway derived gate on
# gateway_effective (and are inert when the site has no gateway). dhcpd_config/vars
# are deliberately excluded: per spec they are genuinely shared site roots the
# switch/site gate must keep screening.
_GATEWAY_ONLY_SITE_ROOTS: tuple[str, ...] = ("port_config", "ip_configs")


def _site_screen_view(eff: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in eff.items() if k not in _GATEWAY_ONLY_SITE_ROOTS}


def _stamp(findings: tuple[Finding, ...], subject: ObjectRef) -> tuple[Finding, ...]:
    """Attach the headline object to every L0 finding so the verdict says WHICH
    object (and the existing evidence path says which attribute)."""
    return tuple(replace(f, subject=subject) for f in findings)


def _changed_roots(payload: Mapping[str, Any]) -> frozenset[str]:
    """Top-level roots the op actually SETS — the only roots Mist processes (and
    thus re-validates) on a root-level-merge PUT. Dash-delete markers ('-attr')
    remove a root from the effective object, so they can never produce a
    violation and are excluded. This is the default L0 scope: it keeps L0 from
    flagging stale committed-OAS types on persisted roots the change never
    touched (which Mist already accepted)."""
    return frozenset(k for k in payload if not k.startswith("-"))


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
    gateway_screen_full: bool = False,
    profile_proposed: IngestOutcome | None = None,
) -> Verdict:
    """Stages 5-10 for ONE site: ingest baseline + proposed, dynamic gate,
    derived gate, diff + checks, verdict. Both `simulate` (single-site) and
    `simulate_org_template` (per assigned site) call this with pre-built
    baseline/proposed raw states — no fetch, no apply here."""
    trace = run.trace
    assert trace is not None  # RunContext.__post_init__ guarantees it

    with trace.stage("ingest.baseline"):
        # A compile/ingest CRASH (e.g. an unresolvable {{var}} on a gateway) is an
        # UNKNOWN, never a hard crash — and never a false-SAFE. Critical for the org
        # fan-out, which simulates many assigned sites: one site whose baseline does
        # not compile must not take down the whole org run (it becomes that site's
        # per-site UNKNOWN). Mirrors the `ir is None` path below.
        try:
            baseline = adapter.ingest(baseline_raw)
        except Exception as e:  # noqa: BLE001 — any ingest failure is UNKNOWN by the cardinal rule
            return _unknown(
                Rejection(stage="ingest", reasons=(f"baseline ingest crashed: {e}",)),
                adapter_findings=adapter_findings, run=run,
                state_meta=state_meta, baseline_unavailable=True,
            )
        if baseline.ir is None:
            return _unknown(
                None, adapter_findings=adapter_findings, run=run,
                state_meta=state_meta, baseline_unavailable=True,
            )
    with trace.stage("ingest.proposed"):
        try:
            proposed = adapter.ingest(proposed_raw)
        except Exception as e:  # noqa: BLE001
            return _unknown(
                Rejection(stage="ingest", reasons=(f"proposed ingest crashed: {e}",)),
                adapter_findings=adapter_findings, run=run, state_meta=state_meta,
            )
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
        rejection = check_derived(
            _site_screen_view(baseline.site_effective), _site_screen_view(proposed.site_effective)
        )
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
        for did in sorted(set(baseline.gateway_effective) | set(proposed.gateway_effective)):
            rejection = check_derived(
                _gw_screen_view(baseline.gateway_effective.get(did, {}), full=gateway_screen_full),
                _gw_screen_view(proposed.gateway_effective.get(did, {}), full=gateway_screen_full),
                allowlist=GATEWAY_EFFECTIVE_ALLOWLIST,
                artifact=f"gateway {did}",
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
                delta_index=delta_index(diff),
            )
        )
    profile_outcome = profile_proposed if profile_proposed is not None else proposed
    dp_rej = device_profile_rejection(
        proposed_raw.devices,
        {**baseline.device_effective, **baseline.gateway_effective},
        {**profile_outcome.device_effective, **profile_outcome.gateway_effective},
    )
    with trace.stage("verdict"):
        return assemble(
            inputs=DecisionInputs(
                rejections=(dp_rej,) if dp_rej else (),
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
    l0_full_object: bool = False,
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
            result = adapter.validate(
                replace(op, payload=effective),
                scope_roots=None if l0_full_object else _changed_roots(op.payload),
            )
            subject = ObjectRef(op.object_type, op.object_id, name=current.get("name"))
            adapter_findings += _stamp(result.findings, subject)
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

    # Build the below-profile proposed: apply ONLY the non-device ops against the
    # baseline (raw), so device-level changes (above the profile) are excluded.
    # This lets the gate diff baseline vs below-profile: if the only changes are
    # device ops, the diff is empty -> gate passes (pure-device plan is safe).
    non_device_ops = tuple(
        op for op in plan.ops if op.object_type != "device"
    )
    if len(non_device_ops) == len(plan.ops):
        # No device ops -> below-profile == proposed_raw; skip the extra ingest.
        profile_proposed = None
    else:
        below_raw = adapter.apply(raw, non_device_ops)
        # This ingest runs BEFORE _simulate_site_state's crash guard, so it needs
        # its own: a compile/ingest CRASH (e.g. an unresolvable gateway {{var}} in
        # the baseline) is UNKNOWN by the cardinal rule, never a hard crash. Both
        # inputs are baseline-derived (raw / baseline+non-device ops), so the
        # baseline is what failed -> baseline_unavailable, mirroring the guard below.
        try:
            if isinstance(below_raw, Rejection):
                # If the non-device apply fails (e.g. missing target), fall back to
                # baseline so the gate sees no below-profile change (conservative:
                # the apply failure was already caught for the full op set above).
                profile_proposed = adapter.ingest(raw)
            else:
                profile_proposed = adapter.ingest(below_raw)
        except Exception as e:  # noqa: BLE001 — any ingest crash is UNKNOWN
            return _unknown(
                Rejection(stage="ingest", reasons=(f"baseline ingest crashed: {e}",)),
                adapter_findings=adapter_findings, run=run,
                state_meta=state_meta, baseline_unavailable=True,
            )

    return _simulate_site_state(
        raw, proposed_raw,
        adapter=adapter, registry=registry, run=run,
        state_meta=state_meta, adapter_findings=adapter_findings,
        profile_proposed=profile_proposed,
    )


def simulate_org_plan(
    plan_data: Mapping[str, Any],
    *,
    provider: StateProvider,
    adapter: MistAdapter | None = None,
    registry: CheckRegistry | None = None,
    run: RunContext | None = None,
    l0_full_object: bool = False,
) -> OrgVerdict:
    run = run or RunContext()
    adapter = adapter or MistAdapter()
    registry = registry or CheckRegistry(ALL_WIRED_CHECKS)

    def org_unknown(
        rejections: tuple[Rejection, ...], *, template_findings: tuple[Finding, ...] = (),
        changes: tuple[OrgChange, ...] = (),
    ) -> OrgVerdict:
        return OrgVerdict(
            decision=Decision.UNKNOWN,
            decision_reasons=tuple(f"[{r.stage}] {x}" for r in rejections for x in r.reasons),
            changes=tuple(changes), per_site={}, driving_sites=(), site_failures={},
            template_findings=tuple(template_findings), org_rejections=tuple(rejections),
        )

    plan = parse_change_plan(plan_data)
    if isinstance(plan, Rejection):
        return org_unknown((plan,))  # changes=() — no parsed ops
    is_org = (
        bool(plan.ops)
        and all(op.object_type in ORG_OBJECT_TYPES for op in plan.ops)
        and not plan.scope.site_id
    )
    # P2b: name EVERY op the plan touches UP FRONT (org-shaped plans), BEFORE
    # check_objects, so an object_gate UNKNOWN (non-empty delete payload /
    # unsupported action) AND every later short-circuit still names all attempted
    # objects. Names hydrate as ops resolve.
    changes = [
        OrgChange(ref=ObjectRef(op.object_type, op.object_id, name=None), action=op.action)
        for op in plan.ops
    ] if is_org else []
    rejection = check_objects(plan)
    if rejection:
        return org_unknown((rejection,), changes=tuple(changes))
    if not is_org:
        return org_unknown((Rejection(
            stage="scope.pre",
            reasons=("site-scoped plan: call simulate, not simulate_org_plan",),
        ),))

    org_scope = OrgScope(org_id=plan.scope.org_id)
    overlays: list[OrgOverlay] = []
    template_findings: list[Finding] = []
    for i, op in enumerate(plan.ops):
        resolved = provider.resolve_org_template(org_scope, op.object_id, op.object_type)
        # P3: thread template_findings through EVERY short-circuit so earlier ops'
        # non-fatal L0 findings stay auditable even if a LATER op fails.
        if not isinstance(resolved, OrgTemplateContext):
            return org_unknown((Rejection(stage="fetch", reasons=tuple(
                f"org-template lookup failed: {f.object}: {f.error}" for f in resolved.failures
            ) or ("org-template lookup failed",)),),
                template_findings=tuple(template_findings), changes=tuple(changes))
        snapshot = dict(resolved.template)
        ref = ObjectRef(op.object_type, op.object_id, name=snapshot.get("name"))
        changes[i] = OrgChange(ref=ref, action=op.action)  # hydrate the resolved name
        if op.action == "delete":
            proposed: Mapping[str, Any] | None = None
        else:
            proposed_t = apply_template(snapshot, op.payload)
            if isinstance(proposed_t, Rejection):
                return org_unknown((proposed_t,),
                    template_findings=tuple(template_findings), changes=tuple(changes))
            l0 = adapter.validate(replace(op, payload=proposed_t),
                scope_roots=None if l0_full_object else _changed_roots(op.payload))
            if l0.fatal:
                return org_unknown((Rejection(stage="l0",
                    reasons=(f"structurally-fatal L0 on proposed {op.object_type} "
                             f"{op.object_id}",)),),
                    template_findings=tuple(template_findings), changes=tuple(changes))
            template_findings.extend(_stamp(l0.findings, ref))
            fg = screen_op(op.object_type, snapshot, proposed_t)
            if fg:
                return org_unknown((fg,), template_findings=tuple(template_findings),
                                   changes=tuple(changes))
            proposed = proposed_t
        overlays.append(OrgOverlay(
            object_type=op.object_type, object_id=op.object_id, name=snapshot.get("name"),
            action=op.action, assigned_site_ids=frozenset(resolved.assigned_site_ids),
            baseline=snapshot, proposed=proposed,
        ))

    ov_tuple = tuple(overlays)
    sites = affected_sites(ov_tuple)
    tf = tuple(template_findings)
    if not sites:
        decision, reasons, driving = decide_org({}, template_findings=tf, org_rejections=())
        reasons = reasons + tuple(
            f"{c.ref.kind} {c.ref.id}: no assigned sites — nothing ripples" for c in changes
        )
        return OrgVerdict(decision=decision, decision_reasons=reasons, changes=tuple(changes),
            per_site={}, driving_sites=driving, site_failures={},
            template_findings=tf, org_rejections=())

    raw_map = provider.fetch_sites(org_scope, site_ids=sites)
    per_site: dict[str, Verdict] = {}
    site_failures: dict[str, str] = {}
    for sid in sites:
        fetched = raw_map.get(sid)
        if not isinstance(fetched, RawSiteState):
            failures = fetched.failures if fetched is not None else ()
            site_failures[sid] = "; ".join(
                f"{f.object}: {f.error}" for f in failures
            ) or "fetch failed"
            # preserve the FetchError's own acquired_at (like the single-site
            # total-fetch-failure path) so the failed site's freshness/age is
            # honest, not test-execution "now"
            acquired_at = fetched.acquired_at if fetched is not None else datetime.now(UTC)
            per_site[sid] = _unknown(
                None, adapter_findings=(), run=run, baseline_unavailable=True,
                state_meta=build_state_meta(
                    StateMeta(acquired_at=acquired_at, host=fetched.host if fetched else "",
                              fetched=(), failures=failures),
                    now=datetime.now(UTC),
                ),
            )
            continue
        base_raw, prop_raw = apply_overlays(fetched, sid, ov_tuple)
        sm = build_state_meta(fetched.meta, now=datetime.now(UTC))
        # P2a FAIL-SAFE: full gateway screening iff the site has a gatewaytemplate
        # overlay (full=True keeps the whole gateway effective -> a gatewaytemplate's
        # own networks IS screened -> never false-SAFE; cost is a possible
        # false-UNKNOWN on combined plans).
        gw_full = any(o.object_type == "gatewaytemplate" and sid in o.assigned_site_ids
                      for o in ov_tuple)
        per_site[sid] = _simulate_site_state(
            base_raw, prop_raw, adapter=adapter, registry=registry, run=run,
            state_meta=sm, adapter_findings=(), profile_proposed=None,
            gateway_screen_full=gw_full,
        )

    decision, reasons, driving = decide_org(per_site, template_findings=tf, org_rejections=())
    return OrgVerdict(
        decision=decision, decision_reasons=reasons, changes=tuple(changes),
        per_site=per_site, driving_sites=driving, site_failures=site_failures,
        template_findings=tf, org_rejections=(),
    )


simulate_org_template = simulate_org_plan  # back-compat alias (single-op is a 1-op plan)
