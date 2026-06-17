# Finding Cause Attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every delta-attributed finding names the changed entity (port/link/device/l3intf/…) that produced it, via a new `Finding.caused_by`; pre-existing findings carry none. Strictly additive — no verdict/severity/coverage change.

**Architecture:** A new `Cause` contract + `Finding.caused_by` field. A pure `analysis/delta_cause.py` exposing a cached `DeltaIndex` (diff lookup) carried on `CheckContext`, plus per-finding Family-2 mapping functions (cut/severance/loop/root). Family-1 checks attribute their already-known changed entity directly; Family-2 checks call the mapping functions; `client_impact` nests per-impact causes. Names resolved centrally by `subjects.py:name_findings`. Adapter findings attribute inline with a baseline-vs-proposed parity guard.

**Tech Stack:** Python 3.14, uv, pytest, ruff, mypy (strict on `src`). Gate: `uv run pytest tests -q && uv run ruff check . && uv run mypy src`.

**Spec:** `docs/superpowers/specs/2026-06-16-finding-cause-attribution-design.md`

**Conventions for every commit:** end the message with
`Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
Run the full gate before each commit; it must be green.

---

## Phase 1 — Contract + plumbing (lands with empty `caused_by` everywhere; no behavior change)

### Task 1: `Cause` dataclass + `Finding.caused_by` field

**Files:**
- Modify: `src/digital_twin/contracts/finding.py`
- Modify: `src/digital_twin/contracts/__init__.py` (export `Cause`)
- Test: `tests/contracts/test_finding_cause.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/contracts/test_finding_cause.py
from digital_twin.contracts import Cause, Finding, FindingCategory, FindingSource, ObjectRef, Severity
from digital_twin.ir import Confidence, ConfidenceLevel


def _f(**kw):
    base = dict(
        source=FindingSource.CHECK, category=FindingCategory.NETWORK, code="x",
        severity=Severity.WARNING, confidence=Confidence(level=ConfidenceLevel.HIGH), message="m",
    )
    base.update(kw)
    return Finding(**base)


def test_caused_by_defaults_empty():
    assert _f().caused_by == ()


def test_cause_carries_ref_and_fields():
    c = Cause(ref=ObjectRef("port", "p1"), fields=("native_vlan",))
    f = _f(caused_by=(c,))
    assert f.caused_by[0].ref.id == "p1"
    assert f.caused_by[0].fields == ("native_vlan",)


def test_cause_fields_default_empty():
    assert Cause(ref=ObjectRef("link", "l1")).fields == ()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/contracts/test_finding_cause.py -q`
Expected: FAIL — `ImportError: cannot import name 'Cause'`.

- [ ] **Step 3: Implement**

In `src/digital_twin/contracts/finding.py`, add after `ObjectRef` (before `Finding`):

```python
@dataclass(frozen=True)
class Cause:
    """A changed entity responsible for a finding. `ref` locates the changed
    object (same ObjectRef vocabulary as Finding.subject); `fields` are the
    NORMALIZED IR field name(s) that changed (e.g. ("native_vlan",), ("poe",),
    ("stp_priority",)) — empty for pure add/remove deltas. Additive, evidence-only:
    never read by the verdict layer."""

    ref: ObjectRef
    fields: tuple[str, ...] = ()
```

Add to `Finding` (last field, trailing default — keeps all existing call sites valid):

```python
    subject: ObjectRef | None = None  # the headline object (which device/vlan/...)
    caused_by: tuple["Cause", ...] = ()  # changed entities that produced this (delta-attributed only)
```

In `src/digital_twin/contracts/__init__.py`, add `Cause` to the imports from `.finding` and to `__all__`.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/contracts/test_finding_cause.py -q && uv run mypy src`
Expected: PASS; mypy clean.

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/contracts/ tests/contracts/test_finding_cause.py
git commit -m "feat(cause-attribution): add Cause contract + Finding.caused_by field"
```

---

### Task 2: `name_findings` resolves `caused_by` refs (top-level + nested impacts)

**Files:**
- Modify: `src/digital_twin/checks/subjects.py`
- Test: `tests/checks/test_subjects_caused_by.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/checks/test_subjects_caused_by.py
from digital_twin.checks.subjects import name_findings
from digital_twin.contracts import Cause, Finding, FindingCategory, FindingSource, ObjectRef, Severity
from digital_twin.ir import Confidence, ConfidenceLevel
from tests.factories import ir_with_one_named_port  # see Step 1b


def _f(**kw):
    base = dict(
        source=FindingSource.CHECK, category=FindingCategory.NETWORK, code="x",
        severity=Severity.WARNING, confidence=Confidence(level=ConfidenceLevel.HIGH), message="m",
    )
    base.update(kw)
    return Finding(**base)


def test_top_level_cause_ref_gets_named():
    ir = ir_with_one_named_port("dev1:ge-0/0/1", "uplink-1")
    f = _f(caused_by=(Cause(ref=ObjectRef("port", "dev1:ge-0/0/1")),))
    named = name_findings((f,), ir, ir)
    assert named[0].caused_by[0].ref.name == "uplink-1"


def test_nested_impacts_cause_ref_gets_named():
    ir = ir_with_one_named_port("dev1:ge-0/0/1", "uplink-1")
    f = _f(evidence={"impacts": [{"mac": "aa", "caused_by": [Cause(ref=ObjectRef("port", "dev1:ge-0/0/1"))]}]})
    named = name_findings((f,), ir, ir)
    assert named[0].evidence["impacts"][0]["caused_by"][0].ref.name == "uplink-1"


def test_device_cause_ref_stays_unnamed():
    ir = ir_with_one_named_port("dev1:ge-0/0/1", "uplink-1")
    f = _f(caused_by=(Cause(ref=ObjectRef("device", "dev1")),))
    named = name_findings((f,), ir, ir)
    assert named[0].caused_by[0].ref.name is None  # devices have no IR name
```

- [ ] **Step 1b: Add the test helper** (if `ir_with_one_named_port` does not already exist in `tests/factories.py`, add it — a minimal IR with one named `Port`). Check first: `grep -n "def ir_with_one_named_port" tests/factories.py`. If absent, add a small factory building an `IR` whose `ports` map contains one `Port(id=..., device_id="dev1", name=...)`.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/checks/test_subjects_caused_by.py -q`
Expected: FAIL — `caused_by[0].ref.name` is `None` (resolver not yet extended).

- [ ] **Step 3: Implement**

In `src/digital_twin/checks/subjects.py`, extend `name_findings`. Add a cause-resolver and a guarded nested-evidence pass; reuse `resolve_subject`/`_name_for`:

```python
def _resolve_cause(cause, prop_ir, base_ir):
    return replace(cause, ref=resolve_subject(cause.ref, prop_ir, base_ir))


def _resolve_caused_by(causes, prop_ir, base_ir):
    return tuple(_resolve_cause(c, prop_ir, base_ir) for c in causes)


def _resolve_nested_impacts(evidence, prop_ir, base_ir):
    """Name the per-impact causes client_impact nests under evidence['impacts'].
    Guarded: only touches the known {'impacts': [{'caused_by': [...]}, ...]} shape."""
    impacts = evidence.get("impacts")
    if not isinstance(impacts, list):
        return evidence
    new_impacts = []
    for imp in impacts:
        if isinstance(imp, dict) and imp.get("caused_by"):
            imp = {**imp, "caused_by": list(_resolve_caused_by(tuple(imp["caused_by"]), prop_ir, base_ir))}
        new_impacts.append(imp)
    return {**evidence, "impacts": new_impacts}


def name_findings(findings, prop_ir, base_ir):
    out = []
    for f in findings:
        out.append(replace(
            f,
            subject=resolve_subject(f.subject, prop_ir, base_ir),
            caused_by=_resolve_caused_by(f.caused_by, prop_ir, base_ir),
            evidence=_resolve_nested_impacts(f.evidence, prop_ir, base_ir),
        ))
    return tuple(out)
```

Add the needed imports (`Cause` is not constructed here, only `replace` already imported). Keep type hints consistent with the existing signature.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/checks/test_subjects_caused_by.py -q && uv run mypy src`
Expected: PASS; mypy clean.

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/checks/subjects.py tests/checks/test_subjects_caused_by.py tests/factories.py
git commit -m "feat(cause-attribution): resolve caused_by names centrally (top-level + nested impacts)"
```

---

### Task 3: Render the cause clause (human) + verify dict serialization

**Files:**
- Modify: `src/digital_twin/drivers/render.py`
- Test: `tests/drivers/test_render_caused_by.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/drivers/test_render_caused_by.py
from digital_twin.contracts import Cause, Finding, FindingCategory, FindingSource, ObjectRef, Severity
from digital_twin.drivers.render import _finding_line
from digital_twin.ir import Confidence, ConfidenceLevel


def _f(caused_by):
    return Finding(
        source=FindingSource.CHECK, category=FindingCategory.NETWORK,
        code="wired.l2.vlan_segmentation.split", severity=Severity.WARNING,
        confidence=Confidence(level=ConfidenceLevel.HIGH), message="vlan 7 partitioned",
        subject=ObjectRef("vlan", "7"), caused_by=caused_by,
    )


def test_single_cause_clause():
    line = _finding_line(_f((Cause(ref=ObjectRef("port", "dev1:mge-0/0/0", name="mge-0/0/0"), fields=("native_vlan",)),)))
    assert '(caused by port "mge-0/0/0" [native_vlan])' in line


def test_multiple_causes_clause():
    line = _finding_line(_f((
        Cause(ref=ObjectRef("port", "dev1:mge-0/0/0", name="mge-0/0/0"), fields=("native_vlan",)),
        Cause(ref=ObjectRef("port", "dev1:mge-0/0/1", name="mge-0/0/1"), fields=("native_vlan",)),
    )))
    assert "caused by" in line and "mge-0/0/0" in line and "mge-0/0/1" in line


def test_no_clause_when_empty():
    assert "caused by" not in _finding_line(_f(()))


def test_cause_without_name_shows_id():
    line = _finding_line(_f((Cause(ref=ObjectRef("device", "dev9")),)))
    assert "caused by device dev9" in line
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/drivers/test_render_caused_by.py -q`
Expected: FAIL — no cause clause emitted.

- [ ] **Step 3: Implement**

In `src/digital_twin/drivers/render.py`, add a helper and append its output in `_finding_line`:

```python
def _cause_clause(f: Finding) -> str:
    if not f.caused_by:
        return ""
    parts = []
    for c in f.caused_by:
        who = f'"{c.ref.name}"' if c.ref.name else c.ref.id
        flds = f" [{', '.join(c.fields)}]" if c.fields else ""
        parts.append(f"{c.ref.kind} {who}{flds}")
    return f" (caused by {', '.join(parts)})"
```

Change the return of `_finding_line` to append the clause:

```python
    return f"  {label} [{f.severity.value}] {f.code}{where}{at}: {f.message}{_cause_clause(f)}"
```

The dict path needs NO change: `verdict_to_dict` → `_plain` dataclass-walks `Finding`, so `caused_by` (tuple of `Cause`) and the nested `evidence["impacts"][i]["caused_by"]` serialize automatically.

- [ ] **Step 4: Add a dict-serialization regression test** (same file):

```python
def test_dict_serializes_caused_by():
    from digital_twin.drivers.render import _plain
    d = _plain(_f((Cause(ref=ObjectRef("port", "p1", name="mge-0/0/0"), fields=("native_vlan",)),)))
    assert d["caused_by"][0]["ref"]["name"] == "mge-0/0/0"
    assert d["caused_by"][0]["fields"] == ["native_vlan"]
```

- [ ] **Step 5: Run + commit**

Run: `uv run pytest tests/drivers/test_render_caused_by.py -q && uv run ruff check . && uv run mypy src`
Expected: PASS; clean.

```bash
git add src/digital_twin/drivers/render.py tests/drivers/test_render_caused_by.py
git commit -m "feat(cause-attribution): render cause clause in human output"
```

---

## Phase 2 — DeltaIndex + Family-2 mapping functions + CheckContext wiring

### Task 4: `DeltaIndex` + `delta_index(diff)`

**Files:**
- Create: `src/digital_twin/analysis/delta_cause.py`
- Test: `tests/analysis/test_delta_cause.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/analysis/test_delta_cause.py
from digital_twin.analysis.delta_cause import delta_index
from digital_twin.ir.diff import EntityRef, IRDiff, Modified


def _diff(added=(), removed=(), modified=()):
    return IRDiff(tuple(added), tuple(removed), tuple(modified))


def test_modified_entity_yields_cause_with_fields():
    di = delta_index(_diff(modified=(Modified(EntityRef("port", "p1"), ("native_vlan",)),)))
    c = di.cause("port", "p1")
    assert c is not None and c.ref.kind == "port" and c.ref.id == "p1" and c.fields == ("native_vlan",)


def test_added_and_removed_have_empty_fields():
    di = delta_index(_diff(added=(EntityRef("l3intf", "x"),), removed=(EntityRef("link", "l9"),)))
    assert di.cause("l3intf", "x").fields == ()
    assert di.cause("link", "l9").fields == ()


def test_unchanged_entity_yields_none():
    di = delta_index(_diff())
    assert di.cause("port", "nope") is None


def test_kinds_query_helper():
    di = delta_index(_diff(modified=(Modified(EntityRef("port", "p1"), ("poe",)),)))
    assert di.in_delta("port", "p1") and not di.in_delta("device", "p1")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/analysis/test_delta_cause.py -q`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement**

```python
# src/digital_twin/analysis/delta_cause.py
"""Pure delta-attribution helper. DeltaIndex is the cached diff lookup ONLY:
given an entity (kind,id), is it in the delta and with which changed IR fields?
It does NO graph analysis. The Family-2 mapping functions (added in later tasks)
take a CheckContext + the affected component/cycle/vid and consult this index."""

from __future__ import annotations

from dataclasses import dataclass

from digital_twin.contracts import Cause, ObjectRef
from digital_twin.ir.diff import IRDiff


@dataclass(frozen=True)
class DeltaIndex:
    _fields: dict[tuple[str, str], tuple[str, ...]]   # (kind,id) -> changed fields
    _addremove: frozenset[tuple[str, str]]            # added or removed (no field set)

    def in_delta(self, kind: str, oid: str) -> bool:
        key = (kind, oid)
        return key in self._fields or key in self._addremove

    def cause(self, kind: str, oid: str) -> Cause | None:
        """Cause for an entity IFF it is in the delta; else None (honesty rule)."""
        key = (kind, oid)
        if key in self._fields:
            return Cause(ref=ObjectRef(kind, oid), fields=self._fields[key])
        if key in self._addremove:
            return Cause(ref=ObjectRef(kind, oid), fields=())
        return None

    def causes(self, kind: str, oids: object) -> tuple[Cause, ...]:
        """Map an iterable of ids of one kind to the subset that is in the delta."""
        out = []
        for oid in oids:  # type: ignore[union-attr]
            c = self.cause(kind, str(oid))
            if c is not None:
                out.append(c)
        return tuple(out)


def delta_index(diff: IRDiff) -> DeltaIndex:
    fields = {(m.ref.kind, m.ref.id): m.changed_fields for m in diff.modified}
    addremove = frozenset(
        (r.kind, r.id) for r in (*diff.added, *diff.removed)
    )
    return DeltaIndex(_fields=fields, _addremove=addremove)
```

- [ ] **Step 4: Run + commit**

Run: `uv run pytest tests/analysis/test_delta_cause.py -q && uv run mypy src`
Expected: PASS; clean.

```bash
git add src/digital_twin/analysis/delta_cause.py tests/analysis/test_delta_cause.py
git commit -m "feat(cause-attribution): DeltaIndex cached diff lookup"
```

---

### Task 5: Carry `delta_index` on `CheckContext`; build it in the pipeline

**Files:**
- Modify: `src/digital_twin/checks/base.py` (add field to `CheckContext`)
- Modify: `src/digital_twin/engine/pipeline.py:226` (construct it)
- Test: `tests/checks/test_check_context_delta_index.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/checks/test_check_context_delta_index.py
from digital_twin.analysis.context import AnalysisContext
from digital_twin.analysis.delta_cause import DeltaIndex, delta_index
from digital_twin.checks.base import CheckContext
from digital_twin.ir import IR
from digital_twin.ir.diff import IRDiff


def test_check_context_has_delta_index_default():
    ir = IR()  # empty IR is valid
    ctx = CheckContext(baseline=AnalysisContext(ir), proposed=AnalysisContext(ir), diff=IRDiff((), (), ()))
    assert isinstance(ctx.delta_index, DeltaIndex)  # default built from empty diff


def test_check_context_accepts_explicit_index():
    ir = IR()
    di = delta_index(IRDiff((), (), ()))
    ctx = CheckContext(baseline=AnalysisContext(ir), proposed=AnalysisContext(ir), diff=IRDiff((), (), ()), delta_index=di)
    assert ctx.delta_index is di
```

(If `IR()` is not constructible with no args, use the smallest existing IR factory in `tests/factories.py`.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/checks/test_check_context_delta_index.py -q`
Expected: FAIL — `CheckContext` has no `delta_index`.

- [ ] **Step 3: Implement**

In `src/digital_twin/checks/base.py`, add the field with a default factory so existing constructions stay valid:

```python
from dataclasses import dataclass, field
from digital_twin.analysis.delta_cause import DeltaIndex, delta_index
from digital_twin.ir.diff import IRDiff

@dataclass(frozen=True)
class CheckContext:
    baseline: AnalysisContext
    proposed: AnalysisContext
    diff: IRDiff
    delta_index: DeltaIndex = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.delta_index is None:
            object.__setattr__(self, "delta_index", delta_index(self.diff))
```

(Frozen dataclass: use `object.__setattr__` in `__post_init__` to default from `diff`. Verify no import cycle: `delta_cause` imports only contracts + `ir.diff`, `base` may import it.)

In `src/digital_twin/engine/pipeline.py` where `CheckContext(...)` is built (~line 226), pass the index explicitly for clarity:

```python
        diff = diff_ir(baseline.ir, proposed.ir)
        results = registry.run_all(
            CheckContext(
                baseline=AnalysisContext(baseline.ir),
                proposed=AnalysisContext(proposed.ir),
                diff=diff,
                delta_index=delta_index(diff),
            )
        )
```

Add `from digital_twin.analysis.delta_cause import delta_index` to pipeline imports.

- [ ] **Step 4: Run + commit**

Run: `uv run pytest tests/checks/ tests/engine/ -q && uv run mypy src`
Expected: PASS; clean (the default keeps every existing `CheckContext(...)` in tests valid).

```bash
git add src/digital_twin/checks/base.py src/digital_twin/engine/pipeline.py tests/checks/test_check_context_delta_index.py
git commit -m "feat(cause-attribution): carry DeltaIndex on CheckContext"
```

---

### Task 6: Family-2 mapping — `causes_for_vlan_cut(ctx, vid, component)`

**Files:**
- Modify: `src/digital_twin/analysis/delta_cause.py`
- Test: `tests/analysis/test_delta_cause_vlan_cut.py` (create)

Two mappings (blackhole vs split need different geometry):
- `causes_for_vlan_cut(ctx, vid, component)` (blackhole `exit_lost`): cause = the **boundary** edges of THIS stranded proposed component that are gone in proposed — baseline vlan-graph edges with **exactly one endpoint inside** the component (`(u in C) ^ (v in C)`), so an unrelated *internal* edge loss is never blamed. Each lost edge contributes its `member_ports` AND `link_ids`, kept only if in the delta.
- `causes_for_vlan_split(ctx, vid)` (segmentation `.split`): the check has no single `component` — cause = baseline edges gone in proposed whose endpoints now land in **different** proposed fragments (the separating edges).

Both are component-local — two independent cuts in one plan never cross-contaminate — and both return `()` when no lost edge is in the delta (honesty rule).

**Confirmed payload:** vlan-graph edges store an `L2Edge` under `data["data"]` (`src/digital_twin/representations/graph_data.py:L2Edge`) with `member_ports: list[str]` and `link_ids: list[str]`. There is NO `data["link"]`.

- [ ] **Step 1: Write the failing tests** — build baseline/proposed IRs via the existing factories (follow `tests/analysis/test_vlan_reachability*.py`). Cases:
  1. `causes_for_vlan_cut`: vlan 7 stranded fragment lost its boundary trunk port `A:ge-0/0/1` (in the delta) → names `A:ge-0/0/1`.
  2. ambiguous: a partition whose lost edge is NOT in the delta → `()`.
  3. **two independent cuts** (vlan 7 cut at `A:ge-0/0/1`, vlan 8 cut at `C:ge-0/0/9`, both in delta) → vlan 7's fragment names ONLY `A:ge-0/0/1`.
  4. **internal-edge-loss is NOT named**: an internal edge inside the stranded fragment is lost but the boundary edge is the real cut → only the boundary edge's port is named (validates the `^` boundary rule).
  5. `causes_for_vlan_split`: a baseline component fragments into two; the separating edge's port is named (and its case where only the **link** id is in the delta, endpoint ports unchanged → the `link` cause is named).

```python
# tests/analysis/test_delta_cause_vlan_cut.py  (fill IRs from factories)
from digital_twin.analysis.context import AnalysisContext
from digital_twin.analysis.delta_cause import causes_for_vlan_cut, delta_index
from digital_twin.checks.base import CheckContext
from digital_twin.ir.diff import diff_ir


def _ctx(base_ir, prop_ir):
    diff = diff_ir(base_ir, prop_ir)
    return CheckContext(AnalysisContext(base_ir), AnalysisContext(prop_ir), diff, delta_index(diff))


def _stranded(ctx, vid):
    return [c for c in ctx.proposed.vlan_components(vid) if not c.reaches_exit][0]


def test_vlan_cut_names_the_changed_trunk_port():
    ctx = _ctx(base_ir, prop_ir)
    causes = causes_for_vlan_cut(ctx, 7, _stranded(ctx, 7))
    assert {c.ref.id for c in causes} == {"A:ge-0/0/1"}


def test_vlan_cut_ambiguous_returns_empty():
    ctx = _ctx(base_ir2, prop_ir2)  # lost edge not in the delta
    assert causes_for_vlan_cut(ctx, 7, _stranded(ctx, 7)) == ()


def test_two_independent_cuts_do_not_cross_contaminate():
    ctx = _ctx(base_ir3, prop_ir3)  # vlan7 cut at A:ge-0/0/1, vlan8 cut at C:ge-0/0/9
    assert {c.ref.id for c in causes_for_vlan_cut(ctx, 7, _stranded(ctx, 7))} == {"A:ge-0/0/1"}
    assert {c.ref.id for c in causes_for_vlan_cut(ctx, 8, _stranded(ctx, 8))} == {"C:ge-0/0/9"}
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/analysis/test_delta_cause_vlan_cut.py -q` → FAIL (function missing).

- [ ] **Step 3: Implement** in `delta_cause.py` — shared **boundary-edge** helpers (reused by severance/root in Tasks 7/8), then BOTH vlan mappings. A cut/severance cause is a *boundary* edge — exactly one endpoint inside the reported fragment — so an unrelated internal edge loss is never blamed. Each lost edge contributes its `member_ports` AND `link_ids` (a link added/removed with unchanged endpoint ports must still be attributable).

```python
def _boundary_lost_edges(base_g, prop_g, nodes) -> list:
    """L2Edge payloads of baseline edges with EXACTLY ONE endpoint in `nodes`
    that are gone in the proposed graph — the boundary cut of this fragment.
    data['data'] is an L2Edge (graph_data.py)."""
    nodeset = set(nodes)
    out = []
    for u, v, data in base_g.edges(data=True):
        if ((u in nodeset) ^ (v in nodeset)) and not prop_g.has_edge(u, v):
            out.append(data["data"])
    return out


def _edge_causes(di, edges) -> tuple[Cause, ...]:
    """Map L2Edge payloads to delta-present port AND link causes."""
    ports: set[str] = set()
    links: set[str] = set()
    for e in edges:
        ports.update(e.member_ports)
        links.update(e.link_ids)
    return tuple(dict.fromkeys((*di.causes("port", sorted(ports)), *di.causes("link", sorted(links)))))


def causes_for_vlan_cut(ctx, vid: int, component) -> tuple[Cause, ...]:
    """Blackhole exit_lost: the stranded proposed `component` lost its boundary
    edge(s) to the rest of the vlan domain."""
    edges = _boundary_lost_edges(ctx.baseline.vlan_graph(vid), ctx.proposed.vlan_graph(vid), component.nodes)
    return _edge_causes(ctx.delta_index, edges)


def causes_for_vlan_split(ctx, vid: int) -> tuple[Cause, ...]:
    """Segmentation split: a baseline vlan component fragmented. Cause = baseline
    edges gone in proposed whose endpoints now sit in DIFFERENT proposed fragments
    (the edges whose loss did the splitting). Naturally local: only separating
    edges qualify."""
    base_g, prop_g = ctx.baseline.vlan_graph(vid), ctx.proposed.vlan_graph(vid)
    comp_of: dict[str, int] = {}
    for i, comp in enumerate(ctx.proposed.vlan_components(vid)):
        for n in comp.nodes:
            comp_of[n] = i
    edges = [
        data["data"] for u, v, data in base_g.edges(data=True)
        if not prop_g.has_edge(u, v) and comp_of.get(u) != comp_of.get(v)
    ]
    return _edge_causes(ctx.delta_index, edges)
```

(`component.nodes` is the `VlanComponent.nodes` frozenset. `DeltaIndex.causes` filters to delta members.)

- [ ] **Step 4: Run + commit**

Run: `uv run pytest tests/analysis/test_delta_cause_vlan_cut.py -q && uv run mypy src`

```bash
git add src/digital_twin/analysis/delta_cause.py tests/analysis/test_delta_cause_vlan_cut.py
git commit -m "feat(cause-attribution): causes_for_vlan_cut mapping (Family-2 rule 1)"
```

---

### Task 7: Family-2 mapping — `causes_for_severance(ctx, island)` (physical L2 cut)

**Files:** Modify `delta_cause.py`; Test `tests/analysis/test_delta_cause_severance.py`.

Rule (spec rule 2): the physical edge usually vanishes via `port.disabled` or port removal (l2_graph drops an edge when either endpoint port is `disabled`). Cause = delta-changed boundary **links OR their endpoint ports** that severed the island from its former domain.

`island` is the newly-isolated node-set (a `VlanComponent`, or a `.nodes` frozenset) the check already computed. Cause = delta-changed ports/links whose disabled/removal dropped a physical L2 **boundary** edge of the island — reusing `_boundary_lost_edges` + `_edge_causes` from Task 6 on the L2 graph. Boundary-local (exactly one endpoint in the island), ports **and** links.

- [ ] **Step 1: Write the failing tests** — baseline: island node connected to core via `core:ge-0/0/0 <-> idf:ge-0/0/0`; proposed: `idf:ge-0/0/0` set `disabled=True` → island isolated. Assert `causes_for_severance` names `idf:ge-0/0/0` (or `core:ge-0/0/0`). Also: a pre-existing island (no delta) → `()`; a **second unrelated severance elsewhere** does NOT appear in this island's causes; and a **link-only removal** (endpoint ports unchanged) → the `link` cause is named.

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** in `delta_cause.py`:

```python
def causes_for_severance(ctx, island) -> tuple[Cause, ...]:
    """Cause = delta-changed ports/links whose removal/disabled state dropped a
    physical L2 boundary edge of the island."""
    nodes = island.nodes if hasattr(island, "nodes") else island
    edges = _boundary_lost_edges(ctx.baseline.l2_graph(), ctx.proposed.l2_graph(), nodes)
    return _edge_causes(ctx.delta_index, edges)
```

(`_edge_causes` already contributes both `member_ports` and `link_ids` from each lost `L2Edge`.)

- [ ] **Step 4: Run + commit** — `git commit -m "feat(cause-attribution): causes_for_severance mapping (Family-2 rule 2)"`

---

### Task 8: Family-2 mapping — `causes_for_loop(ctx, cycle)` and `causes_for_root_move(ctx, component)`

**Files:** Modify `delta_cause.py`; Test `tests/analysis/test_delta_cause_loop_root.py`.

Rule 4 (loop): cause = the delta entity that armed the cycle — an added edge among `cycle.member_ports`' links, or a port in `cycle.member_ports` whose `stp_enabled` changed.
Rule 5 (root move): dual — a device in the component whose `stp_priority` changed → device cause; else a delta-changed link/port that altered connectivity → that link/port. Union; `()` if neither.

- [ ] **Step 1: Write the failing tests** —
  - (loop) a cycle member port whose `stp_enabled` flipped in the delta → that port is named.
  - (loop) **over-naming guard**: a cycle armed by an added link while a *different* cycle member has an unrelated `mtu`-only change → the mtu port is NOT named; the added link IS.
  - (root) a 2-device component where `dev_b.stp_priority` changed → `causes_for_root_move` returns device `dev_b`.
  - (root) topology-driven move: a boundary link/port change → that link/port is returned.

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** in `delta_cause.py`:

```python
_LOOP_PORT_FIELDS = frozenset({"stp_enabled", "stp_edge", "bpdu_filter"})


def causes_for_loop(ctx, cycle) -> tuple[Cause, ...]:
    """Cause = the delta entity that ARMED this cycle: a cycle port whose
    protection field flipped (stp_enabled/stp_edge/bpdu_filter) or that was newly
    added (empty fields), OR an added/removed link in the cycle. An unrelated mtu
    change on a cycle member is NOT loop-relevant and is filtered out."""
    di = ctx.delta_index
    out = []
    for p in sorted(cycle.member_ports):
        c = di.cause("port", p)
        if c is not None and (not c.fields or (_LOOP_PORT_FIELDS & set(c.fields))):
            out.append(c)
    out.extend(di.causes("link", cycle.link_ids))  # added/removed link arming the cycle
    return tuple(dict.fromkeys(out))


def causes_for_root_move(ctx, component_nodes) -> tuple[Cause, ...]:
    """Dual, restricted to THIS component: (a) a device in the component whose
    stp_priority changed; else (b) delta-changed ports/links on a boundary edge of
    the component (added OR removed connectivity)."""
    di = ctx.delta_index
    out = []
    # (a) priority change on a device in the component
    for did in sorted(component_nodes):
        c = di.cause("device", did)
        if c is not None and ("stp_priority" in c.fields or not c.fields):
            out.append(c)
    # (b) topology: boundary edges lost OR gained at the component (ports + links)
    lost = _boundary_lost_edges(ctx.baseline.l2_graph(), ctx.proposed.l2_graph(), component_nodes)
    gained = _boundary_lost_edges(ctx.proposed.l2_graph(), ctx.baseline.l2_graph(), component_nodes)
    out.extend(_edge_causes(di, [*lost, *gained]))
    return tuple(dict.fromkeys(out))
```

(`cycle.member_ports` and `cycle.link_ids` both exist — confirmed in `analysis/cycles.py:Cycle`. `component_nodes` is the proposed component node-set, e.g. `comp` in `stp_root.py`. `_boundary_lost_edges(prop, base, …)` with the graphs swapped yields the *gained* boundary edges — added connectivity. `_edge_causes` contributes ports and links.)

- [ ] **Step 4: Run + commit** — `git commit -m "feat(cause-attribution): causes_for_loop + causes_for_root_move (Family-2 rules 4/5)"`

---

## Phase 3 — Family-1 wiring (each check attributes its already-known changed entity)

### Task 9: Port/link-subject checks — `native_mismatch`, `mtu_mismatch`, `stp_edge`, `poe_disconnect`

**Files:** Modify the four check files; extend their existing tests.

Pattern: on each **non-`preexisting`** (conclusion) finding, set `caused_by` to the delta-changed port(s)/link the check already holds. Pre-existing INFO rows leave `caused_by=()` (the default).

- [ ] **Step 1: Write failing tests** — in each check's existing test module, add a case where the relevant port/link IS in the delta and assert the conclusion finding's `caused_by` names it; add a case asserting a `preexisting` finding has `caused_by=()`. Example for `native_mismatch` (subject is the link, cause is the changed endpoint port):

```python
def test_native_mismatch_names_changed_port():
    verdict_findings = run_native_check(base_ir, prop_ir)  # follow the module's existing harness
    f = next(f for f in verdict_findings if f.code.endswith(".mismatch") or f.severity is Severity.ERROR)
    assert any(c.ref.kind == "port" for c in f.caused_by)
```

- [ ] **Step 2: Run → FAIL** (`caused_by` empty).

- [ ] **Step 3: Implement** — in each `Finding(...)` for a conclusion row, add `caused_by=<causes>`:

`native_mismatch.py` / `mtu_mismatch.py` (subject `ObjectRef("link", lnk.id)`):
```python
                    caused_by=tuple(
                        c for c in (
                            ctx.delta_index.cause("port", lnk.a_port),
                            ctx.delta_index.cause("port", lnk.b_port),
                            ctx.delta_index.cause("link", lnk.id),
                        ) if c is not None
                    ) if severity is not Severity.INFO else (),
```

`stp_edge.py` (subject `ObjectRef("port", end.id)`):
```python
                        caused_by=tuple(
                            c for c in (ctx.delta_index.cause("port", end.id),) if c is not None
                        ) if severity is not Severity.INFO else (),
```

`poe_disconnect.py` (the port `pid` lost PoE; no preexisting branch — all rows are conclusions):
```python
                    caused_by=tuple(
                        c for c in (ctx.delta_index.cause("port", pid),) if c is not None
                    ),
```

- [ ] **Step 4: Run + commit**

Run: `uv run pytest tests/checks/ -q && uv run mypy src`
```bash
git commit -am "feat(cause-attribution): wire Family-1 port/link checks (native/mtu/stp_edge/poe)"
```

---

### Task 10: Device/scope-subject checks — `snooping`, `scope_lint`

**Files:** Modify `snooping.py`, `scope_lint.py`; extend their tests.

The subject IS the changed entity here. `snooping` subject `ObjectRef("device", did)`; `scope_lint` subject `ObjectRef("dhcp_scope", a.id)` / `("dhcp_scope", s.id)`.

- [ ] **Step 1: Write failing tests** — assert a non-INFO snooping finding names the changed device; a non-INFO scope_lint finding names the changed scope; pre-existing INFO rows carry `()`.

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** — add to the non-INFO finding construction:

`snooping.py`:
```python
                            caused_by=tuple(
                                c for c in (ctx.delta_index.cause("device", did),) if c is not None
                            ) if severity is not Severity.INFO else (),
```

`scope_lint.py` (overlap names both scopes of the pair; out_of_subnet names the one scope):
```python
                    caused_by=ctx.delta_index.causes("dhcp_scope", (a.id, b.id)) if severity is not Severity.INFO else (),
```
```python
                    caused_by=ctx.delta_index.causes("dhcp_scope", (s.id,)) if severity is not Severity.INFO else (),
```

- [ ] **Step 4: Run + commit** — `git commit -am "feat(cause-attribution): wire Family-1 device/scope checks (snooping/scope_lint)"`

---

### Task 11: Symptom-subject checks — `gateway_gap`, `dhcp_path`, `ospf_withdrawal`

**Files:** Modify the three checks; extend tests.

Subject is a vlan/device (symptom), so the cause is a *different* entity the check identified: the removed l3intf / removed DHCP provider / withdrawn ospf_intf.

**Confirmed fields** (`src/digital_twin/ir/entities.py`): `DhcpScope` has `.vlan` (not `vlan_id`) and `.id` = `provider:network`; `L3Intf` has `.ip`, `.vlan_id`, `.id`; `OspfIntf` has `.device_id`, `.vlan_id`; `Vlan` has `.gateway`.

- [ ] **Step 1: Write failing tests**
  - `gateway_gap.removed`: removed l3intf in the delta → named.
  - `gateway_gap.gateway_unowned` (GS22-GW): the owner moved — assert `caused_by` names the changed `vlan` (gateway changed) and/or the owner `l3intf` whose `ip` changed.
  - `dhcp_path.removed`: the serving scope **removed** in proposed → named from BASELINE scopes; also assert the vlan is named when only `dhcp_sources` changed.
  - `ospf_withdrawal`: the changed `ospf_intf` for that device/vlan is named.
  - Pre-existing rows → `()`.

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**

`gateway_gap.py` — the `.removed` (ERROR) row: `base_intfs` holds the removed L3 interfaces:
```python
                    caused_by=ctx.delta_index.causes("l3intf", [i.id for i in base_intfs])
                        if code == "removed" else (),
```
The `.gateway_unowned` (ERROR) row (`owners` broke — `vlan.gateway` moved or the owner intf's `ip` changed): name the vlan (gateway change) AND the owner/intf l3intfs in the delta:
```python
                    caused_by=tuple(dict.fromkeys((
                        *( (c,) if (c := ctx.delta_index.cause("vlan", str(vid))) else () ),
                        *ctx.delta_index.causes("l3intf", [i.id for i in (*owners, *intfs)]),
                    ))) if code == "gateway_unowned" and severity is Severity.ERROR else (),
```

`dhcp_path.py` — removed providers are `base_vlan.dhcp_sources` (device id, or `"site"`). Attribute device-id sources, the serving `dhcp_scope`s for this vlan from **both** baseline and proposed (the scope often disappears), and the vlan itself (its `dhcp_sources` changed):
```python
                    caused_by=tuple(dict.fromkeys((
                        *ctx.delta_index.causes("device", [s for s in base_vlan.dhcp_sources if s != "site"]),
                        *ctx.delta_index.causes("dhcp_scope",
                            [sc.id for sc in (*base_ir.dhcp_scopes, *prop_ir.dhcp_scopes) if sc.vlan == vid]),
                        *( (c,) if (c := ctx.delta_index.cause("vlan", str(vid))) else () ),
                    ))),
```
(If nothing matches the delta, `causes(...)` yields `()` and the walrus guards drop to nothing → honesty fallback.)

`ospf_withdrawal.py` — gates on `diff.touches("ospf_intf")`; attribute the changed `ospf_intf`(s) for the row. Device rows (`egress_lost`, subject device `did`) match `oi.device_id == did`; vlan rows (`advertised_removed`/`transit_mutation`, subject vlan `vid`) match `oi.vlan_id == vid`:
```python
                    # device row:
                    caused_by=ctx.delta_index.causes("ospf_intf",
                        [oi.id for oi in (*base_ir.ospf_intfs, *prop_ir.ospf_intfs) if oi.device_id == did]),
                    # vlan row:
                    caused_by=ctx.delta_index.causes("ospf_intf",
                        [oi.id for oi in (*base_ir.ospf_intfs, *prop_ir.ospf_intfs) if oi.vlan_id == vid]),
```
(`OspfIntf.id` is auto-derived; confirm it is in the diff under kind `"ospf_intf"`. The check already gates on that kind, so a withdrawal implies a delta `ospf_intf`.)

- [ ] **Step 4: Run + commit** — `git commit -am "feat(cause-attribution): wire Family-1 symptom-subject checks (gateway_gap/dhcp_path/ospf)"`

---

## Phase 4 — Family-2 wiring + client_impact + goldens

### Task 12: `vlan_segmentation` (split) + `blackhole.exit_lost` wiring

**Files:** Modify `l2_vlan_segmentation.py`, `l2_blackhole.py`; extend tests.

- [ ] **Step 1: Write failing tests** — segmentation split on vlan 7 caused by a changed trunk port → `caused_by` names it; blackhole `exit_lost` likewise; `preexisting` → `()`.

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**

`l2_vlan_segmentation.py` — the check emits ONE aggregate `.split` finding per vlan from node-sets (no per-fragment `component` in scope), so use the dedicated split mapping which finds the separating edges itself. Thread `caused_by` through `_finding` (add a `caused_by: tuple[Cause, ...] = ()` param; set it on the `Finding(...)`), and on the split branch:
```python
from digital_twin.analysis.delta_cause import causes_for_vlan_split
...
                        caused_by=causes_for_vlan_split(ctx, vid),   # split branch only; reshape stays ()
```

`l2_blackhole.py` — the `_finding(...)` helper builds the Finding; add a `caused_by` param and set it. For the `exit_lost` path pass `causes_for_vlan_cut(ctx, vid, comp)` (the stranded proposed component `comp` is in scope); for `preexisting` pass `()`.

- [ ] **Step 4: Run + commit** — `git commit -am "feat(cause-attribution): wire vlan_segmentation + blackhole.exit_lost"`

---

### Task 13: `blackhole.new_member_stranded` (direct) + `l2_isolation` (severance)

**Files:** Modify `l2_blackhole.py`, `l2_isolation.py`; extend tests.

- [ ] **Step 1: Write failing tests** — a new access port stranded on an isolated node → `caused_by` names that port (direct `new_member_ports ∩ delta`); a wireless/WLAN-only new member with no L2 delta on its path → `caused_by=()`; isolation island cut by a disabled port → names that port.

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**

`l2_blackhole.py` (`new_member_stranded` branch): direct attribution from the `new_ports` the check computed, plus the AP-path fallback for wireless/WLAN:
```python
                code == "wired.l2.blackhole.new_member_stranded"
                caused_by = (
                    ctx.delta_index.causes("port", new_ports)            # access ports: direct
                    or causes_for_severance(ctx, comp)                   # else AP-path L2 delta, if any
                )
```
(If neither yields a cause, `causes(...)` returns `()` and `causes_for_severance` returns `()` → honesty fallback satisfied.)

`l2_isolation.py` — on the isolation finding, `caused_by=causes_for_severance(ctx, island)` where `island` is the isolated component the check already computed; pre-existing island → `()`.

- [ ] **Step 4: Run + commit** — `git commit -am "feat(cause-attribution): wire blackhole.new_member_stranded + l2_isolation"`

---

### Task 14: `l2_loop` + `stp_root` wiring

**Files:** Modify `l2_loop.py`, `stp_root.py`; extend tests.

- [ ] **Step 1: Write failing tests** — loop armed by a delta port → named; root move by `stp_priority` change → device named; root move by topology → link/port named; no-delta cause → `()`.

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**

`l2_loop.py` (on the cycle finding): `caused_by=causes_for_loop(ctx, cycle)`.
`stp_root.py` (on the `.moved` finding): `caused_by=causes_for_root_move(ctx, comp)` (`comp` is the proposed component node-set).

- [ ] **Step 4: Run + commit** — `git commit -am "feat(cause-attribution): wire l2_loop + stp_root"`

---

### Task 15: `client_impact` — per-impact causes + top-level union

**Files:** Modify `client_impact.py`; extend `tests/checks/...client_impact` tests.

The impact KIND determines the cause (`_impact_of` already computes it):
- **`disconnect` / `vlan_move`** — the client's own access port changed → cause = `ctx.delta_index.cause("port", client.attach_id)`.
- **`blackhole`** — the client is stranded because an **upstream** edge changed, NOT its access port → cause = `causes_for_vlan_cut(ctx, vlan, comp)` on the client's stranded proposed component `comp` (in scope at the blackhole return). This is the case the feature most needs — attributing only the access port would return empty here.

- [ ] **Step 1: Write failing tests** —
  1. two clients whose **own access ports** changed (vlan_move) → each `evidence["impacts"][i]["caused_by"]` names its own port; top-level `Finding.caused_by` = deduped union.
  2. a client blackholed by an **upstream trunk** change (access port unchanged) → its nested `caused_by` names the upstream trunk port (via `causes_for_vlan_cut`), NOT empty.

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** — add a `caused_by` param to `_entry` and compute it per kind in `_impact_of`:

```python
    def _entry(self, client, impact, detail, caused_by=()):
        return {
            "mac": client.mac, "vlan": client.vlan, "attachment": client.attach_id,
            "impact": impact, "detail": detail, "caused_by": caused_by,
        }
```

In `_impact_of`, at each return:
```python
        # disconnect / vlan_move (access-port cause):
            return self._entry(client, "disconnect", "attach port removed",
                caused_by=ctx.delta_index.causes("port", [client.attach_id]))
        # ... vlan_move similarly with the same causes(...) call ...
        # blackhole (upstream cut — reuse the component mapping):
                                return self._entry(client, "blackhole",
                                    f"vlan {vlan} segment loses its exit",
                                    caused_by=causes_for_vlan_cut(ctx, vlan, comp))
```
Add `from digital_twin.analysis.delta_cause import causes_for_vlan_cut` to the imports.

Then in `run`, build the top-level union and pass it to the aggregate `Finding`:
```python
        union = tuple(dict.fromkeys(c for i in impacts for c in i.get("caused_by", ())))
        findings = (Finding(..., evidence={"impacts": impacts}, caused_by=union),)
```
(The nested `caused_by` lists are named by `name_findings` — Task 2.)

- [ ] **Step 4: Run + commit** — `git commit -am "feat(cause-attribution): wire client_impact per-impact + union causes"`

---

### Task 16: Goldens — motivating scenario + non-load-bearing invariant

**Files:** Modify `tests/golden/test_golden_scenarios.py` + `tests/golden/builders.py` (follow the existing golden harness).

- [ ] **Step 1: Write the motivating golden (failing first if the scenario is new)** — a device plan setting two trunk ports (`mge-0/0/0`, `mge-0/0/1`) to a usage that drops vlans 7/8/10/20. Assert:
  - each `vlan_segmentation.split` / `blackhole.exit_lost` finding has `caused_by` naming one of the two ports;
  - any `blackhole.exit_unlocatable` (pre-existing) finding has `caused_by == ()`.

- [ ] **Step 2: Write the non-load-bearing golden** — run the SAME plan and assert `verdict.decision`, each finding's `severity`, and `coverage` are identical whether or not attribution is populated (compare against the pre-feature expected values pinned in the golden). This pins the spec's "never load-bearing" invariant.

- [ ] **Step 3: Run** — `uv run pytest tests/golden/ -q`. Expected: PASS.

- [ ] **Step 4: Commit** — `git commit -am "test(cause-attribution): motivating + non-load-bearing goldens"`

---

## Phase 5 — Adapter findings + docs + live verify

### Task 17: Adapter / dynamic-gate findings with delta/parity guard

**Files:** Modify `src/digital_twin/adapters/mist/ingest/switch.py` (`invalid_bridge_priority_findings`), `src/digital_twin/adapters/mist/ingest/dynamic_usage.py` (the `unresolved_dynamic_findings` + dhcp range finding builders). Extend their tests.

Rule: attribute a cause ONLY when the offending value CHANGED baseline→proposed; an unchanged malformed baseline → `caused_by=()`.

- [ ] **Step 1: Write failing tests** — `invalid_bridge_priority_findings`: a device whose `stp_config.bridge_priority` is malformed in BOTH baseline and proposed (unchanged) → finding `caused_by == ()`; malformed only in proposed (changed) → `caused_by` names the device. Analogous parity tests for the dynamic-ports and dhcp-range findings.

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** — these builders already receive both effective maps. For `invalid_bridge_priority_findings`, compute the per-side raw value (already in `sides`); set:
```python
                changed = sides["baseline"] != sides["proposed"]
                caused_by = (Cause(ref=ObjectRef("device", did)),) if changed else ()
```
and pass `caused_by=caused_by` to the `Finding(...)`. Do the analogous parity (changed-vs-unchanged on the relevant value) for the dynamic-ports and dhcp-range builders; when the triggering value is unchanged between sides, `caused_by=()`. (Device/dhcp_scope refs have `name=None`; these findings bypass `name_findings`, which is correct since those kinds have no IR name.)

- [ ] **Step 4: Run + commit** — `git commit -am "feat(cause-attribution): adapter findings attribute with baseline/proposed parity"`

---

### Task 18: Docs + roadmap + live verify + memory

**Files:** `docs/ROADMAP.md`, the spec (mark Implemented), the project memory.

- [ ] **Step 1: Full gate** — `uv run pytest tests -q && uv run ruff check . && uv run mypy src`. Must be green.

- [ ] **Step 2: Live verify (read-only)** — with `.env` loaded, run the existing 8 single-site plans; assert verdicts UNCHANGED vs the documented baseline (plan.json UNSAFE; 01/02/06 SAFE; 03/04/07 REVIEW; 05 UNSAFE), and confirm a multi-target plan now shows port-level `(caused by …)` clauses in `render_human`. `.env` MUST NOT be committed; runs are simulate-only.

- [ ] **Step 3: Update docs** — flip the spec status to Implemented; in `docs/ROADMAP.md`, mark the cause-attribution work done and note the deferred follow-ons (cause-first rendering; per-leaf differential; raw-path `fields`; the richer impacted-client reporting cluster builds on the per-impact causes).

- [ ] **Step 4: Update memory** — add the as-built note to `~/.claude/projects/-Users-tmunzer-4-dev-digital-twin/memory/digital-twin-project.md` (the `Cause`/`caused_by` contract; `DeltaIndex` on `CheckContext`; the five Family-2 mapping functions in `analysis/delta_cause.py`; the adapter parity rule; non-load-bearing invariant).

- [ ] **Step 5: Commit** — `git commit -am "docs(cause-attribution): roadmap + spec Implemented + live-verified"`

---

## Self-review checklist (run before execution)

- **Spec coverage:** Cause contract (T1) ✓; name resolution incl. nested (T2) ✓; render human+dict (T3) ✓; DeltaIndex (T4) + CheckContext (T5) ✓; five Family-2 mappings (T6–T8) ✓; Family-1 all 9 checks (T9–T11) ✓; Family-2 wiring incl. client_impact (T12–T15) ✓; goldens incl. non-load-bearing (T16) ✓; adapter parity + L0 exclusion (T17 — L0 untouched by construction) ✓; docs/live/memory (T18) ✓.
- **Honesty rule:** every mapping returns `()` when no delta entity matches (tested in T6–T8).
- **Component-local + boundary attribution (review P1, rounds 1–2):** the Family-2 mappings restrict to the finding's own component/island/cycle via `_boundary_lost_edges(..., nodes)` (exactly one endpoint inside — internal edge losses are never blamed), NEVER the whole-graph delta. Segmentation `.split` has no `component`, so it uses `causes_for_vlan_split` (separating-edge geometry). Pinned by the two-cut + internal-edge tests (T6) and the analogous T7 case.
- **Ports AND links (review P2):** `_edge_causes` maps each lost `L2Edge` to both `member_ports` and `link_ids`, so a link added/removed with unchanged endpoint ports is still attributed (T6/T7/T8). Loop also adds `cycle.link_ids`.
- **Loop over-naming guard (review P2):** `causes_for_loop` filters cycle-member ports to loop-relevant changed fields (`stp_enabled`/`stp_edge`/`bpdu_filter`, or structural add) — an unrelated `mtu` change on a cycle member is not blamed (pinned in T8).
- **Non-load-bearing:** `caused_by` is never read by `decide()`/coverage; pinned by T16.
- **Type consistency:** `DeltaIndex.cause(kind, oid) -> Cause | None`, `.causes(kind, iterable) -> tuple[Cause, ...]`, `causes_for_*` all return `tuple[Cause, ...]` — used consistently in T9–T15.
- **Confirmed payloads (no longer guesses):** graph edges store an `L2Edge` under `data["data"]` with `member_ports`/`link_ids` (T6/T7/T8); `Cycle.link_ids` exists (T8); `DhcpScope.vlan` / `L3Intf.ip` / `OspfIntf.{device_id,vlan_id}` / `Vlan.gateway` (T11).
- **Remaining grep-confirm flag:** `IR()` constructibility for the minimal T5 test (else use the smallest `tests/factories.py` IR factory).
