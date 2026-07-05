# STP Tree Engine v1 Implementation Plan (Spec-4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A pure stable-state STP prediction engine (`analysis/stp_tree.py`: per-component root, per-port roles, blocked set) plus its validation rail (pure comparator vs observed `stp_role`/`stp_state`, live gate tool, replay golden) — with ZERO verdict-facing change.

**Architecture:** One new pure analysis module + one comparator + one gate tool. Election reuses the relocated `root_of`. Directed per-port-end costs (RPC accumulates the RECEIVING port's cost). Self-loop pseudo-edges synthesized from `Port.self_loop_peer` (never from `ir.links`). LAG bundles = one logical STP port pair (l2_graph already collapses members).

**Tech Stack:** Python 3.14, networkx (MultiDiGraph Dijkstra), pytest/ruff/mypy-strict.

**Spec:** `docs/superpowers/specs/2026-07-05-stp-tree-engine-design.md` — read the
"Election + role assignment", "Cost model + confidence", and "Validation rail"
sections before any task; they are the requirements.

## Global Constraints

- **CARDINAL RULE: never false-SAFE.** This slice changes NO verdict — every
  check's behavior must be byte-identical. The full existing suite is the proof;
  no existing test may be edited (except import paths in NO case — none touch
  `_root_of` imports directly).
- **THE INVARIANT (spec):** no verdict-facing consumer exists in this slice, and
  none may be added. `stp_tree()` is reachable only from `AnalysisContext`,
  tests, and `tools/stp_gate.py`.
- `root_of` relocation must be a PURE MOVE: semantics byte-identical, pinned by
  the untouched `stp_root` + `stp_policy` suites.
- Prediction result types are frozen dataclasses; the module does no I/O.
- Deterministic output: same IR → identical `StpTreePrediction` (tuple/sorted
  ordering everywhere; port-name sort supplies the ungrounded port-ID order).
- Gate: `uv run pytest tests -q && uv run ruff check . && uv run mypy src`
  green after every task. (pytest -q prints no summary line; all-dots = pass.)
- `.env` (MIST_HOST/MIST_APITOKEN) is gitignored and MUST NEVER be committed;
  live access is READ-ONLY.
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

## File Structure

- Create: `src/digital_twin/analysis/stp_tree.py` (result types, `root_of`,
  topology prep, election, roles, confidence)
- Create: `src/digital_twin/analysis/stp_agreement.py` (comparator — separate
  file so the engine never imports observation-join code)
- Create: `tools/stp_gate.py` (live gate; `equivalence_gate.py` precedent)
- Modify: `src/digital_twin/analysis/context.py` (memoized `stp_tree()`)
- Modify: `src/digital_twin/checks/wired/stp_root.py` (import relocated helper)
- Modify: `src/digital_twin/checks/wired/stp_policy.py:76` (same)
- Test: `tests/analysis/test_stp_tree.py`, `tests/analysis/test_stp_agreement.py`,
  `tests/golden/test_stp_tree_golden.py` (+ fixture
  `tests/golden/fixtures/tmlab_stp.json`, captured in Task 8)

---

### Task 1: relocate `root_of` (pure move, byte-identical)

**Files:**
- Create: `src/digital_twin/analysis/stp_tree.py`
- Modify: `src/digital_twin/checks/wired/stp_root.py` (drop `_root_of` +
  `_DEFAULT_PRIORITY` + `_ABSTAIN` bodies; import instead)
- Modify: `src/digital_twin/checks/wired/stp_policy.py` (line 76 import)
- Test: `tests/analysis/test_stp_tree.py`

- [ ] **Step 1: Write the failing test** (new import path + pinned semantics)

```python
"""Engine-side pins for the relocated election helper."""
from digital_twin.analysis.stp_tree import ABSTAIN, DEFAULT_PRIORITY, root_of
from digital_twin.ir.builder import IRBuilder
from tests.factories import sw  # the real helpers: sw(did, stp_priority=...)


def test_root_of_semantics_pinned_at_new_home():
    # <2 switches -> None; else min (priority ?? 32768, device_id); assumed flag
    b = IRBuilder()
    b.add_device(sw("aa01", stp_priority=4096)).add_device(sw("bb02"))  # None -> 32768
    ir = b.build()
    assert root_of(ir, frozenset({"aa01"})) is None
    assert root_of(ir, frozenset({"aa01", "bb02"})) == ("aa01", True)
    assert DEFAULT_PRIORITY == 32768 and ABSTAIN == "abstain"
```

(`tests/checks/test_stp_root.py:17` is the established builder pattern —
verify `IRBuilder`'s import path there and reuse it.)

- [ ] **Step 2: Run it — expect FAIL** (`ModuleNotFoundError`)

Run: `uv run pytest tests/analysis/test_stp_tree.py -q`

- [ ] **Step 3: Create the module; MOVE the helper verbatim**

`analysis/stp_tree.py` (module docstring + moved code — bodies copied
UNCHANGED from `stp_root.py`, only names publicized):

```python
"""Stable-state STP tree prediction (Spec-4). Pure — no I/O, no findings.

THE INVARIANT: prediction alone never earns SAFE; every future verdict-facing
consumer must call analysis/stp_agreement.compare_to_observed and cap
confidence on component-level disagreement.
"""
from __future__ import annotations

from digital_twin.ir.entities import DeviceRole
from digital_twin.ir.model import IR

DEFAULT_PRIORITY = 32768
ABSTAIN = "abstain"


def root_of(ir: IR, component: frozenset[str]) -> tuple[str, bool] | str | None:
    """(root device id, any-default-assumed) ... [docstring moved verbatim]"""
    # body moved VERBATIM from checks/wired/stp_root.py:_root_of
```

In `stp_root.py`: delete `_DEFAULT_PRIORITY`, `_ABSTAIN`, `_root_of`; add

```python
from digital_twin.analysis.stp_tree import ABSTAIN as _ABSTAIN
from digital_twin.analysis.stp_tree import root_of as _root_of
```

(alias imports = zero call-site churn). In `stp_policy.py` replace
`from digital_twin.checks.wired.stp_root import _root_of` with
`from digital_twin.analysis.stp_tree import root_of as _root_of`.
Check `stp_policy` for `_ABSTAIN`/`_DEFAULT_PRIORITY` references and rewire
identically if present.

- [ ] **Step 4: Full gate** (the untouched stp_root/stp_policy suites are the
  byte-identical proof)

Run: `uv run pytest tests -q && uv run ruff check . && uv run mypy src`

- [ ] **Step 5: Commit** `refactor(analysis): relocate root_of to stp_tree (one election rule)`

---

### Task 2: result contracts + active topology (exclusions, pseudo-edges, LAG ends)

**Files:**
- Modify: `src/digital_twin/analysis/stp_tree.py`
- Test: `tests/analysis/test_stp_tree.py`

- [ ] **Step 1: Write the failing tests**

```python
def _loop_ports(dev, a="ge-0/0/8", b="ge-0/0/9", reciprocal=True):
    pa = make_port(dev, a, self_loop_peer=f"{dev}:{b}")
    pb = make_port(dev, b, self_loop_peer=f"{dev}:{a}" if reciprocal else None)
    return pa, pb

def test_pseudo_edges_synthesized_without_links():
    # reciprocal claim, NO Link minted (Spec-3 same-device skip) -> one pseudo-edge
    top = active_topology(ir_with(_loop_ports("aa0000000001")))
    assert len(top.pseudo_edges) == 1          # deduped frozenset pair
    assert top.pseudo_edges[0].node == "aa0000000001"

def test_one_sided_claim_synthesizes_nothing_but_notes():
    top = active_topology(ir_with(_loop_ports("aa0000000001", reciprocal=False)))
    assert not top.pseudo_edges and any("one-sided" in n for n in top.notes)

def test_disabled_and_bpdu_filter_ends_excluded():
    # a link whose far end is bpdu_filter'd contributes NO edge and NO port ends

def test_non_switch_ends_excluded():
    # switch<->AP link -> not in the active subgraph

def test_lag_bundle_is_one_logical_edge_with_member_ends():
    # two member links, one bundle_id -> ONE ActiveEdge; ends carry BOTH member
    # ports per side; lag=True
```

- [ ] **Step 2: Run — expect FAIL** (names undefined)

- [ ] **Step 3: Implement contracts + topology prep**

```python
@dataclass(frozen=True)
class PortPrediction:
    port_id: str
    role: str                 # "root" | "designated" | "alternate" | "backup"
    state: str                # "forwarding" | "blocking"
    confidence: ConfidenceLevel
    deciding_factor: str      # "cost"|"bridge_id"|"port_id_tie"|"sole_path"|"root_bridge"
    notes: tuple[str, ...] = ()

@dataclass(frozen=True)
class ComponentTree:
    nodes: frozenset[str]
    root: str | None
    root_assumed_default: bool
    ports: Mapping[str, PortPrediction]

@dataclass(frozen=True)
class StpTreePrediction:
    components: tuple[ComponentTree, ...]
    notes: tuple[str, ...]

# --- internal topology view -------------------------------------------------
@dataclass(frozen=True)
class _End:                     # one side of a logical edge
    node: str                   # VC-folded owner
    ports: tuple[str, ...]      # >1 iff LAG bundle members
    cost: int                   # IEEE cost of the BEST member end (min)
    cost_defaulted: bool        # any contributing speed unknown -> True
    lag: bool

@dataclass(frozen=True)
class _ActiveEdge:
    key: str                    # stable id (sorted link_ids joined)
    a: _End
    b: _End
    link_confidence: ConfidenceLevel

@dataclass(frozen=True)
class _PseudoEdge:              # same-bridge self-loop pair
    node: str
    port_a: str                 # deterministic: min(port name) first
    port_b: str

@dataclass(frozen=True)
class _ActiveTopology:
    edges: tuple[_ActiveEdge, ...]
    pseudo_edges: tuple[_PseudoEdge, ...]
    notes: tuple[str, ...]
```

Rules (spec "Topology preparation"): iterate `build_l2_graph(ir)` edges
(bundles pre-collapsed; same-node edges already absent). Resolve each edge's
member ports to per-node `_End`s via `ir.port(pid).device_id` + `vc_root_map`.
EXCLUDE: any end whose EVERY member port is `disabled` or `bpdu_filter` (a
single excluded member of a LAG only drops that member); edges with a
non-SWITCH-role end; edges with any excluded end. `stp_edge` stays in.
Port cost ladder per member end: `_IEEE_COST[observed_speed or speed]`, else
`_IEEE_COST["1g"]` with `cost_defaulted=True`:

```python
_IEEE_COST = {"10m": 2_000_000, "100m": 200_000, "1g": 20_000, "2.5g": 8_000,
              "5g": 4_000, "10g": 2_000, "25g": 800, "40g": 500, "100g": 200}
```

(802.1t values; 2.5g/5g interpolated per common vendor tables — Junos uses
these.) LAG end cost = min over member ends, `lag=True` (consumer caps MEDIUM).
Pseudo-edges: for every port with reciprocal `self_loop_peer` (both pass the
port exclusions), one `_PseudoEdge` per frozenset pair; one-sided claim → note.

- [ ] **Step 4: Run tests — PASS**, then full gate

- [ ] **Step 5: Commit** `feat(analysis): stp_tree contracts + active topology (pseudo-edges, LAG ends)`

---

### Task 3: election + directed RPC (Dijkstra with taint tracking)

**Files:**
- Modify: `src/digital_twin/analysis/stp_tree.py`
- Test: `tests/analysis/test_stp_tree.py`

- [ ] **Step 1: Failing tests**

```python
def test_rpc_uses_receiving_port_cost_directionally():
    # A(root, 1g port) -- B(10g port): RPC(B) = cost(B's port) = 2_000, NOT 20_000
def test_rpc_taint_propagates_default_cost():
    # unknown speed on the path -> rpc.defaulted True downstream
def test_abstain_component_has_no_roles_and_a_note():
def test_trivial_root_single_switch_with_pseudo_edge():
    # engine-local: root = the switch, root_assumed_default False
def test_equal_cost_parallel_paths_never_compare_payload():
    # P2 pin AT THE DIJKSTRA LAYER (not only role assignment): two identical-
    # cost/same-node-pair standalone links -> RPC computes without TypeError
    # and deterministically (edge_key + counter break the tie)
```

- [ ] **Step 2: Run — FAIL**

- [ ] **Step 3: Implement**

Build a `nx.MultiDiGraph` per component: for each `_ActiveEdge`, add
`a.node -> b.node` weighted `b.cost` and `b.node -> a.node` weighted `a.cost`
(RPC accumulates the RECEIVING end's cost), edge payload = the `_ActiveEdge`.
Elect via `root_of` (ABSTAIN → rootless + note; `< 2 switches` handled BEFORE
`root_of` by the trivial-root rule: exactly one switch node AND ≥1 pseudo-edge
→ `root = that switch`, `root_assumed_default=False`). Run
`nx.single_source_dijkstra(g, root)` on cost, tie-broken deterministically by
`(cost, node_id)` path ordering (implement with a plain heapq Dijkstra —
~20 lines — so the (cost, bridge-id) tiebreak and per-node taint fold are
explicit rather than fought through networkx):

```python
@dataclass(frozen=True)
class _Rpc:
    cost: int
    defaulted: bool                 # any defaulted port cost on the path
    link_conf: ConfidenceLevel      # min link confidence along the path
```

heapq entries are ordered by PRIMITIVES ONLY, with a monotonic counter
guaranteeing comparison NEVER reaches the payload (review P2 — two
equal-cost parallel paths would otherwise make Python compare `_ActiveEdge`,
which is not orderable → TypeError):

```python
counter = itertools.count()
heapq.heappush(q, (cost, node_id, int(defaulted), int(link_conf),
                   edge.key, next(counter), edge))
```

pop-min wins; fold taint from the predecessor's `_Rpc` + the entering end.

- [ ] **Step 4: PASS + full gate**

- [ ] **Step 5: Commit** `feat(analysis): stp_tree election + directed tainted RPC`

---

### Task 4: role assignment (every member port) + confidence assembly

**Files:**
- Modify: `src/digital_twin/analysis/stp_tree.py` (public `predict_stp_tree(ir)`)
- Test: `tests/analysis/test_stp_tree.py`

- [ ] **Step 1: Failing tests** (the spec's pinned cases, one test each)

```python
def test_root_bridge_ports_designated_except_self_loop_pair():
    # P2 pin: root's self-loop -> designated/backup, its other ports designated
def test_root_port_by_cost_is_high_confidence():
def test_root_port_by_bridge_id_tiebreak_is_high():
def test_parallel_links_same_pair_port_id_tie_low():
    # two standalone links A<->B: far side gets ONE root port (port_id_tie, LOW),
    # the other end alternate/blocking — node-level SPT cannot express this
def test_sole_path_deciding_factor():
def test_alternate_ends_block():
def test_self_loop_designated_backup_low():
def test_assumed_default_caps_component_medium():
def test_link_confidence_caps_prediction():
    # MEDIUM link on the deciding comparison -> prediction MEDIUM
def test_defaulted_speed_caps_low():
def test_lag_members_share_bundle_role_capped_medium():
def test_stp_edge_on_switch_link_elected_normally_with_note():
def test_determinism_same_ir_identical_prediction():
```

- [ ] **Step 2: Run — FAIL**

- [ ] **Step 3: Implement `predict_stp_tree`**

Per component, in order:
1. Pseudo-edges FIRST (the exception, wins over rule 2): sort the pair by
   port name; first → `designated/forwarding`, second → `backup/blocking`;
   both ends `port_id_tie`/LOW.
2. Root bridge: every remaining active end on root → `designated`/
   `root_bridge`.
3. Root port per non-root bridge B: candidates = every edge-end entering B
   with `(rpc(neighbor).cost + end.cost, neighbor_id, neighbor_port_min,
   own_port_min)`; unique min by cost → `cost`; broken by neighbor id →
   `bridge_id`; broken later → `port_id_tie` (LOW); single candidate →
   `sole_path`.
4. Designated end per edge: lower `(rpc.cost, node_id)` side (inter-bridge
   ids always differ → never reaches port components) → `designated`.
5. Every remaining end → `alternate`, `deciding_factor` = the comparison that
   lost (cost/bridge_id/port_id_tie).
6. State: root/designated → forwarding; alternate/backup → blocking.
7. Confidence per decision: start HIGH; min-fold the deciding candidates'
   `_Rpc.link_conf` and the edge's `link_confidence`; `defaulted` anywhere in
   the compared candidates → LOW; `port_id_tie` → LOW; LAG end → cap MEDIUM
   + note; component-wide cap MEDIUM when `root_assumed_default`; `stp_edge`
   port → note only.
8. Every member port of an end gets the end's `PortPrediction` (own port_id).

Components/ports sorted; result tuples.

- [ ] **Step 4: PASS + full gate**

- [ ] **Step 5: Commit** `feat(analysis): stp_tree role assignment + per-decision confidence`

---

### Task 5: AnalysisContext memoization

**Files:**
- Modify: `src/digital_twin/analysis/context.py`
- Test: `tests/analysis/test_stp_tree.py`

- [ ] **Step 1: Failing test**

```python
def test_context_memoizes_stp_tree():
    ctx = AnalysisContext(ir)
    assert ctx.stp_tree() is ctx.stp_tree()
```

- [ ] **Step 2: FAIL** → **Step 3: Implement** (`cached_property _stp_tree`
  calling `predict_stp_tree(self._ir)` + `def stp_tree()` accessor — the
  `l2_graph` pattern) → **Step 4: PASS + full gate**

- [ ] **Step 5: Commit** `feat(analysis): memoized stp_tree() on AnalysisContext`

---

### Task 6: agreement comparator

**Files:**
- Create: `src/digital_twin/analysis/stp_agreement.py`
- Test: `tests/analysis/test_stp_agreement.py`

- [ ] **Step 1: Failing tests** — one per bucket:

```python
def test_matched_role_and_state():
def test_role_match_state_mismatch_is_still_mismatch():
def test_mismatch_buckets_key_on_prediction_confidence_exactly():
    # HIGH->mismatched_high, MEDIUM->mismatched_medium, LOW->mismatched_low
def test_absent_or_empty_observed_role_unvalidatable():
def test_unknown_observed_token_unvalidatable_not_mismatch():
    # e.g. stp_role="master" -> unvalidatable bucket
def test_bpdu_inconsistent_reported_separately():
def test_per_component_rollup_flags_disagreement():
```

- [ ] **Step 2: FAIL** → **Step 3: Implement**

```python
_KNOWN_ROLES = frozenset({"root", "designated", "alternate", "backup"})
_PROTECTION = frozenset({"disabled-bpdu-inconsistent"})

@dataclass(frozen=True)
class PortAgreement:
    port_id: str
    predicted: PortPrediction
    observed_role: str | None
    observed_state: str | None
    bucket: str  # "matched"|"mismatched_high"|"mismatched_medium"|
                 # "mismatched_low"|"unvalidatable"|"bpdu_inconsistent"

@dataclass(frozen=True)
class ComponentAgreement:
    nodes: frozenset[str]
    disagreement: bool           # any mismatched_* in the component

@dataclass(frozen=True)
class StpAgreementReport:
    matched: int
    mismatched_high: int
    mismatched_medium: int
    mismatched_low: int
    unvalidatable: int
    bpdu_inconsistent: int
    ports: tuple[PortAgreement, ...]
    components: tuple[ComponentAgreement, ...]


_MISMATCH_BUCKET = {
    ConfidenceLevel.HIGH: "mismatched_high",
    ConfidenceLevel.MEDIUM: "mismatched_medium",
    ConfidenceLevel.LOW: "mismatched_low",
}


def _bucket(pred: PortPrediction, role: str | None, state: str | None) -> str:
    if role in _PROTECTION:
        return "bpdu_inconsistent"
    if role is None or role not in _KNOWN_ROLES:
        return "unvalidatable"          # absent, "", or unknown token — NEVER a mismatch
    if role == pred.role and (state is None or state == pred.state):
        return "matched"                # state compared independently when present
    return _MISMATCH_BUCKET[pred.confidence]   # keyed EXACTLY on prediction tier (P2)


def compare_to_observed(prediction: StpTreePrediction, ir: IR) -> StpAgreementReport:
    rows = []
    for comp in prediction.components:
        for pid in sorted(comp.ports):
            pred = comp.ports[pid]
            port = ir.ports.get(pid)
            role = port.stp_role if port else None       # "" already None-normalized
            state = port.stp_state if port else None
            rows.append(PortAgreement(pid, pred, role, state, _bucket(pred, role, state)))
    comps = tuple(
        ComponentAgreement(
            c.nodes,
            any(r.bucket.startswith("mismatched") for r in rows
                if r.port_id in c.ports),
        )
        for c in prediction.components
    )
    counts = Counter(r.bucket for r in rows)
    return StpAgreementReport(
        matched=counts["matched"], mismatched_high=counts["mismatched_high"],
        mismatched_medium=counts["mismatched_medium"],
        mismatched_low=counts["mismatched_low"],
        unvalidatable=counts["unvalidatable"],
        bpdu_inconsistent=counts["bpdu_inconsistent"],
        ports=tuple(rows), components=comps,
    )
```

(Verify at implementation time whether `Port.stp_role` stores `""` or `None`
for non-participants — Spec-3 ingest applies a falsy guard, but the entity
default is `None`; the comparator treats BOTH as unvalidatable either way.
Needs `from collections import Counter`.)

- [ ] **Step 4: PASS + full gate**

- [ ] **Step 5: Commit** `feat(analysis): stp_agreement comparator (5 buckets + component rollups)`

---

### Task 7: live gate tool

**Files:**
- Create: `tools/stp_gate.py` (mirror `tools/equivalence_gate.py` structure:
  env-driven, per-site loop, exit-code contract, module docstring stating the
  rules)

- [ ] **Step 1: Write the tool** (no unit test — tools/ follows the
  equivalence-gate precedent of being exercised live; the pure logic it calls
  is fully tested in Tasks 2-6)

Contract (spec "Live gate script"):
- Env: `MIST_HOST`, `MIST_APITOKEN`, `DT_GATE_ORG_ID`, `DT_GATE_SITE_IDS`.
- Per site: `fetch_site` → `MistAdapter.ingest` → `predict_stp_tree(ir)` →
  `compare_to_observed`.
- Print the agreement summary per site + a full per-port row for every
  `mismatched_*` and `bpdu_inconsistent` entry.
- Exit 1 iff any site has `mismatched_high > 0` OR total participating
  (non-`unvalidatable`) ports across ALL sites == 0 (vacuous green). MEDIUM/
  LOW mismatches: report-only.
- Optional `--replay-fixture PATH` arg routing through `FixtureProvider`
  (plans in Spec-3 taught us: fixture scope ids are REDACTED — the tool reads
  the fixture's own `scope` rather than requiring env ids in replay mode).

- [ ] **Step 2: Full gate** (ruff/mypy cover the new tool)

- [ ] **Step 3: Commit** `feat(tools): stp_gate — live STP prediction agreement gate`

---

### Task 8: docs wrap (ROADMAP/README) — live verify + golden happen at finish

**Files:**
- Modify: `docs/ROADMAP.md` (Spec-4 entry: engine + rail shipped, consumers
  still deferred, THE INVARIANT quoted), `README.md` module inventory line.
- Note: the TM-LAB live run, fixture capture
  (`tests/golden/fixtures/tmlab_stp.json` via `tools/capture_replay.py`), and
  `tests/golden/test_stp_tree_golden.py` (assert exact predicted roles on the
  REDACTED ids: backbone root port + both self-loop designated/backup pairs,
  LOW tier, report-only agreement) are executed in the finishing phase with
  live access — the golden test lands in the same finishing commit as the
  fixture so CI never sees a dangling reference.

- [ ] **Step 1: Golden test file** (written now, `pytest.mark.skipif` on the
  fixture's absence with reason "captured at live-verify"; the finishing phase
  removes NOTHING — the skip self-resolves when the fixture lands)
- [ ] **Step 2: ROADMAP/README edits**
- [ ] **Step 3: Full gate**
- [ ] **Step 4: Commit** `docs: Spec-4 wrap (roadmap, readme, golden scaffold)`

---

## Live verification (finishing phase, spec-mandated — lab loops still cabled)

1. `uv run python tools/stp_gate.py` against TM-LAB (env from `.env`): expect
   `xe-0/1/3`-side root port in the table, self-loop pairs designated/backup
   at LOW (report-only), **zero `mismatched_high`**.
2. Same against the production org: **zero `mismatched_high`**; record the
   summary in the ledger.
3. Capture `tests/golden/fixtures/tmlab_stp.json`, un-skip the golden, full
   gate, commit.
