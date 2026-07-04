# STP Telemetry Escalation + Self-Loop Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ingest `stp_role` + self-loop observations; add the observed-root ERROR route to `root_protect_risk` (with a route-independent liveness guard that also closes a Spec-2 hole); add `wired.l2.loop.self_loop` — a new detection path that tightens today-SAFE `stp_disable` deltas on self-looped ports to UNSAFE/REVIEW.

**Architecture:** Two observed `Port` facts from data already fetched; escalate-only routes on two existing checks; no new check ids, no gate/allowlist changes, no new capabilities.

**Tech Stack:** Python 3.14, uv, pytest, ruff (100-col), mypy strict on src. Gate: `uv run pytest tests -q && uv run ruff check . && uv run mypy src` (pytest `-q` prints no summary line — all-dots = pass).

**Spec:** `docs/superpowers/specs/2026-07-04-stp-telemetry-escalation-design.md` (approved after 4 review rounds — the P-findings cited below are binding).

**Baseline:** branch `feat/stp-telemetry` off main@`4e60568` (includes Spec-2/PR #42 and the #43 empty-string guard).

---

## Pre-resolved code facts (verified 2026-07-04 — reuse, don't re-derive)

- `ingest/lldp.py:_apply_stp` reads `stp_state` with the #43 falsy guard; `stp_role` joins it (same guard shape per field).
- `ingest/lldp.py:_claims` (line ~63) maps `(src_port, dst_port) → row` with `neighbor_mac` preferred and a `neighbor_system_name` fallback. CAUTION: the NAME fallback can resolve a neighbor to the reporting device itself — the self-loop FACT must come ONLY from the row-level MAC rule (`row["neighbor_mac"] == row["mac"]`, spec P2r2); the link-emission skip must cover same-device claims from EITHER origin.
- `_emit_links` (line ~128) currently mints same-device links; `build_l2_graph` drops them silently. This slice adds the explicit skip (spec P2-3).
- `checks/wired/l2_loop.py`: `applies_to` already covers `touches("port")` (line 51) — NO gate widening needed. `run()` iterates graph cycles via `_judge`; `.self_loop` is a separate pass over ports, added inside `run()` after the cycle loop. Existing codes: `.preexisting`/`.unprotected`/`.unverified`/`.protected`.
- `checks/wired/stp_policy.py`: `_blocking_risk` (~392), `_root_protect_risk` (~508), `_root_protect_unprovable` (~610). The liveness guard wraps `_root_protect_risk`'s entry.
- `ingest/switch.py:983`: `bpdu_filter=bool(usage.get("stp_disable"))` — the ONLY config mapping of `stp_disable`; `Port.stp_enabled` is telemetry-only. The self-loop trigger is `Port.bpdu_filter` False→True in the port diff (spec P1r3-1). `Port.bpdu_filter` is diff-COMPARED (`_IGNORED_BY_KIND["port"]` = `{"is_uplink"}` only).
- Test idioms: `tests/adapters/mist/test_ingest_lldp.py` (`_ctx(stats)` helper, #43's empty-string pin at `test_empty_string_stp_state_is_treated_as_absent`); `tests/checks/test_l2_loop.py`; `tests/checks/test_stp_policy.py` (Spec-2 fixtures: `_run_enable_no_root_port`, chain/triangle topologies, `decide()` idiom).

**Global constraints (bind every task):**
- Escalate-only: observations never suppress or demote any existing finding; absent/empty telemetry keeps today's behavior exactly (pin where cheap).
- ERROR iff HIGH, everywhere. One-sided self-loop evidence caps at WARNING/MEDIUM; the named peer end is never synthesized (spec P1-2).
- The liveness guard (not `disabled`, not `bpdu_filter` flipped by the same delta; `stp_edge` explicitly NOT in the guard) is a precondition of `root_protect_risk` ERROR on BOTH routes (spec P1r3-2).
- Self-loop facts: MAC rule only; `self_loop_reciprocal=True` only when both rows name each other; both fields diff-ignored; no same-device `Link` in `IR.links`.
- Commit messages end with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

### Task 1: IR fields + ingest (role, self-loop facts, emission skip)

**Files:**
- Modify: `src/digital_twin/ir/entities.py` (Port: `stp_role`, `self_loop_peer`, `self_loop_reciprocal`)
- Modify: `src/digital_twin/ir/diff.py` (`_IGNORED_BY_KIND["port"]` += the two self-loop fields)
- Modify: `src/digital_twin/adapters/mist/ingest/lldp.py`
- Test: `tests/adapters/mist/test_ingest_lldp.py`, `tests/ir/test_diff.py` (or wherever port diff-ignores are pinned — grep first)

- [ ] **Step 1: Write the failing ingest tests** (beside the #43 pin; reuse `_ctx`):

```python
def test_stp_role_read_beside_state_with_empty_string_absent():
    stats = [
        {"mac": "aa0000000001", "port_id": "ge-0/0/8", "up": True,
         "stp_state": "forwarding", "stp_role": "designated"},
        {"mac": "aa0000000001", "port_id": "ge-0/0/9", "up": True,
         "stp_state": "blocking", "stp_role": "backup"},
        {"mac": "aa0000000001", "port_id": "bme0", "up": True,
         "stp_state": "", "stp_role": ""},  # non-participant: both absent
    ]
    ir = _ctx(stats).builder.build()
    assert ir.port("aa0000000001:ge-0/0/8").stp_role == "designated"
    assert ir.port("aa0000000001:ge-0/0/9").stp_role == "backup"
    assert ir.port("aa0000000001:bme0").stp_role is None
    assert ir.port("aa0000000001:bme0").stp_state is None


def test_role_only_row_applies_and_earns_the_capability():
    # review P2: a row with non-empty stp_role but empty stp_state is still a
    # real STP observation — an implementation keeping the old
    # `if not stp_state: continue` gate would pass every other test here and
    # silently drop role-only rows
    stats = [
        {"mac": "aa0000000001", "port_id": "xe-0/1/3", "up": True,
         "stp_state": "", "stp_role": "root"},
    ]
    ctx = _ctx(stats)
    assert IRCapability.STP_STATE in LldpIngester().ingest(ctx)
    p = ctx.builder.build().port("aa0000000001:xe-0/1/3")
    assert p.stp_role == "root"
    assert p.stp_state is None
    assert p.stp_enabled is True


def test_reciprocal_self_loop_sets_peer_and_reciprocal_on_both_ports():
    # shaped like the live SWB-3 rows: neighbor_mac == the row's OWN mac
    stats = [
        {"mac": "aa0000000001", "port_id": "ge-0/0/8", "up": True,
         "neighbor_mac": "aa0000000001", "neighbor_port_desc": "ge-0/0/9"},
        {"mac": "aa0000000001", "port_id": "ge-0/0/9", "up": True,
         "neighbor_mac": "aa0000000001", "neighbor_port_desc": "ge-0/0/8"},
    ]
    ir = _ctx(stats).builder.build()
    a, b = ir.port("aa0000000001:ge-0/0/8"), ir.port("aa0000000001:ge-0/0/9")
    assert a.self_loop_peer == "aa0000000001:ge-0/0/9" and a.self_loop_reciprocal
    assert b.self_loop_peer == "aa0000000001:ge-0/0/8" and b.self_loop_reciprocal


def test_one_sided_self_claim_never_synthesizes_the_peer():
    stats = [
        {"mac": "aa0000000001", "port_id": "ge-0/0/8", "up": True,
         "neighbor_mac": "aa0000000001", "neighbor_port_desc": "ge-0/0/9"},
        {"mac": "aa0000000001", "port_id": "ge-0/0/9", "up": True},  # silent
    ]
    ir = _ctx(stats).builder.build()
    a = ir.port("aa0000000001:ge-0/0/8")
    assert a.self_loop_peer == "aa0000000001:ge-0/0/9"
    assert a.self_loop_reciprocal is False
    assert ir.port("aa0000000001:ge-0/0/9").self_loop_peer is None


def test_no_same_device_link_is_minted():
    # P2-3: current _emit_links mints these; this pins the NEW skip — and it
    # must also cover a name-fallback row resolving to the reporting device
    stats = [
        {"mac": "aa0000000001", "port_id": "ge-0/0/8", "up": True,
         "neighbor_mac": "aa0000000001", "neighbor_port_desc": "ge-0/0/9"},
        {"mac": "aa0000000001", "port_id": "ge-0/0/9", "up": True,
         "neighbor_system_name": "SW-A", "neighbor_port_desc": "ge-0/0/8"},
    ]
    ir = _ctx(stats).builder.build()  # _ctx's device fixture must name SW-A
    assert not [
        l for l in ir.links.values()
        if l.a_port.split(":")[0] == l.b_port.split(":")[0]
    ]  # adapt attribute names to the real Link entity — read entities.py first


def test_self_loop_fields_are_diff_ignored():
    # config-identical IRs, one with the observation -> NO port diff entry
    ...  # clone the is_uplink diff-isolation pin (grep tests/ for it)
```

(The `...` body clones the existing `is_uplink` diff-isolation pin — find it with `grep -rn "is_uplink" tests/ir/ tests/adapters/`; the normal two-device-link negative — no self-loop facts minted — likely already falls out of `test_reciprocal...` fixtures but add the explicit pin if absent.)

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement.**

(a) `entities.py` `Port`, next to `stp_state`:

```python
    stp_role: str | None = None  # OBSERVED (root/designated/backup/...; "" -> None)
    # OBSERVED physical self-loop (LLDP: the chassis sees ITSELF). peer = the
    # claimed other end; reciprocal = BOTH rows name each other (evidence tier
    # gate — one-sided claims cap at WARNING/MEDIUM, spec P1-2). Diff-ignored.
    self_loop_peer: str | None = None
    self_loop_reciprocal: bool = False
```

(b) `diff.py`: `"port": frozenset({"is_uplink", "self_loop_peer", "self_loop_reciprocal"})` (keep the existing comment style; `stp_role` is NOT added — it mirrors `stp_state`: compared).

(c) `lldp.py` `_apply_stp`: apply a row if EITHER field is non-empty; set each field only when non-empty:

```python
            state = row.get("stp_state") or None
            role = row.get("stp_role") or None
            if (state is None and role is None) or not row.get("port_id"):
                continue
            ...
                replace(
                    ctx.builder.get_port(pid),
                    stp_state=state if state is not None else port.stp_state,
                    stp_role=role if role is not None else port.stp_role,
                    stp_enabled=True,
                    stp_meta=fact_meta(Provenance.OBSERVED),
                )
```

(adapt to the function's real local naming; `stp_enabled=True` stays tied to a non-empty observation exactly as today — a role-only row is still a real STP observation and earns the capability, per spec).

(d) `lldp.py` new `_apply_self_loops(ctx)` called from `ingest()` before `_emit_links`: one pass over `ctx.raw.port_stats`; a row claims a self-loop iff `row.get("neighbor_mac") and str(row["neighbor_mac"]) == str(row["mac"])` (NO name fallback); collect `port → claimed_peer_port`; reciprocal iff the claimed peer's row claims back exactly; `replace(...)` both fields on each claiming port only.

(e) `_emit_links`: skip any claim whose src and dst ports belong to the same device (covers MAC-rule self-loops AND name-fallback self-resolution):

```python
            if device_of(src) == device_of(dst):  # self-loop: fact lives on the
                continue                          # ports (P2-3); never a Link
```

(use the real helper for extracting the device part of a global port id — grep how `_emit_links` already splits ids).

- [ ] **Step 4: Full gate; commit**

```bash
git add -A && git commit -m "feat(ingest): stp_role + self-loop observations; same-device links never minted"
```

---

### Task 2: route-independent liveness guard on `root_protect_risk` (Spec-2 hole)

**Files:**
- Modify: `src/digital_twin/checks/wired/stp_policy.py`
- Test: `tests/checks/test_stp_policy.py`

- [ ] **Step 1: Write the failing negatives** (Spec-2's chain fixture, both combined-delta variants):

```python
def test_root_protect_plus_admin_disable_in_one_delta_is_not_error():
    # liveness guard: a port disabled by the SAME delta cannot block the root
    # path via root-protect — harm owner is admin_disable; floor still fires
    result = _run_enable_no_root_port(topology="chain",
                                      priorities={"A": 32768, "B": 4096},
                                      also={"disabled": True})
    assert not [f for f in _findall(result, "wired.stp.policy.root_protect_risk")
                if f.severity is Severity.ERROR]


def test_root_protect_plus_stp_disable_in_one_delta_is_not_error():
    # the GRAPH-route variant (spec P1r3-2): the L2 graph KEEPS bpdu_filter'd
    # edges, so without a shared guard the graph route would still ERROR on a
    # port that no longer processes BPDUs — the Spec-2 hole this closes
    result = _run_enable_no_root_port(topology="chain",
                                      priorities={"A": 32768, "B": 4096},
                                      also={"bpdu_filter": True})
    assert not [f for f in _findall(result, "wired.stp.policy.root_protect_risk")
                if f.severity is Severity.ERROR]


def test_root_protect_plus_stp_edge_in_one_delta_still_errors():
    # stp_edge is EXPLICITLY not in the guard (spec P1r2): edge self-heals on
    # BPDU receipt, so root-protect on the root path remains a real risk
    result = _run_enable_no_root_port(topology="chain",
                                      priorities={"A": 32768, "B": 4096},
                                      also={"stp_edge": True})
    f = _find(result, "wired.stp.policy.root_protect_risk")
    assert f.severity is Severity.ERROR
```

(Extend `_run_enable_no_root_port` with an `also=` kwarg applying extra `dataclasses.replace` fields to the proposed port — read the helper first; keep existing callers unchanged.)

- [ ] **Step 2: RED** (the stp_disable variant must FAIL on current code — that's the hole).

- [ ] **Step 3: Implement** — at `_root_protect_risk`'s entry, before any route:

```python
        # Liveness guard (route-independent, spec P1-1/P1r3-2): a port the
        # SAME delta removed from STP participation cannot block the root
        # path — no ERROR from ANY route. stp_edge is deliberately NOT here
        # (edge self-heals on BPDU receipt). The graph keeps bpdu_filter'd
        # edges, so the graph route needs this as much as the observed one.
        if new_port.disabled or (new_port.bpdu_filter and not old_bpdu_filter):
            return None  # floor / admin_disable / edge_on_uplink own the harm
```

(adapt variable names; "removed from participation BY THE SAME DELTA" — a PRE-EXISTING bpdu_filter is a different situation: the port already didn't participate, so root-protect on it is inert too — guard on the PROPOSED state (`new_port.disabled or new_port.bpdu_filter`) and note why in the comment; verify no existing Spec-2 test used a bpdu-filtered fixture expecting ERROR — if one did, STOP and report, that's a spec conflict.)

- [ ] **Step 4: Full gate; commit**

```bash
git add -A && git commit -m "fix(checks): root_protect_risk liveness guard is route-independent (Spec-2 hole)"
```

---

### Task 3: observed-root ERROR route

**Files:**
- Modify: `src/digital_twin/checks/wired/stp_policy.py`
- Test: `tests/checks/test_stp_policy.py`

- [ ] **Step 1: Failing tests:**

```python
def test_observed_root_role_escalates_even_with_external_root():
    # THE motivating case: graph election unprovable (external root) -> graph
    # route alone yields WARNING+note; observed stp_role="root" is the live
    # election result -> ERROR/HIGH
    result = _run_enable_no_root_port_with_role(role="root", election="external")
    f = _find(result, "wired.stp.policy.root_protect_risk")
    assert f.severity is Severity.ERROR
    assert f.confidence.level is ConfidenceLevel.HIGH
    assert f.evidence["observed_role"] == "root"
    assert f.evidence["election_confidence"] == "observed"
    # decide() -> UNSAFE (clone the existing decide() idiom)


def test_observed_designated_role_changes_nothing():
    # negative: only the literal "root" escalates
    result = _run_enable_no_root_port_with_role(role="designated", election="external")
    f = _find(result, "wired.stp.policy.root_protect_risk")
    assert f.severity is Severity.WARNING  # graph route's unprovable tier, unchanged


def test_both_routes_union_evidence():
    # HIGH graph election + only-path AND observed role="root": one ERROR
    # finding carrying only_path AND observed_role
    ...


def test_observed_root_respects_the_liveness_guard():
    # role="root" + stp_disable in the same delta -> no ERROR (guard from T2)
    ...
```

(`_run_enable_no_root_port_with_role` = the T2 helper + `stp_role` set via `dataclasses.replace` on the BASELINE port — observations live in baseline; external-root election = the existing unprovable fixture shape. Fill `...` bodies fully.)

- [ ] **Step 2: RED.**

- [ ] **Step 3: Implement** in `_root_protect_risk` after the liveness guard: if `old_port.stp_role == "root"` (baseline observation, literal match) → ERROR/HIGH with `observed_role`/`election_confidence="observed"`/`severity_reason="port is the observed root port"`; when the graph route ALSO concludes ERROR, emit ONE finding with unioned evidence; when only the graph route concludes (role absent/other), its behavior is byte-identical to today (pin via the existing suite staying green).

- [ ] **Step 4: Full gate; commit**

```bash
git add -A && git commit -m "feat(checks): observed-root route — stp_role=root escalates root_protect_risk"
```

---

### Task 4: `wired.l2.loop.self_loop`

**Files:**
- Modify: `src/digital_twin/checks/wired/l2_loop.py`
- Test: `tests/checks/test_l2_loop.py`

- [ ] **Step 1: Failing tests** (clone the file's fixture idiom; self-loop facts via `dataclasses.replace` on ports):

```python
def test_stp_disable_on_reciprocal_self_loop_is_error_high_unsafe():
    # delta flips Port.bpdu_filter False->True (the stp_disable leaf) on one
    # end of a RECIPROCAL observed self-loop -> contained loop becomes a storm
    result = _run_self_loop(reciprocal=True, flip="bpdu_filter")
    f = _find(result, "wired.l2.loop.self_loop")
    assert f.severity is Severity.ERROR
    assert f.confidence.level is ConfidenceLevel.HIGH
    assert set(f.evidence["ports"]) == {"A:p8", "A:p9"}  # adapt ids
    # decide() -> UNSAFE


def test_one_sided_self_loop_evidence_caps_at_warning_medium():
    result = _run_self_loop(reciprocal=False, flip="bpdu_filter")
    f = _find(result, "wired.l2.loop.self_loop")
    assert f.severity is Severity.WARNING
    assert f.confidence.level is ConfidenceLevel.MEDIUM


def test_other_change_on_self_looped_port_is_info_context():
    result = _run_self_loop(reciprocal=True, flip="description")  # any non-trigger
    f = _find(result, "wired.l2.loop.self_loop")
    assert f.severity is Severity.INFO
    # review P1: the INFO context must not taint the CHECK result — status
    # stays PASS (nothing WARNING+ from this check) and the result confidence
    # stays HIGH (the INFO's confidence is EXCLUDED from the roll-up), so the
    # decision layer never floors REVIEW because of context
    assert result.status is Status.PASS
    assert result.confidence is not None
    assert result.confidence.level is ConfidenceLevel.HIGH


def test_info_self_loop_context_does_not_change_the_verdict():
    # e2e-shaped pin (may live in Task 5's file if the harness fits better):
    # a benign Spec-1 leaf change (description) on a self-looped port ->
    # INFO context present AND decision is exactly what it would be without
    # the self-loop observation (SAFE for the benign leaf) — context never
    # causes REVIEW
    ...


def test_unrelated_delta_is_silent_about_the_self_loop():
    result = _run_self_loop(reciprocal=True, flip=None, elsewhere=True)
    assert not _findall(result, "wired.l2.loop.self_loop")


def test_observed_states_land_in_evidence_when_present():
    ...  # stp_state/role on the pair -> evidence["observed_states"]
```

- [ ] **Step 2: RED.**

- [ ] **Step 3: Implement** — a pass in `run()` after the cycle loop, over baseline ports with `self_loop_peer`:
  - trigger = the port diff shows `bpdu_filter` False→True on the port or its claimed pair (read `changed_fields` + compare old/new — the same per-port idiom stp_policy uses);
  - triggered + `self_loop_reciprocal` → ERROR/HIGH; triggered + one-sided → WARNING/MEDIUM; pair touched otherwise (any port diff on either end) → INFO; untouched → nothing;
  - evidence: `ports` (pair or single+claim), `observed_states` (state/role each end when present), `severity_reason`; `caused_by` via `ctx.delta_index.causes("port", [...])`;
  - message: "physical self-loop observed on <a> ↔ <b>; STP protection disabled by this change — broadcast-storm risk" (ERROR wording) / context wording for INFO;
  - **Aggregation (review P1, explicit):** `l2_loop` uses CUSTOM `worst`/
    `confidences` aggregation that appends a confidence for EVERY finding —
    including INFO. Left as-is, an INFO-only self-loop context at MEDIUM
    confidence would taint the result confidence sub-HIGH and decision.py
    would floor REVIEW — an INFO finding CAUSING a verdict change, the exact
    inverse of the INFO rules. The self-loop pass therefore: (a) contributes
    its finding's confidence to the roll-up ONLY for WARNING-or-worse
    self-loop findings; (b) INFO self-loop findings are excluded from BOTH
    the `worst` status ranking and the `confidences` list (mirror how
    `status_from_findings`' INFO exclusion works, but applied to this check's
    custom aggregation — touch only the self-loop pass's contributions, do
    NOT change the cycle-path aggregation for existing codes).
  - Dedupe: emit once per pair, not once per end.

- [ ] **Step 4: Full gate; commit**

```bash
git add -A && git commit -m "feat(checks): l2.loop.self_loop — observed self-loops guard stp_disable deltas"
```

---

### Task 5: e2e + never-SAFE extension

**Files:**
- Test: `tests/engine/test_pipeline.py` (Spec-2 harness idioms)

- [ ] **Step 1: Write:**
- e2e UNSAFE: fixture world with a reciprocal self-loop (two port_stats rows shaped like the live SWB-3 pair) + a site_setting delta setting `stp_disable: true` on the pair's usage → decision UNSAFE with `wired.l2.loop.self_loop` ERROR.
- e2e observed-root: port_stats gives the uplink `stp_role: "root"`; delta enables `stp_no_root_port` on its usage → UNSAFE with `root_protect_risk` ERROR, `observed_role` in evidence.
- Never-SAFE sanity: the same fixtures with telemetry REMOVED keep today's decisions exactly (REVIEW via the stp.policy floor for the root case; whatever main gives for the stp_disable case — assert it explicitly, it documents the tightening).

- [ ] **Step 2: Full gate; commit**

```bash
git add -A && git commit -m "test(e2e): telemetry-escalation UNSAFE routes + no-telemetry parity pins"
```

---

### Task 6: docs wrap

**Files:**
- Modify: spec Status → `Implemented (2026-07-04; plan docs/superpowers/plans/2026-07-04-stp-telemetry-escalation.md)`
- Modify: `docs/ROADMAP.md` (slice done; tree engine stays deferred, now noted as "viable — telemetry ground truth confirmed"); README check descriptions ONLY if wording went stale (no count change — codes on existing checks).

- [ ] **Step 1: Full gate FIRST (stop if red); docs edits; grep sweep (`self_loop`, `stp_role` consumers all in the two intended checks); commit**

```bash
git add -A && git commit -m "docs: STP telemetry escalation implemented"
```

(Live verification runs from the controller session after the whole-branch review — spec's three steps against TM-LAB while the lab loops are still cabled.)

---

## Self-review checklist

1. **Spec coverage:** ingest facts + emission skip (T1), route-independent liveness guard incl. the graph-route hole + stp_edge-stays-out pin (T2), observed-root route + negatives + union (T3), self-loop tiers incl. one-sided cap + dedupe + trigger=bpdu_filter-flip (T4), e2e + no-telemetry parity (T5), docs (T6). All four review rounds' P-items have named tests.
2. **Placeholders:** `...` stubs name their exact fixture variant and assertions; same convention as Spec-1/2 plans.
3. **Type consistency:** `stp_role: str | None`; `self_loop_peer: str | None` + `self_loop_reciprocal: bool` diff-ignored; trigger reads `Port.bpdu_filter` (bool) transitions only.
