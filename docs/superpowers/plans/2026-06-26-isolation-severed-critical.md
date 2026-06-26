# `isolation.severed` → CRITICAL on exit-anchor loss Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Escalate `wired.l2.isolation.severed` to `CRITICAL` when a severed occupied fragment lost reach to an exit anchor that survives on the far side of the cut — the device-level twin of PR #23's `blackhole.exit_lost` CRITICAL.

**Architecture:** One self-contained severity change in `l2_isolation.py`: compute `lost_anchor_nodes = (baseline_home - fragment) & anchors & baseline_anchors` (reusing the proposed `anchors = exit_anchor_nodes(proposed.ir)` already in `run()`, plus a new `baseline_anchors = exit_anchor_nodes(baseline.ir)`), tier the severity CRITICAL/ERROR/WARNING + a `severity_reason`, and add two anchor evidence keys. No change to candidate selection, confidence, status aggregation, verdict, or coverage.

**Tech Stack:** Python 3.14, uv, pytest, ruff (100-col), mypy (strict on `src`, not tests).

## Global Constraints

- **CRITICAL predicate (explicit form):** `baseline_anchors = exit_anchor_nodes(ctx.baseline.ir)`; `lost_anchor_nodes = (baseline_home - fragment) & anchors & baseline_anchors`; `critical = high and bool(lost_anchor_nodes)`. The lost anchor must sit on the far side of the cut **and** exist as an exit anchor in BOTH baseline and proposed — a delta that severs the fragment while *adding* a brand-new far-side exit is not a lost-gateway event (no baseline reach to lose) and must stay ERROR. This keeps it the twin of `blackhole.exit_lost`, which fires only on a baseline exit no longer reached. Write the `(baseline_home - fragment)` form explicitly (it is equivalent to `baseline_home & ...` today only because the existing `fragment & anchors` suppression means a flagged fragment holds no proposed anchor).
- **Severity tiers:** CRITICAL when `high and lost_anchor_nodes`; ERROR when `high` and no surviving anchor; WARNING when not `high` (even if an anchor exists).
- **Unchanged:** candidate selection (the `fragment & anchors` suppression and `if not occupied: continue`), `confidence`, the `worst = Status.FAIL if high else (...)` aggregation (CRITICAL is on the `high` branch → FAIL like ERROR), `default_severity = Severity.ERROR`, `requires()` = `{WIRED_L2}` (do NOT add `L3_EXITS`), and the human `message`.
- **Verdict-invariant:** `decision.py` gates UNSAFE on NETWORK `ERROR` or `CRITICAL` — this is salience only, not a verdict/coverage change.
- **Gate (run before the commit):** `uv run pytest tests -q && uv run ruff check . && uv run mypy src`. Pyright/IDE diagnostics are noise.
- **Commit trailer:** `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

## File Structure

- `src/digital_twin/checks/wired/l2_isolation.py` **(modify)** — severity tier + `severity_reason` + 2 evidence keys; docstring severity line.
- `tests/checks/test_l2_isolation.py` **(modify)** — extend the `_ir` helper (anchor + link-provenance params); add CRITICAL and below-HIGH-WARNING tests; lock the existing ERROR test's `lost_anchor_nodes == []`.

---

## Task 1: Tier `isolation.severed` severity by exit-anchor loss

**Files:**
- Modify: `src/digital_twin/checks/wired/l2_isolation.py`
- Test: `tests/checks/test_l2_isolation.py`

- [ ] **Step 1: Extend the test `_ir` helper with anchor + link-provenance knobs**

In `tests/checks/test_l2_isolation.py`, the `_ir` helper builds `A(member+client) --link-- B`. Add two keyword-only params so a test can give `B` a real exit anchor (a routed IRB) and weaken the severed link. `irb` and `Provenance` are already imported in this file. Change the signature and the IRB/link lines:

```python
def _ir(
    *,
    uplink_disabled: bool,
    blind_peer: bool = False,
    b_has_irb: bool = False,
    link_prov: Provenance = Provenance.LLDP_TWO_SIDED,
):
    """A(member+client) --up/down-- B. The delta disables A's uplink."""
    b = IRBuilder()
    b.add_device(sw("A")).add_device(sw("B"))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    if b_has_irb:
        b.add_l3intf(irb("B", 10))  # B owns a routed IRB -> B is an exit anchor
    acc = access_port("A", "acc", 10)
    b.add_port(acc)
    b.add_client(wired_client("cc:01", acc.id, vlan=10))
    up = trunk_port("A", "up", tagged=(10,))
    if uplink_disabled:
        up = replace(up, disabled=True)
    b.add_port(up)
    down = trunk_port("B", "down", tagged=(10,))
    if blind_peer:  # stat-ensured peer: no vlan facts, but the LINK is two-sided HIGH
        down = Port(
            id="B:down",
            device_id="B",
            name="down",
            mode=PortMode.TRUNK,
            meta=fact_meta(Provenance.OBSERVED),
        )
    b.add_port(down)
    b.add_link(link("A:up", "B:down", prov=link_prov))
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()
```

> `link` must be called with `prov=link_prov` (the factory's `link(pa, pb, kind=..., bundle=..., prov=...)` signature). The existing callers `_ir(uplink_disabled=...)` and `_ir(uplink_disabled=..., blind_peer=True)` keep working (new params default to today's behavior).

- [ ] **Step 2: Add the failing CRITICAL and WARNING tests; lock the ERROR evidence**

Add these tests after `test_disabling_the_only_uplink_severs_the_member_fragment`:

```python
def test_severed_from_a_surviving_exit_anchor_is_critical():
    # A is cut from B, and B owns a routed IRB (a surviving exit anchor on the
    # far side) -> the severance is the top-severity "lost the gateway" event.
    result = _run(
        _ir(uplink_disabled=False, b_has_irb=True),
        _ir(uplink_disabled=True, b_has_irb=True),
    )
    assert result.status is Status.FAIL
    f = next(f for f in result.findings if "A" in f.affected_entities)
    assert f.severity is Severity.CRITICAL
    assert "B" in f.evidence["lost_anchor_nodes"]
    assert "B" in f.evidence["exit_anchor_nodes"]
    assert f.evidence["severity_reason"] == "severed from a surviving exit anchor"


def test_below_high_severance_with_anchor_is_warning():
    # same severed-from-anchor topology, but the severed link is one-sided LLDP
    # (LOW) -> severance confidence is below HIGH, so it stays WARNING even though
    # an exit anchor exists (the `high` gate dominates).
    result = _run(
        _ir(uplink_disabled=False, b_has_irb=True, link_prov=Provenance.LLDP_ONE_SIDED),
        _ir(uplink_disabled=True, b_has_irb=True, link_prov=Provenance.LLDP_ONE_SIDED),
    )
    f = next(f for f in result.findings if "A" in f.affected_entities)
    assert f.severity is Severity.WARNING
    assert "B" in f.evidence["lost_anchor_nodes"]  # anchor present, but high gate dominates
    assert "B" in f.evidence["exit_anchor_nodes"]
    assert f.evidence["severity_reason"] == "physical severance, severance confidence below HIGH"


def test_severed_with_a_newly_added_far_side_anchor_is_not_critical():
    # the delta severs A from B AND adds a brand-new IRB on B. A never had reach
    # to a gateway in baseline (B's anchor did not exist then), so it did not LOSE
    # one -> ERROR, not CRITICAL. Mirrors blackhole.exit_lost, which fires only on
    # a BASELINE exit that is no longer reached, not on a proposed-only exit.
    result = _run(
        _ir(uplink_disabled=False, b_has_irb=False),  # baseline: B is NOT an anchor
        _ir(uplink_disabled=True, b_has_irb=True),  # proposed: B gains an IRB, A severed
    )
    f = next(f for f in result.findings if "A" in f.affected_entities)
    assert f.severity is Severity.ERROR
    assert f.evidence["lost_anchor_nodes"] == []  # no baseline anchor to lose
    assert "B" in f.evidence["exit_anchor_nodes"]  # B is a proposed anchor, but new
```

And extend the existing exit-less ERROR test to lock that no anchor was lost (add the one assertion; keep the rest):

```python
def test_disabling_the_only_uplink_severs_the_member_fragment():
    result = _run(_ir(uplink_disabled=False), _ir(uplink_disabled=True))
    assert result.status is Status.FAIL
    f = next(f for f in result.findings if "A" in f.affected_entities)
    assert f.severity is Severity.ERROR
    assert f.confidence.level is ConfidenceLevel.HIGH
    assert f.evidence["lost_anchor_nodes"] == []  # exit-less home -> no anchor lost -> ERROR, not CRITICAL
    assert f.evidence["exit_anchor_nodes"] == []  # no exit anywhere in this domain
    assert f.evidence["severity_reason"] == "physical severance, no surviving exit anchor"
```

- [ ] **Step 3: Run the new tests to verify they fail**

Run: `uv run pytest tests/checks/test_l2_isolation.py -k "critical or below_high or only_uplink" -v`
Expected: FAIL — `test_..._is_critical` sees `ERROR` (not yet CRITICAL); the two `lost_anchor_nodes` evidence assertions raise `KeyError` (the key does not exist yet).

- [ ] **Step 4: Implement the severity tier + evidence**

In `src/digital_twin/checks/wired/l2_isolation.py`, first add the baseline anchor set next to the existing proposed-anchor line near the top of `run()`:

```python
        anchors = exit_anchor_nodes(ctx.proposed.ir)
        baseline_anchors = exit_anchor_nodes(ctx.baseline.ir)
```

Then in the per-fragment loop, right after `high = confidence.level is ConfidenceLevel.HIGH` (currently followed by the `totals = {...}` block), compute the anchor-loss and tier:

```python
            high = confidence.level is ConfidenceLevel.HIGH
            # CRITICAL only if the fragment LOST reach to a gateway it had: the
            # anchor must sit on the far side of the cut AND exist in BOTH states
            # (proposed = it survives; baseline = the fragment had reach to lose).
            # A delta that severs the fragment while ADDING a new far-side exit is
            # not a lost-gateway event -> ERROR, the twin of blackhole.exit_lost.
            lost_anchor_nodes = (baseline_home - fragment) & anchors & baseline_anchors
            if high and lost_anchor_nodes:
                severity = Severity.CRITICAL
                severity_reason = "severed from a surviving exit anchor"
            elif high:
                severity = Severity.ERROR
                severity_reason = "physical severance, no surviving exit anchor"
            else:
                severity = Severity.WARNING
                severity_reason = "physical severance, severance confidence below HIGH"
```

Then in the `Finding(...)` append, replace the severity line:

```python
                    severity=severity,
```

and add the three keys to the `evidence` dict (alongside the existing `fragment_nodes`/`lost_peers`/`occupants`):

```python
                        "exit_anchor_nodes": sorted(anchors),
                        "lost_anchor_nodes": sorted(lost_anchor_nodes),
                        "severity_reason": severity_reason,
```

Leave the `worst = Status.FAIL if high else (worst if worst is Status.FAIL else Status.WARN)` line unchanged (CRITICAL is on the `high` branch → FAIL). Leave `message`, `subject`, `affected_entities`, `confidence`, `caused_by`, `default_severity`, and `requires()` unchanged.

- [ ] **Step 5: Update the module docstring**

In `l2_isolation.py`'s docstring, update the severity bullet (currently "ERROR at HIGH confidence, WARNING below") to state the three tiers: CRITICAL when the severed fragment lost reach to an exit anchor surviving on the far side of the cut (HIGH confidence); ERROR when HIGH with no surviving anchor (exit-less domain or the delta removed the only exit); WARNING below HIGH. Keep the rest of the docstring.

- [ ] **Step 6: Run tests + full gate**

Run: `uv run pytest tests/checks/test_l2_isolation.py -q && uv run pytest tests -q && uv run ruff check . && uv run mypy src`
Expected: PASS. `test_gs13_*` (golden) asserts only that `isolation.severed` is present and the decision is UNSAFE — it does not pin severity, so an ERROR→CRITICAL flip there is fine. If any test/golden asserted a severed-from-anchor fragment's severity as `ERROR`, update it to `CRITICAL` (intended) and note it; an exit-less severance asserting ERROR must stay ERROR — if one of those breaks it is a real regression, stop and investigate.

- [ ] **Step 7: Commit**

```bash
git add src/digital_twin/checks/wired/l2_isolation.py tests/checks/test_l2_isolation.py
git commit -m "fix(checks): isolation.severed is CRITICAL when severed from a surviving exit anchor

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- CRITICAL `high and bool(lost_anchor_nodes)` with the explicit `(baseline_home - fragment) & anchors & baseline_anchors` form (anchor lost on the far side in BOTH states) → Step 4 ✓
- ERROR (high, no surviving anchor) / WARNING (below high, even with anchor) → Step 4 tiers ✓
- `requires()` stays WIRED_L2; status aggregation, confidence, candidate selection, `default_severity`, message unchanged → Step 4 leaves them ✓
- Evidence `exit_anchor_nodes` / `lost_anchor_nodes` / `severity_reason` → Step 4 ✓
- Verdict invariance (CRITICAL→FAIL→UNSAFE like ERROR) → unchanged `worst` line; `test_gs13` decision stays UNSAFE ✓
- Tests: CRITICAL, below-HIGH WARNING, exit-less ERROR — each pins all three evidence keys (`lost_anchor_nodes`, `exit_anchor_nodes`, `severity_reason`) for its tier → Step 2 ✓

**Type/name consistency:** `Severity.CRITICAL` exists; `anchors` / `baseline_home` / `fragment` / `high` are existing locals in the loop; `(frozenset - frozenset) & set` → set, `sorted(...)` over it is fine; `link(..., prov=...)` and `irb(did, vlan)` match the factory signatures; `Provenance.LLDP_ONE_SIDED` → LOW confidence.

**Placeholder scan:** none — exact code for every code step; the only conditional ("if another test asserted ERROR for a severed-from-anchor fragment") names the concrete intended change (CRITICAL).
