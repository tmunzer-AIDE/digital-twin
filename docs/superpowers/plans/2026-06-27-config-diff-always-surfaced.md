# Always surface `config_diffs` on UNKNOWN — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface the before→after `ObjectConfigDiff` on every verdict regardless of `Decision.UNKNOWN`, for every op whose `before → after` is computable, across the site / org-template / org-NAC simulate paths.

**Architecture:** Per path: (1) build each op's `object_config_diff` as soon as `effective`/`proposed_t` is known — *before* L0 and the field gate; (2) thread the accumulated diffs through the early-exit helpers; (3) drop the `if decision is not Decision.UNKNOWN` suppression so the final attach is unconditional. The diff stays redacted (no change to `config_diff.py`/`redaction.py`) and strictly non-load-bearing (`decide`/`decide_org` never read it).

**Tech Stack:** Python 3.14, uv, pytest, ruff, mypy. All work in the worktree `/Users/tmunzer/4_dev/digital-twin/.claude/worktrees/config-diff-surfaced` (branch `worktree-fix+config-diff-always-surfaced`, based on `origin/main`).

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-06-27-config-diff-always-surfaced-design.md` is authoritative; the §1 computability table governs which exits carry a diff.
- **Computability boundary:** a diff is included for an op **iff** a real `before` AND `after` exist when the exit fires. In-loop uncomputable exits still pass `tuple(<accumulator>)` so **earlier passed ops survive**; only genuinely pre-loop exits pass `()`.
- **Non-load-bearing:** the diff MUST NOT change any decision/severity/confidence/coverage/finding/reason. `decide`/`decide_org`/`decide` (NAC) take it as no input.
- **Redaction unchanged:** `object_config_diff` already redacts every leaf via `redact_leaf(full_path, …)`; `config_diff.py` and `redaction.py` are NOT modified.
- **No renderer change:** `_render_config_diffs` already consumes `verdict.config_diffs` unconditionally.
- **Gate (run before every commit):** `uv run pytest -q && uv run ruff check . && uv run mypy src` (mypy on `src` only — test-only type noise is not a gate failure).
- **Commit trailer (every commit):** end the message with
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- **Git hazard:** commit ONLY in this worktree. Use `git -C <worktree>` or `cd` into it; never run git in the main checkout `/Users/tmunzer/4_dev/digital-twin`. Verify `git rev-parse --short HEAD` parent before committing.
- **REDACTED sentinel:** `digital_twin.redaction.REDACTED == "‹redacted›"`. `STRIP_KEY_PARTS` includes `"password"`.

## File Structure

- `src/digital_twin/engine/pipeline.py` — the ONLY source file changed. Three regions: `_unknown` helper + `simulate` (site); `simulate_org_plan` (org); `_org_nac_unknown` helper + `simulate_org_nac` (NAC).
- `tests/engine/test_pipeline.py` — site tests (rewrite 2 drop tests, add 2).
- `tests/engine/test_org_plan.py` — org tests (keep the pre-loop drop test, add 3).
- `tests/engine/test_simulate_org_nac.py` — NAC tests (rewrite 2 drop tests, add 2).
- `docs/ROADMAP.md` — record the feature + the superseded 2026-06-23 non-goal.
- NOT changed: `config_diff.py`, `redaction.py`, `drivers/render.py`.

---

### Task 1: Site path (`simulate`)

**Files:**
- Modify: `src/digital_twin/engine/pipeline.py` (`_unknown` ~126-146; `simulate` apply loop ~348-423; final attach ~463-464)
- Test: `tests/engine/test_pipeline.py`

**Interfaces:**
- Consumes: `object_config_diff(*, object_type, object_id, name, action, before, after) -> ObjectConfigDiff` (already imported in `pipeline.py`); `dataclasses.replace` (already imported); `assemble(...)`.
- Produces: `_unknown(rejection, *, adapter_findings, run, state_meta=None, l0_fatal=False, baseline_unavailable=False, config_diffs: tuple[ObjectConfigDiff, ...] = ()) -> Verdict` — the new keyword is **defaulted** (shared with `_simulate_site_state`).

- [ ] **Step 1: Write the failing tests** (in `tests/engine/test_pipeline.py`)

Replace `test_pre_apply_unknown_drops_config_diffs` and `test_post_apply_unknown_drops_config_diffs` (lines ~304-319) with the carries-versions, and add two new tests. Use the existing module fixtures (`SITE`, `SETTING`, `SWITCH`, `_plan`, `_op`, `FakeProvider`). Add `from digital_twin.redaction import REDACTED` to the imports.

```python
def test_field_gate_unknown_carries_config_diff():
    # dhcpd_config.corp.ip is OUT of scope (only type/servers/ip_start/ip_end/gateway
    # are allowlisted) -> field-gate UNKNOWN, in-loop. The diff must now be surfaced.
    bad = {**SETTING, "dhcpd_config": {"corp": {"ip": "9.9.9.9"}}}
    v = simulate(_plan([_op(payload=bad)]), provider=FakeProvider())
    assert v.decision is Decision.UNKNOWN                      # non-load-bearing:
    cds = {d.object_id: d for d in v.config_diffs}             # UNKNOWN + diff coexist
    assert SITE in cds
    assert "dhcpd_config.corp.ip" in {c.path for c in cds[SITE].changes}


def test_derived_gate_unknown_carries_config_diff():
    # vars ripple passes the field gate (vars.* allowlisted) then fails the DERIVED
    # gate inside _simulate_site_state -> post-apply UNKNOWN reached via the final
    # unconditional attach.
    ripple = {**SETTING, "vars": {"dhcp_ip": "10.9.9.9"}}
    v = simulate(_plan([_op(payload=ripple)]), provider=FakeProvider())
    assert v.decision is Decision.UNKNOWN
    assert any("derived_gate" in r for r in v.decision_reasons)
    cds = {d.object_id: d for d in v.config_diffs}
    assert SITE in cds
    assert "vars.dhcp_ip" in {c.path for c in cds[SITE].changes}


def test_object_not_found_keeps_earlier_op_diffs():
    # op0 (site_setting, valid) builds a diff and applies; op1 (device, missing id)
    # hits object-not-found IN the loop -> UNKNOWN. op0's diff must survive.
    good = {**SETTING, "networks": {"corp": {"vlan_id": 10}, "voice": {"vlan_id": 31}}}
    plan = _plan([
        _op(object_type="site_setting", object_id=SITE, payload=good, order=0),
        _op(object_type="device", object_id="nope", payload={"name": "x"}, order=1),
    ])
    v = simulate(plan, provider=FakeProvider())
    assert v.decision is Decision.UNKNOWN
    cds = {d.object_id: d for d in v.config_diffs}
    assert SITE in cds                                  # earlier op survived
    assert "nope" not in cds                            # uncomputable op carries nothing


def test_out_of_scope_secret_leaf_redacted_in_surfaced_diff():
    # switch_mgmt.root_password is out-of-scope (field-gate UNKNOWN) AND secret-keyed.
    # Now that the diff is surfaced, the value must still be redacted.
    secret = {**SETTING, "switch_mgmt": {"root_password": "hunter2"}}
    v = simulate(_plan([_op(payload=secret)]), provider=FakeProvider())
    assert v.decision is Decision.UNKNOWN
    cds = {d.object_id: d for d in v.config_diffs}
    by = {c.path: c for c in cds[SITE].changes}
    assert by["switch_mgmt.root_password"].after == REDACTED
    assert by["switch_mgmt.root_password"].after != "hunter2"


def test_site_l0_fatal_carries_config_diff(monkeypatch):
    # Force L0 fatal (unreachable with natural payloads — effective is always a dict).
    # The diff is built BEFORE validate, so the L0-fatal early return must carry it.
    from digital_twin.adapters.mist.adapter import MistAdapter
    from digital_twin.adapters.mist.validate import L0Result
    monkeypatch.setattr(MistAdapter, "validate", lambda self, op, **k: L0Result(findings=(), fatal=True))
    good = {**SETTING, "networks": {"corp": {"vlan_id": 10}, "voice": {"vlan_id": 31}}}
    v = simulate(_plan([_op(payload=good)]), provider=FakeProvider())
    assert v.decision is Decision.UNKNOWN
    cds = {d.object_id: d for d in v.config_diffs}
    assert SITE in cds


def test_site_apply_reject_carries_config_diff(monkeypatch):
    # Force adapter.apply to reject (post-screen_op, post-build). The in-loop apply
    # rejection early return must carry the already-built diff.
    from digital_twin.adapters.mist.adapter import MistAdapter
    from digital_twin.contracts import Rejection
    monkeypatch.setattr(MistAdapter, "apply",
                        lambda self, raw, ops: Rejection(stage="apply", reasons=("forced",)))
    good = {**SETTING, "networks": {"corp": {"vlan_id": 10}, "voice": {"vlan_id": 31}}}
    v = simulate(_plan([_op(payload=good)]), provider=FakeProvider())
    assert v.decision is Decision.UNKNOWN
    cds = {d.object_id: d for d in v.config_diffs}
    assert SITE in cds
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/engine/test_pipeline.py -q -k "field_gate_unknown_carries or derived_gate_unknown_carries or object_not_found_keeps or out_of_scope_secret or site_l0_fatal_carries or site_apply_reject_carries"`
Expected: FAIL — all six assert non-empty `config_diffs`, but today `config_diffs == ()` on every UNKNOWN (the assertions on `cds[SITE]` / `SITE in cds` raise `KeyError`/`AssertionError`).

- [ ] **Step 3: Add the `config_diffs` parameter to `_unknown`**

In `src/digital_twin/engine/pipeline.py`, change `_unknown` (~126) to accept and attach `config_diffs`:

```python
def _unknown(
    rejection: Rejection | None,
    *,
    adapter_findings: tuple[Finding, ...],
    run: RunContext,
    state_meta: StateMetaView | None = None,
    l0_fatal: bool = False,
    baseline_unavailable: bool = False,
    config_diffs: tuple[ObjectConfigDiff, ...] = (),
) -> Verdict:
    return replace(
        assemble(
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
        ),
        config_diffs=config_diffs,
    )
```

- [ ] **Step 4: Build the diff early and thread it through the loop's direct returns**

In `simulate`, build the per-op diff immediately after `effective` is computed (~380), and REMOVE the later build at ~411-414. The loop body from `effective = ...` becomes:

```python
            effective = effective_update(current, op.payload)
            # Build the before→after NOW (pure structural data, independent of
            # validation) so it is available to every downstream early exit.
            site_diffs.append(object_config_diff(
                object_type=op.object_type, object_id=op.object_id,
                name=current.get("name"), action=op.action,
                before=current, after=effective))
            unknown_roots = frozenset(
                p.split(".", 1)[0] for p in changed_paths(current, effective)
            )
            result = adapter.validate(
                replace(op, payload=effective),
                scope_roots=None if l0_full_object else _changed_roots(op.payload),
                unknown_scope_roots=None if l0_full_object else unknown_roots,
            )
            subject = ObjectRef(op.object_type, op.object_id, name=current.get("name"))
            adapter_findings += _stamp(result.findings, subject)
            if result.fatal:
                return _unknown(
                    None, adapter_findings=adapter_findings, run=run,
                    l0_fatal=True, state_meta=state_meta,
                    config_diffs=tuple(site_diffs),
                )
            rejection = screen_op(op.object_type, current, effective)
            if rejection:
                return _unknown(
                    rejection, adapter_findings=adapter_findings, run=run,
                    state_meta=state_meta, config_diffs=tuple(site_diffs),
                )
            applied = adapter.apply(proposed_raw, (op,))  # apply owns the semantics
            if isinstance(applied, Rejection):
                return _unknown(
                    applied, adapter_findings=adapter_findings, run=run,
                    state_meta=state_meta, config_diffs=tuple(site_diffs),
                )
            proposed_raw = applied
```

(The old `site_diffs.append(object_config_diff(...))` block at ~411-414 is now gone — it moved above the L0 validate.)

- [ ] **Step 5: Thread the two pre-`:380` in-loop exits and the post-loop crash exit**

The object-not-found (~353) and conflict (~367) returns precede the build, so they carry only prior ops — still `tuple(site_diffs)`. Add `config_diffs=tuple(site_diffs)` to both:

```python
            if current is None:
                return _unknown(
                    Rejection(stage="apply", reasons=(
                        f"ops[order={op.order}]: no {op.object_type} with id "
                        f"{op.object_id!r} in fetched state",)),
                    adapter_findings=adapter_findings, run=run,
                    state_meta=state_meta, config_diffs=tuple(site_diffs),
                )
            conflicts = update_conflicts(op.payload)
            if conflicts:
                return _unknown(
                    Rejection(stage="apply", reasons=tuple(
                        f"ops[order={op.order}]: conflicting set AND '-{c}' delete "
                        "marker for the same attribute" for c in conflicts)),
                    adapter_findings=adapter_findings, run=run,
                    state_meta=state_meta, config_diffs=tuple(site_diffs),
                )
```

And the post-loop below-profile ingest-crash return (~451) carries the full `site_diffs`:

```python
        except Exception as e:  # noqa: BLE001 — any ingest crash is UNKNOWN
            return _unknown(
                Rejection(stage="ingest", reasons=(f"baseline ingest crashed: {e}",)),
                adapter_findings=adapter_findings, run=run,
                state_meta=state_meta, baseline_unavailable=True,
                config_diffs=tuple(site_diffs),
            )
```

Leave the pre-loop returns (parse `:301`, check_objects `:304`, org-plan-no-site `:313`, fetch-fail `:327`) UNCHANGED — they precede the loop, so `()` (the default) is correct.

- [ ] **Step 6: Make the final attach unconditional**

Replace the guarded attach (~463-464):

```python
    verdict = _simulate_site_state(
        raw, proposed_raw,
        adapter=adapter, registry=registry, run=run,
        state_meta=state_meta, adapter_findings=adapter_findings,
        profile_proposed=profile_proposed,
    )
    return replace(verdict, config_diffs=tuple(site_diffs))
```

This covers every fall-through return of `_simulate_site_state` (including its internal `_unknown` exits and the derived-gate UNKNOWN), so those internal calls need no threading.

- [ ] **Step 7: Run the six tests to verify they pass**

Run: `uv run pytest tests/engine/test_pipeline.py -q -k "field_gate_unknown_carries or derived_gate_unknown_carries or object_not_found_keeps or out_of_scope_secret or site_l0_fatal_carries or site_apply_reject_carries"`
Expected: PASS (6 passed).

- [ ] **Step 8: Run the site suite + gate**

Run: `uv run pytest tests/engine/test_pipeline.py -q` (expected: all pass; `test_site_update_carries_config_diff` and `test_unknown_short_circuit_has_no_diagrams` unaffected). Then the full gate: `uv run pytest -q && uv run ruff check . && uv run mypy src`.
Expected: green. (Every computable site exit in §1 — field-gate, L0-fatal, apply-reject, derived-gate — now has a direct test; the L0-fatal and apply-reject branches are forced via monkeypatch since natural payloads can't reach them.)

- [ ] **Step 9: Commit**

```bash
git add src/digital_twin/engine/pipeline.py tests/engine/test_pipeline.py
git commit -m "$(cat <<'EOF'
fix(pipeline): surface config_diffs on site UNKNOWN verdicts

Build the per-op diff before L0/field-gate, thread it through simulate()'s
in-loop and post-loop early returns, and make the final attach unconditional.
config_diffs stays redacted and non-load-bearing.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Org-template path (`simulate_org_plan`)

**Files:**
- Modify: `src/digital_twin/engine/pipeline.py` (`org_unknown` closure ~481-490; the overlay-building loop ~521-561; the no-sites attach ~574; the final attach ~618)
- Test: `tests/engine/test_org_plan.py`

**Interfaces:**
- Consumes: `object_config_diff(...)`; the `org_diffs: list[ObjectConfigDiff]` accumulator (defined ~520).
- Produces: `org_unknown(rejections, *, template_findings=(), changes=(), config_diffs=()) -> OrgVerdict` — `config_diffs` **required at every call site** (pass `()` explicitly for pre-loop exits).

- [ ] **Step 1: Write the failing tests** (in `tests/engine/test_org_plan.py`)

Keep `test_org_unknown_drops_config_diffs` (it tests the PRE-loop `check_objects` rejection — `()` stays correct; add a clarifying comment). Add three tests using the existing fixtures (`_plan`, `_upd`, `_del`, `_two_op_provider`, `_single_delete_provider`, `FakeProvider`).

```python
def test_org_field_gate_unknown_carries_config_diff():
    # switch_mgmt is out-of-scope for sitetemplate -> in-loop field-gate UNKNOWN.
    ov = simulate_org_plan(
        _plan(_upd("sitetemplate", "st1", {"switch_mgmt": {"root_password": "x"}})),
        provider=_two_op_provider())
    assert ov.decision is Decision.UNKNOWN
    cds = {d.object_id: d for d in ov.config_diffs}
    assert "st1" in cds and cds["st1"].object_type == "sitetemplate"


def test_org_template_lookup_failed_keeps_earlier_op_diffs():
    # op0 (st1, in-scope) builds a diff; op1 ("ghost", unknown) -> template-lookup
    # failed IN the loop -> UNKNOWN. op0's diff must survive.
    ov = simulate_org_plan(
        _plan(
            _upd("sitetemplate", "st1", {"port_usages": {"trunkB": {"mode": "trunk", "networks": []}}}, order=0),
            _upd("networktemplate", "ghost", {"port_usages": {"x": {"mode": "access"}}}, order=1),
        ),
        provider=_two_op_provider())
    assert ov.decision is Decision.UNKNOWN
    cds = {d.object_id: d for d in ov.config_diffs}
    assert "st1" in cds                      # earlier op survived
    assert "ghost" not in cds                # uncomputable op carries nothing


def test_org_final_unknown_carries_config_diff(monkeypatch):
    # Force the POST-loop decision to UNKNOWN so we exercise the (now unconditional)
    # final attach at ~618, not an in-loop reject. decide_org returns
    # (decision, reasons, driving).
    import digital_twin.engine.pipeline as pl
    monkeypatch.setattr(pl, "decide_org", lambda *a, **k: (Decision.UNKNOWN, ("forced",), ()))
    ov = simulate_org_plan(
        _plan(_upd("sitetemplate", "st1", {"port_usages": {"trunkB": {"mode": "trunk", "networks": []}}})),
        provider=_two_op_provider())
    assert ov.decision is Decision.UNKNOWN
    cds = {d.object_id: d for d in ov.config_diffs}
    assert "st1" in cds


def test_org_l0_fatal_carries_config_diff(monkeypatch):
    # Force L0 fatal on the org op; the diff is built before validate, so the
    # in-loop L0-fatal early return must carry it.
    from digital_twin.adapters.mist.adapter import MistAdapter
    from digital_twin.adapters.mist.validate import L0Result
    monkeypatch.setattr(MistAdapter, "validate", lambda self, op, **k: L0Result(findings=(), fatal=True))
    ov = simulate_org_plan(
        _plan(_upd("sitetemplate", "st1", {"port_usages": {"trunkB": {"mode": "trunk", "networks": []}}})),
        provider=_two_op_provider())
    assert ov.decision is Decision.UNKNOWN
    cds = {d.object_id: d for d in ov.config_diffs}
    assert "st1" in cds


def test_org_apply_template_reject_keeps_earlier_op_diff(monkeypatch):
    # op0 (st1) builds its diff; op1 (nt1) is forced to fail apply_template — the
    # step that would compute op1's `after`, so op1 is uncomputable. op0 survives.
    import digital_twin.engine.pipeline as pl
    from digital_twin.contracts import Rejection
    real = pl.apply_template
    calls = {"n": 0}

    def fake(snapshot, payload):
        calls["n"] += 1
        if calls["n"] >= 2:
            return Rejection(stage="apply", reasons=("forced apply_template fail",))
        return real(snapshot, payload)

    monkeypatch.setattr(pl, "apply_template", fake)
    ov = simulate_org_plan(
        _plan(
            _upd("sitetemplate", "st1", {"port_usages": {"trunkB": {"mode": "trunk", "networks": []}}}, order=0),
            _upd("networktemplate", "nt1", {"port_usages": {"trunkA": {"mode": "trunk", "networks": []}}}, order=1),
        ),
        provider=_two_op_provider())
    assert ov.decision is Decision.UNKNOWN
    cds = {d.object_id: d for d in ov.config_diffs}
    assert "st1" in cds        # earlier op survived
    assert "nt1" not in cds    # apply_template failed -> no `after` -> uncomputable
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/engine/test_org_plan.py -q -k "org_field_gate_unknown_carries or org_template_lookup_failed_keeps or org_final_unknown_carries or org_l0_fatal_carries or org_apply_template_reject_keeps"`
Expected: FAIL — `config_diffs` is `()` on UNKNOWN today, so `"st1"/"nt1" in cds` raises.

- [ ] **Step 3: Add `config_diffs` to `org_unknown`**

Change the nested `org_unknown` closure (~481):

```python
    def org_unknown(
        rejections: tuple[Rejection, ...], *, template_findings: tuple[Finding, ...] = (),
        changes: tuple[OrgChange, ...] = (), config_diffs: tuple[ObjectConfigDiff, ...] = (),
    ) -> OrgVerdict:
        return OrgVerdict(
            decision=Decision.UNKNOWN,
            decision_reasons=tuple(f"[{r.stage}] {x}" for r in rejections for x in r.reasons),
            changes=tuple(changes), per_site={}, driving_sites=(), site_failures={},
            template_findings=tuple(template_findings), org_rejections=tuple(rejections),
            config_diffs=tuple(config_diffs),
        )
```

(Default `()` is fine; the constraint "required at every call site" is satisfied by explicitly passing it below — the default just keeps the three pre-loop calls terse.)

- [ ] **Step 4: Build diffs early in the loop and thread the in-loop exits**

In the overlay loop (~521-561), build the diff for each op right when `before`/`after` exist, BEFORE L0/gate, and REMOVE the end-of-loop build at ~558. Replace the body from `if op.action == "delete":` through `overlays.append(...)`:

```python
        if op.action == "delete":
            proposed: Mapping[str, Any] | None = None
            org_diffs.append(object_config_diff(
                object_type=op.object_type, object_id=op.object_id,
                name=snapshot.get("name"), action=op.action, before=snapshot, after=None))
        else:
            proposed_t = apply_template(snapshot, op.payload)
            if isinstance(proposed_t, Rejection):
                return org_unknown((proposed_t,),
                    template_findings=tuple(template_findings), changes=tuple(changes),
                    config_diffs=tuple(org_diffs))
            org_diffs.append(object_config_diff(
                object_type=op.object_type, object_id=op.object_id,
                name=snapshot.get("name"), action=op.action, before=snapshot, after=proposed_t))
            l0 = adapter.validate(replace(op, payload=proposed_t),
                scope_roots=None if l0_full_object else _changed_roots(op.payload))
            if l0.fatal:
                return org_unknown((Rejection(stage="l0",
                    reasons=(f"structurally-fatal L0 on proposed {op.object_type} "
                             f"{op.object_id}",)),),
                    template_findings=tuple(template_findings), changes=tuple(changes),
                    config_diffs=tuple(org_diffs))
            template_findings.extend(_stamp(l0.findings, ref))
            fg = screen_op(op.object_type, snapshot, proposed_t)
            if fg:
                return org_unknown((fg,), template_findings=tuple(template_findings),
                                   changes=tuple(changes), config_diffs=tuple(org_diffs))
            proposed = proposed_t
        overlays.append(OrgOverlay(
            object_type=op.object_type, object_id=op.object_id, name=snapshot.get("name"),
            action=op.action, assigned_site_ids=frozenset(resolved.assigned_site_ids),
            baseline=snapshot, proposed=proposed,
        ))
        # (the old org_diffs.append(...) at ~558 is removed — built above)
```

Thread the template-lookup-failed exit (~526), which precedes `snapshot` for the current op → carries prior ops:

```python
        if not isinstance(resolved, OrgTemplateContext):
            return org_unknown((Rejection(stage="fetch", reasons=tuple(
                f"org-template lookup failed: {f.object}: {f.error}" for f in resolved.failures
            ) or ("org-template lookup failed",)),),
                template_findings=tuple(template_findings), changes=tuple(changes),
                config_diffs=tuple(org_diffs))
```

Leave the pre-loop `org_unknown` calls (`:494`, `:510`, `:512`) UNCHANGED (default `()`).

- [ ] **Step 5: Make both final attaches unconditional**

No-sites path (~574):

```python
        return OrgVerdict(decision=decision, decision_reasons=reasons, changes=tuple(changes),
            per_site={}, driving_sites=driving, site_failures={},
            template_findings=tf, org_rejections=(),
            config_diffs=tuple(org_diffs))
```

Final path (~618):

```python
    return OrgVerdict(
        decision=decision, decision_reasons=reasons, changes=tuple(changes),
        per_site=per_site, driving_sites=driving, site_failures=site_failures,
        template_findings=tf, org_rejections=(),
        config_diffs=tuple(org_diffs),
    )
```

- [ ] **Step 6: Run the new tests + verify the pre-loop drop test still passes**

Run: `uv run pytest tests/engine/test_org_plan.py -q -k "org_field_gate_unknown_carries or org_template_lookup_failed_keeps or org_final_unknown_carries or org_l0_fatal_carries or org_apply_template_reject_keeps or org_unknown_drops or org_update_carries or org_delete_lists"`
Expected: PASS. `test_org_unknown_drops_config_diffs` (pre-loop `check_objects`) still asserts `()` and passes.

- [ ] **Step 7: Full gate**

Run: `uv run pytest -q && uv run ruff check . && uv run mypy src`
Expected: green.

- [ ] **Step 8: Commit**

```bash
git add src/digital_twin/engine/pipeline.py tests/engine/test_org_plan.py
git commit -m "$(cat <<'EOF'
fix(pipeline): surface config_diffs on org-template UNKNOWN verdicts

Build template-object diffs before L0/gate, thread org_unknown through the
in-loop exits (lookup-failed/apply_template/L0/field-gate carry prior ops),
and drop the decision guard on both final attaches.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Org-NAC path (`simulate_org_nac`)

**Files:**
- Modify: `src/digital_twin/engine/pipeline.py` (`_org_nac_unknown` ~642-647; the NAC apply loop ~684-728; the L0-fatal direct return ~714-718; the final attach ~759-763)
- Test: `tests/engine/test_simulate_org_nac.py`

**Interfaces:**
- Consumes: `object_config_diff(...)`; `nac_diffs: list[ObjectConfigDiff]` accumulator (~682).
- Produces: `_org_nac_unknown(rej: Rejection | None = None, *, adapter_findings=(), l0_fatal: bool = False, config_diffs=()) -> OrgNacVerdict` — `rej` optional (None for the L0-fatal route), `l0_fatal` flag, `config_diffs` keyword.

- [ ] **Step 1: Write the failing tests** (in `tests/engine/test_simulate_org_nac.py`)

Rewrite `test_config_diff_empty_on_unknown` and `test_config_diff_dropped_when_later_op_makes_plan_unknown` (lines ~201-218) to carries-versions, and add two tests. Use the existing fixtures (`BASE`, `_rule`, `_plan`, `_op`, `NacFetch`, `FakeProvider`). Imports needed: `from digital_twin.adapters.mist.validate import L0Result` and (for the redaction-free assertions) none extra.

```python
def test_field_gate_unknown_carries_config_diff():
    # guest_auth_state is not an allowlisted nacrule leaf -> in-loop field-gate UNKNOWN.
    nf = NacFetch(rules=BASE, tags=())
    v = simulate_org_nac(_plan(_op("update", "b", {"guest_auth_state": "x"})),
                         provider=FakeProvider(nf))
    assert v.decision is Decision.UNKNOWN
    cds = {d.object_id: d for d in v.config_diffs}
    assert "b" in cds
    assert "guest_auth_state" in {c.path for c in cds["b"].changes}


def test_later_op_unknown_keeps_all_computable_diffs():
    # op0 (b, valid) and op1 (a, out-of-scope field) are BOTH computable -> both diffs
    # present even though op1 forces UNKNOWN.
    nf = NacFetch(rules=BASE, tags=())
    v = simulate_org_nac(
        _plan(_op("update", "b", {"order": 0}, order=0),
              _op("update", "a", {"guest_auth_state": "x"}, order=1)),
        provider=FakeProvider(nf))
    assert v.decision is Decision.UNKNOWN
    cds = {d.object_id: d for d in v.config_diffs}
    assert "b" in cds and "a" in cds


def test_nac_l0_fatal_direct_return_carries_config_diff(monkeypatch):
    # The NAC L0-fatal branch returns OrgNacVerdict directly (not via the helper).
    # Force fatal=True to exercise it; the op's diff (built before L0) must survive.
    import digital_twin.engine.pipeline as pl
    monkeypatch.setattr(
        pl, "validate_payload",
        lambda *a, **k: L0Result(findings=(), fatal=True))
    nf = NacFetch(rules=BASE, tags=())
    v = simulate_org_nac(_plan(_op("update", "b", {"order": 0})), provider=FakeProvider(nf))
    assert v.decision is Decision.UNKNOWN
    cds = {d.object_id: d for d in v.config_diffs}
    assert "b" in cds


def test_nac_final_unknown_carries_config_diff(monkeypatch):
    # Force the POST-loop decision to UNKNOWN so we exercise the (now unconditional)
    # final attach at ~759, not an in-loop reject. decide returns (decision, reasons).
    import digital_twin.engine.pipeline as pl
    monkeypatch.setattr(pl, "decide", lambda *a, **k: (Decision.UNKNOWN, ("forced",)))
    nf = NacFetch(rules=BASE, tags=())
    v = simulate_org_nac(_plan(_op("update", "b", {"order": 0})), provider=FakeProvider(nf))
    assert v.decision is Decision.UNKNOWN
    cds = {d.object_id: d for d in v.config_diffs}
    assert "b" in cds
```

When rewriting, **delete the two old drop tests** `test_config_diff_empty_on_unknown`
(~201) and `test_config_diff_dropped_when_later_op_makes_plan_unknown` (~209) — they
are replaced by `test_field_gate_unknown_carries_config_diff` and
`test_later_op_unknown_keeps_all_computable_diffs` above.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/engine/test_simulate_org_nac.py -q -k "field_gate_unknown_carries or later_op_unknown_keeps or l0_fatal_direct or nac_final_unknown"`
Expected: FAIL — the field-gate / later-op / L0-fatal cases are UNKNOWN with `config_diffs == ()` today.

- [ ] **Step 3: Generalize `_org_nac_unknown`**

Change the helper (~642):

```python
def _org_nac_unknown(
    rej: Rejection | None = None, *, adapter_findings: tuple[Finding, ...] = (),
    l0_fatal: bool = False, config_diffs: tuple[ObjectConfigDiff, ...] = (),
) -> OrgNacVerdict:
    decision, reasons = decide(DecisionInputs(
        rejections=(rej,) if rej else (), l0_fatal=l0_fatal, baseline_unavailable=False,
        check_results=(), adapter_findings=adapter_findings))
    return OrgNacVerdict(decision, reasons, (), (), adapter_findings,
                         (rej,) if rej else (), tuple(config_diffs))
```

(`OrgNacVerdict` positional order is `decision, reasons, changes, check_results, adapter_findings, org_rejections, config_diffs` — confirm against `verdict/org_nac_verdict.py`.)

- [ ] **Step 4: Build create/update diffs early; route L0-fatal through the helper; thread the in-loop exits**

In the NAC loop (~684-728): the delete build at ~695 stays. For create/update, move the build to right after `effective` is finalized (~706-708), BEFORE the L0 call (~711), and REMOVE the build at ~722. Thread every in-loop exit:

```python
    for op in sorted(plan.ops, key=lambda o: o.order):
        exists = op.object_id in baseline_raw
        if op.action in ("update", "delete") and not exists:
            return _org_nac_unknown(Rejection(stage="apply", reasons=(
                f"ops[order={op.order}]: no nacrule with id {op.object_id!r}",)),
                adapter_findings=adapter_findings, config_diffs=tuple(nac_diffs))
        if op.action == "create" and exists:
            return _org_nac_unknown(Rejection(stage="apply", reasons=(
                f"ops[order={op.order}]: nacrule id {op.object_id!r} already exists",)),
                adapter_findings=adapter_findings, config_diffs=tuple(nac_diffs))
        if op.action == "delete":
            nac_diffs.append(object_config_diff(
                object_type="nacrule", object_id=op.object_id,
                name=baseline_raw[op.object_id].get("name"),
                action="delete", before=baseline_raw[op.object_id], after={}))
            proposed_raw.pop(op.object_id, None)
            continue
        if update_conflicts(op.payload):
            return _org_nac_unknown(Rejection(stage="apply", reasons=(
                f"ops[order={op.order}]: conflicting set AND '-' delete marker",)),
                adapter_findings=adapter_findings, config_diffs=tuple(nac_diffs))
        current = baseline_raw.get(op.object_id, {"id": op.object_id})
        effective = effective_update(current, op.payload)
        if op.action == "create":
            effective["id"] = op.object_id
        nac_diffs.append(object_config_diff(
            object_type="nacrule", object_id=op.object_id,
            name=effective.get("name") if op.action == "create" else current.get("name"),
            action=op.action,
            before={} if op.action == "create" else current, after=effective))
        scope_roots = None if (op.action == "create" or l0_full_object) \
            else _changed_roots(op.payload)
        l0 = validate_payload("nacrule", effective, scope_roots=scope_roots)
        subject = ObjectRef("nacrule", op.object_id, name=current.get("name"))
        adapter_findings += _stamp(l0.findings, subject)
        if l0.fatal:
            return _org_nac_unknown(
                adapter_findings=adapter_findings, l0_fatal=True,
                config_diffs=tuple(nac_diffs))
        fg = screen_op("nacrule", current, effective)
        if fg:
            return _org_nac_unknown(fg, adapter_findings=adapter_findings,
                                    config_diffs=tuple(nac_diffs))
        proposed_raw[op.object_id] = effective
        # (the old nac_diffs.append at ~722 is removed — built above)
```

Note: `_stamp(l0.findings, subject)` must run before the `l0.fatal` return so the fatal verdict still carries the L0 findings — match the existing order (findings stamped, then the fatal check). The snippet above preserves that.

- [ ] **Step 5: Make the final attach unconditional**

Final return (~759):

```python
    return OrgNacVerdict(
        decision, reasons, nac_changes(diff, base_map, prop_map),
        results, adapter_findings, (),
        tuple(nac_diffs),
    )
```

- [ ] **Step 6: Run the new tests + the unaffected create/delete/update diff tests**

Run: `uv run pytest tests/engine/test_simulate_org_nac.py -q`
Expected: PASS. `test_config_diff_update_shows_redacted_before_after`, `_create_`, `_delete_` (non-UNKNOWN) unaffected. (Note: `test_nac_final_unknown_carries_config_diff` asserts `"b" in cds` regardless of decision; the unconditional attach is what guarantees it on any decision.)

- [ ] **Step 7: Full gate**

Run: `uv run pytest -q && uv run ruff check . && uv run mypy src`
Expected: green.

- [ ] **Step 8: Commit**

```bash
git add src/digital_twin/engine/pipeline.py tests/engine/test_simulate_org_nac.py
git commit -m "$(cat <<'EOF'
fix(pipeline): surface config_diffs on org-NAC UNKNOWN verdicts

Build rule diffs before L0/gate, route the direct L0-fatal branch through the
generalized _org_nac_unknown helper (l0_fatal + config_diffs), thread all
in-loop exits, and drop the final decision guard.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Roadmap + full-suite verification

**Files:**
- Modify: `docs/ROADMAP.md`

- [ ] **Step 1: Record the feature in `docs/ROADMAP.md`**

Add an entry under the most recent dated/changelog section (match the file's existing format — read the top of `docs/ROADMAP.md` first and mirror the surrounding entries' style):

```markdown
- **config_diffs surfaced on UNKNOWN** (2026-06-27): the before→after `ObjectConfigDiff`
  is now attached to every verdict regardless of `Decision.UNKNOWN`, for each op whose
  `before → after` is computable, across the site / org-template / org-NAC paths
  (field-gate / L0-fatal / apply-reject / derived-gate UNKNOWNs all carry the diff).
  Scoping is **per op, not per verdict**: an op with no computable `before → after`
  (parse, object-not-found, baseline fetch failure, org `apply_template` reject)
  contributes no diff, but every earlier computable op's diff in the same plan still
  survives. Supersedes the 2026-06-23 "config diffs on UNKNOWN/rejected plans"
  non-goal. The diff stays redacted and non-load-bearing.
```

- [ ] **Step 2: Run the full gate one final time**

Run: `uv run pytest -q && uv run ruff check . && uv run mypy src`
Expected: green — full suite passes (the three rewritten drop tests now assert "carries"; the org pre-loop drop test still asserts `()`); ruff clean; mypy clean on `src`.

- [ ] **Step 3: Confirm no unintended golden churn**

Run: `uv run pytest -q -k golden` (or the project's golden suite path) and review any changed golden: every newly-populated `config_diffs` block on a previously-UNKNOWN golden must be **correct and redacted** — review leaf-by-leaf, never blind-regenerate. If a golden needs updating, update it in this commit with the reason noted.
Expected: either no golden churn, or reviewed+justified updates.

- [ ] **Step 4: Commit**

```bash
git add docs/ROADMAP.md
git commit -m "$(cat <<'EOF'
docs(roadmap): record config_diffs-on-UNKNOWN; supersede 2026-06-23 non-goal

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```
