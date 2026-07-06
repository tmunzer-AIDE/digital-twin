# STP Blocked-Link Reachability Taint Implementation Plan (Spec-5)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `wired.l2.blackhole` per-VLAN reachability STP-aware so a VLAN reachable only via a spanning-tree-blocked link no longer reads SAFE — hard-stranding telemetry-confirmed blocks, soft-flooring REVIEW on unconfirmed ones.

**Architecture:** A pure `StpReachability(baseline, proposed)` helper (reached via a memoized `CheckContext.stp_reachability` property) joins `stp_tree()` predictions to `vlan_graph` edges, computes a baseline-licensed hard/soft classification, and serves STP-aware per-VLAN components. `blackhole` swaps its component source to this helper and appends a sub-HIGH confidence when a delta-relevant reach depends on a soft-only block.

**Tech Stack:** Python 3.14, networkx (MultiGraph edge removal), pytest/ruff/mypy-strict.

**Spec:** `docs/superpowers/specs/2026-07-05-stp-reachability-taint-design.md` — read "Architecture", "The three-way removal test", and "Symmetric baseline/proposed" before Tasks 2–5; they are the requirements.

## Global Constraints

- **CARDINAL RULE: never false-SAFE. MIRROR RULE: never false-UNSAFE.** The hard path only removes illusory reach and only on telemetry-confirmed HIGH blocks; the soft path never manufactures a hard finding.
- **THE INVARIANT (Spec-4):** prediction alone never earns/moves a verdict. Hard-taint requires baseline `compare_to_observed` agreement WITH matched evidence on the licensing component. Disagreement or vacuity → soft at most.
- **No new check id, no new finding code, no verdict-precedence change.** Hard strands flow through blackhole's existing `exit_lost`/`new_member_stranded` severities; the soft floor is a sub-HIGH `CheckResult` confidence + note.
- **`vlan_components()` stays unchanged**; only `wired.l2.blackhole` opts into STP-aware components this slice. No other check changes.
- **Blocked-edge classification is SIDE-LOCAL**: baseline edges vs baseline tree, proposed edges vs proposed tree; `PortPrediction.confidence == HIGH` read from the side being classified. Only the license is baseline-derived and shared.
- **Removal condition is `PortPrediction.state == "blocking"`**, never `role == "alternate"`.
- **Blocked-edge key**: `(vlan_id, frozenset(member_ports))`.
- **Determinism**: STP-aware components stay sorted (reuse `vlan_components`'s existing sort); no set-iteration leaks into output.
- Gate after every task: `uv run pytest tests -q && uv run ruff check . && uv run mypy src` (pytest -q prints NO summary line; all dots = pass).
- `.env` (MIST_HOST/MIST_APITOKEN) is gitignored, live access READ-ONLY.
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

## File Structure

- Modify: `src/digital_twin/analysis/stp_agreement.py` — `ComponentAgreement` gains `matched_count` + `bpdu_inconsistent_count` + `agreement_clean` property; `compare_to_observed` rollup fills them.
- Create: `src/digital_twin/analysis/stp_reachability.py` — the pair-aware `StpReachability` helper.
- Modify: `src/digital_twin/checks/base.py` — memoized `CheckContext.stp_reachability` property.
- Modify: `src/digital_twin/checks/wired/l2_blackhole.py` — swap all 8 `vlan_components` sites to the STP-aware source; add the soft floor.
- Test: `tests/analysis/test_stp_agreement.py` (extend), `tests/analysis/test_stp_reachability.py` (new), `tests/checks/test_l2_blackhole.py` (extend), `tests/golden/test_stp_reachability_scenarios.py` (new).

---

### Task 1: `ComponentAgreement` matched/bpdu counts + `agreement_clean`

**Files:**
- Modify: `src/digital_twin/analysis/stp_agreement.py`
- Test: `tests/analysis/test_stp_agreement.py`

**Interfaces:**
- Produces: `ComponentAgreement(nodes: frozenset[str], disagreement: bool, matched_count: int, bpdu_inconsistent_count: int)` with a property `agreement_clean: bool == (matched_count > 0 and not disagreement and bpdu_inconsistent_count == 0)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/analysis/test_stp_agreement.py` (reuse the file's existing IR/prediction builders — grep for an existing test that constructs a `StpTreePrediction` with observed telemetry and mirror its setup):

```python
def test_component_agreement_reports_matched_and_bpdu_counts():
    # a component with 2 matched ports, 0 mismatch, 0 bpdu -> clean
    report = _report_for(  # existing helper that runs compare_to_observed
        # build: 2 ports predicted+observed agreeing (matched), same component
    )
    comp = report.components[0]
    assert comp.matched_count == 2
    assert comp.bpdu_inconsistent_count == 0
    assert comp.agreement_clean is True


def test_agreement_clean_false_when_all_unvalidatable():
    report = _report_for(  # ports predicted but observed role None -> unvalidatable
    )
    comp = report.components[0]
    assert comp.matched_count == 0
    assert comp.agreement_clean is False  # vacuous, not clean


def test_agreement_clean_false_when_bpdu_inconsistent_present():
    report = _report_for(  # one matched, one observed "disabled-bpdu-inconsistent"
    )
    comp = report.components[0]
    assert comp.matched_count == 1
    assert comp.bpdu_inconsistent_count == 1
    assert comp.agreement_clean is False  # protection bucket blocks clean license


def test_agreement_clean_false_on_mismatch():
    report = _report_for(  # one matched, one mismatched
    )
    comp = report.components[0]
    assert comp.disagreement is True
    assert comp.agreement_clean is False
```

Fill each `_report_for(...)` body using the file's established construction pattern (find the existing `compare_to_observed` tests and copy their IR + `StpTreePrediction` builders — the four buckets already have single-port tests you can lift the setup from).

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/analysis/test_stp_agreement.py -k "matched_and_bpdu or agreement_clean" -v`
Expected: FAIL — `ComponentAgreement` has no `matched_count`.

- [ ] **Step 3: Extend `ComponentAgreement` and the rollup**

In `src/digital_twin/analysis/stp_agreement.py`, replace the `ComponentAgreement` dataclass:

```python
@dataclass(frozen=True)
class ComponentAgreement:
    nodes: frozenset[str]
    disagreement: bool  # any mismatched_* among this component's OWN ports
    matched_count: int  # ports in the `matched` bucket
    bpdu_inconsistent_count: int  # ports in the `bpdu_inconsistent` bucket

    @property
    def agreement_clean(self) -> bool:
        """Non-vacuous, mismatch-free, protection-free observed agreement — the
        licence a hard reachability taint requires (Spec-5). `disagreement`
        alone is False for an all-`bpdu_inconsistent` component, which is NOT
        clean agreement, so both counts are consulted explicitly."""
        return self.matched_count > 0 and not self.disagreement and self.bpdu_inconsistent_count == 0
```

In `compare_to_observed`, replace the `components` comprehension so each component also counts its own rows:

```python
    components = tuple(
        ComponentAgreement(
            nodes=comp.nodes,
            disagreement=any(
                r.bucket.startswith("mismatched") for r in rows if r.port_id in comp.ports
            ),
            matched_count=sum(
                1 for r in rows if r.port_id in comp.ports and r.bucket == "matched"
            ),
            bpdu_inconsistent_count=sum(
                1 for r in rows if r.port_id in comp.ports and r.bucket == "bpdu_inconsistent"
            ),
        )
        for comp in prediction.components
    )
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/analysis/test_stp_agreement.py -q`
Expected: PASS (all, including the pre-existing agreement tests — the two new fields are additive).

- [ ] **Step 5: Full gate + commit**

```bash
uv run pytest tests -q && uv run ruff check . && uv run mypy src
git add src/digital_twin/analysis/stp_agreement.py tests/analysis/test_stp_agreement.py
git commit -m "feat(analysis): ComponentAgreement matched/bpdu counts + agreement_clean"
```

---

### Task 2: `StpReachability` classification + hard-removed components

**Files:**
- Create: `src/digital_twin/analysis/stp_reachability.py`
- Test: `tests/analysis/test_stp_reachability.py`

**Interfaces:**
- Consumes: `AnalysisContext` (`.ir`, `.stp_tree()`, `.vlan_graph(vid)`, `.exit_for(vid)`, `.ir.vlans`); `compare_to_observed` + `ComponentAgreement.agreement_clean` (Task 1); `vlan_components(vlan_graph, exit_res)` and `VlanComponent` from `analysis.vlan_reachability`; `PortPrediction`/`ComponentTree` from `analysis.stp_tree`.
- Produces: `StpReachability(baseline: AnalysisContext, proposed: AnalysisContext)` with `.baseline_components(vid) -> tuple[VlanComponent, ...]` and `.proposed_components(vid) -> tuple[VlanComponent, ...]` (hard-eligible blocked edges removed, per side). Internal (used by Task 3): `._classify(actx, vid) -> tuple[set[frozenset[str]], set[frozenset[str]]]` returning `(hard_keys, soft_keys)` of `frozenset(member_ports)`.

**Reference — Spec-4 shapes you rely on** (verify by reading `analysis/stp_tree.py`): `StpTreePrediction.components: tuple[ComponentTree, ...]`; `ComponentTree(nodes: frozenset[str], root, root_assumed_default, ports: Mapping[str, PortPrediction])`; `PortPrediction(port_id, role, state, confidence: ConfidenceLevel, deciding_factor, notes)`. Edge payload: `vlan_graph.edges(keys=True, data=True)` → `data["data"]` is an `L2Edge` with `.member_ports: list[str]`, `.vlans: set[int]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/analysis/test_stp_reachability.py`. Use the shared factories (`from tests.factories import sw, trunk_port, link, make_port`, `from digital_twin.ir import IRBuilder`) and `AnalysisContext`. The motivating topology: two switches A,B joined by two trunk links L1 (VLAN 10 NOT carried) and L2 (VLAN 10 carried), where the proposed STP tree blocks L2's B-end; baseline telemetry confirms the block (observed `stp_role`/`stp_state` on those ports matched). Exit (an IRB on VLAN 10) sits on A.

```python
from digital_twin.analysis.context import AnalysisContext
from digital_twin.analysis.stp_reachability import StpReachability


def test_hard_block_strands_pruned_vlan_component():
    base, prop = _pruned_onto_block_pair(block_confirmed=True)  # helper below
    sr = StpReachability(AnalysisContext(base), AnalysisContext(prop))
    # VLAN 10 members on B: in the FULL graph they reach A's exit via L2; with the
    # hard-eligible block on L2 removed they do NOT -> a stranded component appears
    comps = sr.proposed_components(10)
    b_comp = next(c for c in comps if any(n.startswith("bb") for n in c.nodes))
    assert not b_comp.reaches_exit  # hard-removed: the block is load-bearing


def test_no_baseline_agreement_leaves_edge_soft_not_removed():
    base, prop = _pruned_onto_block_pair(block_confirmed=False)  # no observed telemetry
    sr = StpReachability(AnalysisContext(base), AnalysisContext(prop))
    comps = sr.proposed_components(10)
    b_comp = next(c for c in comps if any(n.startswith("bb") for n in c.nodes))
    assert b_comp.reaches_exit  # vacuous agreement -> soft-only -> NOT hard-removed


def test_low_confidence_block_is_soft_not_removed():
    base, prop = _pruned_onto_block_pair(block_confirmed=True, block_confidence="low")
    sr = StpReachability(AnalysisContext(base), AnalysisContext(prop))
    comps = sr.proposed_components(10)
    b_comp = next(c for c in comps if any(n.startswith("bb") for n in c.nodes))
    assert b_comp.reaches_exit  # LOW proposed confidence -> soft-only


def test_new_intra_component_edge_is_soft_only():
    # the blocked edge does NOT exist in baseline (added by the delta) between two
    # nodes already in one clean baseline component -> soft-only, not hard-removed
    base, prop = _pruned_onto_block_pair(block_confirmed=True, edge_new_in_proposed=True)
    sr = StpReachability(AnalysisContext(base), AnalysisContext(prop))
    comps = sr.proposed_components(10)
    b_comp = next(c for c in comps if any(n.startswith("bb") for n in c.nodes))
    assert b_comp.reaches_exit  # existed-in-baseline clause fails -> soft


def test_bpdu_inconsistent_component_does_not_license_hard():
    base, prop = _pruned_onto_block_pair(block_confirmed=True, baseline_bpdu=True)
    sr = StpReachability(AnalysisContext(base), AnalysisContext(prop))
    comps = sr.proposed_components(10)
    b_comp = next(c for c in comps if any(n.startswith("bb") for n in c.nodes))
    assert b_comp.reaches_exit  # agreement_clean False (bpdu) -> soft


def test_baseline_components_use_baseline_side_blocking():
    # a block present in BOTH sides, baseline-confirmed -> removed from baseline
    # view too (symmetry): baseline B-component is also stranded
    base, prop = _pruned_onto_block_pair(block_confirmed=True, preexisting=True)
    sr = StpReachability(AnalysisContext(base), AnalysisContext(prop))
    b_base = next(c for c in sr.baseline_components(10) if any(n.startswith("bb") for n in c.nodes))
    b_prop = next(c for c in sr.proposed_components(10) if any(n.startswith("bb") for n in c.nodes))
    assert not b_base.reaches_exit and not b_prop.reaches_exit  # symmetric strand


def test_no_predicted_blocks_matches_plain_vlan_components():
    # a tree topology (no cycles, no blocks) -> STP-aware == plain
    base, prop = _simple_tree_pair()
    ctx = AnalysisContext(prop)
    sr = StpReachability(AnalysisContext(base), ctx)
    assert sr.proposed_components(10) == ctx.vlan_components(10)
```

Write the two builder helpers `_pruned_onto_block_pair(...)` and `_simple_tree_pair()` in the test file. `_pruned_onto_block_pair` must: build switches `aa01`/`bb02`, two trunk links between them (L1 without VLAN 10 in tagged set, L2 with VLAN 10), an IRB exit on VLAN 10 on `aa01`, access members on `bb02`; then set the ports' `stp_role`/`stp_state` so the proposed `stp_tree()` predicts L2's block, and (when `block_confirmed`) set baseline observed `stp_role`/`stp_state` to MATCH that block on the relevant baseline ports. Inspect `tests/analysis/test_stp_tree.py` for how to drive a self-loop/parallel-link block deterministically and reuse that shape. The flags: `block_confidence` (make the block fall to a port-id tie / defaulted speed for LOW), `edge_new_in_proposed` (L2 absent from baseline), `baseline_bpdu` (baseline observed role `disabled-bpdu-inconsistent`), `preexisting` (block present + confirmed in both sides).

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/analysis/test_stp_reachability.py -q`
Expected: FAIL — `stp_reachability` module does not exist.

- [ ] **Step 3: Implement `StpReachability` (classification + hard-removed components)**

```python
"""STP-aware per-VLAN reachability (Spec-5). Pure — no I/O, no findings.

Blocked-link taint of blackhole reachability. Blocking is read SIDE-LOCALLY
(baseline edges vs baseline tree, proposed vs proposed); only the hard/soft
LICENCE is baseline-derived (compare_to_observed on the baseline) and shared.
A hard-eligible blocked edge is removed from that side's reachability graph; a
soft-only edge is kept (its effect is a REVIEW floor, handled by the check).
"""
from __future__ import annotations

import networkx as nx

from digital_twin.analysis.context import AnalysisContext
from digital_twin.analysis.stp_agreement import ComponentAgreement, compare_to_observed
from digital_twin.analysis.stp_tree import PortPrediction
from digital_twin.analysis.vlan_reachability import VlanComponent, vlan_components
from digital_twin.ir.confidence import ConfidenceLevel


def _predictions(actx: AnalysisContext) -> dict[str, PortPrediction]:
    """port_id -> its PortPrediction for this side's tree (empty if unpredicted)."""
    out: dict[str, PortPrediction] = {}
    for comp in actx.stp_tree().components:
        out.update(comp.ports)
    return out


def _edge_key(member_ports: list[str]) -> frozenset[str]:
    return frozenset(member_ports)


def _edge_keys(actx: AnalysisContext, vid: int) -> set[frozenset[str]]:
    g = actx.vlan_graph(vid)
    return {_edge_key(data["data"].member_ports) for _, _, data in g.edges(data=True)}


class StpReachability:
    def __init__(self, baseline: AnalysisContext, proposed: AnalysisContext) -> None:
        self._baseline = baseline
        self._proposed = proposed
        # baseline licence, computed once
        self._base_pred = _predictions(baseline)
        self._prop_pred = _predictions(proposed)
        report = compare_to_observed(baseline.stp_tree(), baseline.ir)
        self._base_agreements: tuple[ComponentAgreement, ...] = report.components
        self._base_comp_keys: dict[int, set[frozenset[str]]] = {}
        self._hard_cache: dict[tuple[str, int], tuple[VlanComponent, ...]] = {}

    # -- licence -------------------------------------------------------------
    def _clean_component_for(self, u: str, v: str) -> bool:
        """Both endpoints in ONE baseline STP component whose agreement is clean."""
        for a in self._base_agreements:
            if u in a.nodes and v in a.nodes:
                return a.agreement_clean
        return False

    def _baseline_edge_keys(self, vid: int) -> set[frozenset[str]]:
        if vid not in self._base_comp_keys:
            self._base_comp_keys[vid] = _edge_keys(self._baseline, vid)
        return self._base_comp_keys[vid]

    # -- side-local classification ------------------------------------------
    def _classify(
        self, actx: AnalysisContext, vid: int
    ) -> tuple[set[frozenset[str]], set[frozenset[str]]]:
        """(hard_keys, soft_keys) of blocked edges on THIS side of `vid`."""
        pred = self._base_pred if actx is self._baseline else self._prop_pred
        hard: set[frozenset[str]] = set()
        soft: set[frozenset[str]] = set()
        g = actx.vlan_graph(vid)
        base_keys = self._baseline_edge_keys(vid)
        for u, v, data in g.edges(data=True):
            edge = data["data"]
            blocking_ports = [p for p in edge.member_ports if _blocks(pred, p)]
            if not blocking_ports:
                continue
            key = _edge_key(edge.member_ports)
            block_conf = min(pred[p].confidence for p in blocking_ports)
            licensed = (
                key in base_keys  # (a) existed in baseline
                and self._clean_component_for(u, v)  # (b)+(c) same clean baseline component
                and block_conf is ConfidenceLevel.HIGH  # (d) side-local HIGH
            )
            (hard if licensed else soft).add(key)
        return hard, soft

    # -- removed-graph reachability -----------------------------------------
    def _components(
        self, actx: AnalysisContext, vid: int, remove: set[frozenset[str]]
    ) -> tuple[VlanComponent, ...]:
        g = actx.vlan_graph(vid)
        if not remove:
            return vlan_components(g, actx.exit_for(vid))
        h = g.copy()
        for u, v, key, data in list(h.edges(keys=True, data=True)):
            if _edge_key(data["data"].member_ports) in remove:
                h.remove_edge(u, v, key=key)
        return vlan_components(h, actx.exit_for(vid))

    def baseline_components(self, vid: int) -> tuple[VlanComponent, ...]:
        ck = ("b", vid)
        if ck not in self._hard_cache:
            hard, _ = self._classify(self._baseline, vid)
            self._hard_cache[ck] = self._components(self._baseline, vid, hard)
        return self._hard_cache[ck]

    def proposed_components(self, vid: int) -> tuple[VlanComponent, ...]:
        ck = ("p", vid)
        if ck not in self._hard_cache:
            hard, _ = self._classify(self._proposed, vid)
            self._hard_cache[ck] = self._components(self._proposed, vid, hard)
        return self._hard_cache[ck]


def _blocks(pred: dict[str, PortPrediction], pid: str) -> bool:
    p = pred.get(pid)
    return p is not None and p.state == "blocking"
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/analysis/test_stp_reachability.py -q`
Expected: PASS. If a builder cannot force the intended block, fix the builder (drive the block exactly as `test_stp_tree.py`'s parallel-link/self-loop tests do) — do NOT weaken the classification.

- [ ] **Step 5: Full gate + commit**

```bash
uv run pytest tests -q && uv run ruff check . && uv run mypy src
git add src/digital_twin/analysis/stp_reachability.py tests/analysis/test_stp_reachability.py
git commit -m "feat(analysis): StpReachability classification + hard-removed components"
```

---

### Task 3: soft-dependence query + blocked-edge-set relevance

**Files:**
- Modify: `src/digital_twin/analysis/stp_reachability.py`
- Test: `tests/analysis/test_stp_reachability.py`

**Interfaces:**
- Produces: `StpReachability.proposed_soft_dependent_components(vid) -> tuple[VlanComponent, ...]` (populated proposed hard-view components that reach exit with hard blocks removed but NOT once soft blocks are also removed) and `.blocked_edge_keys_changed(vid) -> bool` (baseline vs proposed hard∪soft key sets differ).

- [ ] **Step 1: Write the failing tests**

```python
def test_soft_dependence_detected_when_only_soft_block_carries_reach():
    # VLAN 10 reaches exit only via a SOFT-only blocked edge -> soft-dependent
    base, prop = _pruned_onto_block_pair(block_confirmed=False)  # soft-only
    sr = StpReachability(AnalysisContext(base), AnalysisContext(prop))
    soft = sr.proposed_soft_dependent_components(10)
    assert any(any(n.startswith("bb") for n in c.nodes) for c in soft)


def test_hard_dependence_is_not_soft_dependent():
    # a hard-eligible block already strands the component (it does NOT reach exit
    # in the hard view) -> NOT reported as soft-dependent (the hard path owns it)
    base, prop = _pruned_onto_block_pair(block_confirmed=True)
    sr = StpReachability(AnalysisContext(base), AnalysisContext(prop))
    assert sr.proposed_soft_dependent_components(10) == ()


def test_forwarding_path_is_not_soft_dependent():
    # VLAN 10 carried on BOTH links; blocking one leaves a forwarding path
    base, prop = _redundant_both_carry_pair()
    sr = StpReachability(AnalysisContext(base), AnalysisContext(prop))
    assert sr.proposed_soft_dependent_components(10) == ()


def test_blocked_edge_keys_changed_true_when_soft_set_differs():
    base, prop = _pruned_onto_block_pair(block_confirmed=False, block_new_in_proposed=True)
    sr = StpReachability(AnalysisContext(base), AnalysisContext(prop))
    assert sr.blocked_edge_keys_changed(10) is True


def test_blocked_edge_keys_changed_false_when_identical():
    base, prop = _pruned_onto_block_pair(block_confirmed=False, preexisting=True)
    sr = StpReachability(AnalysisContext(base), AnalysisContext(prop))
    assert sr.blocked_edge_keys_changed(10) is False
```

Add `_redundant_both_carry_pair()` (both links carry VLAN 10) and the `block_new_in_proposed` flag (block absent in baseline classification, present in proposed) to the builders.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/analysis/test_stp_reachability.py -k "soft_depend or hard_depend or forwarding_path or blocked_edge_keys" -v`
Expected: FAIL — methods undefined.

- [ ] **Step 3: Implement the two methods**

Add to `StpReachability`:

```python
    def proposed_soft_dependent_components(self, vid: int) -> tuple[VlanComponent, ...]:
        """Populated proposed components that reach their exit in the hard-removed
        view but NOT once soft blocks are also removed — i.e. the reach depends on
        a soft-only (unconfirmed) block. The hard-stranded case is excluded: such
        a component already fails to reach exit in the hard view."""
        hard, soft = self._classify(self._proposed, vid)
        hard_view = self.proposed_components(vid)
        reaching = [c for c in hard_view if c.has_members and c.reaches_exit]
        if not reaching or not soft:
            return ()
        hardsoft_view = self._components(self._proposed, vid, hard | soft)
        # A component is soft-dependent iff ANY of its nodes stops reaching the
        # exit once soft edges also go (removing soft edges can SPLIT it — a
        # partial split still strands some members). Strict `c.nodes - reaching`
        # (not `& reaching`) so a partial soft-dependence still floors REVIEW —
        # missing it would be a false-SAFE.
        reaching_nodes = {n for c in hardsoft_view if c.reaches_exit for n in c.nodes}
        return tuple(c for c in reaching if c.nodes - reaching_nodes)

    def blocked_edge_keys_changed(self, vid: int) -> bool:
        bh, bs = self._classify(self._baseline, vid)
        ph, ps = self._classify(self._proposed, vid)
        return (bh | bs) != (ph | ps)
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/analysis/test_stp_reachability.py -q`
Expected: PASS.

- [ ] **Step 5: Full gate + commit**

```bash
uv run pytest tests -q && uv run ruff check . && uv run mypy src
git add src/digital_twin/analysis/stp_reachability.py tests/analysis/test_stp_reachability.py
git commit -m "feat(analysis): StpReachability soft-dependence + blocked-edge relevance"
```

---

### Task 4: `CheckContext.stp_reachability` + blackhole component-source migration

**Files:**
- Modify: `src/digital_twin/checks/base.py`
- Modify: `src/digital_twin/checks/wired/l2_blackhole.py`
- Test: `tests/checks/test_l2_blackhole.py`

**Interfaces:**
- Consumes: `StpReachability` (Tasks 2–3).
- Produces: `CheckContext.stp_reachability -> StpReachability` (memoized).

- [ ] **Step 1: Write the failing tests**

Append to `tests/checks/test_l2_blackhole.py`:

```python
def test_blackhole_hard_strands_pruned_onto_block_vlan():
    # motivating: VLAN 10 pruned off the forwarding link, carried only on a
    # tree-blocked link, baseline telemetry confirms the block; the delta severs
    # the (VLAN-10-irrelevant) forwarding path -> exit_lost -> FAIL/UNSAFE-eligible
    ctx = _pruned_onto_block_check_ctx(block_confirmed=True)  # helper
    result = L2BlackholeCheck().run(ctx)
    codes = {f.code for f in result.findings}
    assert "wired.l2.blackhole.exit_lost" in codes
    assert result.status is Status.FAIL


def test_blackhole_preexisting_symmetric_block_is_info_not_fail():
    # the same confirmed block in BOTH sides, delta unrelated -> INFO, not FAIL
    ctx = _pruned_onto_block_check_ctx(block_confirmed=True, preexisting=True)
    result = L2BlackholeCheck().run(ctx)
    assert result.status is not Status.FAIL
    assert all(f.severity is not Severity.CRITICAL for f in result.findings)


def test_blackhole_context_exposes_memoized_stp_reachability():
    ctx = _simple_check_ctx()
    assert ctx.stp_reachability is ctx.stp_reachability
```

Write `_pruned_onto_block_check_ctx(...)` and `_simple_check_ctx()` by wrapping the Task-2 builders in a `CheckContext(AnalysisContext(base), AnalysisContext(prop))` (grep this test file for how existing tests build a `CheckContext`).

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/checks/test_l2_blackhole.py -k "pruned_onto_block or preexisting_symmetric or memoized_stp" -v`
Expected: FAIL — no `stp_reachability` property; blackhole still uses the cyclic graph.

- [ ] **Step 3a: Add the memoized property to `CheckContext`**

In `src/digital_twin/checks/base.py`, in the `CheckContext` dataclass (which already holds `baseline`, `proposed`, `delta_index`), add — mirroring the lazy `delta_index` pattern (import inside the property to avoid an analysis→checks cycle if one exists; verify import direction first):

```python
    @property
    def stp_reachability(self) -> "StpReachability":
        cached = getattr(self, "_stp_reachability", None)
        if cached is None:
            from digital_twin.analysis.stp_reachability import StpReachability
            cached = StpReachability(self.baseline, self.proposed)
            object.__setattr__(self, "_stp_reachability", cached)
        return cached
```

Add `from typing import TYPE_CHECKING` guard + `if TYPE_CHECKING: from digital_twin.analysis.stp_reachability import StpReachability` for the annotation. (If `CheckContext` is a frozen dataclass, `object.__setattr__` is required — confirm and match.)

- [ ] **Step 3b: Migrate all 8 `vlan_components` sites in `l2_blackhole.py`**

Swap each — baseline → `ctx.stp_reachability.baseline_components(vid)`, proposed → `ctx.stp_reachability.proposed_components(vid)`:
- `run()` wireless_in_play guard (proposed): `ctx.proposed.vlan_components(vid)` → `ctx.stp_reachability.proposed_components(vid)`.
- `_ap_blind_spots` baseline domain: `ctx.baseline.vlan_components(vid)` → `ctx.stp_reachability.baseline_components(vid)`.
- `_ap_blind_spots` `config_member_nodes` (baseline): same swap.
- `_ap_blind_spots` proposed loop: `ctx.proposed.vlan_components(vid)` → proposed variant.
- `_check_vlan` `components` (proposed): proposed variant.
- `_check_vlan` `baseline_components`: baseline variant.
- `_vlan_changed`: `ctx.baseline.vlan_components(vid) != ctx.proposed.vlan_components(vid)` → `ctx.stp_reachability.baseline_components(vid) != ctx.stp_reachability.proposed_components(vid)`.
- `_wlan_unresolved_notes` `delivery(side)`: this takes an `AnalysisContext` and calls `side.vlan_components(vid)`. Refactor `delivery` to take a components-getter instead:

```python
        def delivery(get: "Callable[[int], tuple[VlanComponent, ...]]") -> dict[str, set[int]]:
            out: dict[str, set[int]] = {}
            for vid in vids:
                for comp in get(vid):
                    for node in comp.nodes:
                        out.setdefault(node, set()).add(vid)
            return out

        base = delivery(ctx.stp_reachability.baseline_components)
        prop = delivery(ctx.stp_reachability.proposed_components)
```

Add the needed imports (`Callable`, `VlanComponent`).

- [ ] **Step 4: Run to verify they pass, then the full blackhole suite**

Run: `uv run pytest tests/checks/test_l2_blackhole.py -q`
Expected: PASS — the two new scenarios plus every pre-existing blackhole test. Pre-existing tests are unaffected because their synthetic fixtures carry no observed `stp_state` → `compare_to_observed` all-`unvalidatable` → `agreement_clean` False → no hard removal → STP-aware components equal the plain ones.

- [ ] **Step 5: Full gate + commit**

```bash
uv run pytest tests -q && uv run ruff check . && uv run mypy src
git add src/digital_twin/checks/base.py src/digital_twin/checks/wired/l2_blackhole.py tests/checks/test_l2_blackhole.py
git commit -m "feat(checks): blackhole consumes STP-aware components (hard blocked-link taint)"
```

If any pre-existing golden/e2e test outside `test_l2_blackhole.py` changes verdict, STOP and report it — do not blindly update. An expected change is only a fixture that has both observed `stp_state` AND a pruned-onto-confirmed-block VLAN; anything else is a regression.

---

### Task 5: blackhole soft REVIEW floor (relevance-gated sub-HIGH confidence)

**Files:**
- Modify: `src/digital_twin/checks/wired/l2_blackhole.py`
- Test: `tests/checks/test_l2_blackhole.py`

**Interfaces:**
- Consumes: `StpReachability.proposed_soft_dependent_components(vid)`, `.blocked_edge_keys_changed(vid)` (Task 3); existing `_vlan_changed`, `_exit_changed`.

- [ ] **Step 1: Write the failing tests**

```python
def test_blackhole_soft_dependence_floors_review_without_hard_finding():
    # VLAN 10 reaches exit only via a SOFT-only block (unconfirmed) and the delta
    # is relevant -> sub-HIGH confidence (REVIEW floor) + note, NO hard finding
    ctx = _pruned_onto_block_check_ctx(block_confirmed=False)
    result = L2BlackholeCheck().run(ctx)
    assert result.confidence is not None and result.confidence.level is not ConfidenceLevel.HIGH
    assert any("predicted blocking" in n for n in result.coverage.notes)
    assert not any(f.severity is Severity.CRITICAL for f in result.findings)  # no hard strand


def test_blackhole_soft_dependence_suppressed_when_irrelevant():
    # a pre-existing soft dependence, delta does NOT touch the blocked-edge set /
    # vlan / exit -> no REVIEW floor from this path
    ctx = _pruned_onto_block_check_ctx(block_confirmed=False, preexisting=True, unrelated_delta=True)
    result = L2BlackholeCheck().run(ctx)
    # confidence not dragged below HIGH by the soft path (may still be HIGH pass)
    assert result.confidence is None or result.confidence.level is ConfidenceLevel.HIGH
    assert not any("predicted blocking" in n for n in result.coverage.notes)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/checks/test_l2_blackhole.py -k "soft_dependence" -v`
Expected: FAIL — no soft floor yet.

- [ ] **Step 3: Add the soft-floor pass in `run()`**

In `L2BlackholeCheck.run`, inside the per-`vid` loop (after `_check_vlan`), add a call collecting soft notes + a sub-HIGH confidence. Implement a helper:

```python
    def _soft_taint(
        self, ctx: CheckContext, vid: int, confidences: list[Confidence]
    ) -> list[str]:
        """Spec-5 soft floor: a delta-relevant reach that survives only because of
        a soft-only (unconfirmed / low-confidence / unlicensed) predicted block is
        not SAFE-certifiable — append a sub-HIGH confidence (REVIEW floor) + note.
        Never a hard finding. Relevance = the vlan/exit/blocked-edge-set changed."""
        sr = ctx.stp_reachability
        relevant = _vlan_changed(ctx, vid) or _exit_changed(ctx, vid) or sr.blocked_edge_keys_changed(vid)
        if not relevant:
            return []
        soft_dep = sr.proposed_soft_dependent_components(vid)
        if not soft_dep:
            return []
        confidences.append(
            Confidence(
                level=ConfidenceLevel.MEDIUM,
                reasons=(f"vlan {vid} reachability depends on a link predicted "
                         "blocking, unconfirmed by telemetry",),
            )
        )
        nodes = sorted(n for c in soft_dep for n in c.nodes)
        return [
            f"vlan {vid}: exit reachability depends on a link predicted blocking "
            f"(unconfirmed) — nodes {nodes}"
        ]
```

Call it in the loop and extend `notes`:

```python
        for vid in sorted(set(ctx.baseline.ir.vlans) | set(ctx.proposed.ir.vlans)):
            statuses.append(self._check_vlan(ctx, vid, findings, confidences))
            notes.extend(self._soft_taint(ctx, vid, confidences))
            wireless_in_play = wireless_in_play or (
                _vlan_changed(ctx, vid)
                and any(c.wireless_members for c in ctx.stp_reachability.proposed_components(vid))
            )
            notes.extend(self._ap_blind_spots(ctx, vid))
```

(The `wireless_in_play` line's `vlan_components` was already migrated in Task 4 — shown here for placement only.)

- [ ] **Step 4: Run to verify they pass, then the full blackhole suite**

Run: `uv run pytest tests/checks/test_l2_blackhole.py -q`
Expected: PASS. The soft note substring `"predicted blocking"` matches the note text.

- [ ] **Step 5: Full gate + commit**

```bash
uv run pytest tests -q && uv run ruff check . && uv run mypy src
git add src/digital_twin/checks/wired/l2_blackhole.py tests/checks/test_l2_blackhole.py
git commit -m "feat(checks): blackhole soft REVIEW floor on unconfirmed blocked-link reach"
```

Same STOP rule as Task 4: any unrelated golden flipping to REVIEW is a regression to report, not to paper over.

---

### Task 6: end-to-end scenario goldens

**Files:**
- Create: `tests/golden/test_stp_reachability_scenarios.py`

**Interfaces:**
- Consumes: the public verdict path (`assemble`/`decide` or the existing golden harness — grep `tests/golden/` for the established end-to-end entry, e.g. `simulate`/`run_checks`).

- [ ] **Step 1: Write the failing scenario tests**

One end-to-end test per spec "Testing" bullet, driving the full verdict (not just the check), using the golden harness the other `tests/golden/` files use:

```python
def test_scenario_hard_strand_is_unsafe():
    verdict = _run(_pruned_onto_block_plan(block_confirmed=True))
    assert verdict.decision is Decision.UNSAFE
    assert any(f.code == "wired.l2.blackhole.exit_lost" for f in verdict.findings)


def test_scenario_preexisting_symmetric_unchanged():
    verdict = _run(_pruned_onto_block_plan(block_confirmed=True, preexisting=True, unrelated_delta=True))
    assert verdict.decision is not Decision.UNSAFE  # INFO context only


def test_scenario_soft_low_confidence_is_review_not_unsafe():
    verdict = _run(_pruned_onto_block_plan(block_confirmed=False))
    assert verdict.decision is Decision.REVIEW
    assert not any(f.severity is Severity.CRITICAL for f in verdict.findings)


def test_scenario_disagreement_stays_soft():
    verdict = _run(_pruned_onto_block_plan(block_confirmed=True, telemetry_contradicts=True))
    assert verdict.decision is not Decision.UNSAFE  # mismatch -> soft only


def test_scenario_vacuous_agreement_stays_soft():
    verdict = _run(_pruned_onto_block_plan(block_confirmed=False))  # no telemetry
    assert verdict.decision is Decision.REVIEW


def test_scenario_new_intra_component_edge_soft():
    verdict = _run(_pruned_onto_block_plan(block_confirmed=True, edge_new_in_proposed=True))
    assert verdict.decision is not Decision.UNSAFE  # not baseline-licensed -> soft
```

- [ ] **Step 2: Run to verify they fail (or error on missing builders), implement builders, run to PASS**

Build `_pruned_onto_block_plan(...)` producing a `(baseline_raw, ChangePlan)` or the shape the golden harness consumes; reuse Task-2 topology. Run: `uv run pytest tests/golden/test_stp_reachability_scenarios.py -q` → PASS.

- [ ] **Step 3: Full gate + commit**

```bash
uv run pytest tests -q && uv run ruff check . && uv run mypy src
git add tests/golden/test_stp_reachability_scenarios.py
git commit -m "test(golden): Spec-5 blocked-link reachability scenarios (hard/soft/pre-existing)"
```

---

### Task 7: docs wrap + live verify

**Files:**
- Modify: `docs/ROADMAP.md`, `README.md`

- [ ] **Step 1: ROADMAP + README**

Add the Spec-5 entry to `docs/ROADMAP.md` in the STP program section (near Spec-4): blocked-link reachability taint shipped for blackhole; confidence-gated soft/hard; baseline-agreement-licensed; `l2_isolation` + other consumers still deferred; spec/plan paths. Update the `README.md` `analysis/` inventory line to mention STP-aware reachability.

- [ ] **Step 2: Full gate + commit**

```bash
uv run pytest tests -q && uv run ruff check . && uv run mypy src
git add docs/ROADMAP.md README.md
git commit -m "docs: Spec-5 wrap (roadmap, readme)"
```

- [ ] **Step 3: Live verify (finishing phase, README worktree caution: the user's uncommitted README draft lives in the MAIN checkout, not here — if `git status --short README.md` in THIS worktree shows unexpected changes, stop)**

Simulate against the production org (`set -a && source ../../../.env && set +a`) via a benign plan: expect **no new UNSAFE** (production has 10/10 STP agreement and no known pruned-onto-blocked-link VLAN). Record the observed blackhole coverage/notes summary in the ledger. This step is run by the controller in the finishing phase, not inside a task subagent.

---

## Notes for the executor

- **Why existing tests are safe (Task 4/5):** hard removal requires `agreement_clean` (matched observed telemetry). Synthetic fixtures without `stp_state` produce all-`unvalidatable` agreement → no hard removal → STP-aware components equal plain ones. The soft floor additionally requires delta-relevance AND soft-dependence (reach only via a predicted block) — a shape current fixtures don't shipwith. Any flip is therefore either the intended new behavior on a telemetry-bearing pruned-onto-block fixture, or a regression to investigate — never a golden to blindly rewrite.
- **Determinism:** `StpReachability` reuses `vlan_components`'s sort; the classification sets are keyed by `frozenset(member_ports)`; no set iteration reaches output ordering.
