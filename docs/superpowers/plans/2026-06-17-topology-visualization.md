# Topology Visualization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Emit mermaid charts (L2, per-VLAN, Routed VLAN exits) on every normal check/verdict path, with the blast radius severity-highlighted and `caused_by` in captions, exposed as `Verdict.diagrams` + a markdown helper for the elicitation UI. (All `_unknown(...)` short-circuits keep `diagrams=()`.)

**Architecture:** A new pure `viz/` package consumes the existing graph builders (`build_l2_graph`/`build_vlan_graph`) + a `Highlight` index built from findings. The pipeline calls `safe_build_diagrams(proposed.ir, verdict.findings)` after `assemble()` and attaches the result to the `Verdict`. `Diagram` is a dumb DTO in `contracts/`; mermaid styling lives only in `viz/mermaid.py`.

**Tech Stack:** Python 3.14, networkx, pytest/ruff/mypy. Spec: `docs/superpowers/specs/2026-06-17-topology-visualization-design.md`.

**Run all tests/lint from the repo root with:** `.venv/bin/python -m pytest`, `.venv/bin/ruff check .`, `.venv/bin/mypy`.

---

## File structure

| File | Responsibility |
|---|---|
| `src/digital_twin/contracts/diagram.py` (create) | `Diagram` DTO (view/title/severity/mermaid/notes) |
| `src/digital_twin/contracts/__init__.py` (modify) | export `Diagram` |
| `src/digital_twin/ir/entities.py` (modify) | add `Device.name` |
| `src/digital_twin/adapters/mist/ingest/switch.py` (modify) | populate `Device.name` |
| `src/digital_twin/ir/diff.py` (modify) | per-kind ignore: `device → name` |
| `src/digital_twin/checks/subjects.py` (modify) | `_name_for("device")` returns `Device.name` |
| `src/digital_twin/viz/__init__.py` (create) | package marker |
| `src/digital_twin/viz/highlight.py` (create) | `build_highlight(findings, ir) -> Highlight` |
| `src/digital_twin/viz/mermaid.py` (create) | `build_diagrams` / `safe_build_diagrams` + chart builders + styling |
| `src/digital_twin/viz/markdown.py` (create) | `to_markdown(diagrams)` |
| `src/digital_twin/verdict/verdict.py` (modify) | add `Verdict.diagrams` |
| `src/digital_twin/engine/pipeline.py` (modify) | attach diagrams post-`assemble` |
| `src/digital_twin/drivers/render.py` (modify) | `render_diagrams_markdown` + titles in `render_human` |

---

## Task 1: `Diagram` DTO

**Files:**
- Create: `src/digital_twin/contracts/diagram.py`
- Modify: `src/digital_twin/contracts/__init__.py`
- Test: `tests/contracts/test_diagram.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/contracts/test_diagram.py
import dataclasses

import pytest

from digital_twin.contracts import Diagram, Severity


def test_diagram_constructs_with_defaults():
    d = Diagram(view="l2", title="L2 topology", severity=None, mermaid="graph LR")
    assert d.view == "l2" and d.notes == ()


def test_diagram_is_frozen():
    d = Diagram(view="l2", title="t", severity=Severity.ERROR, mermaid="graph LR")
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.view = "x"  # type: ignore[misc]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/contracts/test_diagram.py -q`
Expected: FAIL — `ImportError: cannot import name 'Diagram'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/digital_twin/contracts/diagram.py
"""Diagram: a dumb DTO for one rendered topology chart (mermaid source).

Pure value type — NO mermaid styling lives here (that is viz/mermaid.py), so
verdict/ can hold a Diagram without importing the renderer.
"""

from __future__ import annotations

from dataclasses import dataclass

from .finding import Severity


@dataclass(frozen=True)
class Diagram:
    view: str  # "l2" | "vlan:<id>" | "l3_exits"
    title: str
    severity: Severity | None  # worst severity highlighted here (ordering); None = nothing
    mermaid: str
    notes: tuple[str, ...] = ()  # captions: cause lines, "N findings not localized"
```

Then in `src/digital_twin/contracts/__init__.py` add the import and `__all__` entry:

```python
from .diagram import Diagram
from .finding import Cause, Finding, FindingCategory, FindingSource, ObjectRef, Severity
```

(Add `"Diagram",` to `__all__`. Keep the existing `from .finding import ...` line — only ADD the `from .diagram import Diagram` line above it.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/contracts/test_diagram.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/contracts/diagram.py src/digital_twin/contracts/__init__.py tests/contracts/test_diagram.py
git commit -m "feat(viz): Diagram DTO in contracts"
```

---

## Task 2: `Device.name` IR field + ingest

**Files:**
- Modify: `src/digital_twin/ir/entities.py` (the `Device` dataclass)
- Modify: `src/digital_twin/adapters/mist/ingest/switch.py:372` (the `Device(...)` call)
- Test: `tests/adapters/mist/test_ingest_switch.py`

- [ ] **Step 1: Write the failing test** (append to the test file)

```python
def test_device_name_populated_from_raw():
    # device display name flows from the raw device into the IR
    from digital_twin.adapters.mist.ingest.base import IngestContext
    from digital_twin.ir import IRBuilder

    ctx = IngestContext(
        raw=raw_site(devices=({**SWITCH_A, "name": "core-sw-1"},)),
        site_effective=dict(SITE_EFFECTIVE),
        device_effective={"aa0000000001": {**SITE_EFFECTIVE, **SWITCH_A}},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    assert ctx.builder.build().device("aa0000000001").name == "core-sw-1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/adapters/mist/test_ingest_switch.py::test_device_name_populated_from_raw -q`
Expected: FAIL — `TypeError: Device.__init__() got an unexpected keyword argument 'name'` (after Step 3a) or `AttributeError`/None before.

- [ ] **Step 3: Write minimal implementation**

In `src/digital_twin/ir/entities.py`, add a `name` field to `Device` (right after `model`):

```python
    model: str | None = None
    name: str | None = None  # display name (from raw device `name`); DIFF-IGNORED (see ir/diff.py)
```

In `src/digital_twin/adapters/mist/ingest/switch.py`, the `Device(...)` call (~line 372) — add `name=dev.get("name")`:

```python
                Device(
                    id=did,
                    role=role,
                    site=ctx.raw.scope.site_id,
                    model=dev.get("model"),
                    name=dev.get("name"),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/adapters/mist/test_ingest_switch.py -q`
Expected: PASS (whole file — confirms no other ingest test broke).

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/ir/entities.py src/digital_twin/adapters/mist/ingest/switch.py tests/adapters/mist/test_ingest_switch.py
git commit -m "feat(viz): Device.name in the IR, populated at ingest"
```

---

## Task 3: device-only `name` diff ignore

**Files:**
- Modify: `src/digital_twin/ir/diff.py`
- Test: `tests/ir/test_diff.py`

- [ ] **Step 1: Write the failing test** (append)

```python
def test_device_name_change_is_not_a_diff():
    from digital_twin.ir import IRBuilder
    from digital_twin.ir.entities import Device, DeviceRole

    base = IRBuilder().add_device(
        Device(id="d1", role=DeviceRole.SWITCH, site="s1", name="old")
    ).build()
    prop = IRBuilder().add_device(
        Device(id="d1", role=DeviceRole.SWITCH, site="s1", name="new")
    ).build()
    assert diff_ir(base, prop).is_empty()  # rename is display-only, not a config change


def test_vlan_name_change_is_still_a_diff():
    # regression guard: name ignore is DEVICE-ONLY (Vlan.name stays a real field)
    from digital_twin.ir import IRBuilder
    from digital_twin.ir.entities import Vlan

    base = IRBuilder().add_vlan(Vlan(vlan_id=30, name="old")).build()
    prop = IRBuilder().add_vlan(Vlan(vlan_id=30, name="new")).build()
    mods = {(m.ref.kind, m.ref.id): m.changed_fields for m in diff_ir(base, prop).modified}
    assert "name" in mods[("vlan", "30")]
```

(`diff_ir` is already imported at the top of `tests/ir/test_diff.py`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/ir/test_diff.py::test_device_name_change_is_not_a_diff -q`
Expected: FAIL — the device rename currently registers as a `("device","d1")` modification.

- [ ] **Step 3: Write minimal implementation**

In `src/digital_twin/ir/diff.py`, add a per-kind ignore map and thread the kind into `_changed_fields`:

```python
_IGNORED_FIELDS = {"meta", "stp_meta"}
# Per-kind display-only fields: never a config change. Device.name is a label
# (Vlan.name/Port.name are key-derived identity and STAY compared).
_IGNORED_BY_KIND: dict[str, frozenset[str]] = {"device": frozenset({"name"})}
```

```python
def _changed_fields(kind: str, a: Any, b: Any) -> tuple[str, ...]:
    ignored = _IGNORED_FIELDS | _IGNORED_BY_KIND.get(kind, frozenset())
    changed = [
        f.name
        for f in fields(a)
        if f.name not in ignored and getattr(a, f.name) != getattr(b, f.name)
    ]
    return tuple(sorted(changed))  # field-order independent -> stable fixtures
```

In `diff_ir`, pass the kind (the first element of `key`):

```python
    for key in sorted(base.keys() & prop.keys()):
        changed = _changed_fields(key[0], base[key], prop[key])
        if changed:
            modified.append(Modified(EntityRef(*key), changed))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/ir/test_diff.py -q`
Expected: PASS (whole file).

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/ir/diff.py tests/ir/test_diff.py
git commit -m "feat(viz): device-only name ignore in diff_ir"
```

---

## Task 4: re-enable device-subject names

**Files:**
- Modify: `src/digital_twin/checks/subjects.py` (`_name_for`)
- Test: `tests/checks/test_subjects.py`

- [ ] **Step 1: Write the failing test** — REPLACE the body of `test_resolve_device_does_not_use_model_as_name` and add a new one:

```python
def test_resolve_device_uses_device_name():
    # now that the IR has Device.name, device subjects resolve to it (not model)
    ir = _ir()  # _ir()'s device has model "EX4100-48P" and NO name yet
    assert resolve_subject(ObjectRef("device", _DID), ir, ir).name is None  # no name set


def test_resolve_device_name_when_present():
    from digital_twin.ir import IRBuilder
    from digital_twin.ir.entities import Device, DeviceRole

    ir = IRBuilder().add_device(
        Device(id=_DID, role=DeviceRole.SWITCH, site="s1", model="EX4100-48P", name="core-1")
    ).build()
    assert resolve_subject(ObjectRef("device", _DID), ir, ir).name == "core-1"
```

(Delete the old `test_resolve_device_does_not_use_model_as_name`; the first test above replaces it — a model-only device still resolves to `None`, proving model is never used.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/checks/test_subjects.py::test_resolve_device_name_when_present -q`
Expected: FAIL — `_name_for` has no device branch, so name is `None`.

- [ ] **Step 3: Write minimal implementation**

In `src/digital_twin/checks/subjects.py`, add a device branch to `_name_for` (after the `vlan` branch):

```python
    if kind == "device":
        d = ir.devices.get(oid)
        return d.name if d else None  # Device.name only — model is NOT an identity
```

Update the module docstring line that says devices have no name source to: "devices resolve via `Device.name` (a display name; `model` is never used)."

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/checks/test_subjects.py -q`
Expected: PASS (whole file).

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/checks/subjects.py tests/checks/test_subjects.py
git commit -m "feat(viz): device-subject names resolve via Device.name"
```

---

## Task 5: `viz/highlight.py` — finding → graph-entity index

**Files:**
- Create: `src/digital_twin/viz/__init__.py` (empty)
- Create: `src/digital_twin/viz/highlight.py`
- Test: `tests/viz/__init__.py` (empty), `tests/viz/test_highlight.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/viz/test_highlight.py
from digital_twin.contracts import (
    Cause, Finding, FindingCategory, FindingSource, ObjectRef, Severity,
)
from digital_twin.ir import Confidence, ConfidenceLevel, IRBuilder
from digital_twin.ir.entities import Device, DeviceRole, Port, PortMode, Vlan
from digital_twin.viz.highlight import build_highlight

_HIGH = Confidence(level=ConfidenceLevel.HIGH)


def _ir():
    b = IRBuilder()
    b.add_device(Device(id="aabb01", role=DeviceRole.SWITCH, site="s1"))
    b.add_device(Device(id="aabb02", role=DeviceRole.SWITCH, site="s1"))
    b.add_port(Port(id="aabb01:ge-0/0/1", device_id="aabb01", name="ge-0/0/1",
                    mode=PortMode.ACCESS))
    b.add_vlan(Vlan(vlan_id=30, name="voice"))
    return b.build()


def _f(**kw):
    base = dict(
        source=FindingSource.CHECK, category=FindingCategory.NETWORK, code="t.x",
        severity=Severity.ERROR, confidence=_HIGH, message="m",
    )
    return Finding(**{**base, **kw})


def test_additive_vlan_subject_also_highlights_device_nodes():
    # subject is the vlan; component_nodes are the broken devices — BOTH highlight
    f = _f(subject=ObjectRef("vlan", "30"), evidence={"vlan": 30, "component_nodes": ["aabb01"]})
    hl = build_highlight((f,), _ir())
    assert 30 in hl.vlans
    assert "aabb01" in hl.nodes


def test_worst_severity_wins_per_node():
    warn = _f(severity=Severity.WARNING, affected_entities=("aabb01",))
    err = _f(severity=Severity.ERROR, affected_entities=("aabb01",))
    hl = build_highlight((warn, err), _ir())
    assert hl.nodes["aabb01"].severity is Severity.ERROR


def test_port_and_link_resolve_to_device_nodes():
    port_f = _f(affected_entities=("aabb01:ge-0/0/1",))
    link_f = _f(affected_entities=("aabb01:ge-0/0/1__aabb02:ge-0/0/2",))
    hl = build_highlight((port_f, link_f), _ir())
    assert "aabb01" in hl.nodes and "aabb02" in hl.nodes


def test_mist_device_id_is_normalized_to_mac():
    f = _f(subject=ObjectRef("device", "00000000-0000-0000-1000-aabb01"))
    hl = build_highlight((f,), _ir())
    assert "aabb01" in hl.nodes


def test_gateway_mist_id_2000_normalized():
    # gateway Mist ids use the 2000 type tag — normalize generically, not just 1000
    f = _f(subject=ObjectRef("device", "00000000-0000-0000-2000-aabb01"))
    hl = build_highlight((f,), _ir())
    assert "aabb01" in hl.nodes


def test_caused_by_is_a_cause_line_not_a_highlight():
    f = _f(
        affected_entities=("aabb01",),
        caused_by=(Cause(ref=ObjectRef("link", "aabb02:p__aabb01:q"), fields=("native_vlan",)),),
    )
    hl = build_highlight((f,), _ir())
    assert "aabb02" not in hl.nodes  # cause is NOT highlighted
    assert any("native_vlan" in c for c in hl.causes)


def test_unlocalized_finding_is_counted():
    f = _f(subject=ObjectRef("dhcp_scope", "site:corp"))
    hl = build_highlight((f,), _ir())
    assert hl.unlocalized == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/viz/test_highlight.py -q`
Expected: FAIL — `ModuleNotFoundError: digital_twin.viz.highlight`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/digital_twin/viz/__init__.py
```

```python
# src/digital_twin/viz/highlight.py
"""Map findings onto graph entities for diagram highlighting.

ADDITIVE: a finding contributes ALL the entities it references — `subject`,
structured `evidence` keys, and `affected_entities` — never short-stopping. The
worst severity wins per entity. `caused_by` is collected as caption text and is
NEVER highlighted (cause != blast radius). Findings that resolve to no graph
entity are counted in `unlocalized`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from digital_twin.contracts import Finding, Severity
from digital_twin.ir import IR
from digital_twin.ir.indexes import node_for, vc_root_map

_SEV_RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.ERROR: 2, Severity.CRITICAL: 3}
_MIST_DEV_HEAD = "00000000-0000-0000-"


@dataclass
class Hit:
    severity: Severity
    labels: list[str] = field(default_factory=list)


@dataclass
class Highlight:
    nodes: dict[str, Hit]  # graph node id (VC-folded device) -> worst hit
    vlans: dict[int, Hit]  # vlan id -> worst hit
    causes: list[str]  # caption lines from caused_by (NOT highlighted)
    unlocalized: int  # findings that resolved to no graph entity


def _mac(device_id: str) -> str:
    # Mist device ids are 00000000-0000-0000-XXXX-<mac>, XXXX a type tag (1000
    # switch/ap, 2000 gateway, ...). Normalize to the trailing segment generally.
    parts = device_id.split("-")
    if len(parts) == 5 and device_id.startswith(_MIST_DEV_HEAD):
        return parts[-1]
    return device_id


def build_highlight(findings: Iterable[Finding], ir: IR) -> Highlight:
    vc = vc_root_map(ir)

    def node(raw_dev_id: str) -> str | None:
        m = _mac(raw_dev_id)
        return node_for(vc, m) if m in ir.devices or node_for(vc, m) in ir.devices else None

    def port_node(pid: str) -> str | None:
        return node(pid.split(":", 1)[0]) if ":" in pid else None

    hl = Highlight(nodes={}, vlans={}, causes=[], unlocalized=0)

    def add_node(nid: str | None, sev: Severity, label: str) -> bool:
        if nid is None:
            return False
        cur = hl.nodes.get(nid)
        if cur is None or _SEV_RANK[sev] > _SEV_RANK[cur.severity]:
            hl.nodes[nid] = Hit(sev, (cur.labels if cur else []) + [label])
        else:
            cur.labels.append(label)
        return True

    def add_vlan(vid: int, sev: Severity, label: str) -> bool:
        cur = hl.vlans.get(vid)
        if cur is None or _SEV_RANK[sev] > _SEV_RANK[cur.severity]:
            hl.vlans[vid] = Hit(sev, (cur.labels if cur else []) + [label])
        else:
            cur.labels.append(label)
        return True

    for f in findings:
        label = f"{f.code}: {f.message}"
        hit_any = False

        # 1) subject (typed)
        s = f.subject
        if s is not None:
            if s.kind == "device":
                hit_any |= add_node(node(s.id), f.severity, label)
            elif s.kind == "vlan":
                hit_any |= add_vlan(int(s.id), f.severity, label) if s.id.isdigit() else False
            elif s.kind == "port":
                hit_any |= add_node(port_node(s.id), f.severity, label)
            elif s.kind == "link":
                for pid in s.id.split("__"):
                    hit_any |= add_node(port_node(pid), f.severity, label)

        # 2) structured evidence keys
        ev: Any = f.evidence
        for vid in _ints(ev.get("vlan")) + _ints(ev.get("affected_vlans")):
            hit_any |= add_vlan(vid, f.severity, label)
        _node_keys = ("device", "component_nodes", "fragment_nodes",
                      "baseline_root", "proposed_root")
        for did in [d for k in _node_keys for d in _strs(ev.get(k))]:
            hit_any |= add_node(node(did), f.severity, label)
        for pid in _strs(ev.get("port")) + _strs(ev.get("new_member_ports")):
            hit_any |= add_node(port_node(pid), f.severity, label)
        for lid in _strs(ev.get("link")):
            for pid in lid.split("__"):
                hit_any |= add_node(port_node(pid), f.severity, label)
        for imp in ev.get("impacts") or ():
            if isinstance(imp, dict):
                att = imp.get("attachment")
                if isinstance(att, str):
                    hit_any |= add_node(port_node(att) or node(att), f.severity, label)
                if isinstance(imp.get("vlan"), int):
                    hit_any |= add_vlan(imp["vlan"], f.severity, label)

        # 3) affected_entities (untyped) disambiguated against the IR
        for ent in f.affected_entities:
            if ent in ir.devices or node(ent) in ir.devices:
                hit_any |= add_node(node(ent), f.severity, label)
            elif ent.isdigit() and int(ent) in ir.vlans:
                hit_any |= add_vlan(int(ent), f.severity, label)
            elif ent in ir.ports:
                hit_any |= add_node(port_node(ent), f.severity, label)
            elif "__" in ent:
                for pid in ent.split("__"):
                    hit_any |= add_node(port_node(pid), f.severity, label)

        # cause attribution -> caption text, NEVER a highlight
        for c in f.caused_by:
            who = c.ref.name or c.ref.id
            flds = f" [{', '.join(c.fields)}]" if c.fields else ""
            hl.causes.append(f"{f.code}: caused by {c.ref.kind} {who}{flds}")

        if not hit_any:
            hl.unlocalized += 1

    return hl


def _ints(v: Any) -> list[int]:
    if isinstance(v, int):
        return [v]
    if isinstance(v, (list, tuple)):
        return [x for x in v if isinstance(x, int)]
    return []


def _strs(v: Any) -> list[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, (list, tuple)):
        return [x for x in v if isinstance(x, str)]
    return []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/viz/test_highlight.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/viz/__init__.py src/digital_twin/viz/highlight.py tests/viz/__init__.py tests/viz/test_highlight.py
git commit -m "feat(viz): build_highlight — additive finding->entity index"
```

---

## Task 6: `viz/mermaid.py` — styling, helpers, L2 chart + `build_diagrams`/`safe_build_diagrams`

**Files:**
- Create: `src/digital_twin/viz/mermaid.py`
- Test: `tests/viz/test_mermaid.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/viz/test_mermaid.py
from digital_twin.contracts import Finding, FindingCategory, FindingSource, ObjectRef, Severity
from digital_twin.ir import Confidence, ConfidenceLevel, IRBuilder
from digital_twin.ir.entities import (
    Device, DeviceRole, Link, LinkKind, Port, PortMode, Vlan, link_id,
)
from digital_twin.viz.mermaid import build_diagrams, safe_build_diagrams

_HIGH = Confidence(level=ConfidenceLevel.HIGH)


def _ir():
    b = IRBuilder()
    b.add_device(Device(id="aabb01", role=DeviceRole.SWITCH, site="s1", name="core-1"))
    b.add_device(Device(id="aabb02", role=DeviceRole.SWITCH, site="s1", name="idf-3"))
    b.add_port(Port(id="aabb01:ge-0/0/1", device_id="aabb01", name="ge-0/0/1",
                    mode=PortMode.TRUNK, tagged_vlans=(30,)))
    b.add_port(Port(id="aabb02:ge-0/0/1", device_id="aabb02", name="ge-0/0/1",
                    mode=PortMode.TRUNK, tagged_vlans=(30,)))
    b.add_link(Link(id=link_id("aabb01:ge-0/0/1", "aabb02:ge-0/0/1"),
                    a_port="aabb01:ge-0/0/1", b_port="aabb02:ge-0/0/1", kind=LinkKind.PHYSICAL))
    b.add_vlan(Vlan(vlan_id=20, name="data"))   # unaffected
    b.add_vlan(Vlan(vlan_id=30, name="voice"))  # affected by the test finding
    b.add_vlan(Vlan(vlan_id=100, name="iot"))   # unaffected; numeric-sort guard
    return b.build()


def _f(**kw):
    base = dict(source=FindingSource.CHECK, category=FindingCategory.NETWORK, code="t.x",
                severity=Severity.ERROR, confidence=_HIGH, message="boom")
    return Finding(**{**base, **kw})


def test_l2_chart_present_and_well_formed():
    diagrams = build_diagrams(_ir(), ())
    l2 = next(d for d in diagrams if d.view == "l2")
    assert l2.mermaid.startswith("graph LR")
    assert "classDef" in l2.mermaid
    assert "core-1" in l2.mermaid  # device display name in a label


def test_l2_highlights_affected_device():
    diagrams = build_diagrams(_ir(), (_f(affected_entities=("aabb01",)),))
    l2 = next(d for d in diagrams if d.view == "l2")
    assert "class " in l2.mermaid and ":::" not in l2.mermaid  # uses `class n crit;` form
    assert l2.severity is Severity.ERROR


def test_every_class_target_node_is_declared():
    # structural invariant: no `class nX` line references an undeclared node id
    diagrams = build_diagrams(_ir(), (_f(affected_entities=("aabb01",)),))
    for d in diagrams:
        declared = {
            ln.split("[")[0].strip().rstrip("(")
            for ln in d.mermaid.splitlines() if "[" in ln
        }
        for ln in d.mermaid.splitlines():
            if ln.strip().startswith("class "):
                body = ln.strip()[len("class "):].rstrip(";")  # "n0,n1 crit"
                targets, _cls = body.rsplit(" ", 1)
                for nid in targets.split(","):
                    assert nid.strip() in declared, f"{nid} not declared in {d.view}"


def test_safe_build_diagrams_swallows_errors(monkeypatch):
    import digital_twin.viz.mermaid as m

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(m, "build_diagrams", _boom)
    assert safe_build_diagrams(_ir(), ()) == ()


def test_causes_appear_in_l2_notes():
    from digital_twin.contracts import Cause, ObjectRef
    f = _f(affected_entities=("aabb01",),
           caused_by=(Cause(ref=ObjectRef("link", "aabb02:p__aabb01:q"), fields=("native_vlan",)),))
    l2 = next(d for d in build_diagrams(_ir(), (f,)) if d.view == "l2")
    assert any("native_vlan" in n for n in l2.notes)  # cause is a visible caption, not %%
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/viz/test_mermaid.py -q`
Expected: FAIL — `ModuleNotFoundError: digital_twin.viz.mermaid`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/digital_twin/viz/mermaid.py
"""Render IR topology + a Highlight into mermaid Diagram(s).

`build_diagrams` is PURE and may raise (tests catch render bugs). The pipeline
calls `safe_build_diagrams`, the ONLY place that swallows exceptions (-> ()), so
a render bug never sinks a verdict. v1 highlights NODES only (link/port findings
already mapped to endpoint device nodes by build_highlight); finding labels and
cause attribution go in Diagram.notes (VISIBLE via to_markdown), never as a class
and never as `%%` comments (mermaid does not render those).
"""

from __future__ import annotations

from digital_twin.contracts import Diagram, Finding, Severity
from digital_twin.ir import IR
from digital_twin.representations.l2_graph import build_l2_graph

from .highlight import Hit, Highlight, build_highlight

_CLASSDEFS = (
    "  classDef crit fill:#fdd,stroke:#c00,stroke-width:2px;",
    "  classDef warn fill:#fff3cd,stroke:#e0a800;",
    "  classDef info fill:#eef,stroke:#88a;",
)
_SEV_CLASS = {
    Severity.CRITICAL: "crit", Severity.ERROR: "crit",
    Severity.WARNING: "warn", Severity.INFO: "info",
}
_SEV_RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.ERROR: 2, Severity.CRITICAL: 3}


def _safe(text: object, cap: int = 120) -> str:
    t = (
        str(text).replace("\n", " ").replace('"', "'").replace("[", "(").replace("]", ")")
        .replace("|", "/").replace("<", "‹").replace(">", "›")
    )
    return t if len(t) <= cap else t[: cap - 1] + "…"


def _label(*parts: object) -> str:
    return "<br/>".join(_safe(p) for p in parts if p is not None and str(p) != "")


class _Ids:
    """Per-chart synthetic node ids (mermaid ids cannot contain : / - .)."""

    def __init__(self) -> None:
        self._map: dict[str, str] = {}

    def get(self, key: str) -> str:
        if key not in self._map:
            self._map[key] = f"n{len(self._map)}"
        return self._map[key]


def _worst(*sevs: Severity | None) -> Severity | None:
    present = [s for s in sevs if s is not None]
    return max(present, key=lambda s: _SEV_RANK[s]) if present else None


def _class_lines(ids: _Ids, node_hits: dict[str, Hit]) -> tuple[list[str], list[str]]:
    """(`class nX cls;` lines, human caption strings) for nodes declared on THIS
    chart. Captions go into Diagram.notes (VISIBLE via to_markdown)."""
    classes: list[str] = []
    captions: list[str] = []
    for raw_id, hit in node_hits.items():
        if raw_id not in ids._map:  # node not on this chart
            continue
        classes.append(f"  class {ids.get(raw_id)} {_SEV_CLASS[hit.severity]};")
        for lbl in hit.labels:
            captions.append(_safe(f"{hit.severity.value}: {lbl}"))
    return classes, captions


def _l2_diagram(ir: IR, hl: Highlight) -> Diagram:
    g = build_l2_graph(ir)
    ids = _Ids()
    lines = ["graph LR", *_CLASSDEFS]
    for node in g.nodes:
        dev = ir.devices.get(node)
        label = _label(dev.name or node if dev else node, dev.role.value if dev else "?")
        lines.append(f'  {ids.get(node)}["{label}"]')
    for u, v, data in g.edges(data=True):
        edge = data["data"]
        lbl = ",".join(str(x) for x in sorted(edge.vlans)) or edge.kind
        lines.append(f'  {ids.get(u)} ---|"{_safe(lbl)}"| {ids.get(v)}')
    cls, captions = _class_lines(ids, hl.nodes)
    lines += cls
    causes = [_safe(c) for c in hl.causes]
    unloc = [f"{hl.unlocalized} finding(s) not localized"] if hl.unlocalized else []
    sev = _worst(*(h.severity for raw, h in hl.nodes.items() if raw in ids._map))
    return Diagram(view="l2", title="L2 topology", severity=sev,
                   mermaid="\n".join(lines), notes=tuple(captions + causes + unloc))


def build_diagrams(ir: IR, findings: tuple[Finding, ...]) -> tuple[Diagram, ...]:
    hl = build_highlight(findings, ir)
    return (_l2_diagram(ir, hl),)  # per-VLAN + L3 added in Tasks 7-8


def safe_build_diagrams(ir: IR, findings: tuple[Finding, ...]) -> tuple[Diagram, ...]:
    try:
        return build_diagrams(ir, findings)
    except Exception:  # noqa: BLE001 — diagrams are presentational; never sink a verdict
        return ()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/viz/test_mermaid.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/viz/mermaid.py tests/viz/test_mermaid.py
git commit -m "feat(viz): mermaid L2 chart + build_diagrams/safe_build_diagrams"
```

---

## Task 7: per-VLAN charts

**Files:**
- Modify: `src/digital_twin/viz/mermaid.py`
- Test: `tests/viz/test_mermaid.py`

- [ ] **Step 1: Write the failing test** (append)

```python
def test_per_vlan_chart_emitted_and_affected_first():
    diagrams = build_diagrams(_ir(), (_f(evidence={"vlan": 30, "component_nodes": ["aabb01"]}),))
    vlan_order = [d.view for d in diagrams if d.view.startswith("vlan:")]
    assert {"vlan:20", "vlan:30", "vlan:100"} <= set(vlan_order)
    assert vlan_order[0] == "vlan:30"  # the affected VLAN sorts before ALL unaffected
    # unaffected VLANs follow in NUMERIC order (vlan:100 must NOT precede vlan:20)
    assert vlan_order.index("vlan:20") < vlan_order.index("vlan:100")
    v30 = next(d for d in diagrams if d.view == "vlan:30")
    assert v30.severity is Severity.ERROR


def test_vlan_subject_label_appears_in_chart_notes():
    # a pure vlan-subject finding (no node) must still show its code+reason caption
    v30 = next(
        d for d in build_diagrams(_ir(), (_f(subject=ObjectRef("vlan", "30")),))
        if d.view == "vlan:30"
    )
    assert any("t.x" in n for n in v30.notes)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/viz/test_mermaid.py::test_per_vlan_chart_emitted_and_affected_first -q`
Expected: FAIL — only the L2 diagram is returned.

- [ ] **Step 3: Write minimal implementation**

First add these imports to the top of `mermaid.py` (used by this task and Task 8):

```python
import networkx as nx

from digital_twin.analysis.exits import resolve_exit
from digital_twin.ir.indexes import node_for, vc_root_map
from digital_twin.representations.vlan_graph import build_vlan_graph
```

Add a per-VLAN builder:

```python
def _vlan_diagram(ir: IR, l2: nx.MultiGraph, vid: int, hl: Highlight) -> Diagram:
    vc = vc_root_map(ir)
    g = build_vlan_graph(ir, l2, vid)
    ids = _Ids()
    # resolve_exit covers IRB/SVI (is_exit) AND boundary-uplink GATEWAY nodes on a
    # carrying edge; union the owners of any l3intf for the vlan (incl GATEWAY-role
    # interfaces, which resolve_exit rule 1 — IRB/SVI only — does not see).
    exit_nodes = set(resolve_exit(ir, g).nodes)
    for intf in ir.l3intfs:
        if intf.vlan_id == vid:
            exit_nodes.add(node_for(vc, intf.device_id))
    lines = ["graph LR", *_CLASSDEFS]
    for node in set(g.nodes) | exit_nodes:  # add exit devices absent from the subgraph
        dev = ir.devices.get(node)
        name = (dev.name or node) if dev else node
        if node in exit_nodes:
            lines.append(f'  {ids.get(node)}(["{_label(name, "exit")}"])')
        else:
            lines.append(f'  {ids.get(node)}["{_label(name, dev.role.value if dev else "?")}"]')
    for u, v, _data in g.edges(data=True):
        lines.append(f'  {ids.get(u)} ---|"{_safe(vid)}"| {ids.get(v)}')
    cls, captions = _class_lines(ids, hl.nodes)
    lines += cls
    vhit = hl.vlans.get(vid)
    vname = ir.vlans[vid].name if vid in ir.vlans and ir.vlans[vid].name else None
    title = f"VLAN {vid}" + (f' "{vname}"' if vname else "")
    sev = _worst(vhit.severity if vhit else None,
                 *(h.severity for raw, h in hl.nodes.items() if raw in ids._map))
    vlan_caps = [_safe(f"{vhit.severity.value}: {lbl}") for lbl in vhit.labels] if vhit else []
    unloc = [f"{hl.unlocalized} finding(s) not localized"] if hl.unlocalized else []
    return Diagram(view=f"vlan:{vid}", title=title, severity=sev,
                   mermaid="\n".join(lines), notes=tuple(captions + vlan_caps + unloc))
```

Replace `build_diagrams` body (numeric VLAN ordering — `vlan:100` must NOT sort before `vlan:30`):

```python
def _vlan_id_of(d: Diagram) -> int:
    return int(d.view.split(":", 1)[1])


def build_diagrams(ir: IR, findings: tuple[Finding, ...]) -> tuple[Diagram, ...]:
    hl = build_highlight(findings, ir)
    l2 = build_l2_graph(ir)
    out: list[Diagram] = [_l2_diagram(ir, hl)]
    vlan_diagrams = [_vlan_diagram(ir, l2, vid, hl) for vid in sorted(ir.vlans)]

    def _order(d: Diagram) -> tuple[bool, int, int]:
        rank = _SEV_RANK[d.severity] if d.severity is not None else -1
        return (d.severity is None, -rank, _vlan_id_of(d))  # affected first, then numeric id

    vlan_diagrams.sort(key=_order)
    out += vlan_diagrams
    return tuple(out)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/viz/test_mermaid.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/viz/mermaid.py tests/viz/test_mermaid.py
git commit -m "feat(viz): per-VLAN charts (affected-first), gateway exit nodes"
```

---

## Task 8: Routed VLAN exits (L3) chart

**Files:**
- Modify: `src/digital_twin/viz/mermaid.py`
- Test: `tests/viz/test_mermaid.py`

- [ ] **Step 1: Write the failing test** (append)

```python
def test_l3_exits_chart_includes_gateway_role_interface():
    from digital_twin.ir.entities import L3Intf, L3Role, Vlan

    ir = (
        IRBuilder()
        .add_device(Device(id="gw01", role=DeviceRole.GATEWAY, site="s1", name="srx"))
        .add_vlan(Vlan(vlan_id=2, name="mgmt"))  # subnet-less, but has an l3intf
        .add_l3intf(L3Intf(device_id="gw01", role=L3Role.GATEWAY, vlan_id=2))
        .build()
    )
    diagrams = build_diagrams(ir, ())
    l3 = next(d for d in diagrams if d.view == "l3_exits")
    assert "VLAN 2" in l3.mermaid
    assert "srx" in l3.mermaid  # gateway-role interface present
```

(`IRBuilder.add_l3intf(L3Intf(...))` is the real builder API — verified in `src/digital_twin/ir/model.py:115`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/viz/test_mermaid.py::test_l3_exits_chart_includes_gateway_role_interface -q`
Expected: FAIL — no `l3_exits` diagram yet.

- [ ] **Step 3: Write minimal implementation**

First add the `L3Intf` import to `mermaid.py`:

```python
from digital_twin.ir.entities import L3Intf
```

Add the L3 builder and append it last in `build_diagrams`:

```python
def _l3_exits_diagram(ir: IR, hl: Highlight) -> Diagram:
    vc = vc_root_map(ir)
    by_vlan: dict[int, list[L3Intf]] = {}
    for intf in ir.l3intfs:
        if intf.vlan_id is not None:
            by_vlan.setdefault(intf.vlan_id, []).append(intf)
    routed = sorted(set(by_vlan) | {vid for vid, v in ir.vlans.items() if v.subnet is not None})
    ids = _Ids()
    lines = ["graph LR", *_CLASSDEFS]
    for vid in routed:
        name = ir.vlans[vid].name if vid in ir.vlans and ir.vlans[vid].name else None
        lines.append(f'  {ids.get(f"vlan:{vid}")}["{_label(f"VLAN {vid}", name)}"]')
        for intf in by_vlan.get(vid, []):
            dev = ir.devices.get(node_for(vc, intf.device_id))
            ikey = f"intf:{intf.id}"
            iname = dev.name if dev and dev.name else intf.device_id
            lines.append(f'  {ids.get(ikey)}(["{_label(iname, intf.role.value)}"])')
            lines.append(f'  {ids.get(f"vlan:{vid}")} -->|"served by"| {ids.get(ikey)}')
    # highlight affected VLAN boxes
    classes: list[str] = []
    for vid, hit in hl.vlans.items():
        if f"vlan:{vid}" in ids._map:
            classes.append(f"  class {ids.get(f'vlan:{vid}')} {_SEV_CLASS[hit.severity]};")
    lines += classes
    sev = _worst(*(h.severity for vid, h in hl.vlans.items() if f"vlan:{vid}" in ids._map))
    vlan_caps = [
        _safe(f"{hit.severity.value}: {lbl}")
        for vid, hit in hl.vlans.items() if f"vlan:{vid}" in ids._map
        for lbl in hit.labels
    ]
    unloc = [f"{hl.unlocalized} finding(s) not localized"] if hl.unlocalized else []
    return Diagram(view="l3_exits", title="Routed VLAN exits", severity=sev,
                   mermaid="\n".join(lines), notes=tuple(vlan_caps + unloc))
```

In `build_diagrams`, append before `return tuple(out)`:

```python
    out.append(_l3_exits_diagram(ir, hl))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/viz/test_mermaid.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/viz/mermaid.py tests/viz/test_mermaid.py
git commit -m "feat(viz): Routed VLAN exits (L3) chart"
```

---

## Task 9: `viz/markdown.py` — assemble the UI blob

**Files:**
- Create: `src/digital_twin/viz/markdown.py`
- Test: `tests/viz/test_markdown.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/viz/test_markdown.py
from digital_twin.contracts import Diagram, Severity
from digital_twin.viz.markdown import to_markdown


def test_to_markdown_wraps_each_diagram():
    d = Diagram(view="l2", title="L2 topology", severity=Severity.ERROR,
                mermaid="graph LR\n  n0[x]", notes=("1 finding(s) not localized",))
    md = to_markdown((d,))
    assert "## L2 topology" in md
    assert "```mermaid" in md and "graph LR" in md
    assert "1 finding(s) not localized" in md  # notes rendered as caption


def test_to_markdown_empty_is_empty_string():
    assert to_markdown(()) == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/viz/test_markdown.py -q`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/digital_twin/viz/markdown.py
"""Assemble Diagrams into one markdown blob (```mermaid blocks) for the UI."""

from __future__ import annotations

from collections.abc import Iterable

from digital_twin.contracts import Diagram


def to_markdown(diagrams: Iterable[Diagram]) -> str:
    blocks: list[str] = []
    for d in diagrams:
        block = [f"## {d.title}", "", "```mermaid", d.mermaid, "```"]
        block += [f"> {n}" for n in d.notes]
        blocks.append("\n".join(block))
    return "\n\n".join(blocks)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/viz/test_markdown.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/viz/markdown.py tests/viz/test_markdown.py
git commit -m "feat(viz): to_markdown helper"
```

---

## Task 10: `Verdict.diagrams` field

**Files:**
- Modify: `src/digital_twin/verdict/verdict.py`
- Test: `tests/verdict/test_assembly.py` (or `tests/drivers/test_render.py`)

- [ ] **Step 1: Write the failing test** (append to `tests/drivers/test_render.py`)

```python
def test_verdict_holds_diagrams_and_serializes():
    from digital_twin.contracts import Diagram, Severity
    from digital_twin.drivers.render import verdict_to_dict

    v = _verdict()
    d = Diagram(view="l2", title="L2", severity=Severity.ERROR, mermaid="graph LR")
    v2 = dataclasses.replace(v, diagrams=(d,))
    assert verdict_to_dict(v2)["diagrams"][0]["view"] == "l2"
```

(Add `import dataclasses` at the top of the test file if absent.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/drivers/test_render.py::test_verdict_holds_diagrams_and_serializes -q`
Expected: FAIL — `TypeError: ... unexpected keyword argument 'diagrams'`.

- [ ] **Step 3: Write minimal implementation**

In `src/digital_twin/verdict/verdict.py`, import `Diagram` and add the field (last, with a default so all existing constructors keep working):

```python
from digital_twin.contracts import Diagram, Finding, Severity
```

```python
    state_meta: StateMetaView | None = None  # freshness (None pre-fetch)
    trace_ref: str | None = None  # run id of the trace record
    diagrams: tuple[Diagram, ...] = ()  # topology charts (mermaid); () when no proposed IR
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/drivers/test_render.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/verdict/verdict.py tests/drivers/test_render.py
git commit -m "feat(viz): Verdict.diagrams field"
```

---

## Task 11: pipeline hook

**Files:**
- Modify: `src/digital_twin/engine/pipeline.py` (`_simulate_site_state`, the final `assemble(...)` return ~line 241)
- Test: `tests/engine/test_pipeline.py`

- [ ] **Step 1: Write the failing test** (append; reuses `_raw`, `_plan`, `_op`, `FakeProvider`, `simulate`, `dc_replace`)

```python
def test_normal_verdict_carries_diagrams():
    payload = {**SETTING, "networks": {"corp": {"vlan_id": 10}, "voice": {"vlan_id": 31}}}
    v = simulate(_plan([_op(payload=payload)]), provider=FakeProvider())
    assert v.diagrams  # non-empty: at least the L2 chart
    assert any(d.view == "l2" for d in v.diagrams)


def test_unknown_short_circuit_has_no_diagrams():
    # an out-of-scope raw path returns via _unknown() -> no diagrams
    bad = {**SETTING, "dhcpd_config": {"corp": {"ip": "9.9.9.9"}}}
    v = simulate(_plan([_op(payload=bad)]), provider=FakeProvider())
    assert v.decision is Decision.UNKNOWN
    assert v.diagrams == ()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/engine/test_pipeline.py::test_normal_verdict_carries_diagrams -q`
Expected: FAIL — `v.diagrams` is empty (no hook yet).

- [ ] **Step 3: Write minimal implementation**

In `src/digital_twin/engine/pipeline.py` add the import:

```python
from digital_twin.viz.mermaid import safe_build_diagrams
```

Find the FINAL `return assemble(...)` inside `_simulate_site_state` (under `with trace.stage("verdict"):`, ~lines 241-252). Capture it into `verdict` and attach diagrams — the exact current call is:

```python
    with trace.stage("verdict"):
        verdict = assemble(
            inputs=DecisionInputs(
                rejections=(dp_rej,) if dp_rej else (),
                l0_fatal=False,
                baseline_unavailable=False,
                check_results=results,
                adapter_findings=adapter_findings,
            ),
            ir_diff=diff,
            state_meta=state_meta,
            trace_ref=run.run_id,
        )
        return replace(verdict, diagrams=safe_build_diagrams(proposed.ir, verdict.findings))
```

(Only this one site changes: capture → `replace(..., diagrams=...)`. `proposed.ir` is the right IR — the checks and `diff` use `proposed.ir`, so the findings' entity ids match it. `replace` is already imported (`from dataclasses import replace`). All `_unknown(...)` returns are left untouched, so every short-circuit — including the derived-gate UNKNOWN — keeps `diagrams=()` via the field default.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/engine/test_pipeline.py -q`
Expected: PASS (whole file).

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/engine/pipeline.py tests/engine/test_pipeline.py
git commit -m "feat(viz): attach diagrams to the site verdict (pipeline hook)"
```

---

## Task 12: render — `render_diagrams_markdown` + titles in `render_human`

**Files:**
- Modify: `src/digital_twin/drivers/render.py`
- Test: `tests/drivers/test_render.py`

- [ ] **Step 1: Write the failing test** (append)

```python
def test_render_diagrams_markdown_and_titles():
    from digital_twin.contracts import Diagram, Severity
    from digital_twin.drivers.render import render_diagrams_markdown, render_human

    d = Diagram(view="l2", title="L2 topology", severity=Severity.ERROR, mermaid="graph LR")
    v = dataclasses.replace(_verdict(), diagrams=(d,))
    assert "```mermaid" in render_diagrams_markdown(v)
    assert "L2 topology" in render_human(v)  # titles listed in human output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/drivers/test_render.py::test_render_diagrams_markdown_and_titles -q`
Expected: FAIL — `ImportError: cannot import name 'render_diagrams_markdown'`.

- [ ] **Step 3: Write minimal implementation**

In `src/digital_twin/drivers/render.py` add the import and helper, and list titles in `render_human`:

```python
from digital_twin.viz.markdown import to_markdown
```

```python
def render_diagrams_markdown(verdict: Verdict) -> str:
    """The paste-ready mermaid blob for the elicitation UI."""
    return to_markdown(verdict.diagrams)
```

In `render_human`, before the `state_meta` block, add:

```python
    for d in verdict.diagrams:
        lines.append(f"  diagram: {d.title}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/drivers/test_render.py -q`
Expected: PASS.

- [ ] **Step 5: Final verification + commit**

Run the full gate:

```bash
.venv/bin/python -m pytest -q && .venv/bin/ruff check . && .venv/bin/mypy
```

Expected: all pass, ruff clean, mypy clean.

```bash
git add src/digital_twin/drivers/render.py tests/drivers/test_render.py
git commit -m "feat(viz): render_diagrams_markdown + diagram titles in human output"
```

---

## Self-review notes (for the implementer)

- **Org path:** `simulate_org_template` builds per-site verdicts through `_simulate_site_state`, so per-site diagrams come for free — no extra task. Org-level aggregate is roadmap.
- **`add_l3intf` API (Task 8):** verified — `IRBuilder.add_l3intf(L3Intf(...))` at `src/digital_twin/ir/model.py:115`.
- **VC folding:** `build_highlight` maps device/port ids through `node_for(vc_root_map(ir), mac)` so highlighted ids match graph node ids.
- **Notes placement (deliberate):** per-node finding-label captions are per-chart
  (only the nodes on that chart); the `unlocalized` count rides EVERY chart's
  notes; cause lines are centralized on the **L2 overview** only (repeating them
  across ~20 VLAN charts would be noise). Per-chart cause scoping is roadmap.
- **No silent caps:** v1 emits the full VLAN set; the payload guard is roadmap (`docs/ROADMAP.md` §6).
