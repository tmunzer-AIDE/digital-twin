# Derived-gate coverage gaps implementation plan

**Spec:** `docs/superpowers/specs/2026-06-27-derived-gate-coverage-gaps-design.md`  
**Status:** PROPOSED  
**Date:** 2026-06-27

## Architecture

Split today's single `UNKNOWN` bucket into two verdict-layer inputs:

- **Hard UNKNOWN** remains `DecisionInputs.rejections` / `l0_fatal` /
  `baseline_unavailable`. These are pre-simulation or untrustworthy-simulation
  failures and still dominate everything.
- **Coverage-gap UNKNOWN** is a new `DecisionInputs.coverage_gaps` channel for
  post-ingest gates where the modeled IR is valid-enough to run checks:
  `check_derived(...)` rejections (`derived_gate` leaf gaps and `dhcp_*` semantic
  gaps) plus `device_profile_rejection(...)`.

`decide()` becomes:

```text
hard-UNKNOWN > UNSAFE > coverage-gap UNKNOWN > REVIEW > SAFE
```

`decide_org()` also flips cross-site rollup precedence so a valid site's `UNSAFE`
outranks another site's `UNKNOWN`:

```python
_PRECEDENCE = {SAFE: 0, REVIEW: 1, UNKNOWN: 2, UNSAFE: 3}
```

Coverage gaps surface as neutral adapter/operational findings:

```python
code="coverage.gap"
category=FindingCategory.OPERATIONAL
severity=Severity.WARNING
confidence=Confidence(HIGH)
evidence["stage"] = rejection.stage
```

They never drive UNSAFE themselves. UNSAFE only comes from modeled NETWORK
ERROR/CRITICAL findings.

## Files

- Modify `src/digital_twin/verdict/decision.py`
- Inspect `src/digital_twin/verdict/verdict.py` (no code change expected unless
  type-checking reveals it is needed; `assemble()` already passes
  `DecisionInputs` to `decide()` and flattens `adapter_findings`)
- Modify `src/digital_twin/verdict/org_verdict.py`
- Modify `src/digital_twin/scope/derived_gate.py`
- Modify `src/digital_twin/scope/device_profile_gate.py`
- Modify `src/digital_twin/engine/pipeline.py`
- Modify tests:
  - `tests/verdict/test_decision.py`
  - `tests/verdict/test_org_verdict.py`
  - `tests/scope/test_derived_gate.py`
  - `tests/scope/test_device_profile_gate.py`
  - `tests/engine/test_pipeline.py`
  - `tests/engine/test_gateway_derived.py`
  - `tests/engine/test_pipeline_device_profile.py`
  - `tests/engine/test_org_plan.py`
  - `tests/golden/test_golden_scenarios.py`
- Modify `docs/ROADMAP.md`

## Plan notes

Test names below are grounded in the repository at plan-writing time. If a nearby
test is renamed before execution, preserve the assertion shape and adapt the name;
do not treat a stale name as permission to weaken the behavioral check.

Behavior-changing tasks must not stage a red commit. Tasks 3 and 4 include a full
offline `uv run pytest -q` checkpoint because they alter core verdict precedence
and can move broad goldens from bare UNKNOWN to assessed findings.

## Task 1: Decision tier

### Goal

Add `coverage_gaps` to `DecisionInputs`, implement the 5-tier precedence in
`decide()`, and keep existing callers source-compatible via a default `()`.

### Code

In `src/digital_twin/verdict/decision.py`:

1. Update the module docstring precedence line.
2. Add a defaulted field:

```python
coverage_gaps: tuple[Rejection, ...] = ()
```

3. Keep the current hard-UNKNOWN block for `rejections`, `l0_fatal`, and
   `baseline_unavailable`.
4. Keep UNSAFE evaluation before REVIEW.
5. Insert coverage-gap UNKNOWN between UNSAFE and REVIEW:

```python
gap_reasons = [
    f"COVERAGE GAP [{r.stage}]: {reason}"
    for r in inputs.coverage_gaps
    for reason in r.reasons
]
if gap_reasons:
    return Decision.UNKNOWN, tuple(gap_reasons)
```

In `src/digital_twin/verdict/verdict.py`:

- No finding synthesis is expected here. `coverage_gaps` drives decision
  precedence only; Task 3 emits the operator-facing `coverage.gap` findings through
  `adapter_findings`, where their `subject`, `affected_entities`, and rich
  `evidence` can be attached. `assemble()` already flattens `adapter_findings` into
  `Verdict.findings` and passes the full `DecisionInputs` object to `decide()`.
  Touch this file only if type-checking or tests show it needs a comment/doc
  adjustment.

### Tests

Update `tests/verdict/test_decision.py`:

- Rename/update `test_precedence_unknown_beats_unsafe` to
  `test_hard_unknown_still_beats_unsafe`.
- Add the coverage-gap tier tests with this assertion shape:

```python
def test_hard_unknown_still_beats_unsafe():
    res = _result(Status.FAIL, [_finding(Severity.ERROR)])
    d, reasons = decide(
        _inputs(
            rejections=(Rejection(stage="envelope", reasons=("bad",)),),
            check_results=(res,),
        )
    )
    assert d is Decision.UNKNOWN
    assert any(reason.startswith("UNSUPPORTED [envelope]") for reason in reasons)


def test_coverage_gap_alone_is_unknown_with_coverage_prefix():
    d, reasons = decide(
        _inputs(coverage_gaps=(Rejection(stage="derived_gate", reasons=("x",)),))
    )
    assert d is Decision.UNKNOWN
    assert reasons == ("COVERAGE GAP [derived_gate]: x",)


def test_coverage_gap_plus_network_error_is_unsafe():
    res = _result(Status.FAIL, [_finding(Severity.ERROR)])
    d, reasons = decide(
        _inputs(
            coverage_gaps=(Rejection(stage="derived_gate", reasons=("x",)),),
            check_results=(res,),
        )
    )
    assert d is Decision.UNSAFE
    assert any("t:" in reason for reason in reasons)


def test_coverage_gap_plus_warning_is_unknown_not_review():
    res = _result(Status.WARN, [_finding(Severity.WARNING)])
    d, reasons = decide(
        _inputs(
            coverage_gaps=(Rejection(stage="derived_gate", reasons=("x",)),),
            check_results=(res,),
        )
    )
    assert d is Decision.UNKNOWN
    assert reasons == ("COVERAGE GAP [derived_gate]: x",)
```

- Add `test_org_nac_default_no_coverage_gap_path_unchanged` only if a direct
  `decide()` regression is useful: call `DecisionInputs(...)` without
  `coverage_gaps` and assert existing WARNING/UNSAFE behavior is unchanged.

No assembly finding test belongs in Task 1; the `coverage.gap` finding contract is
pipeline-owned and tested in Task 3.

### Verify

```bash
uv run pytest tests/verdict/test_decision.py -q
uv run ruff check src/digital_twin/verdict tests/verdict
uv run mypy
```

### Commit

```bash
git add src/digital_twin/verdict tests/verdict
git commit -m "verdict: add coverage-gap decision tier"
```

## Task 2: Gate evidence enrichment

### Goal

Make the two gate functions expose coverage-gap evidence structurally. Do **not**
parse operator-facing `Rejection.reasons` to recover paths, DHCP rows, or device
ids later; reason text is display prose and may change.

### Code

In `src/digital_twin/scope/derived_gate.py`:

- Add a frozen result DTO:

```python
@dataclass(frozen=True)
class DerivedGap:
    rejection: Rejection
    paths: tuple[str, ...] = ()
    dhcp_row: str | None = None
```

- Add `check_derived_gap(...) -> DerivedGap | None` with the current
  `check_derived(...)` logic:
  - for out-of-scope leaf gaps, return `DerivedGap(rejection, paths=tuple(offending))`;
  - for DHCP semantic gaps, return `DerivedGap(rejection, dhcp_row=name)`.
- Keep `check_derived(...) -> Rejection | None` as a compatibility wrapper:

```python
gap = check_derived_gap(baseline, proposed, artifact=artifact, allowlist=allowlist)
return gap.rejection if gap is not None else None
```

- When `dhcp_row_rejection(...)` returns a rejection inside the row loop, wrap it
  with artifact + row context before returning the `DerivedGap`:

```python
rej = Rejection(
    stage=rej.stage,
    reasons=tuple(
        f"dhcpd_config.{name} in {artifact}: {reason}"
        for reason in rej.reasons
    ),
)
return DerivedGap(rejection=rej, dhcp_row=name)
```

Do not change `dhcp_row_rejection(...)` itself; it stays row-local and reusable.

In `src/digital_twin/scope/device_profile_gate.py`:

- Add a frozen result DTO:

```python
@dataclass(frozen=True)
class DeviceProfileGap:
    rejection: Rejection
    device_id: str
    paths: tuple[str, ...]
```

- Add `device_profile_gap(...) -> DeviceProfileGap | None` with the current
  detection logic.
- Keep `device_profile_rejection(...) -> Rejection | None` as a compatibility
  wrapper for existing unit tests and any external callers:

```python
gap = device_profile_gap(devices, baseline_eff, proposed_eff)
return gap.rejection if gap is not None else None
```

- Compute `offending = tuple(p for p in changed if allowed(p, patterns))`.
- If `offending`, return `DeviceProfileGap(rejection=..., device_id=did,
  paths=offending)` and include the leaf list in the rejection reason.
- Keep detection semantics unchanged.

Suggested reason shape:

```python
f"device {did} has a deviceprofile_id and the edit changes overridable leaf "
f"{path} on its effective config; the unmodeled device-profile layer could "
"override the outcome"
```

or one reason containing a comma-separated list. The tests should only require the
device id, `device_profile_gate`, and the leaf path(s), not exact prose.

### Tests

Update `tests/scope/test_derived_gate.py`:

- Existing `test_dhcp_row_screen_runs_inside_check_derived` should assert the
  rejection reason includes both `dhcpd_config.n` and the artifact (`site` by
  default, or pass `artifact="gateway gw1"` for a more explicit assertion).
- Add a structured helper test for `check_derived_gap(...)`:
  - an out-of-scope leaf returns `gap.paths == ("extra",)` and
    `gap.dhcp_row is None`;
  - a DHCP transition returns `gap.dhcp_row == "n"` and `gap.paths == ()`.

Update `tests/scope/test_device_profile_gate.py`:

- Add or update a profiled gateway/switch case to assert the returned reason names
  the changed overridable leaf, e.g. `ip_configs.corp.ip` or
  `port_usages.office.mode`.
- Add a structured helper test for `device_profile_gap(...)` asserting
  `gap.device_id == <did>` and `gap.paths` contains the changed overridable leaf.

### Verify

```bash
uv run pytest tests/scope/test_derived_gate.py tests/scope/test_device_profile_gate.py -q
uv run ruff check src/digital_twin/scope tests/scope
uv run mypy
```

### Commit

```bash
git add src/digital_twin/scope tests/scope
git commit -m "scope: enrich coverage-gap rejection evidence"
```

## Task 3: Pipeline accumulation and attributed findings

### Goal

Stop `_simulate_site_state` from returning early for post-sim gates. Accumulate
coverage gaps, run checks, route `dp_rej` through `coverage_gaps`, and emit
localized `coverage.gap` findings with the pinned attribution fields.

### Code

In `src/digital_twin/engine/pipeline.py`:

1. Update the top docstring line for stage 8: derived gate no longer always means
   immediate UNKNOWN; it records coverage gaps and proceeds.
2. Import `FindingCategory`, `FindingSource`, `Severity`, and `Confidence` /
   `ConfidenceLevel` where needed. Import the structured helpers from Task 2:
   `check_derived_gap` and `device_profile_gap`.
3. Add a small helper near `_gw_screen_view`:

```python
def _coverage_gap_finding(
    rejection: Rejection,
    *,
    artifact: str,
    subject: ObjectRef | None,
    affected_entities: tuple[str, ...] = (),
    paths: tuple[str, ...] = (),
    dhcp_row: str | None = None,
) -> Finding:
    evidence: dict[str, Any] = {
        "stage": rejection.stage,
        "artifact": artifact,
        "reasons": list(rejection.reasons),
    }
    if paths:
        evidence["paths"] = list(paths)
    if dhcp_row:
        evidence["dhcp_row"] = dhcp_row
    return Finding(
        source=FindingSource.ADAPTER,
        category=FindingCategory.OPERATIONAL,
        code="coverage.gap",
        severity=Severity.WARNING,
        confidence=Confidence(level=ConfidenceLevel.HIGH),
        message="; ".join(f"{rejection.stage}: {r}" for r in rejection.reasons),
        affected_entities=affected_entities,
        evidence=evidence,
        subject=subject,
    )
```

4. Do not add parsing helpers. All structured attribution comes from the Task 2
   DTOs:
   - `DerivedGap.paths` for effective leaf gaps;
   - `DerivedGap.dhcp_row` for DHCP semantic gaps;
   - `DeviceProfileGap.device_id` and `.paths` for device-profile gaps.

5. Replace the three derived-gate early returns with accumulation:

```python
coverage_gaps: list[Rejection] = []
coverage_gap_findings: list[Finding] = []

gap = check_derived_gap(...)
if gap:
    coverage_gaps.append(gap.rejection)
    coverage_gap_findings.append(_coverage_gap_finding(
        gap.rejection,
        artifact="site",
        subject=ObjectRef("site", baseline_raw.scope.site_id),
        paths=gap.paths,
        dhcp_row=gap.dhcp_row,
    ))
```

For device-effective:

```python
subject=ObjectRef("device", did)
affected_entities=(did,)
artifact=f"device {did}"
```

For gateway-effective, still use the device subject kind:

```python
subject=ObjectRef("device", did)
affected_entities=(did,)
artifact=f"gateway {did}"
```

6. Keep running all three gate loops. Do not stop after the first coverage gap;
   the operator should see every accumulated gap.
7. After checks, route `dp_gap` into both lists:

```python
dp_gap = device_profile_gap(...)
if dp_gap:
    coverage_gaps.append(dp_gap.rejection)
    coverage_gap_findings.append(_coverage_gap_finding(
        dp_gap.rejection,
        artifact=f"device {dp_gap.device_id}",
        subject=ObjectRef("device", dp_gap.device_id),
        affected_entities=(dp_gap.device_id,),
        paths=dp_gap.paths,
    ))
```

No fallback `subject=None` path is expected for device-profile gaps: the gate
computes the device id and changed paths structurally. Do not invent
`ObjectRef("gateway", ...)`; gateways are `device` subjects.

8. Call `assemble` with:

```python
inputs=DecisionInputs(
    rejections=(),
    coverage_gaps=tuple(coverage_gaps),
    ...
    adapter_findings=(*adapter_findings, *coverage_gap_findings),
)
```

This is the single source of operator-facing gap findings:
`coverage_gaps` drives decision precedence, and `coverage_gap_findings` rides in
`adapter_findings` for output/visualization. There should be exactly one
`coverage.gap` finding per accumulated coverage-gap rejection.

### Tests

Update `tests/engine/test_pipeline.py`:

- `test_vars_ripple_unknown_at_derived_gate` becomes:
  - decision UNKNOWN
  - reason starts with `COVERAGE GAP [derived_gate]`
  - checks ran (`check_results` non-empty)
  - `coverage.gap` finding exists
  - config_diffs still present for the post-apply UNKNOWN case
- Add `test_coverage_gap_with_modeled_unsafe_returns_unsafe` using an in-scope
  change that produces an existing NETWORK ERROR plus an out-of-scope effective
  gap. If building this from scratch is awkward, use `tests/engine/test_org_plan.py`
  in Task 5 for the org/template variant and keep this as a single-site unit with a
  tiny fake registry that returns a NETWORK ERROR result.

Update `tests/engine/test_gateway_derived.py`:

- Existing gateway out-of-scope netmask case should now assert:
  - decision UNKNOWN
  - `COVERAGE GAP [derived_gate]`
  - `coverage.gap` finding subject is `ObjectRef("device", did)`, not gateway
  - affected_entities contains the gateway/device id

Update `tests/engine/test_pipeline_device_profile.py`:

- Existing device-profile UNKNOWN assertions should now look for
  `COVERAGE GAP [device_profile_gate]` and a `coverage.gap` finding naming the
  changed overridable leaf.
- Device-only and mixed non-taint tests remain not tainted.
- Ingest crash test remains hard UNKNOWN and must not get `coverage.gap`.

### Verify

```bash
uv run pytest tests/engine/test_pipeline.py tests/engine/test_gateway_derived.py tests/engine/test_pipeline_device_profile.py -q
uv run pytest -q
uv run ruff check src/digital_twin/engine tests/engine
uv run mypy
```

The full-suite checkpoint belongs in this task, not only Task 5: once the pipeline
routes post-sim gates through `coverage_gaps`, existing goldens may gain findings
even if the touched engine tests pass. Reconcile that churn before committing.

### Commit

```bash
git add src/digital_twin/engine tests/engine
git commit -m "pipeline: run checks under coverage gaps"
```

## Task 4: Org rollup precedence

### Goal

Change cross-site org rollup so any valid per-site `UNSAFE` leads the org headline
over other sites' `UNKNOWN`, while org-level hard rejections still short-circuit.

### Code

In `src/digital_twin/verdict/org_verdict.py`:

- Update module docstring.
- Change `_PRECEDENCE`:

```python
_PRECEDENCE = {
    Decision.SAFE: 0,
    Decision.REVIEW: 1,
    Decision.UNKNOWN: 2,
    Decision.UNSAFE: 3,
}
```

The existing `max((worst, template_floor), key=...)` logic keeps template REVIEW
below site UNKNOWN/UNSAFE. `org_rejections` still return UNKNOWN before rollup.

### Tests

Update `tests/verdict/test_org_verdict.py`:

- Replace `test_unknown_site_wins` and add the new rollup assertions with this
  shape:

```python
def test_unsafe_site_beats_unknown_site():
    per = {"s1": _verdict(Decision.UNSAFE), "s2": _verdict(Decision.UNKNOWN)}
    decision, _r, driving = decide_org(per, template_findings=(), org_rejections=())
    assert decision is Decision.UNSAFE
    assert driving == ("s1",)


def test_all_unknown_sites_still_unknown():
    per = {"s1": _verdict(Decision.UNKNOWN), "s2": _verdict(Decision.UNKNOWN)}
    decision, _r, driving = decide_org(per, template_findings=(), org_rejections=())
    assert decision is Decision.UNKNOWN
    assert driving == ("s1", "s2")


def test_unknown_site_still_beats_review_site():
    per = {"s1": _verdict(Decision.REVIEW), "s2": _verdict(Decision.UNKNOWN)}
    decision, _r, driving = decide_org(per, template_findings=(), org_rejections=())
    assert decision is Decision.UNKNOWN
    assert driving == ("s2",)
```

- Keep `test_org_rejections_short_circuit_unknown` unchanged.
- Keep template finding REVIEW floor tests unchanged.

Update `tests/engine/test_org_plan.py`:

- Add an org fan-out case with at least two sites where one site is hard UNKNOWN
  due fetch failure and another site is UNSAFE; assert org decision UNSAFE and
  `site_failures` still lists the failed site. Existing golden
  `test_ms_b_one_site_fetch_fails_rolls_up_unknown` may need a sibling builder or
  assertion update depending on whether the non-failed site produces UNSAFE.

### Verify

```bash
uv run pytest tests/verdict/test_org_verdict.py tests/engine/test_org_plan.py -q
uv run pytest -q
uv run ruff check src/digital_twin/verdict tests/verdict tests/engine/test_org_plan.py
uv run mypy
```

Do not defer broad failures to Task 5. If the precedence flip exposes expected
golden churn, pull the relevant Task 5 expectation update forward so this commit is
green.

### Commit

```bash
git add src/digital_twin/verdict tests/verdict tests/engine/test_org_plan.py
git commit -m "verdict: let org unsafe outrank unknown sites"
```

## Task 5: Org/template e2e, golden inventory, and visual attribution

### Goal

Exercise the motivating org-template/delete cases end-to-end and deliberately
review all golden churn caused by post-sim gaps now running checks.

### Golden inventory first

Before changing golden expectations, run a focused inventory against current
fixtures/tests.

Suggested temporary command:

```bash
uv run pytest tests/golden/test_golden_scenarios.py -q \
  -k "unknown or derived or profile or delete or gatewaytemplate or sitetemplate or networktemplate" \
  --tb=short
```

Then grep current tests for assertions and comments that describe bare post-sim
UNKNOWN:

```bash
rg -n "derived_gate|device_profile_gate|dhcp_|bare UNKNOWN|UNKNOWN via" tests/golden tests/engine
```

Record the inventory in the PR/commit message or a short comment near the updated
golden section. Do not blind-regenerate expectations.

### Tests

Update `tests/engine/test_org_plan.py`:

- `test_single_networktemplate_delete_recompiles_and_collapses` should assert a
  deterministic final decision if the site now has modeled UNSAFE plus no
  coverage gap, or assert `coverage.gap` when the template delete also removes
  unmodeled leaves. Keep the config diff assertion in
  `test_org_delete_lists_removed_leaves`.
- Add a delete/update case where one op produces an out-of-scope effective gap and
  another modeled change produces `wired.l2.blackhole.exit_lost`; assert per-site
  UNSAFE, org UNSAFE, and `coverage.gap` present.
- Add a no-modeled-breakage coverage-gap case if not covered in
  `tests/engine/test_pipeline.py`: org/per-site UNKNOWN with `coverage.gap`, not
  SAFE.

Update `tests/golden/test_golden_scenarios.py`:

- `test_dp_a_profiled_gateway_device_taints_unknown` should no longer expect a bare
  hard UNKNOWN. It should assert:
  - per-site has `coverage.gap`
  - reason prefix is `COVERAGE GAP [device_profile_gate]` if it remains UNKNOWN, or
    org/per-site UNSAFE if the modeled gateway gap rises above it
  - the finding names the changed overridable leaf
- Review gatewaytemplate and sitetemplate delete/update goldens that previously
  bailed out at derived/device-profile gates.
- Confirm raw field-gate goldens such as `test_gt_b_unmodeled_field_edit_is_unknown`
  and `test_gt_c_networks_field_edit_is_unknown` remain hard UNKNOWN with no
  `coverage.gap`.

Update visual attribution tests if needed:

- Add to `tests/viz/test_visual_map.py` or an engine test:
  - a gateway-effective coverage gap uses subject kind `device`
  - it produces a `device:<did>` visual entry, not `gateway:<did>`

### Verify

```bash
uv run pytest tests/engine/test_org_plan.py tests/golden/test_golden_scenarios.py tests/viz/test_visual_map.py -q
uv run ruff check tests/engine tests/golden tests/viz
uv run mypy
```

### Commit

```bash
git add tests/engine tests/golden tests/viz
git commit -m "tests: cover org coverage-gap precedence"
```

## Task 6: ROADMAP, full gate, and final review

### Goal

Document the core verdict-precedence redesign and run the complete offline gate.

### Docs

Update `docs/ROADMAP.md`:

- Add a completed or in-progress entry under verdict/orchestration debt:
  derived/device-profile/DHCP post-sim gates are coverage gaps, not blanket hard
  UNKNOWN.
- Note the remaining non-goal: raw field-gate out-of-scope updates still hard
  UNKNOWN before IR/checks.
- Note that org-NAC is unaffected.

### Full gate

Run:

```bash
uv run pytest -q
uv run ruff check .
uv run mypy
```

If failures are only expected golden changes, inspect and update them deliberately;
otherwise fix before committing.

### Self-review checklist

- `DecisionInputs.coverage_gaps` defaults to `()`, so every existing constructor
  keeps working.
- Hard UNKNOWN still dominates for:
  - parse/scope/object-gate/apply
  - L0 fatal
  - field gate
  - baseline/fetch/ingest unavailable
- `check_derived` detection is unchanged except DHCP reason enrichment.
- `device_profile_rejection` detection is unchanged except leaf evidence.
- `coverage.gap` findings are OPERATIONAL/WARNING/HIGH and never drive UNSAFE.
- `decide()` returns UNKNOWN (not REVIEW, not SAFE) for coverage-gap-only runs.
- `decide()` returns UNSAFE for coverage-gap + NETWORK ERROR/CRITICAL.
- Org `UNSAFE` outranks per-site `UNKNOWN`, but `org_rejections` still short-circuit.
- Gateway coverage-gap subject uses `ObjectRef("device", did)`.
- Config diffs remain present for post-apply coverage-gap UNKNOWN/UNSAFE verdicts.
- Diagrams/visual_map are built for coverage-gap verdicts with proposed IR, because
  `_simulate_site_state` reaches the normal assembly path.

### Commit

```bash
git add docs/ROADMAP.md
git commit -m "docs: record coverage-gap verdict precedence"
```

## Final verification commands

```bash
git status --short
uv run pytest -q
uv run ruff check .
uv run mypy
```

Expected: clean working tree except deliberate docs/spec/plan changes, all tests
green, ruff clean, mypy clean.

## Spec coverage

- Spec §1 two UNKNOWN kinds -> Task 1.
- Spec §2 non-fatal derived/device-profile gates -> Tasks 2 and 3.
- Spec §3 `decide()` precedence and org-NAC non-impact -> Task 1.
- Spec §3b org rollup -> Task 4.
- Spec §4 neutral `coverage.gap`, attribution fields, `COVERAGE GAP` reason text
  -> Tasks 1, 3, and 5.
- Spec §5 no-false-SAFE -> Task 1 precedence tests + Task 3/5 e2e.
- Spec §6 golden inventory and post-sim-gate churn -> Task 5.
- Spec relationship to WLAN work / roadmap -> Task 6.
