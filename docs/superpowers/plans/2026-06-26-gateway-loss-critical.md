# Gateway-loss CRITICAL + VLAN-split context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `wired.l2.blackhole.exit_lost` CRITICAL at HIGH exit confidence, and demote `wired.l2.vlan_segmentation.split` to contextual INFO — moving operator salience from the noisy symptom to the real harm.

**Architecture:** Two independent one-check severity changes. `exit_lost` severity splits off the shared blackhole severity line by the existing `lost_exit` boolean. `segmentation.split` lowers to INFO and stops setting `Status.WARN`. No verdict logic, IR, or ingest changes.

**Tech Stack:** Python 3.14, uv, pytest, ruff (100-col), mypy (strict on `src`, not tests).

## Global Constraints

- **Verdict unchanged:** `decision.py:64` gates UNSAFE on NETWORK `ERROR` OR `CRITICAL`, so escalating `exit_lost` ERROR→CRITICAL keeps the verdict UNSAFE — this is label/salience only.
- **CRITICAL is precise:** only `exit_lost` (a previously-reachable populated segment now cut off from a confidently-located exit) at HIGH confidence. `new_member_stranded` stays `ERROR`/WARNING; `exit_unlocatable` unchanged; `l2_isolation` unchanged.
- **Never-false-SAFE (segmentation):** `split`→INFO is safe because any harmful split is independently floored by `blackhole.exit_lost`/`exit_unlocatable`/`isolation.severed`.
- **Gate (run before every commit that touches `src`):** `uv run pytest tests -q && uv run ruff check . && uv run mypy src`. Pyright/IDE diagnostics are noise.
- **Commit trailer:** `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

## File Structure

- `src/digital_twin/checks/wired/l2_blackhole.py` **(modify)** — `exit_lost` severity → CRITICAL/WARNING by `lost_exit`; docstring.
- `src/digital_twin/checks/wired/l2_vlan_segmentation.py` **(modify)** — split → INFO, drop `Status.WARN`, `default_severity` → INFO; docstring.
- `tests/checks/test_l2_blackhole.py`, `tests/checks/test_l2_vlan_segmentation.py`, `tests/golden/test_golden_scenarios.py` **(modify)**.

---

## Task 1: `blackhole.exit_lost` → CRITICAL at HIGH confidence

**Files:**
- Modify: `src/digital_twin/checks/wired/l2_blackhole.py`
- Test: `tests/checks/test_l2_blackhole.py`, `tests/golden/test_golden_scenarios.py`

- [ ] **Step 1: Update the failing unit test + add coverage**

In `tests/checks/test_l2_blackhole.py`, the existing `test_losing_a_high_confidence_exit_fails` asserts `Severity.ERROR` for losing a HIGH-confidence exit — that is the exit_lost case and now legitimately becomes CRITICAL. Change its assertion:

```python
def test_losing_a_high_confidence_exit_fails():
    result = L2BlackholeCheck().run(_ctx(_ir(connected=True), _ir(connected=False)))
    assert result.status is Status.FAIL
    f = result.findings[0]
    assert f.code == "wired.l2.blackhole.exit_lost"
    assert f.severity is Severity.CRITICAL   # gateway path lost -> top severity
    assert "10" in f.message
```

Add a below-HIGH guard test (exit_lost stays WARNING when the exit confidence is not HIGH). Mirror the file's existing fixtures: find the test in this file that produces a MEDIUM/LOW-confidence exit (e.g. a boundary-uplink via one-sided LLDP — search the file for a `LLDP_ONE_SIDED`/MEDIUM exit_lost case). If one exists, assert its `exit_lost` finding `severity is Severity.WARNING`; if none exists, add one using the same `_ir(...)` helper with a one-sided/non-HIGH exit link, asserting:

```python
    f = next(x for x in result.findings if x.code == "wired.l2.blackhole.exit_lost")
    assert f.severity is Severity.WARNING  # below-HIGH exit confidence: not CRITICAL
```

And add (or confirm) a `new_member_stranded` test asserting it is **unchanged** at ERROR (HIGH):

```python
    f = next(x for x in result.findings if x.code == "wired.l2.blackhole.new_member_stranded")
    assert f.severity is Severity.ERROR  # "never reached", not "lost the gateway"
```

> Look for the file's existing `new_member_stranded` test (the cause-attribution test file `test_l2_blackhole_new_member_caused_by.py` exercises it) — if a HIGH new_member test already asserts ERROR, leave it; do not duplicate. The point is to lock ERROR so Task 1's change doesn't accidentally touch it.

- [ ] **Step 2: Run the tests to verify the CRITICAL one fails**

Run: `uv run pytest tests/checks/test_l2_blackhole.py -k "high_confidence_exit" -v`
Expected: FAIL — the finding is `ERROR`, the test now wants `CRITICAL`.

- [ ] **Step 3: Implement the severity split**

In `src/digital_twin/checks/wired/l2_blackhole.py`, the `_finding(...)` append currently uses one shared severity line:

```python
                    severity=Severity.ERROR if high else Severity.WARNING,
```

Replace it with a `lost_exit`-dependent severity (CRITICAL for the loss case, ERROR for new members):

```python
                    severity=(
                        (Severity.CRITICAL if high else Severity.WARNING)
                        if lost_exit
                        else (Severity.ERROR if high else Severity.WARNING)
                    ),
```

The surrounding `code`/`message`/`caused_by` (already branched on `lost_exit`) and the `worst = _aggregate([worst, Status.FAIL if high else Status.WARN])` line are **unchanged** (CRITICAL and ERROR both map to FAIL).

- [ ] **Step 4: Update the module docstring**

In `l2_blackhole.py`'s module docstring, update the severity summary so it states: `exit_lost` (a populated segment that lost its path to a located exit) is CRITICAL at HIGH confidence / WARNING below; `new_member_stranded` is ERROR at HIGH / WARNING below; `exit_unlocatable` is INSUFFICIENT_DATA. Keep the rest.

- [ ] **Step 5: Update the golden severity expectation**

In `tests/golden/test_golden_scenarios.py`, the cause-attribution golden pins a `(code, severity)` `Counter` (~line 1531). Change the `exit_lost` entry from ERROR to CRITICAL:

```python
            ("wired.l2.blackhole.exit_lost", Severity.CRITICAL): 2,
```

Leave the `("wired.l2.vlan_segmentation.split", Severity.WARNING): 2` line as-is (Task 2 changes it). Also update the block comment above `test_ca_motivating_...` (~line 1436): "blackhole.exit_lost (ERROR)" → "blackhole.exit_lost (CRITICAL)". The golden's `assert v.decision is Decision.UNSAFE` stays correct (CRITICAL gates UNSAFE).

- [ ] **Step 6: Run tests + gate**

Run: `uv run pytest tests/checks/test_l2_blackhole.py -q && uv run pytest tests/golden -q && uv run pytest tests -q && uv run ruff check . && uv run mypy src`
Expected: PASS. If any *other* test/golden asserted `exit_lost` ERROR, update it to CRITICAL (that is the intended, load-bearing change) and note it in the report; a non-exit_lost ERROR assertion (OSPF, isolation, etc.) must stay ERROR — if one of those breaks, it's a real regression, stop and investigate.

- [ ] **Step 7: Commit**

```bash
git add src/digital_twin/checks/wired/l2_blackhole.py tests/checks/test_l2_blackhole.py tests/golden/test_golden_scenarios.py
git commit -m "fix(checks): blackhole.exit_lost is CRITICAL at HIGH exit confidence (gateway path lost)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `vlan_segmentation.split` → contextual INFO

**Files:**
- Modify: `src/digital_twin/checks/wired/l2_vlan_segmentation.py`
- Test: `tests/checks/test_l2_vlan_segmentation.py`, `tests/golden/test_golden_scenarios.py`

- [ ] **Step 1: Update the failing unit test**

In `tests/checks/test_l2_vlan_segmentation.py`, `test_split_warns_high_confidence` asserts `Status.WARN` + `Severity.WARNING` for a split. The split is now contextual INFO and the check no longer floors WARN. Rewrite it:

```python
def test_split_is_info_context():
    base = _chain_ir(("A", "B"), ("B", "C"))  # one domain A-B-C
    prop = _chain_ir(("A", "B"))  # C cut off -> 2 components
    result = L2VlanSegmentationCheck().run(_ctx(base, prop))
    # the split is observational context, NOT a harm carrier (blackhole/isolation
    # report the real harm) -> INFO, and the check does not floor REVIEW
    assert result.status is Status.PASS
    f = next(f for f in result.findings if f.code == "wired.l2.vlan_segmentation.split")
    assert f.severity is Severity.INFO
```

> If other tests in this file (or `test_l2_vlan_segmentation_caused_by.py`) assert the split's `Status.WARN`/`Severity.WARNING`, update them the same way (status PASS, severity INFO). The `caused_by`/`code`/`evidence` assertions are unchanged — only severity/status move.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/checks/test_l2_vlan_segmentation.py -k "split" -v`
Expected: FAIL — today the split is `WARN`/`WARNING`.

- [ ] **Step 3: Implement the demotion**

In `src/digital_twin/checks/wired/l2_vlan_segmentation.py`:

1. Change `default_severity = Severity.WARNING` to `default_severity = Severity.INFO`.
2. In the split branch, remove the `status = Status.WARN` line and change the finding's `severity=Severity.WARNING` to `severity=Severity.INFO`:

```python
            if split:
                findings.append(
                    self._finding(
                        code="wired.l2.vlan_segmentation.split",
                        severity=Severity.INFO,
                        message=f"vlan {vid}: broadcast domain partitioned by the delta",
                        vid=vid,
                        base=base_comps,
                        prop=prop_comps,
                        caused_by=causes_for_vlan_split(ctx, vid),
                    )
                )
                continue
```

The `reshape` INFO branch and everything else are unchanged. With the `status = Status.WARN` line gone and no other code setting it, `status` stays `Status.PASS` for the whole run (the check is now purely observational).

> If `status` becomes an unused-but-assigned local that ruff flags, keep the initial `status = Status.PASS` and the `CheckResult(status=status, ...)` as-is (it is still read). Do not remove the `status` variable.

- [ ] **Step 4: Update the module docstring**

In `l2_vlan_segmentation.py`'s module docstring, update the SPLIT line so it reads as INFO context (no longer WARN): a split is reported as INFO context — the real harm (a populated piece losing its exit) is carried by `blackhole`/`isolation`. Keep the reshape/expansion line.

- [ ] **Step 5: Update the golden severity expectation**

In `tests/golden/test_golden_scenarios.py`, change the `Counter` split entry (~line 1532) from WARNING to INFO:

```python
            ("wired.l2.vlan_segmentation.split", Severity.INFO): 2,
```

Update the block comment (~line 1436): "vlan_segmentation.split (WARNING)" → "vlan_segmentation.split (INFO)". The decision stays UNSAFE (carried by the CRITICAL `exit_lost`). If a golden's coverage/status assertion depended on segmentation contributing WARN, update it to reflect PASS (segmentation no longer floors anything) and note why.

- [ ] **Step 6: Run tests + gate**

Run: `uv run pytest tests/checks/test_l2_vlan_segmentation.py -q && uv run pytest tests/golden -q && uv run pytest tests -q && uv run ruff check . && uv run mypy src`
Expected: PASS. If any other test asserted `split` WARNING/WARN, update to INFO/PASS (intended). Any unrelated failure is a real regression — stop and investigate.

- [ ] **Step 7: Commit**

```bash
git add src/digital_twin/checks/wired/l2_vlan_segmentation.py tests/checks/test_l2_vlan_segmentation.py tests/golden/test_golden_scenarios.py
git commit -m "fix(checks): vlan_segmentation.split is contextual INFO (harm carried by blackhole/isolation)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- `exit_lost` → CRITICAL at HIGH, WARNING below → Task 1 Step 3 ✓
- `new_member_stranded` stays ERROR → Task 1 Step 1 lock test ✓
- `exit_unlocatable` / `isolation` unchanged → not touched ✓
- `split` → INFO, no `Status.WARN`, `default_severity` INFO → Task 2 Step 3 ✓
- Verdict unchanged (UNSAFE) → golden `assert v.decision is Decision.UNSAFE` kept in both tasks ✓
- Never-false-SAFE (segmentation demotion) → covered by the spec rationale; the golden still decides UNSAFE via blackhole ✓
- Existing test/golden severity expectations updated → Task 1 Steps 1/5, Task 2 Steps 1/5 ✓

**Type/name consistency:** `Severity.CRITICAL`/`Severity.INFO` are existing enum members; `lost_exit` is the existing local in `_check_vlan`; `Status.PASS`/`Status.WARN`/`Status.FAIL` existing.

**Placeholder scan:** none — exact code and exact golden lines given; the "if another test asserts the old severity" notes name the concrete intended change (CRITICAL / INFO), not a TBD.
