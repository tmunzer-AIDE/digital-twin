# Org-plan DELETE-ripple + multiple templates per plan — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Simulate deleting org templates (networktemplate/gatewaytemplate/sitetemplate) assigned to sites — and multiple org ops per plan applied atomically per affected site — so the twin reports the config-collapse ripple instead of a blanket UNKNOWN.

**Architecture:** Approach A "org overlays." Each org op → an `OrgOverlay` (`proposed=None` ⇔ layer removed). `object_gate` allows `delete` + multiple org ops; `simulate_org_plan` resolves each op, unions affected sites, and per affected site pins every overlay the site is assigned to onto its layer slot (baseline/proposed) → the existing `_simulate_site_state` fan-out → an `OrgVerdict` that names a *set* of changed objects.

**Tech Stack:** Python 3.14, uv, pytest, ruff, mypy (strict on `src`). Gate: `uv run pytest tests -q && uv run ruff check . && uv run mypy src`.

**Spec:** `docs/superpowers/specs/2026-06-18-org-plan-delete-ripple-design.md`

**Every commit** ends with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` and must pass the full gate.

## File structure
- `src/digital_twin/engine/org_overlay.py` (NEW) — `OrgOverlay`, `affected_sites`, `apply_overlays`, `_pin`. Pure.
- `src/digital_twin/verdict/org_verdict.py` (MODIFY) — `OrgChange` dataclass; `OrgVerdict.template_id` → `changes`.
- `src/digital_twin/scope/object_gate.py` (MODIFY) — relax ORG mode (delete + multi-op + dedup + empty-delete-payload).
- `src/digital_twin/engine/pipeline.py` (MODIFY) — `simulate_org_plan` (generalize `simulate_org_template` → thin alias).
- `src/digital_twin/engine/org_template.py` (MODIFY) — remove the now-dead single-op `override_template`/`_pin`; keep `apply_template`.
- `src/digital_twin/drivers/render.py`, `mcp_server.py`, `cli.py` (MODIFY) — `template_id` → `changes` in the org dict/human render + the MCP unknown-org helper.

---

## Phase 1 — the overlay model (pure, no breakage)

### Task 1: `OrgChange` + `OrgOverlay` + `affected_sites`

**Files:**
- Modify: `src/digital_twin/verdict/org_verdict.py`
- Create: `src/digital_twin/engine/org_overlay.py`
- Test: `tests/engine/test_org_overlay.py` (create)

- [ ] **Step 1: Write the failing test** (`tests/engine/test_org_overlay.py`):

```python
from digital_twin.contracts import ObjectRef
from digital_twin.engine.org_overlay import OrgOverlay, affected_sites
from digital_twin.verdict.org_verdict import OrgChange


def _ov(otype="networktemplate", oid="nt1", sites=("s1",), proposed=None, action="delete"):
    return OrgOverlay(
        object_type=otype, object_id=oid, name=oid, action=action,
        assigned_site_ids=frozenset(sites), baseline={"id": oid}, proposed=proposed,
    )


def test_overlay_delete_has_none_proposed():
    o = _ov()
    assert o.proposed is None and o.action == "delete"


def test_overlay_update_carries_proposed():
    o = _ov(proposed={"id": "nt1", "x": 1}, action="update")
    assert o.proposed == {"id": "nt1", "x": 1} and o.action == "update"


def test_affected_sites_is_sorted_union():
    a = _ov(oid="nt1", sites=("s2", "s1"))
    b = _ov(oid="gt1", otype="gatewaytemplate", sites=("s2", "s3"))
    assert affected_sites((a, b)) == ("s1", "s2", "s3")


def test_org_change_holds_ref_and_action():
    c = OrgChange(ref=ObjectRef("networktemplate", "nt1", "name"), action="delete")
    assert c.ref.id == "nt1" and c.action == "delete"
```

- [ ] **Step 2: Run → FAIL** (`uv run pytest tests/engine/test_org_overlay.py -q`) — module + `OrgChange` missing.

- [ ] **Step 3: Implement.** In `src/digital_twin/verdict/org_verdict.py`, add `ObjectRef` to the `digital_twin.contracts` import and define (after the imports, before `OrgVerdict`):

```python
@dataclass(frozen=True)
class OrgChange:
    """One org object a plan touches, for the multi-object OrgVerdict."""
    ref: ObjectRef                  # kind=object_type, id, name
    action: str                     # "update" | "delete"
```

Create `src/digital_twin/engine/org_overlay.py`:

```python
"""Org-plan overlays (delete-ripple design §core model). Each org op becomes an
OrgOverlay; `proposed is None` means the layer is ABSENT (a delete), distinct from
{} (an empty-but-present template). The per-site filter uses `assigned_site_ids`
(the canonical resolver output), never the raw site.<type>_id field."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import replace as dc_replace
from typing import Any

from digital_twin.providers.base import RawSiteState


@dataclass(frozen=True)
class OrgOverlay:
    object_type: str                          # networktemplate | gatewaytemplate | sitetemplate
    object_id: str
    name: str | None
    action: str                               # "update" | "delete"
    assigned_site_ids: frozenset[str]
    baseline: Mapping[str, Any]
    proposed: Mapping[str, Any] | None         # None == REMOVED (layer absent)


def affected_sites(overlays: tuple[OrgOverlay, ...]) -> tuple[str, ...]:
    """Deterministic union of each overlay's baseline assigned_site_ids. Structured
    as a helper so a future site-reassignment op can feed a baseline∪proposed union;
    MVP = baseline assignment (a template op cannot change assignment)."""
    out: set[str] = set()
    for o in overlays:
        out |= o.assigned_site_ids
    return tuple(sorted(out))


def _pin(raw: RawSiteState, object_type: str, value: Mapping[str, Any] | None) -> RawSiteState:
    """Replace exactly the named template field. `None` => layer absent (delete).
    Explicit branch keeps mypy happy without casting."""
    v: dict[str, Any] | None = dict(value) if value is not None else None
    if object_type == "gatewaytemplate":
        return dc_replace(raw, gatewaytemplate=v)
    if object_type == "sitetemplate":
        return dc_replace(raw, sitetemplate=v)
    return dc_replace(raw, networktemplate=v)  # networktemplate default
```

(`apply_overlays` is Task 2.)

- [ ] **Step 4: Run → PASS**, then FULL gate `uv run pytest tests -q && uv run ruff check . && uv run mypy src`. (Existing `OrgVerdict` still has `template_id` — unchanged here; `OrgChange` is purely additive.)

- [ ] **Step 5: Commit:**
```
git add src/digital_twin/engine/org_overlay.py src/digital_twin/verdict/org_verdict.py tests/engine/test_org_overlay.py
git commit -m "$(printf 'feat(org-delete): OrgOverlay + OrgChange + affected_sites\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

### Task 2: `apply_overlays` (generalized per-site pin)

**Files:**
- Modify: `src/digital_twin/engine/org_overlay.py`
- Test: `tests/engine/test_org_overlay.py` (extend)

This generalizes the existing `override_template` (which pins ONE template) to pin a SET of overlays — only those the site is assigned to.

- [ ] **Step 1: Write the failing test** (append):

```python
from datetime import UTC, datetime

from digital_twin.engine.org_overlay import apply_overlays
from digital_twin.providers.base import RawSiteState, SiteScope, StateMeta


def _raw(nt=None, gt=None, st=None):
    return RawSiteState(
        scope=SiteScope(org_id="o1", site_id="s1"), site={"id": "s1"}, setting={},
        networktemplate=nt, gatewaytemplate=gt, sitetemplate=st, devices=(),
        device_stats=(), port_stats=(), wireless_clients=(), wired_clients=(),
        derived_setting=None,
        meta=StateMeta(acquired_at=datetime.now(UTC), host="t", fetched=(), failures=()),
    )


def test_apply_overlays_delete_pins_proposed_none():
    fetched = _raw(nt={"id": "nt1", "live": True})
    o = _ov(otype="networktemplate", oid="nt1", sites=("s1",), proposed=None, action="delete")
    base, prop = apply_overlays(fetched, "s1", (o,))
    assert base.networktemplate == {"id": "nt1"}     # baseline pinned to the snapshot
    assert prop.networktemplate is None              # proposed = layer ABSENT


def test_apply_overlays_only_pins_assigned_overlays():
    fetched = _raw(nt={"id": "ntX"}, gt={"id": "gtX"})
    nt_op = _ov(otype="networktemplate", oid="nt1", sites=("s1",), action="delete")
    gt_op = _ov(otype="gatewaytemplate", oid="gt1", sites=("s2",), action="delete")  # NOT s1
    base, prop = apply_overlays(fetched, "s1", (nt_op, gt_op))
    assert prop.networktemplate is None              # s1 IS assigned nt1 -> pinned
    assert prop.gatewaytemplate == {"id": "gtX"}     # s1 NOT assigned gt1 -> untouched


def test_apply_overlays_combines_two_overlays_on_one_site():
    fetched = _raw(nt={"id": "ntX"}, gt={"id": "gtX"})
    nt_op = _ov(otype="networktemplate", oid="nt1", sites=("s1",), action="delete")
    gt_op = _ov(otype="gatewaytemplate", oid="gt1", sites=("s1",),
                proposed={"id": "gt1", "edited": True}, action="update")
    base, prop = apply_overlays(fetched, "s1", (nt_op, gt_op))
    assert prop.networktemplate is None and prop.gatewaytemplate == {"id": "gt1", "edited": True}
```

- [ ] **Step 2: Run → FAIL** (`apply_overlays` missing).

- [ ] **Step 3: Implement** (append to `org_overlay.py`):

```python
def apply_overlays(
    fetched: RawSiteState, site_id: str, overlays: tuple[OrgOverlay, ...]
) -> tuple[RawSiteState, RawSiteState]:
    """(baseline_raw, proposed_raw) for one site: pin every overlay the site is
    assigned to (site_id in overlay.assigned_site_ids) onto its layer slot —
    baseline=overlay.baseline, proposed=overlay.proposed (None == layer absent).
    A site not assigned to a given overlay is NOT pinned for it. Untouched layers
    keep the fetched copy (fetch-race guard). Order-independent: a site has ≤1
    overlay per layer slot."""
    base_raw, prop_raw = fetched, fetched
    for o in overlays:
        if site_id not in o.assigned_site_ids:
            continue
        base_raw = _pin(base_raw, o.object_type, o.baseline)
        prop_raw = _pin(prop_raw, o.object_type, o.proposed)
    return base_raw, prop_raw
```

- [ ] **Step 4: Run → PASS**, then FULL gate.

- [ ] **Step 5: Commit:**
```
git add src/digital_twin/engine/org_overlay.py tests/engine/test_org_overlay.py
git commit -m "$(printf 'feat(org-delete): apply_overlays pins assigned overlays per site\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

## Phase 2 — gate + verdict shape

### Task 3: `object_gate` — allow delete + multiple org ops

**Files:**
- Modify: `src/digital_twin/scope/object_gate.py`
- Test: `tests/scope/test_object_gate.py` (extend)

- [ ] **Step 1: Write the failing tests** (append to `tests/scope/test_object_gate.py`; reuse its `_op`/`_org_plan` helpers — `_op(object_type, object_id, action, order)` and `_org_plan(ops, site_id=None)` already exist):

```python
def _del(object_type, object_id, order=0, payload=None):
    return ChangeOp(action="delete", order=order, object_type=object_type,
                    object_id=object_id, payload=payload if payload is not None else {})


def test_org_delete_allowed():
    assert check_objects(_org_plan([_del("networktemplate", "nt1")])) is None


def test_org_multiple_ops_allowed():
    r = check_objects(_org_plan([
        _del("networktemplate", "nt1", order=0),
        _del("gatewaytemplate", "gt1", order=1),
    ]))
    assert r is None


def test_org_duplicate_type_id_rejected():
    r = check_objects(_org_plan([
        _del("networktemplate", "nt1", order=0),
        _del("networktemplate", "nt1", order=1),
    ]))
    assert isinstance(r, Rejection) and any("duplicate" in x for x in r.reasons)


def test_org_delete_with_nonempty_payload_rejected():
    r = check_objects(_org_plan([_del("networktemplate", "nt1", payload={"x": 1})]))
    assert isinstance(r, Rejection) and any("delete payload" in x for x in r.reasons)


def test_org_mixed_delete_update_allowed():
    upd = ChangeOp(action="update", order=1, object_type="gatewaytemplate",
                   object_id="gt1", payload={"name": "x"})
    assert check_objects(_org_plan([_del("networktemplate", "nt1", order=0), upd])) is None


def test_site_delete_still_rejected():
    # a device delete (SITE mode) is still rejected by the action gate
    from digital_twin.contracts import ChangeScope
    plan = ChangePlan(source="mist", scope=ChangeScope(org_id="o1", site_id="s1"),
                      ops=(ChangeOp(action="delete", order=0, object_type="device",
                                    object_id="d1", payload={}),))
    r = check_objects(plan)
    assert isinstance(r, Rejection) and any("delete" in x for x in r.reasons)
```

- [ ] **Step 2: Run → FAIL** (delete rejected; multi-op rejected with the old "one template" message).

- [ ] **Step 3: Implement.** In `src/digital_twin/scope/object_gate.py`, add `_ORG_ACTIONS = ("update", "delete")`. Replace the action loop + the `is_org` single-op block with action handling that is org-aware, and add the dedup + delete-payload checks. The new `check_objects` body:

```python
_M1_ACTION = "update"
_ORG_ACTIONS = ("update", "delete")


def check_objects(plan: ChangePlan) -> Rejection | None:
    reasons: list[str] = []
    if plan.source != _M1_SOURCE:
        reasons.append(f"unsupported source {plan.source!r} (M1 supports only 'mist')")
    ops = plan.ops
    is_org = (
        bool(ops)
        and all(op.object_type in ORG_OBJECT_TYPES for op in ops)
        and not plan.scope.site_id
    )
    if is_org:
        for op in ops:
            if op.action not in _ORG_ACTIONS:
                reasons.append(
                    f"ops[order={op.order}]: unsupported action {op.action!r} "
                    "(org ops support 'update' | 'delete')"
                )
            if op.action == "delete" and op.payload:
                reasons.append(
                    f"ops[order={op.order}]: delete payload must be empty "
                    "(a delete has no proposed object)"
                )
        seen: set[tuple[str, str]] = set()
        for op in ops:
            key = (op.object_type, op.object_id)
            if key in seen:
                reasons.append(
                    f"duplicate org op for {op.object_type} {op.object_id!r} "
                    "(one op per object in M1)"
                )
            seen.add(key)
    else:  # SITE mode + everything else — UNCHANGED from today
        for op in ops:
            if op.action != _M1_ACTION:
                reasons.append(
                    f"ops[order={op.order}]: unsupported action {op.action!r} "
                    "(M1 supports only 'update')"
                )
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

NOTE: the previous "exactly one org op" test (`test_org_mode_rejects_mixed_types_same_id`, `test_org_mode_rejects_multiple_template_ids*`) asserted the OLD single-op rule. Those now change meaning: multiple DISTINCT `(type,id)` org ops are allowed; only DUPLICATE `(type,id)` is rejected. UPDATE those existing tests: the multiple-distinct-ids cases now expect `None` (allowed); keep a duplicate-`(type,id)` case expecting rejection. Read `tests/scope/test_object_gate.py` and adjust the 2-3 affected tests accordingly (the mixed-types-same-id case = duplicate-ish? No — different types, same id is NOT a `(type,id)` duplicate, so it is now ALLOWED; update that test to expect None).

- [ ] **Step 4: Run → PASS** (new + adjusted existing), then FULL gate.

- [ ] **Step 5: Commit:**
```
git add src/digital_twin/scope/object_gate.py tests/scope/test_object_gate.py
git commit -m "$(printf 'feat(org-delete): object_gate allows delete + multiple org ops, rejects dup/payload\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

### Task 4: `OrgVerdict.template_id` → `changes` (shape migration)

**Files:**
- Modify: `src/digital_twin/verdict/org_verdict.py`
- Modify: `src/digital_twin/engine/pipeline.py` (the 3 `OrgVerdict(...)` constructions in the current `simulate_org_template`)
- Modify: `src/digital_twin/drivers/render.py`, `src/digital_twin/drivers/mcp_server.py`
- Test: `tests/verdict/test_org_verdict.py`, `tests/drivers/test_render.py`, `tests/drivers/test_mcp_server.py`, `tests/test_public_api.py`, `tests/drivers/test_cli.py`

This replaces the single `template_id` with `changes: tuple[OrgChange, ...]`. The single-op update path stays behaviorally identical except the shape. Do it as one cohesive migration so the suite stays green.

- [ ] **Step 1: Write/adjust the failing test** — in `tests/verdict/test_org_verdict.py`, change every `OrgVerdict(... template_id="x" ...)` construction to `changes=(OrgChange(ref=ObjectRef("networktemplate", "x", None), action="update"),)`, and assert `ov.changes[0].ref.id == "x"` where a test previously asserted `ov.template_id == "x"`. Run → FAIL (field doesn't exist yet).

- [ ] **Step 2: Implement the dataclass change.** In `org_verdict.py`, replace `template_id: str` with:
```python
    changes: tuple[OrgChange, ...]   # the org objects this plan touches (multi-object-native)
```
The `decide_org` function does not reference `template_id` (its 0-site reasons are generic) — leave it unchanged.

- [ ] **Step 3: Update the 3 constructions in the CURRENT `simulate_org_template`** (`pipeline.py` ~lines 430, 501, 548) — build a single-element `changes` from the resolved op. Add near the top of the function a helper value once `object_type`/`template_id`/`snapshot` are known:
```python
    # built after resolve; the early org_unknown() calls (pre-resolve) pass changes=()
```
Change `org_unknown` to take `changes` (default `()`):
```python
    def org_unknown(rejections, *, template_findings=(), changes=()):
        return OrgVerdict(
            decision=Decision.UNKNOWN,
            decision_reasons=tuple(f"[{r.stage}] {x}" for r in rejections for x in r.reasons),
            changes=tuple(changes), per_site={}, driving_sites=(), site_failures={},
            template_findings=tuple(template_findings), org_rejections=tuple(rejections),
        )
```
For the two success constructions (0-site and the fan-out), pass:
```python
        changes=(OrgChange(ref=ObjectRef(object_type, template_id, name=snapshot.get("name")),
                           action=op.action),)
```
(Import `OrgChange` from `digital_twin.verdict.org_verdict`.)

- [ ] **Step 4: Update the drivers.** In `render.py` `org_verdict_to_dict`, replace `"template_id": ov.template_id` with:
```python
        "changes": [
            {"object_type": c.ref.kind, "object_id": c.ref.id, "name": c.ref.name,
             "action": c.action}
            for c in ov.changes
        ],
```
In `render_org_human`, replace the `template: {ov.template_id}` fragment with a compact list, e.g.:
```python
    changed = ", ".join(f"{c.action} {c.ref.kind} {c.ref.id}" for c in ov.changes) or "(none)"
    lines = [f"org decision: {ov.decision.name}  changes: {changed}"]
```
In `mcp_server.py` `_unknown_org_dict(reason, template_id="")` — it builds an `OrgVerdict` and/or a dict with `template_id`. Replace its `template_id=...` with `changes=()` (the unknown-org early path has no resolved objects), and drop `template_id` from the returned dict (or set `"changes": []`). Adjust its signature to not require `template_id`.

- [ ] **Step 5: Update the affected driver/api tests** — `tests/drivers/test_render.py`, `tests/drivers/test_mcp_server.py`, `tests/test_public_api.py`, `tests/drivers/test_cli.py`: anywhere they assert `template_id` in an org dict/verdict, switch to the `changes` shape (`d["changes"][0]["object_id"]` etc.). Run the affected tests → PASS.

- [ ] **Step 6: FULL gate green**, then commit:
```
git add -A
git commit -m "$(printf 'feat(org-delete): OrgVerdict.template_id -> changes (multi-object-native)\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

## Phase 3 — the fan-out + goldens + wrap

### Task 5: `simulate_org_plan` — multi-op + delete fan-out

**Files:**
- Modify: `src/digital_twin/engine/pipeline.py`
- Modify: `src/digital_twin/engine/org_template.py` (remove now-dead `override_template`/`_pin`; keep `apply_template`)
- Test: `tests/engine/test_org_plan.py` (create)

Generalize the resolved-op handling of `simulate_org_template` into a multi-op loop building `OrgOverlay`s, then fan out over `affected_sites` with `apply_overlays`. Keep `simulate_org_template = simulate_org_plan` as a thin alias (same signature) so all existing callers/tests keep working.

- [ ] **Step 1: Write the failing tests** (`tests/engine/test_org_plan.py`) using a `FakeProvider` that implements `resolve_org_template(scope, id, object_type) -> OrgTemplateContext` (template + assigned_site_ids) and `fetch_sites(scope, site_ids) -> {sid: RawSiteState}` (study `tests/engine/test_pipeline_*` and `tests/providers/test_mist_api.py`'s `FakeProvider` for the exact shapes). Cover:
  1. **single networktemplate delete** assigned to 1 site → the site recompiles with `networktemplate=None`; assert `OrgVerdict.changes` names the delete and the per-site verdict reflects the collapse (e.g. a vlan defined only in the template vanishes → a finding / non-SAFE).
  2. **0-site delete** → `decision == SAFE`, `changes` present, a reason mentioning "no assigned sites".
  3. **two ops, one shared site, combined collapse** — op A (delete networktemplate nt1) and op B (delete gatewaytemplate gt1), both assigned to site S, where each ALONE leaves S still SAFE/incomplete but TOGETHER produces a finding. Assert: applying only A (single-op plan) → no finding; only B → no finding; BOTH → the finding appears, and `changes` names both. (This is the proof A does what B cannot.)
  4. **mixed delete+update** in one plan → both overlays applied to the shared site.

- [ ] **Step 2: Run → FAIL** (`simulate_org_plan` missing / single-op only).

- [ ] **Step 3: Implement** `simulate_org_plan` in `pipeline.py`. Reuse the existing helpers (`parse_change_plan`, `check_objects`, the ORG guard, `apply_template`, `adapter.validate`, `screen_op`, `_changed_roots`, `_stamp`, `_simulate_site_state`, `decide_org`, `build_state_meta`). Structure:

```python
def simulate_org_plan(plan_data, *, provider, adapter=None, registry=None, run=None,
                      l0_full_object=False) -> OrgVerdict:
    run = run or RunContext()
    adapter = adapter or MistAdapter()
    registry = registry or CheckRegistry(ALL_WIRED_CHECKS)

    def org_unknown(rejections, *, template_findings=(), changes=()):
        ...  # as in Task 4

    plan = parse_change_plan(plan_data)
    if isinstance(plan, Rejection):
        return org_unknown((plan,))
    rejection = check_objects(plan)
    if rejection:
        return org_unknown((rejection,))
    is_org = bool(plan.ops) and all(op.object_type in ORG_OBJECT_TYPES for op in plan.ops) \
        and not plan.scope.site_id
    if not is_org:
        return org_unknown((Rejection(stage="scope.pre",
            reasons=("site-scoped plan: call simulate, not simulate_org_plan",)),))

    org_scope = OrgScope(org_id=plan.scope.org_id)
    overlays: list[OrgOverlay] = []
    template_findings: list[Finding] = []
    changes: list[OrgChange] = []
    for op in plan.ops:
        resolved = provider.resolve_org_template(org_scope, op.object_id, op.object_type)
        if not isinstance(resolved, OrgTemplateContext):
            return org_unknown((Rejection(stage="fetch", reasons=tuple(
                f"org-template lookup failed: {f.object}: {f.error}" for f in resolved.failures
            ) or ("org-template lookup failed",)),))
        snapshot = dict(resolved.template)
        ref = ObjectRef(op.object_type, op.object_id, name=snapshot.get("name"))
        if op.action == "delete":
            proposed: Mapping[str, Any] | None = None
        else:
            proposed_t = apply_template(snapshot, op.payload)
            if isinstance(proposed_t, Rejection):
                return org_unknown((proposed_t,))
            l0 = adapter.validate(replace(op, payload=proposed_t),
                scope_roots=None if l0_full_object else _changed_roots(op.payload))
            if l0.fatal:
                return org_unknown((Rejection(stage="l0",
                    reasons=(f"structurally-fatal L0 on proposed {op.object_type} "
                             f"{op.object_id}",)),))
            template_findings.extend(_stamp(l0.findings, ref))
            fg = screen_op(op.object_type, snapshot, proposed_t)
            if fg:
                return org_unknown((fg,), template_findings=tuple(template_findings))
            proposed = proposed_t
        overlays.append(OrgOverlay(
            object_type=op.object_type, object_id=op.object_id, name=snapshot.get("name"),
            action=op.action, assigned_site_ids=frozenset(resolved.assigned_site_ids),
            baseline=snapshot, proposed=proposed,
        ))
        changes.append(OrgChange(ref=ref, action=op.action))

    ov_tuple = tuple(overlays)
    sites = affected_sites(ov_tuple)
    tf = tuple(template_findings)
    if not sites:
        decision, reasons, driving = decide_org({}, template_findings=tf, org_rejections=())
        # auditable 0-site: append the changed objects to the reason
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
            # ... IDENTICAL per-site FetchError handling as the current simulate_org_template
            #     (site_failures[sid] + per_site[sid] = _unknown(baseline_unavailable=True, ...))
            continue
        base_raw, prop_raw = apply_overlays(fetched, sid, ov_tuple)
        sm = build_state_meta(fetched.meta, now=datetime.now(UTC))
        gw_full = any(o.object_type == "gatewaytemplate" and sid in o.assigned_site_ids
                      for o in ov_tuple)
        per_site[sid] = _simulate_site_state(base_raw, prop_raw, adapter=adapter,
            registry=registry, run=run, state_meta=sm, adapter_findings=(),
            profile_proposed=None, gateway_screen_full=gw_full)

    decision, reasons, driving = decide_org(per_site, template_findings=tf, org_rejections=())
    return OrgVerdict(decision=decision, decision_reasons=reasons, changes=tuple(changes),
        per_site=per_site, driving_sites=driving, site_failures=site_failures,
        template_findings=tf, org_rejections=())


simulate_org_template = simulate_org_plan  # back-compat alias (single-op is a 1-op plan)
```

Copy the per-site FetchError block VERBATIM from the current `simulate_org_template` (the `_unknown(... baseline_unavailable=True ...)` construction with the preserved `acquired_at`). Add imports: `OrgOverlay, affected_sites, apply_overlays` from `engine.org_overlay`; `OrgChange` from `verdict.org_verdict`. Then DELETE the old single-op body of `simulate_org_template` (now the alias) and remove `override_template`/`_pin` from `engine/org_template.py` (keep `apply_template`); fix that import in pipeline.

- [ ] **Step 4: Run → PASS** (incl. the two-op-collapse proof), then FULL gate (the alias keeps every existing org test green).

- [ ] **Step 5: Commit:**
```
git add -A
git commit -m "$(printf 'feat(org-delete): simulate_org_plan multi-op + delete fan-out (overlays)\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

### Task 6: Goldens

**Files:** `tests/golden/test_golden_scenarios.py` + `tests/golden/builders.py` (follow the existing org-golden harness — the gatewaytemplate/sitetemplate goldens are the template).

- [ ] **Step 1: Write the goldens.** Using the multi-template `FixtureProvider` (`tests/golden/builders.py` already builds org-template fixtures):
  - **OD-collapse** (the proof): a site assigned to two templates; a plan deleting both where each alone is harmless but together collapses effective config into a finding → assert the org verdict names BOTH `changes` and the per-site finding only appears with both; single-op controls produce no finding.
  - **OD-single**: delete a networktemplate that defines the site's vlans → UNSAFE naming the assigned site(s); the per-site findings name the stranded vlans.
  - **OD-zero**: delete a template assigned to 0 sites → SAFE with the auditable "no assigned sites" reason and the `changes` entry.
  - **OD-mixed**: one plan = delete networktemplate + update gatewaytemplate, shared site → combined per-site verdict.
  - **OD-equiv**: a single-template UPDATE org plan produces the same decision + per-site shape as before, differing only in the `template_id → changes` field (pins the non-regression).

- [ ] **Step 2: Run** `uv run pytest tests/golden -q` → PASS. **Step 3: FULL gate. Step 4: Commit** `feat(org-delete): goldens (two-op collapse, single/zero/mixed, equiv)`.

---

### Task 7: Docs + roadmap + live verify + memory

**Files:** `docs/ROADMAP.md`, the spec (mark Implemented), project memory.

- [ ] **Step 1: FULL gate** green.
- [ ] **Step 2: Live verify (read-only)** — with `.env` loaded, pick a real template assigned to sites and simulate its delete (`digital-twin --plan <a delete plan json>`); confirm the honest collapse verdict (UNSAFE naming the affected sites, per-site findings naming the stranded vlans). Re-run the 8 single-site plans → verdicts unchanged. `.env` MUST NOT be committed; runs are read-only/simulate-only.
- [ ] **Step 3: Docs** — spec status → Implemented; in `docs/ROADMAP.md` flip the DELETE-ripple bullet to reflect "delete + multiple templates per plan DONE" and note the still-deferred pieces (org_networks, WLAN/RF; site-reassignment; apply path).
- [ ] **Step 4: Memory** — add the as-built note to `~/.claude/projects/-Users-tmunzer-4-dev-digital-twin/memory/digital-twin-project.md` (OrgOverlay/`proposed=None`==absent; `apply_overlays`; `object_gate` delete+multi-op+dedup+empty-payload; `simulate_org_plan`; `OrgVerdict.changes`).
- [ ] **Step 5: Commit** `docs(org-delete): roadmap + spec Implemented + live-verified`.

---

## Self-review checklist
- **Spec coverage:** OrgOverlay/OrgChange (T1) ✓; apply_overlays + assigned-site filter (T2) ✓; object_gate delete+multi-op+dedup+empty-payload (T3) ✓; OrgVerdict.changes multi-object (T4) ✓; simulate_org_plan delete + combined-per-site + per-site failure + 0-site auditable + resolve-fail-before-fan-out + gateway_screen_full (T5) ✓; the two-op-collapse golden + equivalence (T6) ✓; docs/live/memory (T7) ✓.
- **`proposed=None` ⇔ layer absent** (never `{}`): `apply_overlays`/`_pin` pin `None`, pinned in T2 tests.
- **Deletes skip L0/field-gate, keep baseline resolution**: T5 branches on `op.action == "delete"`.
- **Resolve failure short-circuits BEFORE fan-out**: the per-op resolve loop returns org_unknown inside the loop (T5).
- **Type consistency:** `OrgOverlay(object_type,object_id,name,action,assigned_site_ids,baseline,proposed)`, `OrgChange(ref,action)`, `affected_sites(overlays)->tuple[str,...]`, `apply_overlays(fetched,site_id,overlays)->(base,prop)`, `simulate_org_plan(...)->OrgVerdict` — consistent across T1–T6.
- **Grep-confirm-before-coding:** the per-site FetchError block to copy verbatim (T5); the exact `FakeProvider`/`OrgTemplateContext` shapes in the engine tests (T5); the existing object_gate tests that change meaning (T3); the full set of `template_id` references in tests (T4 — `tests/test_public_api.py`, `tests/drivers/test_render.py`, `tests/drivers/test_mcp_server.py`, `tests/drivers/test_cli.py`, `tests/verdict/test_org_verdict.py`).
