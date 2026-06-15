# Multi-site / org-template simulation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Simulate a `networktemplate` (switch template) edit across all sites assigned to it, returning each site's full `Verdict` plus an org-level rollup decision (`OrgVerdict`) — reusing the entire existing per-site pipeline.

**Architecture:** Extract the per-site pipeline core (`_simulate_site_state`, stages 5–10) so both the unchanged single-site `simulate()` and a new `simulate_org_template()` call it. The org path classifies the plan as ORG mode, applies the edit to ONE resolved template snapshot, overrides each fetched site's `networktemplate` with the baseline / proposed snapshot (so the per-site diff is exactly the edit), runs org-level L0/field gates once, fans out the per-site core, and aggregates into `OrgVerdict`.

**Tech Stack:** Python 3.14, uv, pytest, the existing Mist adapter/providers/scope/verdict layers. Gate after every task: `uv run pytest tests -q && uv run ruff check . && uv run mypy src`.

**Spec:** `docs/superpowers/specs/2026-06-14-multisite-org-template-simulation-design.md`

---

## File Structure

- **Modify** `src/digital_twin/engine/pipeline.py` — extract `_simulate_site_state` + `_unknown`; keep `simulate` unchanged; add `simulate_org_template`.
- **Modify** `src/digital_twin/scope/object_gate.py` — SITE/ORG mode classification.
- **Modify** `src/digital_twin/scope/allowlist.py` — `ORG_OBJECT_TYPES`, `RAW_ALLOWLIST["networktemplate"]`.
- **Modify** `src/digital_twin/scope/field_gate.py` — `screen_op` template branch.
- **Modify** `src/digital_twin/adapters/mist/validate/schema.py` — `networktemplate` in `_SCHEMA_FILES`.
- **Modify** `src/digital_twin/providers/base.py` — broaden `FetchError.scope`; `OrgTemplateContext`; `resolve_org_template` on the protocol.
- **Modify** `src/digital_twin/providers/mist_api.py` — implement `resolve_org_template`.
- **Modify** `src/digital_twin/observability/replay/store.py` (FixtureProvider) — multi-site fixture support + `resolve_org_template`.
- **Create** `src/digital_twin/engine/org_template.py` — template apply + snapshot override helpers.
- **Create** `src/digital_twin/verdict/org_verdict.py` — `OrgVerdict` + `decide_org`.
- **Modify** `src/digital_twin/drivers/{cli.py,render.py,mcp_server.py}` — dispatch by mode + org rendering.
- **Create** `tests/golden/fixtures/multisite/` + multi-site goldens; unit tests alongside each module.

---

## Task 1: Extract the per-site pipeline core (`_simulate_site_state`)

Pure refactor — no behavior change. `simulate` keeps its contract; the GS goldens and pipeline tests must pass unchanged.

**Files:**
- Modify: `src/digital_twin/engine/pipeline.py`
- Test: `tests/engine/test_pipeline.py` (existing — must stay green), `tests/golden/test_golden_scenarios.py` (existing — must stay green)

- [ ] **Step 1: Add a module-level `_unknown` helper**

In `pipeline.py`, add at module level (after `_EMPTY_DIFF`):

```python
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
```

- [ ] **Step 2: Add `_simulate_site_state` (stages 5–10)**

In `pipeline.py`, add at module level:

```python
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
    assert trace is not None

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
            return _unknown(rejection, adapter_findings=adapter_findings, run=run, state_meta=state_meta)
        for did in sorted(set(baseline.device_effective) | set(proposed.device_effective)):
            rejection = check_derived(
                baseline.device_effective.get(did, {}),
                proposed.device_effective.get(did, {}),
                artifact=f"device {did}",
            )
            if rejection:
                return _unknown(rejection, adapter_findings=adapter_findings, run=run, state_meta=state_meta)
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
```

NOTE the one deliberate change vs the inlined original: `unresolved_dynamic_findings` is passed `proposed_raw.port_stats` (was `raw.port_stats`). In single-site `baseline_raw is raw is proposed_raw`'s base, so `raw.port_stats == proposed_raw.port_stats` (apply never touches `port_stats`) — behavior identical. Using `proposed_raw` keeps the core self-contained.

- [ ] **Step 3: Rewrite `simulate` to call the core**

Replace the body of `simulate` from the `# 5 — baseline ingest` comment (line 173) through the end of the function with:

```python
    return _simulate_site_state(
        raw, proposed_raw,
        adapter=adapter, registry=registry, run=run,
        state_meta=state_meta, adapter_findings=adapter_findings,
    )
```

Delete the now-unused inner `unknown(...)` closure (lines 74-92) and replace its call sites in the remaining `simulate` body (stages 1-4) with `_unknown(..., adapter_findings=adapter_findings, run=run)` (add `state_meta=state_meta` where the original passed it). Keep stages 1 (scope.pre), 3 (fetch), and 2+4+6 (the per-op apply loop) exactly as they are except for the `unknown` → `_unknown` rename.

- [ ] **Step 4: Run the full suite — behavior must be unchanged**

Run: `uv run pytest tests -q`
Expected: PASS (same count as before — 727). If any GS golden or pipeline test changed verdict, the extraction altered behavior — revert and find the difference (most likely a missed `state_meta` pass-through or the `raw` vs `proposed_raw` port_stats note above).

- [ ] **Step 5: Gate + commit**

```bash
uv run pytest tests -q && uv run ruff check . && uv run mypy src
git add src/digital_twin/engine/pipeline.py
git commit -m "multisite T1: extract _simulate_site_state (stages 5-10) + shared _unknown; simulate unchanged"
```

---

## Task 2: SITE/ORG mode classification + networktemplate scope (gates)

**Files:**
- Modify: `src/digital_twin/scope/allowlist.py`, `src/digital_twin/scope/object_gate.py`, `src/digital_twin/scope/field_gate.py`, `src/digital_twin/adapters/mist/validate/schema.py`
- Test: `tests/scope/test_object_gate.py`, the allowlist test module, `tests/scope/test_field_gate.py`, the L0 schema test module

- [ ] **Step 1: Add ORG object types + networktemplate allowlist (failing test)**

Append to the allowlist test module:

```python
def test_networktemplate_allowlist_equals_site_setting_exactly():
    from digital_twin.scope.allowlist import ORG_OBJECT_TYPES, RAW_ALLOWLIST
    assert ORG_OBJECT_TYPES == ("networktemplate",)
    assert RAW_ALLOWLIST["networktemplate"] == RAW_ALLOWLIST["site_setting"]
```

- [ ] **Step 2: Run → fail; implement**

In `src/digital_twin/scope/allowlist.py`, after `SUPPORTED_OBJECT_TYPES` (line 14) add:

```python
# Org-level object types simulated by fan-out (NOT single-site). networktemplate
# carries the SAME modeled config layer as a site_setting, so its raw field gate
# reuses the site_setting leaf tuple EXACTLY (switch_matching stays out -> UNKNOWN).
ORG_OBJECT_TYPES: tuple[str, ...] = ("networktemplate",)
```

After `RAW_ALLOWLIST` is defined (after line 124), add:

```python
RAW_ALLOWLIST["networktemplate"] = RAW_ALLOWLIST["site_setting"]
```

Run: `uv run pytest tests -k test_networktemplate_allowlist_equals_site_setting_exactly -q` → PASS.

- [ ] **Step 3: object_gate mode classification (failing tests)**

Append to `tests/scope/test_object_gate.py`:

```python
def _nt_op(object_id="nt1", action="update"):
    return ChangeOp(action=action, order=0, object_type="networktemplate",
                    object_id=object_id, payload={})


def _org_plan(ops, org_id="o1", site_id=None):
    return ChangePlan(source="mist", scope=ChangeScope(org_id=org_id, site_id=site_id),
                      ops=tuple(ops))


def test_org_mode_template_plan_passes():
    # ORG mode triggers ONLY when ALL ops are networktemplate AND site_id absent
    assert check_objects(_org_plan([_nt_op()])) is None


def test_networktemplate_with_site_id_is_out_of_scope_single_site():
    # site_id present -> NOT org mode -> falls into SITE logic -> the EXISTING
    # "unsupported object_type" rejection (preserves test_template_object_type_*)
    r = check_objects(_org_plan([_nt_op()], site_id="s1"))
    assert isinstance(r, Rejection)
    assert any("networktemplate" in reason for reason in r.reasons)


def test_org_mode_rejects_multiple_template_ids():
    # envelope already rejects two ops on the SAME id; here two DISTINCT ids
    r = check_objects(_org_plan([_nt_op(object_id="ntA"), ChangeOp(
        action="update", order=1, object_type="networktemplate", object_id="ntB", payload={})]))
    assert isinstance(r, Rejection)
    assert any("one template" in reason for reason in r.reasons)


def test_mixing_site_and_org_object_types_rejects():
    # not all-networktemplate -> SITE logic -> the networktemplate op is reported
    # as unsupported (and site_id is required) -> rejected
    r = check_objects(_org_plan([_nt_op(), _op(object_type="device", object_id="d1")], site_id=None))
    assert isinstance(r, Rejection)
    assert any("networktemplate" in reason for reason in r.reasons)
```

- [ ] **Step 4: Run → fail; implement object_gate**

Rewrite `src/digital_twin/scope/object_gate.py:check_objects` to classify mode. Replace the function body with:

```python
from digital_twin.scope.allowlist import SUPPORTED_OBJECT_TYPES


def check_objects(plan: ChangePlan) -> Rejection | None:
    reasons: list[str] = []
    if plan.source != _M1_SOURCE:
        reasons.append(f"unsupported source {plan.source!r} (M1 supports only 'mist')")
    ops = plan.ops
    # ORG mode ONLY when EVERY op is networktemplate AND there is no site_id.
    # Anything else (incl. networktemplate WITH a site_id, or a mix) falls into
    # the SITE branch, which preserves the existing per-op diagnostics verbatim.
    is_org = (
        bool(ops)
        and all(op.object_type == "networktemplate" for op in ops)
        and not plan.scope.site_id
    )
    for op in ops:
        if op.action != _M1_ACTION:
            reasons.append(
                f"ops[order={op.order}]: unsupported action {op.action!r} (M1 supports only 'update')"
            )
    if is_org:
        if len({op.object_id for op in ops}) > 1:
            reasons.append("one template per plan in M1 (multiple networktemplate ids)")
    else:  # SITE mode + everything else — UNCHANGED from today
        if not plan.scope.site_id:
            reasons.append("scope.site_id is required (M1 simulates exactly one site)")
        for op in ops:
            if op.object_type not in SUPPORTED_OBJECT_TYPES:
                reasons.append(
                    f"ops[order={op.order}]: unsupported object_type {op.object_type!r} "
                    "(templates/org objects fan out beyond one site; not modeled in M1)"
                )
            elif (
                op.object_type == "site_setting"
                and plan.scope.site_id
                and op.object_id != plan.scope.site_id
            ):
                reasons.append(
                    f"ops[order={op.order}]: site_setting object_id {op.object_id!r} "
                    f"!= scope.site_id {plan.scope.site_id!r} (cross-site fan-out)"
                )
    return Rejection(stage=_STAGE, reasons=tuple(reasons)) if reasons else None
```

Run the new + existing object_gate tests: `uv run pytest tests/scope/test_object_gate.py -q` → PASS. This design keeps EVERY existing test green: a `networktemplate`/`gatewaytemplate`/`sitetemplate` op with the default `site_id="s1"` is NOT org mode → falls into the SITE branch → the existing per-op "unsupported object_type `<name>`" reason (so `test_template_object_type_rejects_as_fanout`, `test_template_modification_rejects_switch_and_gateway`, and `test_all_offending_ops_reported` are unchanged). `ORG_OBJECT_TYPES` from Task 2 Step 2 is still used by the CLI/engine to detect mode; the gate keys on the literal `"networktemplate"`.

- [ ] **Step 4b: Guard `simulate()` against an ORG plan (failing test → fix)**

An ORG plan now PASSES `check_objects` (no rejection), so a caller passing it to the single-site `simulate()` would hit the bare `assert plan.scope.site_id is not None` (pipeline.py:109) → crash. Replace that assert with an explicit UNKNOWN.

Append to `tests/engine/test_pipeline.py`:

```python
def test_simulate_rejects_org_plan_with_unknown_not_crash():
    from digital_twin.engine.pipeline import simulate
    from digital_twin.verdict.decision import Decision
    plan = {"source": "mist", "scope": {"org_id": "o1"},  # no site_id
            "ops": [{"action": "update", "order": 0, "object_type": "networktemplate",
                     "object_id": "nt1", "payload": {}}]}
    v = simulate(plan, provider=_AnyProvider())  # never fetches — guarded before fetch
    assert v.decision is Decision.UNKNOWN
    assert any("simulate_org_template" in r for r in v.decision_reasons)
```

(`_AnyProvider` can be any object — the guard returns before `fetch_site`.) In `pipeline.py:simulate`, replace the fetch-stage assert:

```python
        assert plan.scope.site_id is not None  # object gate guaranteed it
```

with:

```python
        if plan.scope.site_id is None:  # an ORG (template) plan reached single-site simulate
            return _unknown(
                Rejection(
                    stage="scope.pre",
                    reasons=("org/template plan has no site_id — call simulate_org_template, not simulate",),
                ),
                adapter_findings=adapter_findings, run=run,
            )
```

Run: `uv run pytest tests/engine/test_pipeline.py -k org_plan -q` → PASS.

- [ ] **Step 5: screen_op template branch + L0 schema (failing tests)**

Append to `tests/scope/test_field_gate.py`:

```python
def test_screen_op_networktemplate_allows_modeled_leaf_no_role_check():
    from digital_twin.scope.field_gate import screen_op
    current = {"id": "nt1", "networks": {"corp": {"vlan_id": 10}}}
    payload = {"id": "nt1", "networks": {"corp": {"vlan_id": 20}}}
    assert screen_op("networktemplate", current, payload) is None  # vlan_id is modeled


def test_screen_op_networktemplate_rejects_switch_matching():
    from digital_twin.scope.field_gate import screen_op
    r = screen_op("networktemplate",
                  {"id": "nt1", "switch_matching": {"enable": True}},
                  {"id": "nt1", "switch_matching": {"enable": False}})
    assert r is not None  # switch_matching not allowlisted
```

Append to the L0 schema test module:

```python
def test_networktemplate_l0_schema_registered():
    from digital_twin.adapters.mist.validate import validate_payload
    res = validate_payload("networktemplate", {"id": "nt1", "ospf_config": {"enabled": True}})
    assert res.fatal is False  # a valid template body validates
```

- [ ] **Step 6: Run → fail; implement screen_op branch + schema map**

In `src/digital_twin/scope/field_gate.py:screen_op`, change the role-check guard so it only applies to `device`, and add `networktemplate` to the allowlisted object types. Replace the role-check block:

```python
    if object_type == "device" and current.get("type") != "switch":
        return Rejection(...)  # unchanged
```

This already only fires for `device`, so no change is needed there — but confirm the subsequent `allowlist = RAW_ALLOWLIST.get(object_type, ())` line resolves `RAW_ALLOWLIST["networktemplate"]` (it does, from Task 2). So `screen_op("networktemplate", ...)` runs the changed-leaf check against the networktemplate allowlist with NO role check. Verify no other branch rejects a non-`device`/`site_setting` object_type. If `screen_op` has an early `object_type not in (...)` guard, add `"networktemplate"`.

In `src/digital_twin/adapters/mist/validate/schema.py`, add to `_SCHEMA_FILES` (line 28):

```python
    "networktemplate": "networktemplate.schema.json",
```

Run: `uv run pytest tests/scope/test_field_gate.py -k networktemplate -q` and the L0 test → PASS.

- [ ] **Step 7: Gate + commit**

```bash
uv run pytest tests -q && uv run ruff check . && uv run mypy src
git add src/digital_twin/scope src/digital_twin/adapters/mist/validate/schema.py tests
git commit -m "multisite T2: SITE/ORG mode classification + networktemplate allowlist/screen_op/L0"
```

---

## Task 3: Provider — `FetchError` widening, `OrgTemplateContext`, `resolve_org_template`

**Files:**
- Modify: `src/digital_twin/providers/base.py`, `src/digital_twin/providers/mist_api.py`
- Test: `tests/providers/test_base.py` (or the provider test module), `tests/providers/test_mist_api.py`

- [ ] **Step 1: Broaden `FetchError.scope` + add `OrgTemplateContext` (failing test)**

Append to the provider/base test module:

```python
def test_org_template_context_and_orgscope_fetch_error():
    from digital_twin.providers.base import FetchError, OrgScope, OrgTemplateContext
    from datetime import UTC, datetime
    ctx = OrgTemplateContext(template={"id": "nt1"}, assigned_site_ids=("s1", "s2"))
    assert ctx.assigned_site_ids == ("s1", "s2")
    # FetchError must now accept an OrgScope (org-level lookup failure)
    err = FetchError(scope=OrgScope(org_id="o1"), failures=(), acquired_at=datetime.now(UTC), host="h")
    assert err.scope.org_id == "o1"
```

- [ ] **Step 2: Run → fail; implement in `base.py`**

In `src/digital_twin/providers/base.py`:
1. Change `FetchError.scope: SiteScope` (line 80) to `scope: SiteScope | OrgScope`.
2. After `OrgScope` (line 28) add:

```python
@dataclass(frozen=True)
class OrgTemplateContext:
    """Resolution of a networktemplate change: the current template JSON (the
    baseline SNAPSHOT) + the ids of every site assigned to it."""
    template: JsonObj
    assigned_site_ids: tuple[str, ...]
```

3. Add to the `StateProvider` protocol (after `fetch_sites`):

```python
    def resolve_org_template(
        self, scope: OrgScope, template_id: str
    ) -> OrgTemplateContext | FetchError:
        """List the org's sites, filter to those whose networktemplate_id ==
        template_id, and fetch the template. A lookup failure (sites or template)
        is a FetchError (whole-plan UNKNOWN). 0 assigned sites is a SUCCESS with
        an empty assigned_site_ids tuple."""
        ...
```

Run the base test → PASS.

- [ ] **Step 3: `MistApiProvider.resolve_org_template` (failing test)**

Append to `tests/providers/test_mist_api.py` (mirror its existing mocking style — the file already stubs `mistapi` calls; follow `test_fetch_sites_*` patterns):

```python
def test_resolve_org_template_filters_assigned_sites(monkeypatch):
    from digital_twin.providers.base import OrgScope, OrgTemplateContext
    prov = _provider()  # the test module's helper that builds a MistApiProvider with a fake session
    monkeypatch.setattr(prov, "_org_sites", lambda scope: [
        {"id": "s1", "networktemplate_id": "ntX"},
        {"id": "s2", "networktemplate_id": "ntY"},
        {"id": "s3", "networktemplate_id": "ntX"},
    ])
    monkeypatch.setattr(prov, "_networktemplate", lambda scope, nid: {"id": nid, "networks": {}})
    ctx = prov.resolve_org_template(OrgScope(org_id="o1"), "ntX")
    assert isinstance(ctx, OrgTemplateContext)
    assert set(ctx.assigned_site_ids) == {"s1", "s3"}
    assert ctx.template["id"] == "ntX"
```

- [ ] **Step 4: Run → fail; implement**

In `src/digital_twin/providers/mist_api.py`, add the method (reuse the existing private `_org_sites(scope)` and `_networktemplate(scope, nt_id)`; mirror the error handling of `fetch_sites` at lines 92-120 — a lookup exception becomes a `FetchError`, not a raise):

```python
    def resolve_org_template(
        self, scope: OrgScope, template_id: str
    ) -> OrgTemplateContext | FetchError:
        from datetime import UTC, datetime
        try:
            sites = self._org_sites(scope)
            template = self._networktemplate(SiteScope(scope.org_id, ""), template_id)
        except Exception as exc:  # noqa: BLE001 — total lookup failure is a VALUE
            return FetchError(
                scope=scope,
                failures=(FetchFailure(object="org_template", error=str(exc)),),
                acquired_at=datetime.now(UTC),
                host=self._host,
            )
        if template is None:
            return FetchError(
                scope=scope,
                failures=(FetchFailure(object="networktemplate", error=f"{template_id} not found"),),
                acquired_at=datetime.now(UTC),
                host=self._host,
            )
        assigned = tuple(
            str(s["id"]) for s in sites
            if s.get("id") and str(s.get("networktemplate_id") or "") == template_id
        )
        return OrgTemplateContext(template=dict(template), assigned_site_ids=assigned)
```

Confirm `self._host`, `FetchFailure`, `OrgTemplateContext`, `SiteScope` are imported in `mist_api.py` (add imports as needed). Run the mist_api test → PASS.

- [ ] **Step 5: Gate + commit**

```bash
uv run pytest tests -q && uv run ruff check . && uv run mypy src
git add src/digital_twin/providers tests
git commit -m "multisite T3: FetchError scope widening + OrgTemplateContext + resolve_org_template (mist provider)"
```

---

## Task 4: Template apply + snapshot-override helpers

**Files:**
- Create: `src/digital_twin/engine/org_template.py`
- Test: `tests/engine/test_org_template.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/engine/test_org_template.py`:

```python
"""Template apply + the baseline-snapshot override rule (guardrail #3)."""

import pytest

from dataclasses import replace
from datetime import UTC, datetime

from digital_twin.contracts import Rejection
from digital_twin.engine.org_template import apply_template, override_template
from digital_twin.providers.base import FetchFailure, RawSiteState, SiteScope, StateMeta


def _raw(nt):
    return RawSiteState(
        scope=SiteScope("o1", "s1"), site={"id": "s1", "networktemplate_id": "nt1"},
        setting={"id": "s1"}, networktemplate=nt, devices=(), device_stats=(),
        port_stats=(), wireless_clients=(), wired_clients=(), derived_setting=None,
        meta=StateMeta(acquired_at=datetime.now(UTC), host="h", fetched=("site",), failures=()),
    )


def test_apply_template_edits_one_snapshot():
    snap = {"id": "nt1", "networks": {"corp": {"vlan_id": 10}}}
    out = apply_template(snap, {"networks": {"corp": {"vlan_id": 20}}})
    assert out == {"id": "nt1", "networks": {"corp": {"vlan_id": 20}}}  # root replace, id preserved


def test_apply_template_set_and_delete_conflict_rejects():
    r = apply_template({"id": "nt1"}, {"networks": {}, "-networks": ""})
    assert isinstance(r, Rejection) and r.stage == "apply"


def test_override_template_baseline_and_proposed_differ_only_by_edit():
    # the fetched site carries a STALE template copy; override pins both sides to
    # the resolved snapshot / proposed snapshot so the diff is exactly the edit
    fetched = _raw(nt={"id": "nt1", "networks": {"corp": {"vlan_id": 999}}})  # stale
    snapshot = {"id": "nt1", "networks": {"corp": {"vlan_id": 10}}}
    proposed = {"id": "nt1", "networks": {"corp": {"vlan_id": 20}}}
    base_raw, prop_raw = override_template(fetched, snapshot, proposed)
    assert base_raw.networktemplate == snapshot      # NOT the stale 999
    assert prop_raw.networktemplate == proposed
    # everything else identical
    assert base_raw.setting == prop_raw.setting and base_raw.devices == prop_raw.devices
```

- [ ] **Step 2: Run → fail; implement**

Create `src/digital_twin/engine/org_template.py`:

```python
"""Org-template apply + the baseline-snapshot override (multisite design §3).

A networktemplate is one org object shared by every assigned site. We apply the
edit to ONE resolved snapshot and override each fetched site's networktemplate
with the snapshot (baseline) / proposed snapshot, so the per-site diff is EXACTLY
the edit — never a fetch-time race between resolve_org_template and fetch_sites.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace as dc_replace
from typing import Any

from digital_twin.adapters.mist.apply.objects import effective_update, update_conflicts
from digital_twin.contracts import Rejection
from digital_twin.providers.base import RawSiteState

_Json = Mapping[str, Any]


def apply_template(snapshot: _Json, payload: _Json) -> dict[str, Any] | Rejection:
    """The proposed template = snapshot + edit (Mist root-level update semantics).
    A set-AND-delete on the same attribute is an authoring error -> Rejection."""
    conflicts = update_conflicts(payload)
    if conflicts:
        return Rejection(
            stage="apply",
            reasons=tuple(
                f"conflicting set AND '-{c}' delete marker for the same attribute"
                for c in conflicts
            ),
        )
    return effective_update(snapshot, payload)


def override_template(
    fetched_raw: RawSiteState, snapshot: _Json, proposed: _Json
) -> tuple[RawSiteState, RawSiteState]:
    """(baseline_raw, proposed_raw) for one site, both pinned to the ONE snapshot
    — discards the per-site-fetched template copy to avoid a fetch race."""
    baseline_raw = dc_replace(fetched_raw, networktemplate=dict(snapshot))
    proposed_raw = dc_replace(fetched_raw, networktemplate=dict(proposed))
    return baseline_raw, proposed_raw
```

Run: `uv run pytest tests/engine/test_org_template.py -q` → PASS.

- [ ] **Step 3: Gate + commit**

```bash
uv run pytest tests -q && uv run ruff check . && uv run mypy src
git add src/digital_twin/engine/org_template.py tests/engine/test_org_template.py
git commit -m "multisite T4: template apply (effective_update + conflict) + baseline-snapshot override"
```

---

## Task 5: `OrgVerdict` + the org rollup (`decide_org`)

**Files:**
- Create: `src/digital_twin/verdict/org_verdict.py`
- Test: `tests/verdict/test_org_verdict.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/verdict/test_org_verdict.py`:

```python
"""OrgVerdict rollup: worst-of per-site by precedence + template_findings floor."""

from digital_twin.contracts import (
    Finding, FindingCategory, FindingSource, Severity,
)
from digital_twin.ir import Confidence, ConfidenceLevel
from digital_twin.verdict.decision import Decision
from digital_twin.verdict.org_verdict import decide_org


def _verdict(decision):
    # a minimal stand-in carrying only what decide_org reads (.decision)
    from digital_twin.verdict.verdict import Verdict
    from digital_twin.ir import IRDiff
    from digital_twin.verdict.confidence_summary import summarize
    return Verdict(
        decision=decision, decision_reasons=(), overall_severity=None, findings=(),
        check_results=(), coverage={}, confidence_summary=summarize(()),
        ir_diff=IRDiff((), (), ()),
    )


def _op_finding():
    return Finding(
        source=FindingSource.ADAPTER, category=FindingCategory.OPERATIONAL,
        code="l0.schema.x", severity=Severity.ERROR,
        confidence=Confidence(level=ConfidenceLevel.HIGH), message="schema",
    )


def test_rollup_is_worst_of_sites():
    per = {"s1": _verdict(Decision.SAFE), "s2": _verdict(Decision.UNSAFE), "s3": _verdict(Decision.REVIEW)}
    decision, reasons, driving = decide_org(per, template_findings=(), org_rejections=())
    assert decision is Decision.UNSAFE
    assert driving == ("s2",)


def test_unknown_site_wins():
    per = {"s1": _verdict(Decision.UNSAFE), "s2": _verdict(Decision.UNKNOWN)}
    decision, _r, driving = decide_org(per, template_findings=(), org_rejections=())
    assert decision is Decision.UNKNOWN and driving == ("s2",)


def test_template_findings_floor_review():
    per = {"s1": _verdict(Decision.SAFE)}
    decision, _r, driving = decide_org(per, template_findings=(_op_finding(),), org_rejections=())
    assert decision is Decision.REVIEW and driving == ()  # driven by the template, not a site


def test_zero_sites_is_safe():
    decision, reasons, driving = decide_org({}, template_findings=(), org_rejections=())
    assert decision is Decision.SAFE
    assert any("no sites" in r for r in reasons)


def test_zero_sites_with_template_finding_is_review():
    # a non-fatal template L0 floors REVIEW even with zero assigned sites
    decision, _r, _d = decide_org({}, template_findings=(_op_finding(),), org_rejections=())
    assert decision is Decision.REVIEW
```

- [ ] **Step 2: Run → fail; implement**

Create `src/digital_twin/verdict/org_verdict.py`:

```python
"""Org-level rollup over per-site Verdicts (multisite design §7).

decision = worst under UNKNOWN > UNSAFE > REVIEW > SAFE over (every per-site
Verdict's decision) AND (template_findings: an operational ERROR/CRITICAL floors
REVIEW). org_rejections (short-circuit causes) are handled by the engine BEFORE
fan-out; when present the engine builds an UNKNOWN OrgVerdict directly.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from digital_twin.contracts import Finding, FindingCategory, Rejection, Severity
from digital_twin.verdict.decision import Decision
from digital_twin.verdict.verdict import Verdict

_PRECEDENCE = {Decision.SAFE: 0, Decision.REVIEW: 1, Decision.UNSAFE: 2, Decision.UNKNOWN: 3}


@dataclass(frozen=True)
class OrgVerdict:
    decision: Decision
    decision_reasons: tuple[str, ...]
    template_id: str
    per_site: Mapping[str, Verdict]
    driving_sites: tuple[str, ...]
    site_failures: Mapping[str, str]
    template_findings: tuple[Finding, ...]
    org_rejections: tuple[Rejection, ...]


def decide_org(
    per_site: Mapping[str, Verdict],
    *,
    template_findings: tuple[Finding, ...],
    org_rejections: tuple[Rejection, ...],
) -> tuple[Decision, tuple[str, ...], tuple[str, ...]]:
    if org_rejections:  # short-circuit cause -> UNKNOWN (engine usually handles pre-fan-out)
        reasons = tuple(f"[{r.stage}] {reason}" for r in org_rejections for reason in r.reasons)
        return Decision.UNKNOWN, reasons, ()
    # template-level operational ERROR/CRITICAL floors REVIEW (computed FIRST, so
    # it still applies when there are zero assigned sites)
    template_floor = Decision.REVIEW if any(
        f.category is FindingCategory.OPERATIONAL
        and f.severity in (Severity.ERROR, Severity.CRITICAL)
        for f in template_findings
    ) else Decision.SAFE
    if not per_site:
        if template_floor is Decision.REVIEW:
            return Decision.REVIEW, (
                "template-level L0 finding floors REVIEW; template assigned to no sites",
            ), ()
        return Decision.SAFE, ("template valid; assigned to no sites; no impact simulated",), ()
    worst = max(
        (v.decision for v in per_site.values()),
        key=lambda d: _PRECEDENCE[d],
    )
    decision = max((worst, template_floor), key=lambda d: _PRECEDENCE[d])
    driving = tuple(sorted(sid for sid, v in per_site.items() if v.decision is decision)) \
        if decision is worst and _PRECEDENCE[worst] >= _PRECEDENCE[template_floor] else ()
    # build reasons
    reasons: list[str] = []
    if decision is template_floor and template_floor is Decision.REVIEW and not driving:
        reasons.append("template-level L0 finding floors the rollup to REVIEW")
    for sid in driving:
        reasons.append(f"site {sid}: {per_site[sid].decision.value}")
    if not reasons:
        reasons.append(f"rollup decision {decision.value}")
    return decision, tuple(reasons), driving
```

NOTE: when a template REVIEW floor and a site REVIEW coincide, `driving` lists the site(s); when the floor alone drives it (all sites SAFE), `driving` is empty and the reason cites the template — matching the spec.

Run: `uv run pytest tests/verdict/test_org_verdict.py -q` → PASS.

- [ ] **Step 3: Gate + commit**

```bash
uv run pytest tests -q && uv run ruff check . && uv run mypy src
git add src/digital_twin/verdict/org_verdict.py tests/verdict/test_org_verdict.py
git commit -m "multisite T5: OrgVerdict + decide_org rollup (worst-of + template-findings floor + 0-sites SAFE)"
```

---

## Task 6: `simulate_org_template` engine

**Files:**
- Modify: `src/digital_twin/engine/pipeline.py`
- Test: `tests/engine/test_org_pipeline.py` (uses a fake provider — no fixture yet)

- [ ] **Step 1: Write the failing tests (fake in-test provider)**

Create `tests/engine/test_org_pipeline.py`. Use a tiny fake provider implementing the two methods, building two sites that share a template; assert the org rollup. (This is the engine wiring test; the realistic fixture goldens are Task 8.)

```python
from dataclasses import dataclass
from datetime import UTC, datetime

from digital_twin.engine.pipeline import simulate_org_template
from digital_twin.providers.base import (
    FetchError, FetchFailure, OrgScope, OrgTemplateContext, RawSiteState, SiteScope, StateMeta,
)
from digital_twin.verdict.decision import Decision


def _meta():
    return StateMeta(acquired_at=datetime.now(UTC), host="h", fetched=("site", "setting", "devices"), failures=())


def _site(sid, *, setting, devices, nt):
    return RawSiteState(
        scope=SiteScope("o1", sid), site={"id": sid, "networktemplate_id": "nt1"},
        setting=setting, networktemplate=nt, devices=tuple(devices), device_stats=(),
        port_stats=(), wireless_clients=(), wired_clients=(), derived_setting=None, meta=_meta(),
    )


@dataclass
class _FakeProvider:
    sites: dict
    template: dict

    def resolve_org_template(self, scope, template_id):
        return OrgTemplateContext(template=self.template, assigned_site_ids=tuple(self.sites))

    def fetch_sites(self, scope, site_ids=None, *, include_derived=False):
        ids = list(site_ids) if site_ids is not None else list(self.sites)
        return {sid: self.sites[sid] for sid in ids}

    def fetch_site(self, scope, *, include_derived=False):  # unused here
        raise NotImplementedError


def _plan(payload):
    return {"source": "mist", "scope": {"org_id": "o1"},
            "ops": [{"action": "update", "order": 0, "object_type": "networktemplate",
                     "object_id": "nt1", "payload": payload}]}


def test_org_template_rejects_site_plan_with_unknown():
    # a normal site_setting/device plan must NOT be fanned out as a template
    prov = _FakeProvider(sites={}, template={})
    site_plan = {"source": "mist", "scope": {"org_id": "o1", "site_id": "s1"},
                 "ops": [{"action": "update", "order": 0, "object_type": "device",
                          "object_id": "d1", "payload": {}}]}
    ov = simulate_org_template(site_plan, provider=prov)
    assert ov.decision is Decision.UNKNOWN
    assert any("simulate" in r and "org" in r.lower() for r in ov.decision_reasons)


def test_org_template_zero_sites_is_safe():
    prov = _FakeProvider(sites={}, template={"id": "nt1", "networks": {}})
    ov = simulate_org_template(_plan({"networks": {}}), provider=prov)
    assert ov.decision is Decision.SAFE and ov.per_site == {}


def test_org_template_out_of_scope_leaf_is_unknown():
    # switch_matching is denied -> org field gate -> whole-plan UNKNOWN, no fan-out
    prov = _FakeProvider(
        sites={"s1": _site("s1", setting={"id": "s1"}, devices=(), nt={"id": "nt1"})},
        template={"id": "nt1", "switch_matching": {"enable": True}},
    )
    ov = simulate_org_template(_plan({"switch_matching": {"enable": False}}), provider=prov)
    assert ov.decision is Decision.UNKNOWN and ov.per_site == {}
    assert ov.org_rejections  # structured cause present


def test_org_template_per_site_and_rollup():
    # a benign vlan add to the template -> both sites SAFE -> rollup SAFE
    tmpl = {"id": "nt1", "networks": {"corp": {"vlan_id": 10}}}
    s1 = _site("s1", setting={"id": "s1"}, devices=(), nt=tmpl)
    s2 = _site("s2", setting={"id": "s2"}, devices=(), nt=tmpl)
    prov = _FakeProvider(sites={"s1": s1, "s2": s2}, template=tmpl)
    ov = simulate_org_template(_plan({"networks": {"corp": {"vlan_id": 10}, "extra": {"vlan_id": 11}}}),
                               provider=prov)
    assert set(ov.per_site) == {"s1", "s2"}
    assert ov.decision is Decision.SAFE


def test_org_template_fetch_failed_site_is_unknown():
    tmpl = {"id": "nt1", "networks": {}}
    s1 = _site("s1", setting={"id": "s1"}, devices=(), nt=tmpl)
    prov = _FakeProvider(sites={"s1": s1, "s2": "FAIL"}, template=tmpl)
    # make s2 a FetchError
    prov.sites["s2"] = FetchError(scope=SiteScope("o1", "s2"), failures=(FetchFailure("site", "503"),),
                                  acquired_at=datetime.now(UTC), host="h")
    ov = simulate_org_template(_plan({"networks": {}}), provider=prov)
    assert ov.per_site["s2"].decision is Decision.UNKNOWN
    assert ov.decision is Decision.UNKNOWN
    assert "s2" in ov.site_failures
```

- [ ] **Step 2: Run → fail; implement `simulate_org_template`**

In `src/digital_twin/engine/pipeline.py`, add (imports: `OrgScope`, `OrgTemplateContext` from providers.base; `apply_template`, `override_template` from `digital_twin.engine.org_template`; `OrgVerdict`, `decide_org` from `digital_twin.verdict.org_verdict`; `screen_op` already imported; `validate_payload` via `adapter.validate` on a synthetic op):

```python
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

    def org_unknown(rejections, *, template_findings=()):
        return OrgVerdict(
            decision=Decision.UNKNOWN,
            decision_reasons=tuple(f"[{r.stage}] {x}" for r in rejections for x in r.reasons),
            template_id=template_id, per_site={}, driving_sites=(), site_failures={},
            template_findings=tuple(template_findings), org_rejections=tuple(rejections),
        )

    plan = parse_change_plan(plan_data)
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
        and all(op.object_type == "networktemplate" for op in plan.ops)
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
            reasons=tuple(f"org-template lookup failed: {f.object}: {f.error}" for f in resolved.failures)
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
        decision, reasons, driving = decide_org({}, template_findings=template_findings, org_rejections=())
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
            site_failures[sid] = "; ".join(f"{f.object}: {f.error}" for f in failures) or "fetch failed"
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

    decision, reasons, driving = decide_org(per_site, template_findings=template_findings, org_rejections=())
    return OrgVerdict(
        decision=decision, decision_reasons=reasons, template_id=template_id,
        per_site=per_site, driving_sites=driving, site_failures=site_failures,
        template_findings=template_findings, org_rejections=(),
    )
```

- [ ] **Step 3: Run → pass**

Run: `uv run pytest tests/engine/test_org_pipeline.py -q` → PASS (4 tests). Debug any mismatch with the per-site `_simulate_site_state` wiring.

- [ ] **Step 4: Public API export + gate + commit**

Export `simulate_org_template` and `OrgVerdict` from wherever `simulate`/`Verdict` are publicly exported (check `src/digital_twin/__init__.py` or the engine package `__init__`; mirror the existing `simulate` export). Then:

```bash
uv run pytest tests -q && uv run ruff check . && uv run mypy src
git add src/digital_twin/engine/pipeline.py src/digital_twin/__init__.py tests/engine/test_org_pipeline.py
git commit -m "multisite T6: simulate_org_template engine (resolve -> apply -> org gates -> fan-out -> rollup)"
```

---

## Task 7: Drivers — CLI/MCP dispatch by mode + org rendering

**Files:**
- Modify: `src/digital_twin/drivers/render.py`, `src/digital_twin/drivers/cli.py`, `src/digital_twin/drivers/mcp_server.py`
- Test: `tests/drivers/test_render.py`, `tests/drivers/test_cli.py`

- [ ] **Step 1: `org_verdict_to_dict` + `render_org_human` (failing test)**

Append to `tests/drivers/test_render.py`:

```python
def test_org_verdict_to_dict_shape():
    from digital_twin.drivers.render import org_verdict_to_dict
    from digital_twin.verdict.org_verdict import OrgVerdict
    from digital_twin.verdict.decision import Decision
    ov = OrgVerdict(decision=Decision.UNSAFE, decision_reasons=("site s1: unsafe",),
                    template_id="nt1", per_site={}, driving_sites=("s1",),
                    site_failures={}, template_findings=(), org_rejections=())
    d = org_verdict_to_dict(ov)
    assert d["decision"] == "unsafe" and d["template_id"] == "nt1"
    assert d["driving_sites"] == ["s1"]
```

- [ ] **Step 2: Run → fail; implement render**

In `src/digital_twin/drivers/render.py`, add `org_verdict_to_dict(ov: OrgVerdict) -> dict` (mirror the existing `verdict_to_dict`: serialize decision/reasons/template_id/driving_sites/site_failures, and `per_site` as `{sid: verdict_to_dict(v)}`, and `template_findings` via the existing finding serializer) and `render_org_human(ov) -> str` (a header line with the org decision + template id, then a per-site table: `sid  decision  top-reason  freshness`). Reuse the existing `verdict_to_dict`/`render_human` helpers for per-site.

Run the render test → PASS.

- [ ] **Step 3: CLI dispatch by mode (+ `_RecordingProvider` forwarding)**

First, `_RecordingProvider` (cli.py:25) currently forwards only `fetch_site`/`fetch_sites`; the org path calls `resolve_org_template`, so add a passthrough (no recording — see the replay note):

```python
    def resolve_org_template(self, scope, template_id):
        return self._inner.resolve_org_template(scope, template_id)
```

In `cli.py`, after parsing the plan JSON, detect ORG mode **defensively** — any malformed shape must NOT crash the driver; it falls through to the SITE path (`simulate` → envelope → UNKNOWN, the existing safety):

```python
def _is_org_plan(plan_data: object) -> bool:
    if not isinstance(plan_data, dict):
        return False
    ops = plan_data.get("ops")
    scope = plan_data.get("scope")
    return (
        isinstance(ops, list) and bool(ops)
        and all(isinstance(o, dict) and o.get("object_type") == "networktemplate" for o in ops)
        and isinstance(scope, dict) and not scope.get("site_id")
    )
```

If `_is_org_plan(plan_data)` → `simulate_org_template(plan_data, provider=recording, ...)` → `render_org_human` / `org_verdict_to_dict` (respect `--json`), exit code from the ORG decision (existing `Decision -> exit` map: SAFE=0/REVIEW=10/UNSAFE=20/UNKNOWN=30). Else → the unchanged SITE path. A malformed plan (`ops` missing / non-list / op without `object_type`) returns `False` → SITE path → envelope rejects → UNKNOWN exit 30 (no driver crash).

**`--replay-store` for org runs (MVP):** the org path does NOT record. `_RecordingProvider.recorded` stays `None` for org runs (resolve/fetch_sites don't set it), so the existing `if args.replay_store and recording.recorded is not None:` guard naturally skips capture — no crash, just no single-site fixture written. Document this in the `--replay-store` help text ("single-site runs only"); multi-site replay capture is out of scope (the spec's deferred fixture-optimization territory).

Add CLI tests: (1) an org-template plan JSON + the multi-site `FixtureProvider` (from Task 8) OR a fake provider, asserting the exit code + that the rendered output shows the org decision + a per-site line; (2) a **malformed plan regression** — `{"source": "mist", "ops": "not-a-list"}` (and a plan with an op missing `object_type`) → the driver does NOT crash, exits **30** with an UNKNOWN verdict (the SITE-path envelope rejection).

- [ ] **Step 4: MCP tool**

In `src/digital_twin/drivers/mcp_server.py`, add a `simulate_org_template_change` tool (or branch the existing `simulate_change` tool on plan mode) returning `org_verdict_to_dict`; never throws (mirror the existing tool's error envelope → an UNKNOWN OrgVerdict dict on failure).

- [ ] **Step 5: Gate + commit**

```bash
uv run pytest tests -q && uv run ruff check . && uv run mypy src
git add src/digital_twin/drivers tests/drivers
git commit -m "multisite T7: CLI/MCP dispatch by mode + org_verdict_to_dict/render_org_human"
```

---

## Task 8: Multi-site fixture + goldens + live verification + roadmap + memory

**Files:**
- Modify: `src/digital_twin/observability/replay/store.py` (FixtureProvider), `tests/golden/` (fixture + goldens), `docs/ROADMAP.md`, memory

- [ ] **Step 1: Multi-site `FixtureProvider` (failing test)**

Extend `FixtureProvider` to support a multi-site fixture: a JSON doc with `{"template": {...}, "sites": {sid: <single-site fixture doc>, ...}}`. Implement `resolve_org_template(scope, template_id)` (filter `sites[*].site.networktemplate_id == template_id`, return the `template` + assigned ids) and ensure `fetch_sites(scope, site_ids)` returns the per-site `RawSiteState` map (reuse the existing single-site load per entry). Write a unit test loading a tiny 2-site fixture and asserting `resolve_org_template` returns both assigned ids + the template.

- [ ] **Step 2: Build the multi-site golden fixture**

Create `tests/golden/fixtures/multisite/` (or a builder) with TWO sites sharing one `networktemplate`: site A has a switch with an IRB/exit that depends on a template network `corp` (vlan 10); site B does NOT use `corp`. Apply the existing redactor. Add a `multisite_doc()` builder in `tests/golden/builders.py` mirroring the single-site builders (reuse `fixture_doc()` per site + a shared template block).

- [ ] **Step 3: Goldens MS-a..d (failing → passing)**

Append to `tests/golden/test_golden_scenarios.py` (a `_simulate_org` helper like `_simulate` but calling `simulate_org_template` over the multi-site `FixtureProvider`):

```python
def test_ms_a_template_network_removal_breaks_one_site_unsafe(tmp_path):
    # remove `corp` from the template -> site A loses its exit (UNSAFE), site B SAFE
    doc, plan = multisite_remove_corp()
    ov = _simulate_org(doc, plan, tmp_path)
    assert ov.decision is Decision.UNSAFE, ov.decision_reasons
    assert "siteA" in ov.driving_sites and ov.per_site["siteB"].decision is Decision.SAFE


def test_ms_b_one_site_fetch_fails_rolls_up_unknown(tmp_path):
    doc, plan = multisite_with_failed_site()
    ov = _simulate_org(doc, plan, tmp_path)
    assert ov.decision is Decision.UNKNOWN
    assert "siteB" in ov.site_failures


def test_ms_c_cosmetic_template_edit_is_safe(tmp_path):
    doc, plan = multisite_add_unused_vlan()
    ov = _simulate_org(doc, plan, tmp_path)
    assert ov.decision is Decision.SAFE, ov.decision_reasons


def test_ms_d_zero_assigned_sites_is_safe(tmp_path):
    doc, plan = multisite_template_with_no_assigned_sites()
    ov = _simulate_org(doc, plan, tmp_path)
    assert ov.decision is Decision.SAFE
```

Debug each as in the GS26 round (inspect `ov.decision_reasons`, `ov.per_site[sid].decision`). The withdrawn network must carry only modeled leaves (else UNKNOWN at the org field gate).

- [ ] **Step 4: Full gate**

`uv run pytest tests -q && uv run ruff check . && uv run mypy src`

- [ ] **Step 5: Commit goldens**

```bash
git add src/digital_twin/observability/replay/store.py tests/golden
git commit -m "multisite T8: multi-site FixtureProvider + goldens MS-a..d"
```

- [ ] **Step 6: Live verification (read-only)**

The live org has multi-site networktemplates. Run a read-only `simulate_org_template` against a real template assigned to ≥2 sites (a cosmetic edit, e.g. add an unused vlan) and confirm it runs end-to-end and the rollup is consistent with the per-site verdicts. Use the CLI org path:

```bash
set -a; source .env; set +a; uv run digital-twin --plan org-template-plan.json 2>/dev/null | head -20
```

Confirm: an org decision line + a per-site breakdown; no crash; the rollup equals the worst per-site decision. Also re-run the 8 single-site plans (Task 1 must not have regressed them): `for p in plan.json test-plans/*.json; do printf '%s ' "$p"; uv run digital-twin --plan "$p" 2>/dev/null | head -1; done` → unchanged verdicts.

- [ ] **Step 7: Roadmap + memory + commit**

In `docs/ROADMAP.md` §3, flip "multi-site / org-template simulation" + the networktemplate part of "networktemplate / sitetemplate as first-class object_types" to ✅ with a summary (networktemplate vertical slice done; `simulate_org_template` + `OrgVerdict`; snapshot-override; per-site core reuse; gatewaytemplate/sitetemplate + delete + multi-template deferred). Append a memory round (the durable lessons: the `_simulate_site_state` extraction seam, the snapshot-override race fix, the SITE/ORG mode classification, template findings live only on OrgVerdict).

```bash
git add docs/ROADMAP.md
git commit -m "multisite: roadmap networktemplate org-simulation -> done; single-site plans unchanged"
```

---

## Self-Review (planner)

**Spec coverage:** pipeline split (T1) ✓; SITE/ORG mode + single-template-id + mixing rejections (T2) ✓; networktemplate allowlist = site_setting exact tuple + switch_matching excluded (T2) ✓; screen_op template branch + L0 schema (T2) ✓; FetchError widening + OrgTemplateContext + resolve_org_template (T3) ✓; template apply + snapshot-override race fix (T4) ✓; OrgVerdict + worst-of rollup + template-findings floor + 0-sites SAFE (T5) ✓; simulate_org_template incl. short-circuits (lookup-fail/fatal-L0/conflict/field-gate → org_rejections UNKNOWN), per-site fetch-fail → UNKNOWN, snapshot override (T6) ✓; drivers (T7) ✓; multi-site fixture + goldens MS-a..d + live + roadmap/memory (T8) ✓.

**Placeholder scan:** test code is complete; provider/driver/fixture glue (T3 mist_api, T7 drivers, T8 FixtureProvider) gives complete signatures + the exact existing patterns to mirror (`fetch_sites` batching, `verdict_to_dict`, single-site fixture load) rather than verbatim code, because that code depends on private helpers in files the implementer will read — acceptable for glue; the behavioral tests are complete and pin the contract.

**Type consistency:** `OrgTemplateContext(template, assigned_site_ids)`, `OrgVerdict(...)` fields, `decide_org(per_site, *, template_findings, org_rejections) -> (Decision, reasons, driving)`, `apply_template`/`override_template`, `_simulate_site_state(...)`/`_unknown(...)` signatures are identical across the tasks that define and call them. `template_findings` flows: org-level only on `OrgVerdict` (T6 Step 3 sets per-site `adapter_findings=()`), floored via `decide_org` (T5).
