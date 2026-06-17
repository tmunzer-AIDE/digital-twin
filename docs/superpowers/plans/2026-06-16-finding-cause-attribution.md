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

The rule (spec rule 1): cause = delta-changed ports/links incident to the affected component whose change removed `vid`'s carriage — edges that carried `vid` in the **baseline** vlan-graph and no longer do, mapped to their backing port(s), kept only if in the delta. If none, `()`.

- [ ] **Step 1: Write the failing test** — build a baseline IR where switch A↔B trunk carries vlan 7, and a proposed IR where the trunk port `A:ge-0/0/1` changed (vlan 7 no longer carried), partitioning vlan 7. Assert `causes_for_vlan_cut` returns the changed port. Use the existing graph/IR factories in `tests/factories.py` (follow the pattern used by `tests/analysis/test_vlan_reachability*.py` — grep for an existing two-switch-trunk fixture and reuse it). Also assert the ambiguous case (a partition with no delta-incident edge) returns `()`.

```python
# tests/analysis/test_delta_cause_vlan_cut.py  (sketch — fill IR from factories)
from digital_twin.analysis.context import AnalysisContext
from digital_twin.analysis.delta_cause import causes_for_vlan_cut, delta_index
from digital_twin.checks.base import CheckContext
from digital_twin.ir.diff import diff_ir
# build baseline_ir / proposed_ir via factories so vlan 7 loses carriage on A:ge-0/0/1


def _ctx(base_ir, prop_ir):
    diff = diff_ir(base_ir, prop_ir)
    return CheckContext(AnalysisContext(base_ir), AnalysisContext(prop_ir), diff, delta_index(diff))


def test_vlan_cut_names_the_changed_trunk_port():
    ctx = _ctx(base_ir, prop_ir)
    stranded = [c for c in ctx.proposed.vlan_components(7) if not c.reaches_exit][0]
    causes = causes_for_vlan_cut(ctx, 7, stranded)
    assert any(c.ref.id == "A:ge-0/0/1" for c in causes)


def test_vlan_cut_ambiguous_returns_empty():
    # a partition whose removed edge is NOT in the delta -> no guess
    ctx = _ctx(base_ir2, prop_ir2)
    stranded = [c for c in ctx.proposed.vlan_components(7) if not c.reaches_exit][0]
    assert causes_for_vlan_cut(ctx, 7, stranded) == ()
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/analysis/test_delta_cause_vlan_cut.py -q` → FAIL (function missing).

- [ ] **Step 3: Implement** in `delta_cause.py`:

```python
from digital_twin.ir.indexes import vc_root_map  # if needed for node mapping

def _vlan_edge_ports(vlan_graph, ir) -> set[str]:
    """Port ids backing the edges of a vlan graph (L2 edges are port-derived;
    the edge 'data' / endpoint ports identify the backing ports)."""
    ports: set[str] = set()
    for _a, _b, data in vlan_graph.edges(data=True):
        link = data.get("link")
        if link is not None:
            ports.add(link.a_port)
            ports.add(link.b_port)
    return ports


def causes_for_vlan_cut(ctx, vid: int, component) -> tuple[Cause, ...]:
    base_ports = _vlan_edge_ports(ctx.baseline.vlan_graph(vid), ctx.baseline.ir)
    prop_ports = _vlan_edge_ports(ctx.proposed.vlan_graph(vid), ctx.proposed.ir)
    lost = base_ports - prop_ports  # carriage-removed backing ports
    di = ctx.delta_index
    causes = [di.cause("port", p) for p in sorted(lost)]
    # also any link removed for this vlan that is itself in the delta
    return tuple(c for c in causes if c is not None)
```

NOTE: confirm the vlan-graph edge attribute name carrying the `Link` (grep `build_vlan_graph` in `src/digital_twin/representations/vlan_graph.py` for the edge `data=` key; adjust `_vlan_edge_ports` to the real key — it may be `"data"` holding the link or the link object directly). Pin it in the test.

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

- [ ] **Step 1: Write the failing test** — baseline: island node connected to core via link `core:ge-0/0/0 <-> idf:ge-0/0/0`; proposed: `idf:ge-0/0/0` set `disabled=True`, island now isolated. Assert `causes_for_severance` names port `idf:ge-0/0/0` (or `core:ge-0/0/0`). Also: a pre-existing island (no delta) → `()`.

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** in `delta_cause.py`:

```python
def _l2_edge_ports(l2_graph) -> set[str]:
    ports: set[str] = set()
    for _a, _b, data in l2_graph.edges(data=True):
        link = data.get("link")
        if link is not None:
            ports.add(link.a_port)
            ports.add(link.b_port)
    return ports


def causes_for_severance(ctx, island) -> tuple[Cause, ...]:
    """island: the VlanComponent / node-set newly isolated. Cause = delta-changed
    ports/links whose removal/disabled state dropped a physical L2 edge touching
    the island's nodes."""
    base_ports = _l2_edge_ports(ctx.baseline.l2_graph())
    prop_ports = _l2_edge_ports(ctx.proposed.l2_graph())
    lost = base_ports - prop_ports
    di = ctx.delta_index
    out = [di.cause("port", p) for p in sorted(lost)]
    # also removed links present in the delta
    for r in ctx.diff.removed:
        if r.kind == "link":
            out.append(di.cause("link", r.id))
    return tuple(dict.fromkeys(c for c in out if c is not None))  # dedup, preserve order
```

(Confirm the l2_graph edge `data` key holds the `Link` — same grep as Task 6. The `island` arg lets a future refinement restrict to boundary edges; v1 may use the whole lost-edge set, which is acceptable per the honesty rule since all are real delta changes.)

- [ ] **Step 4: Run + commit** — `git commit -m "feat(cause-attribution): causes_for_severance mapping (Family-2 rule 2)"`

---

### Task 8: Family-2 mapping — `causes_for_loop(ctx, cycle)` and `causes_for_root_move(ctx, component)`

**Files:** Modify `delta_cause.py`; Test `tests/analysis/test_delta_cause_loop_root.py`.

Rule 4 (loop): cause = the delta entity that armed the cycle — an added edge among `cycle.member_ports`' links, or a port in `cycle.member_ports` whose `stp_enabled` changed.
Rule 5 (root move): dual — a device in the component whose `stp_priority` changed → device cause; else a delta-changed link/port that altered connectivity → that link/port. Union; `()` if neither.

- [ ] **Step 1: Write the failing tests** — (loop) build a cycle where one member port's `stp_enabled` flipped True→? in the delta; assert that port is named. (root) build a 2-device component where `dev_b.stp_priority` changed so the elected root moves; assert `causes_for_root_move` returns device `dev_b`. Add a topology-driven root-move case asserting the changed link/port is returned.

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** in `delta_cause.py`:

```python
def causes_for_loop(ctx, cycle) -> tuple[Cause, ...]:
    di = ctx.delta_index
    out = [di.cause("port", p) for p in sorted(cycle.member_ports)]
    return tuple(c for c in out if c is not None)


def causes_for_root_move(ctx, component_nodes) -> tuple[Cause, ...]:
    di = ctx.delta_index
    out = []
    # (a) priority change on a device in the component
    for did in sorted(component_nodes):
        c = di.cause("device", did)
        if c is not None and ("stp_priority" in c.fields or not c.fields):
            out.append(c)
    # (b) topology change: ports of the component's devices that changed
    for p in sorted(_l2_edge_ports(ctx.baseline.l2_graph()) ^ _l2_edge_ports(ctx.proposed.l2_graph())):
        c = di.cause("port", p)
        if c is not None:
            out.append(c)
    return tuple(dict.fromkeys(out))
```

(`cycle.member_ports` exists — confirmed in `l2_loop.py`. `component_nodes` is the device-id node set the check already has, e.g. `comp` in `stp_root.py`.)

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

- [ ] **Step 1: Write failing tests** — `gateway_gap.removed`: assert `caused_by` names the removed l3intf. `dhcp_path.removed`: assert it names the changed provider entity (device or site dhcp_scope). `ospf_withdrawal`: assert it names the changed `ospf_intf`. Pre-existing rows → `()`.

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**

`gateway_gap.py` — for the `removed` (ERROR) row, `base_intfs` holds the removed L3 interfaces (they are in `diff.removed`):
```python
                    caused_by=ctx.delta_index.causes("l3intf", tuple(i.id for i in base_intfs))
                        if code == "removed" else (),
```

`dhcp_path.py` — the removed providers are `base_vlan.dhcp_sources` (strings: a gateway device id, or `"site"`). Attribute device-id sources to devices, and a changed site `dhcp_scope` serving this vlan:
```python
                    caused_by=tuple(
                        c for c in (
                            *(ctx.delta_index.cause("device", s) for s in base_vlan.dhcp_sources if s != "site"),
                            *(ctx.delta_index.cause("dhcp_scope", sc.id)
                              for sc in prop_ir.dhcp_scopes if getattr(sc, "vlan_id", None) == vid),
                        ) if c is not None
                    ),
```
(Confirm `DhcpScope` has a `vlan_id`/`vlan` attribute via `grep -n "class DhcpScope" -A12 src/digital_twin/ir/entities.py`; use the real attribute name. If a source is neither a device nor a delta scope, it drops to `()` per the honesty rule.)

`ospf_withdrawal.py` — the check gates on `diff.touches("ospf_intf")`; for `egress_lost`/`advertised_removed`, attribute the changed ospf_intf(s) for that device/vlan. Add (using the device id `did` / vlan `vid` the row already has):
```python
                    caused_by=tuple(
                        c for c in (
                            ctx.delta_index.cause("ospf_intf", oi.id)
                            for oi in (*base_ir.ospf_intfs, *prop_ir.ospf_intfs)
                            if oi.device_id == did  # for vlan rows: oi.vlan_id == vid
                        ) if c is not None
                    ),
```
(Adjust the predicate to the row: device rows match `oi.device_id == did`; vlan rows match `oi.vlan_id == vid`. Confirm `OspfIntf` field names via grep.)

- [ ] **Step 4: Run + commit** — `git commit -am "feat(cause-attribution): wire Family-1 symptom-subject checks (gateway_gap/dhcp_path/ospf)"`

---

## Phase 4 — Family-2 wiring + client_impact + goldens

### Task 12: `vlan_segmentation` + `blackhole.exit_lost` wiring (causes_for_vlan_cut)

**Files:** Modify `l2_vlan_segmentation.py`, `l2_blackhole.py`; extend tests.

- [ ] **Step 1: Write failing tests** — segmentation split on vlan 7 caused by a changed trunk port → `caused_by` names it; blackhole `exit_lost` likewise; `preexisting` → `()`.

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** — in `l2_vlan_segmentation.py`, on the split finding (subject `ObjectRef("vlan", str(vid))`), pass the stranded component and vid:
```python
from digital_twin.analysis.delta_cause import causes_for_vlan_cut
...
                    caused_by=causes_for_vlan_cut(ctx, vid, component),
```
In `l2_blackhole.py`, the `_finding(...)` helper builds the Finding; thread `caused_by` through it. For the `exit_lost` code path pass `causes_for_vlan_cut(ctx, vid, comp)`; for `preexisting` pass `()`. Add a `caused_by: tuple[Cause, ...] = ()` parameter to `_finding` and set `caused_by=caused_by` on its `Finding(...)`.

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

- [ ] **Step 1: Write failing test** — two clients on different changed ports → each `evidence["impacts"][i]["caused_by"]` names its own port; top-level `Finding.caused_by` is the deduped union of both.

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** — in `_impact_of`, attach the changed entity on that client's attachment path. For a PORT-attached client (`client.attach_id` is a port id), the cause is `ctx.delta_index.cause("port", client.attach_id)`. Store it in the impact dict:
```python
        impact["caused_by"] = tuple(
            c for c in (ctx.delta_index.cause("port", client.attach_id),) if c is not None
        )
```
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
- **Non-load-bearing:** `caused_by` is never read by `decide()`/coverage; pinned by T16.
- **Type consistency:** `DeltaIndex.cause(kind, oid) -> Cause | None`, `.causes(kind, iterable) -> tuple[Cause, ...]`, `causes_for_*` all return `tuple[Cause, ...]` — used consistently in T9–T15.
- **Grep-confirm-before-coding flags** (call out in the relevant task, do not guess): the vlan-graph / l2-graph edge `data` key holding the `Link` (T6/T7); `DhcpScope` vlan attribute name (T11); `OspfIntf` field names (T11); `IR()` constructibility for the minimal test (T5).
