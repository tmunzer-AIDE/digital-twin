# Visual Attribution Map Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a verdict-level `VisualMap` keyed by `(view, entity)` with `origin`/`affected` tiers so the approval UI can distinguish the change locus from the blast radius, scoped per rendered view so VLAN highlight-bleed is structurally impossible.

**Architecture:** A new pure builder `viz/visual_map.py` turns `(baseline_ir, proposed_ir, findings)` into `VisualMap = {view_id: {entity_key: VisualEntry}}`. The `Verdict` gains a `visual_map` field populated in the pipeline. `viz/mermaid.py` is refactored to paint each chart from its view's sub-map (fixing the bleed in our own output) and `viz/highlight.py` is deleted — the map is the single mechanism.

**Tech Stack:** Python 3.14, uv, pytest, ruff (100-col), mypy (strict on `src`, not `tests`). networkx for graphs.

## Global Constraints

- **Verdict-neutral:** the map is presentational. `verdict/decision.py` MUST NOT read it; SAFE/REVIEW/UNSAFE/UNKNOWN and every finding `severity` are unchanged. (Locked by an invariance test in Task 7.)
- **Views = existing `Diagram.view` ids:** `l2`, `vlan:<vid>`, `l3_exits`. No parallel vocabulary.
- **Entity keys:** `kind:id` where `kind ∈ {device, vlan, port, link, intf}`. The `id` may contain colons; consumers split on the FIRST colon only. `VisualEntry` ALSO carries structured `kind`/`id` so no string-parsing is required.
- **Builder takes BOTH IRs:** removed-entity ownership exists only in `baseline_ir`; everything rendered is resolved against `proposed_ir`.
- **v1 tiers:** `origin` and `affected` only. No `primary`/`secondary` (deferred).
- **No phantom nodes (v1):** only entities resolving in `proposed_ir` get a self-entry; removed-entity origins fall back to the owner `device:<node>` (guaranteed on `l2`; on a `vlan` view only if the owner still participates).
- **Gate (run before every commit that touches `src`):** `uv run pytest tests -q && uv run ruff check . && uv run mypy src`. Pyright IDE diagnostics are noise.
- **Commit trailer:** `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

## File Structure

- `src/digital_twin/contracts/visual_map.py` **(create)** — pure value types: `VisualTier`, `FindingRef`, `VisualEntry`, `VisualMap` alias, `entity_key()`.
- `src/digital_twin/contracts/__init__.py` **(modify)** — export the new types.
- `src/digital_twin/viz/visual_map.py` **(create)** — the pure builder `build_visual_map(baseline_ir, proposed_ir, findings)` + entity/owner helpers + per-view membership index + contribution extraction + reconciliation.
- `src/digital_twin/verdict/verdict.py` **(modify)** — add `Verdict.visual_map` field.
- `src/digital_twin/engine/pipeline.py:273` **(modify)** — populate `visual_map` (dual IR) and pass both IRs to diagrams.
- `src/digital_twin/viz/mermaid.py` **(modify)** — `build_diagrams(baseline_ir, proposed_ir, findings)`; paint each chart from its view sub-map; add an `origin` classDef.
- `src/digital_twin/viz/highlight.py` **(delete)** — replaced by the map.
- `tests/viz/test_visual_map.py` **(create)** — unit tests for the builder.
- `tests/viz/test_highlight.py` **(delete)** — superseded.
- `tests/viz/test_mermaid.py` **(modify)** — new signature + bleed regression.
- `tests/golden/` **(modify)** — worked-example golden + serialization shape.
- `docs/ROADMAP.md` **(modify)** — record feature + deferred fast-follows.

---

## Task 1: Contract types (`visual_map.py`)

**Files:**
- Create: `src/digital_twin/contracts/visual_map.py`
- Modify: `src/digital_twin/contracts/__init__.py`
- Test: `tests/contracts/test_visual_map.py`

**Interfaces:**
- Produces:
  - `class VisualTier(StrEnum)`: `ORIGIN = "origin"`, `AFFECTED = "affected"`
  - `@dataclass(frozen=True) class FindingRef(index: int, code: str, subject: ObjectRef | None = None)`
  - `@dataclass(frozen=True) class VisualEntry(kind: str, id: str, tier: VisualTier, severity: Severity, findings: tuple[FindingRef, ...])`
  - `VisualMap = dict[str, dict[str, VisualEntry]]` (view_id → entity_key → entry)
  - `def entity_key(kind: str, id: str) -> str` → `f"{kind}:{id}"`

- [ ] **Step 1: Write the failing test**

```python
# tests/contracts/test_visual_map.py
from digital_twin.contracts import (
    FindingRef,
    ObjectRef,
    Severity,
    VisualEntry,
    VisualMap,
    VisualTier,
    entity_key,
)


def test_entity_key_joins_kind_and_id():
    assert entity_key("device", "aabb01") == "device:aabb01"
    # id may itself contain colons; key is still split-on-first-colon recoverable
    assert entity_key("port", "aabb01:ge-0/0/1") == "port:aabb01:ge-0/0/1"


def test_visual_entry_is_frozen_and_carries_structured_kind_id():
    e = VisualEntry(
        kind="device", id="aabb01", tier=VisualTier.ORIGIN,
        severity=Severity.WARNING,
        findings=(FindingRef(index=0, code="t.x", subject=ObjectRef("vlan", "10")),),
    )
    assert e.tier is VisualTier.ORIGIN
    assert e.kind == "device" and e.id == "aabb01"
    assert e.findings[0].index == 0


def test_visual_map_alias_usable_as_nested_dict():
    m: VisualMap = {"l2": {"device:aabb01": VisualEntry(
        kind="device", id="aabb01", tier=VisualTier.AFFECTED,
        severity=Severity.ERROR, findings=(),
    )}}
    assert m["l2"]["device:aabb01"].severity is Severity.ERROR
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/contracts/test_visual_map.py -q`
Expected: FAIL with `ImportError: cannot import name 'VisualEntry'`.

- [ ] **Step 3: Write the contract module**

```python
# src/digital_twin/contracts/visual_map.py
"""VisualMap: a presentation-only attribution layer keyed by (view, entity).

PURELY presentational — verdict/decision.py never reads it. Each entry records
how central an entity is to the change (`tier`) and how bad it is (`severity`),
the two axes kept independent. Keyed per rendered view (l2 | vlan:<id> |
l3_exits) so a finding scoped to one VLAN can never paint another VLAN's chart.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .finding import ObjectRef, Severity


class VisualTier(StrEnum):
    ORIGIN = "origin"  # the changed thing (from caused_by) — visually distinct
    AFFECTED = "affected"  # the blast radius that loses service


@dataclass(frozen=True)
class FindingRef:
    """A back-link to the exact finding instance, NOT a bare code: two findings
    can share a code (blackhole on vlan 10 and vlan 20), so `index` (position in
    Verdict.findings) is what disambiguates the instance for the UI."""

    index: int
    code: str
    subject: ObjectRef | None = None


@dataclass(frozen=True)
class VisualEntry:
    kind: str  # device | vlan | port | link | intf — structured, no string-parsing
    id: str  # raw entity id (may contain colons, e.g. s1:ge-0/0/1)
    tier: VisualTier
    severity: Severity  # worst severity touching this (view, entity)
    findings: tuple[FindingRef, ...]  # instances touching this (view, entity)


# view_id -> entity_key -> entry. entity_key == f"{kind}:{id}".
VisualMap = dict[str, dict[str, VisualEntry]]


def entity_key(kind: str, id: str) -> str:
    """`kind:id`. Consumers split on the FIRST colon only (id may contain more)."""
    return f"{kind}:{id}"
```

- [ ] **Step 4: Export from the contracts package**

In `src/digital_twin/contracts/__init__.py`, add the import after the `finding` import line and add the names to `__all__` (keep `__all__` alphabetically tidy):

```python
from .visual_map import FindingRef, VisualEntry, VisualMap, VisualTier, entity_key
```

Add to `__all__`: `"FindingRef"`, `"VisualEntry"`, `"VisualMap"`, `"VisualTier"`, `"entity_key"`.

- [ ] **Step 5: Run tests + gate**

Run: `uv run pytest tests/contracts/test_visual_map.py -q && uv run ruff check src/digital_twin/contracts && uv run mypy src`
Expected: PASS; mypy clean.

- [ ] **Step 6: Commit**

```bash
git add src/digital_twin/contracts/visual_map.py src/digital_twin/contracts/__init__.py tests/contracts/test_visual_map.py
git commit -m "feat(contracts): VisualMap/VisualEntry/FindingRef visual-attribution types

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Entity resolution & owner-expansion helpers

**Files:**
- Create: `src/digital_twin/viz/visual_map.py`
- Test: `tests/viz/test_visual_map.py`

**Interfaces:**
- Consumes: `node_for`, `vc_root_map` from `digital_twin.ir.indexes`; `IR` from `digital_twin.ir`.
- Produces (module-private except where noted):
  - `def _mac(device_id: str) -> str`
  - `def _node(ir: IR, raw: str) -> str | None` — VC-folded device node if `raw` resolves to a device, else None
  - `def _port_node(ir: IR, pid: str) -> str | None` — owning device node of a port id (`split(":", 1)[0]`), IR-checked
  - `def _resolve_affected(ent: str, ir: IR) -> tuple[str, str] | None` — `(kind, id)` for an untyped `affected_entities` value, ONLY if it resolves in the IR (device/vlan/port); else None
  - `def owner_device_nodes(kind: str, ent_id: str, baseline_ir: IR, proposed_ir: IR) -> list[str]` — endpoint/owner device node(s) for a port/link/l3intf/device cause; `[]` for vlan

These are the building blocks for both projection tasks; resolving against the IR (never by string shape) is the guard against MACs becoming bogus port entities.

- [ ] **Step 1: Write the failing test**

```python
# tests/viz/test_visual_map.py
from digital_twin.ir import IRBuilder
from digital_twin.ir.entities import (
    Device,
    DeviceRole,
    L3Intf,
    L3Role,
    Port,
    PortMode,
    Vlan,
)
from digital_twin.viz import visual_map as vm


def _baseline():
    b = IRBuilder()
    b.add_device(Device(id="s1", role=DeviceRole.SWITCH, site="site1"))
    b.add_device(Device(id="s2", role=DeviceRole.SWITCH, site="site1"))
    b.add_port(Port(id="s1:ge-0/0/1", device_id="s1", name="ge-0/0/1", mode=PortMode.ACCESS))
    b.add_vlan(Vlan(vlan_id=10, name="data"))
    b.add_l3intf(L3Intf(device_id="s1", role=L3Role.IRB, vlan_id=10))
    return b.build()


def test_mac_normalizes_mist_device_id():
    assert vm._mac("00000000-0000-0000-1000-aabb01") == "aabb01"
    assert vm._mac("s1") == "s1"


def test_node_resolves_only_real_devices():
    ir = _baseline()
    assert vm._node(ir, "s1") == "s1"
    assert vm._node(ir, "00000000-0000-0000-2000-s1") == "s1"  # gateway 2000 tag
    assert vm._node(ir, "not-a-device") is None


def test_resolve_affected_rejects_client_mac():
    ir = _baseline()
    # a colon-bearing MAC must NOT become a port-ish entity
    assert vm._resolve_affected("aa:bb:cc:dd:ee:ff", ir) is None
    assert vm._resolve_affected("s1", ir) == ("device", "s1")
    assert vm._resolve_affected("10", ir) == ("vlan", "10")
    assert vm._resolve_affected("s1:ge-0/0/1", ir) == ("port", "s1:ge-0/0/1")


def test_owner_device_nodes_for_port_link_l3intf():
    base = _baseline()
    prop = _baseline()
    assert vm.owner_device_nodes("port", "s1:ge-0/0/1", base, prop) == ["s1"]
    assert sorted(vm.owner_device_nodes("link", "s1:ge-0/0/1__s2:ge-0/0/2", base, prop)) == ["s1", "s2"]
    # l3intf owner resolves via BASELINE (works even if removed in proposed)
    iid = "s1:l3:irb:10"
    assert vm.owner_device_nodes("l3intf", iid, base, prop) == ["s1"]
    assert vm.owner_device_nodes("vlan", "10", base, prop) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/viz/test_visual_map.py -q`
Expected: FAIL with `ModuleNotFoundError: digital_twin.viz.visual_map`.

- [ ] **Step 3: Write the helpers**

```python
# src/digital_twin/viz/visual_map.py
"""Pure builder: (baseline_ir, proposed_ir, findings) -> VisualMap.

Keyed per rendered view so a VLAN-scoped finding can never paint another VLAN's
chart. Removed-entity OWNERSHIP resolves against baseline_ir; everything rendered
resolves against proposed_ir. decision.py never reads the result.
"""

from __future__ import annotations

from digital_twin.ir import IR
from digital_twin.ir.indexes import node_for, vc_root_map

_MIST_DEV_HEAD = "00000000-0000-0000-"


def _mac(device_id: str) -> str:
    parts = device_id.split("-")
    if len(parts) == 5 and device_id.startswith(_MIST_DEV_HEAD):
        return parts[-1]
    return device_id


def _node(ir: IR, raw: str) -> str | None:
    """VC-folded device node for `raw`, or None if it is not a device."""
    vc = vc_root_map(ir)
    m = _mac(raw)
    if m in ir.devices or node_for(vc, m) in ir.devices:
        return node_for(vc, m)
    return None


def _port_node(ir: IR, pid: str) -> str | None:
    return _node(ir, pid.split(":", 1)[0]) if ":" in pid else None


def _resolve_affected(ent: str, ir: IR) -> tuple[str, str] | None:
    """(kind, id) for an untyped affected_entities value — ONLY if it resolves in
    the IR. Never promote by string shape (a colon-bearing MAC stays unresolved)."""
    if _node(ir, ent) is not None:
        return ("device", _node(ir, ent) or ent)
    if ent.isdigit() and int(ent) in ir.vlans:
        return ("vlan", ent)
    if ent in ir.ports:
        return ("port", ent)
    return None


def owner_device_nodes(
    kind: str, ent_id: str, baseline_ir: IR, proposed_ir: IR
) -> list[str]:
    """Owner/endpoint device node(s) for a cause. l3intf owner comes from BASELINE
    (it may be removed in proposed). vlan causes own no device -> []."""
    if kind == "device":
        n = _node(proposed_ir, ent_id) or _node(baseline_ir, ent_id)
        return [n] if n else []
    if kind == "port":
        n = _port_node(proposed_ir, ent_id) or _port_node(baseline_ir, ent_id)
        return [n] if n else []
    if kind == "link":
        out: list[str] = []
        for pid in ent_id.split("__"):
            n = _port_node(proposed_ir, pid) or _port_node(baseline_ir, pid)
            if n and n not in out:
                out.append(n)
        return out
    if kind == "l3intf":
        for intf in baseline_ir.l3intfs:
            if intf.id == ent_id:
                n = node_for(vc_root_map(baseline_ir), intf.device_id)
                return [n]
        return []
    return []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/viz/test_visual_map.py -q`
Expected: PASS.

- [ ] **Step 5: Gate + commit**

```bash
uv run pytest tests -q && uv run ruff check . && uv run mypy src
git add src/digital_twin/viz/visual_map.py tests/viz/test_visual_map.py
git commit -m "feat(viz): entity resolution + owner-expansion helpers for VisualMap

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Per-view membership index

**Files:**
- Modify: `src/digital_twin/viz/visual_map.py`
- Test: `tests/viz/test_visual_map.py`

**Interfaces:**
- Consumes: `build_l2_graph`, `build_vlan_graph` from `digital_twin.representations.{l2_graph,vlan_graph}`; `L3Intf` from `digital_twin.ir.entities`.
- Produces:
  - `@dataclass class _ViewIndex` with `vlan_nodes: dict[int, set[str]]`, `routed_vlans: set[int]`, `intfs_by_vlan: dict[int, list[L3Intf]]`
  - `def _build_view_index(proposed_ir: IR) -> _ViewIndex`
  - `_ViewIndex.node_in_vlan(self, node: str, vid: int) -> bool`
  - `_ViewIndex.intfs_for_vlan(self, vid: int) -> list[L3Intf]`

This decides, against the PROPOSED IR (what the diagrams draw), whether a node is renderable on a VLAN view and which interfaces serve a referenced VLAN — the data the scoping rule needs.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/viz/test_visual_map.py
import networkx as nx

from digital_twin.ir.entities import Link


def _two_switch_vlan_ir():
    b = IRBuilder()
    b.add_device(Device(id="s1", role=DeviceRole.SWITCH, site="site1"))
    b.add_device(Device(id="s2", role=DeviceRole.SWITCH, site="site1"))
    # trunk between s1 and s2 carrying vlan 10; s3 isolated, only vlan 20
    b.add_device(Device(id="s3", role=DeviceRole.SWITCH, site="site1"))
    b.add_port(Port(id="s1:ge-0/0/0", device_id="s1", name="ge-0/0/0",
                    mode=PortMode.TRUNK, tagged_vlans=(10,)))
    b.add_port(Port(id="s2:ge-0/0/0", device_id="s2", name="ge-0/0/0",
                    mode=PortMode.TRUNK, tagged_vlans=(10,)))
    b.add_port(Port(id="s3:ge-0/0/1", device_id="s3", name="ge-0/0/1",
                    mode=PortMode.ACCESS, native_vlan=20))
    b.add_link(Link(a_port="s1:ge-0/0/0", b_port="s2:ge-0/0/0"))
    b.add_vlan(Vlan(vlan_id=10, name="data", subnet="10.0.10.0/24"))
    b.add_vlan(Vlan(vlan_id=20, name="voice"))
    b.add_l3intf(L3Intf(device_id="s1", role=L3Role.IRB, vlan_id=10))
    return b.build()


def test_view_index_vlan_membership_is_scoped():
    ir = _two_switch_vlan_ir()
    idx = vm._build_view_index(ir)
    assert idx.node_in_vlan("s1", 10) and idx.node_in_vlan("s2", 10)
    assert not idx.node_in_vlan("s3", 10)  # s3 is not in vlan 10's graph
    assert idx.node_in_vlan("s3", 20)


def test_view_index_routed_and_interfaces():
    ir = _two_switch_vlan_ir()
    idx = vm._build_view_index(ir)
    assert 10 in idx.routed_vlans  # has a subnet / IRB
    assert [i.vlan_id for i in idx.intfs_for_vlan(10)] == [10]
    assert idx.intfs_for_vlan(20) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/viz/test_visual_map.py -k view_index -q`
Expected: FAIL with `AttributeError: module ... has no attribute '_build_view_index'`.

- [ ] **Step 3: Implement the index**

Add to `src/digital_twin/viz/visual_map.py` (imports at top, `_ViewIndex` below the helpers):

```python
from dataclasses import dataclass, field

import networkx as nx

from digital_twin.ir.entities import L3Intf
from digital_twin.representations.l2_graph import build_l2_graph
from digital_twin.representations.vlan_graph import build_vlan_graph
```

```python
@dataclass
class _ViewIndex:
    vlan_nodes: dict[int, set[str]] = field(default_factory=dict)
    routed_vlans: set[int] = field(default_factory=set)
    intfs_by_vlan: dict[int, list[L3Intf]] = field(default_factory=dict)

    def node_in_vlan(self, node: str, vid: int) -> bool:
        return node in self.vlan_nodes.get(vid, set())

    def intfs_for_vlan(self, vid: int) -> list[L3Intf]:
        return self.intfs_by_vlan.get(vid, [])


def _build_view_index(proposed_ir: IR) -> _ViewIndex:
    idx = _ViewIndex()
    l2 = build_l2_graph(proposed_ir)
    for vid in proposed_ir.vlans:
        g: nx.MultiGraph = build_vlan_graph(proposed_ir, l2, vid)
        idx.vlan_nodes[vid] = set(g.nodes)
    for intf in proposed_ir.l3intfs:
        if intf.vlan_id is not None:
            idx.intfs_by_vlan.setdefault(intf.vlan_id, []).append(intf)
    # routed == has a subnet OR is served by an l3 interface (mirrors _l3_exits_diagram)
    idx.routed_vlans = {
        vid for vid, v in proposed_ir.vlans.items() if v.subnet is not None
    } | set(idx.intfs_by_vlan)
    return idx
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/viz/test_visual_map.py -k view_index -q`
Expected: PASS. (If `node_in_vlan("s3", 10)` is unexpectedly True, confirm `build_vlan_graph` excludes non-carrying nodes — it should; do not weaken the assertion.)

- [ ] **Step 5: Gate + commit**

```bash
uv run pytest tests -q && uv run ruff check . && uv run mypy src
git add src/digital_twin/viz/visual_map.py tests/viz/test_visual_map.py
git commit -m "feat(viz): per-view membership index (vlan graphs, routed vlans, interfaces)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Affected-side projection

**Files:**
- Modify: `src/digital_twin/viz/visual_map.py`
- Test: `tests/viz/test_visual_map.py`

**Interfaces:**
- Consumes: `_ViewIndex`, `_node`, `_port_node`, `_resolve_affected`; `Finding`, `FindingRef`, `VisualTier` from contracts.
- Produces:
  - `@dataclass(frozen=True) class _Contribution(view: str, kind: str, id: str, tier: VisualTier, ref: FindingRef)`
  - `def _affected_contributions(f: Finding, index: int, proposed_ir: IR, idx: _ViewIndex) -> list[_Contribution]`

Implements the AFFECTED tier of the scoping rule: referenced VLANs (subject/evidence/impacts), referenced nodes/ports (IR-resolved), the **paired-array** rule for `impacts[]` (each `attachment` → only its own `vlan`), and view scoping (`l2` for any node; `vlan:<vid>` only for in-graph nodes; `l3_exits` only for interfaces serving referenced VLANs). A finding with no referenced VLAN does not touch any `vlan:` or `l3_exits` view.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/viz/test_visual_map.py
from digital_twin.contracts import (
    Finding,
    FindingCategory,
    FindingSource,
    ObjectRef,
    Severity,
)
from digital_twin.ir import Confidence, ConfidenceLevel

_HIGH = Confidence(level=ConfidenceLevel.HIGH)


def _f(**kw):
    base = dict(source=FindingSource.CHECK, category=FindingCategory.NETWORK,
               code="t.x", severity=Severity.WARNING, confidence=_HIGH, message="m")
    return Finding(**{**base, **kw})


def _views(contribs, view):
    return {(c.kind, c.id) for c in contribs if c.view == view}


def test_affected_vlan_scoped_finding_does_not_touch_other_vlans():
    ir = _two_switch_vlan_ir()
    idx = vm._build_view_index(ir)
    # blackhole on vlan 10, component nodes s1,s2
    f = _f(subject=ObjectRef("vlan", "10"),
           evidence={"vlan": 10, "component_nodes": ["s1", "s2"]})
    cs = vm._affected_contributions(f, 0, ir, idx)
    assert ("device", "s1") in _views(cs, "vlan:10")
    assert ("vlan", "10") in _views(cs, "vlan:10")
    assert _views(cs, "vlan:20") == set()  # never touches vlan 20
    assert ("device", "s1") in _views(cs, "l2")  # l2 carries the nodes


def test_affected_no_vlan_finding_is_l2_only():
    ir = _two_switch_vlan_ir()
    idx = vm._build_view_index(ir)
    # isolation.severed: device subject, fragment nodes, NO vlan
    f = _f(subject=ObjectRef("device", "s1"),
           evidence={"fragment_nodes": ["s1", "s2"]}, affected_entities=("s1", "s2"))
    cs = vm._affected_contributions(f, 0, ir, idx)
    assert _views(cs, "l2") == {("device", "s1"), ("device", "s2")}
    assert all(not c.view.startswith("vlan:") for c in cs)
    assert all(c.view != "l3_exits" for c in cs)


def test_affected_paired_impacts_do_not_cross_product():
    ir = _two_switch_vlan_ir()
    idx = vm._build_view_index(ir)
    # client impact: vlan 10 client on s1, vlan 20 client on s3 (distinct nodes).
    # s1 exists in vlan10 graph; s3 in vlan20 graph. A cross-product bug would
    # paint s1 on vlan20 / s3 on vlan10.
    f = _f(code="wired.client.impact.active_clients",
           affected_entities=("aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"),
           evidence={"impacts": [
               {"mac": "aa:bb:cc:dd:ee:01", "vlan": 10, "attachment": "s1:ge-0/0/0"},
               {"mac": "aa:bb:cc:dd:ee:02", "vlan": 20, "attachment": "s3:ge-0/0/1"},
           ]})
    cs = vm._affected_contributions(f, 0, ir, idx)
    assert ("device", "s1") in _views(cs, "vlan:10")
    assert ("device", "s3") in _views(cs, "vlan:20")
    assert ("device", "s3") not in _views(cs, "vlan:10")
    assert ("device", "s1") not in _views(cs, "vlan:20")
    # the client MAC must NOT have resolved to any entity
    assert all(c.kind != "port" or c.id != "aa:bb:cc:dd:ee:01" for c in cs)


def test_affected_l3_exits_only_serving_interfaces():
    ir = _two_switch_vlan_ir()
    idx = vm._build_view_index(ir)
    f = _f(subject=ObjectRef("vlan", "10"), evidence={"vlan": 10, "component_nodes": ["s1"]})
    cs = vm._affected_contributions(f, 0, ir, idx)
    l3 = _views(cs, "l3_exits")
    assert ("vlan", "10") in l3
    assert ("intf", "s1:l3:irb:10") in l3  # serves vlan 10
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/viz/test_visual_map.py -k affected -q`
Expected: FAIL with `AttributeError: ... '_affected_contributions'`.

- [ ] **Step 3: Implement affected-side projection**

Add to `src/digital_twin/viz/visual_map.py`:

```python
from typing import Any

from digital_twin.contracts import (
    Finding,
    FindingRef,
    ObjectRef,
    VisualTier,
)


@dataclass(frozen=True)
class _Contribution:
    view: str
    kind: str
    id: str
    tier: VisualTier
    ref: FindingRef


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


_NODE_EV_KEYS = ("component_nodes", "fragment_nodes", "baseline_root", "proposed_root")
_PORT_EV_KEYS = ("port", "new_member_ports")


def _ref(f: Finding, index: int) -> FindingRef:
    return FindingRef(index=index, code=f.code, subject=f.subject)


def _affected_contributions(
    f: Finding, index: int, proposed_ir: IR, idx: _ViewIndex
) -> list[_Contribution]:
    ref = _ref(f, index)
    out: list[_Contribution] = []

    def add(view: str, kind: str, ent_id: str) -> None:
        out.append(_Contribution(view, kind, ent_id, VisualTier.AFFECTED, ref))

    # ----- finding-wide scalar references -----
    vlans: set[int] = set()
    nodes: set[str] = set()  # device node ids
    if f.subject is not None:
        if f.subject.kind == "vlan" and f.subject.id.isdigit():
            vlans.add(int(f.subject.id))
        elif f.subject.kind == "device":
            n = _node(proposed_ir, f.subject.id)
            if n:
                nodes.add(n)
        elif f.subject.kind == "port":
            n = _port_node(proposed_ir, f.subject.id)
            if n:
                nodes.add(n)
        elif f.subject.kind == "link":
            for pid in f.subject.id.split("__"):
                n = _port_node(proposed_ir, pid)
                if n:
                    nodes.add(n)
    ev: Any = f.evidence
    vlans.update(_ints(ev.get("vlan")) + _ints(ev.get("affected_vlans")))
    for k in _NODE_EV_KEYS:
        for did in _strs(ev.get(k)):
            n = _node(proposed_ir, did)
            if n:
                nodes.add(n)
    for k in _PORT_EV_KEYS:
        for pid in _strs(ev.get(k)):
            n = _port_node(proposed_ir, pid)
            if n:
                nodes.add(n)
    for ent in f.affected_entities:
        resolved = _resolve_affected(ent, proposed_ir)
        if resolved is None:
            continue
        rk, rid = resolved
        if rk == "device":
            nodes.add(rid)
        elif rk == "vlan":
            vlans.add(int(rid))
        elif rk == "port":
            n = _port_node(proposed_ir, rid)
            if n:
                nodes.add(n)

    # l2: every referenced node
    for n in nodes:
        add("l2", "device", n)
    # vlan:<vid>: nodes that exist in that vlan's graph + the vlan box; l3_exits
    for vid in vlans:
        add(f"vlan:{vid}", "vlan", str(vid))
        for n in nodes:
            if idx.node_in_vlan(n, vid):
                add(f"vlan:{vid}", "device", n)
        if vid in idx.routed_vlans:
            add("l3_exits", "vlan", str(vid))
            for intf in idx.intfs_for_vlan(vid):
                if intf.device_id and _node(proposed_ir, intf.device_id):
                    add("l3_exits", "intf", intf.id)

    # ----- paired arrays (impacts[]): attachment pairs ONLY with its own vlan -----
    for imp in ev.get("impacts") or ():
        if not isinstance(imp, dict):
            continue
        att = imp.get("attachment")
        ivid = imp.get("vlan")
        att_node = (
            _port_node(proposed_ir, att) or _node(proposed_ir, att)
            if isinstance(att, str) else None
        )
        if att_node:
            add("l2", "device", att_node)
        if isinstance(ivid, int):
            add(f"vlan:{ivid}", "vlan", str(ivid))
            if att_node and idx.node_in_vlan(att_node, ivid):
                add(f"vlan:{ivid}", "device", att_node)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/viz/test_visual_map.py -k affected -q`
Expected: PASS.

- [ ] **Step 5: Gate + commit**

```bash
uv run pytest tests -q && uv run ruff check . && uv run mypy src
git add src/digital_twin/viz/visual_map.py tests/viz/test_visual_map.py
git commit -m "feat(viz): affected-side per-view projection with paired-array scoping

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Origin-side projection

**Files:**
- Modify: `src/digital_twin/viz/visual_map.py`
- Test: `tests/viz/test_visual_map.py`

**Interfaces:**
- Consumes: `owner_device_nodes`, `_ViewIndex`, `_Contribution`, `_ref`.
- Produces:
  - `def _origin_contributions(f: Finding, index: int, baseline_ir: IR, proposed_ir: IR, idx: _ViewIndex) -> list[_Contribution]`

Implements the ORIGIN tier: `caused_by` → owner device(s) (port/link/l3intf expansion via Task 2), projected onto `l2` always and onto a referenced VLAN view only if the owner participates in that VLAN's proposed graph (participation caveat). For `client.impact`, per-impact `caused_by` pairs with that impact's VLAN — NOT the finding-wide union. The interface self-entry on `l3_exits` is emitted only when the interface resolves in proposed IR.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/viz/test_visual_map.py
from digital_twin.contracts import Cause


def test_origin_port_cause_surfaces_owner_device_on_l2_and_vlan():
    ir = _two_switch_vlan_ir()
    idx = vm._build_view_index(ir)
    f = _f(subject=ObjectRef("vlan", "10"), evidence={"vlan": 10, "component_nodes": ["s2"]},
           caused_by=(Cause(ref=ObjectRef("port", "s1:ge-0/0/0"), fields=("disabled",)),))
    cs = vm._origin_contributions(f, 0, ir, ir, idx)
    assert any(c.view == "l2" and c.kind == "device" and c.id == "s1"
               and c.tier is vm.VisualTier.ORIGIN for c in cs)
    # s1 participates in vlan 10 -> origin shows on vlan:10 too
    assert any(c.view == "vlan:10" and c.id == "s1" and c.tier is vm.VisualTier.ORIGIN
               for c in cs)


def test_origin_removed_l3intf_falls_back_to_owner_on_l2_only():
    base = _two_switch_vlan_ir()
    # proposed: the IRB on s1 for vlan 10 is REMOVED, and s1 no longer carries vlan 10
    pb = IRBuilder()
    pb.add_device(Device(id="s1", role=DeviceRole.SWITCH, site="site1"))
    pb.add_device(Device(id="s2", role=DeviceRole.SWITCH, site="site1"))
    pb.add_device(Device(id="s3", role=DeviceRole.SWITCH, site="site1"))
    pb.add_vlan(Vlan(vlan_id=10, name="data", subnet="10.0.10.0/24"))
    pb.add_vlan(Vlan(vlan_id=20, name="voice"))
    proposed = pb.build()
    idx = vm._build_view_index(proposed)
    f = _f(subject=ObjectRef("vlan", "10"), evidence={"vlan": 10},
           caused_by=(Cause(ref=ObjectRef("l3intf", "s1:l3:irb:10"), fields=()),))
    cs = vm._origin_contributions(f, 0, base, proposed, idx)
    assert any(c.view == "l2" and c.id == "s1" and c.tier is vm.VisualTier.ORIGIN for c in cs)
    # s1 no longer participates in vlan 10's proposed graph -> no forced vlan origin
    assert not any(c.view == "vlan:10" and c.kind == "device" for c in cs)
    # and no dangling intf self-entry (the interface is gone from proposed)
    assert not any(c.kind == "intf" for c in cs)


def test_origin_per_impact_cause_pairs_with_its_vlan():
    ir = _two_switch_vlan_ir()
    idx = vm._build_view_index(ir)
    f = _f(code="wired.client.impact.active_clients",
           caused_by=(Cause(ref=ObjectRef("port", "s1:ge-0/0/0")),
                      Cause(ref=ObjectRef("port", "s3:ge-0/0/1"))),
           evidence={"impacts": [
               {"mac": "m1", "vlan": 10, "attachment": "s1:ge-0/0/0",
                "caused_by": [Cause(ref=ObjectRef("port", "s1:ge-0/0/0"))]},
               {"mac": "m2", "vlan": 20, "attachment": "s3:ge-0/0/1",
                "caused_by": [Cause(ref=ObjectRef("port", "s3:ge-0/0/1"))]},
           ]})
    cs = vm._origin_contributions(f, 0, ir, ir, idx)
    # s3's cause (vlan 20) must NOT appear as origin on vlan:10
    assert not any(c.view == "vlan:10" and c.id == "s3" for c in cs)
    assert any(c.view == "vlan:20" and c.id == "s3" and c.tier is vm.VisualTier.ORIGIN
               for c in cs)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/viz/test_visual_map.py -k origin -q`
Expected: FAIL with `AttributeError: ... '_origin_contributions'`.

- [ ] **Step 3: Implement origin-side projection**

Add to `src/digital_twin/viz/visual_map.py`:

```python
def _origin_owner_views(
    owner: str, vlans: set[int], proposed_ir: IR, idx: _ViewIndex, ref: FindingRef
) -> list[_Contribution]:
    """An owner device projects onto l2 always, and onto each referenced vlan view
    ONLY if it still participates in that vlan's proposed graph (no phantom nodes)."""
    cs = [_Contribution("l2", "device", owner, VisualTier.ORIGIN, ref)]
    for vid in vlans:
        if idx.node_in_vlan(owner, vid):
            cs.append(_Contribution(f"vlan:{vid}", "device", owner, VisualTier.ORIGIN, ref))
    return cs


def _origin_contributions(
    f: Finding, index: int, baseline_ir: IR, proposed_ir: IR, idx: _ViewIndex
) -> list[_Contribution]:
    ref = _ref(f, index)
    out: list[_Contribution] = []

    def emit_cause(cause_ref: ObjectRef, vlans: set[int]) -> None:
        # the interface's own self-entry only when it resolves in proposed IR
        if cause_ref.kind == "l3intf" and any(
            i.id == cause_ref.id for i in proposed_ir.l3intfs
        ):
            for vid in vlans:
                if vid in idx.routed_vlans:
                    out.append(_Contribution("l3_exits", "intf", cause_ref.id,
                                             VisualTier.ORIGIN, ref))
        for owner in owner_device_nodes(cause_ref.kind, cause_ref.id, baseline_ir, proposed_ir):
            out.extend(_origin_owner_views(owner, vlans, proposed_ir, idx, ref))

    ev: Any = f.evidence
    impacts = ev.get("impacts") or ()
    has_paired = any(isinstance(i, dict) and "caused_by" in i for i in impacts)

    if has_paired:
        # per-impact causes pair with their own vlan (no finding-wide union)
        for imp in impacts:
            if not isinstance(imp, dict):
                continue
            ivid = imp.get("vlan")
            vlans = {ivid} if isinstance(ivid, int) else set()
            for c in imp.get("caused_by") or ():
                if isinstance(c.ref, ObjectRef):
                    emit_cause(c.ref, vlans)
    else:
        # finding-wide caused_by + the finding's referenced vlans
        vlans = set()
        if f.subject is not None and f.subject.kind == "vlan" and f.subject.id.isdigit():
            vlans.add(int(f.subject.id))
        vlans.update(_ints(ev.get("vlan")) + _ints(ev.get("affected_vlans")))
        for c in f.caused_by:
            emit_cause(c.ref, vlans)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/viz/test_visual_map.py -k origin -q`
Expected: PASS.

- [ ] **Step 5: Gate + commit**

```bash
uv run pytest tests -q && uv run ruff check . && uv run mypy src
git add src/digital_twin/viz/visual_map.py tests/viz/test_visual_map.py
git commit -m "feat(viz): origin-side projection (owner expansion, participation caveat, per-impact pairing)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Reconciliation — `build_visual_map`

**Files:**
- Modify: `src/digital_twin/viz/visual_map.py`
- Test: `tests/viz/test_visual_map.py`

**Interfaces:**
- Consumes: `_affected_contributions`, `_origin_contributions`, `_build_view_index`, `entity_key`, `VisualEntry`, `VisualTier`, `Severity`.
- Produces: `def build_visual_map(baseline_ir: IR, proposed_ir: IR, findings: Sequence[Finding]) -> VisualMap`

Merges all contributions into the map. Per `(view, entity)`: tier precedence `ORIGIN > AFFECTED`; severity worst-wins (independent of tier); `findings` deduped by `index`, sorted by `index`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/viz/test_visual_map.py
from digital_twin.contracts import VisualTier as Tier


def test_build_map_origin_beats_affected_severity_orthogonal():
    ir = _two_switch_vlan_ir()
    # one finding makes s1 affected (warning); a second makes s1 origin (info-severity)
    affected = _f(severity=Severity.WARNING, affected_entities=("s1",))
    origin = _f(severity=Severity.INFO, subject=ObjectRef("vlan", "10"),
                evidence={"vlan": 10},
                caused_by=(Cause(ref=ObjectRef("device", "s1")),))
    m = vm.build_visual_map(ir, ir, (affected, origin))
    e = m["l2"]["device:s1"]
    assert e.tier is Tier.ORIGIN          # origin wins
    assert e.severity is Severity.WARNING  # severity worst-wins, independent of tier
    assert {r.index for r in e.findings} == {0, 1}


def test_build_map_headline_bleed_regression():
    ir = _two_switch_vlan_ir()
    # blackhole on vlan 10 hitting s1; vlan 20 and untouched vlans must stay clean
    f = _f(subject=ObjectRef("vlan", "10"),
           evidence={"vlan": 10, "component_nodes": ["s1", "s2"]})
    m = vm.build_visual_map(ir, ir, (f,))
    assert "device:s1" in m.get("vlan:10", {})
    assert "device:s1" not in m.get("vlan:20", {})  # THE FIX
    assert "vlan:20" not in m or all(k.startswith("vlan:") is False for k in [])  # no vlan20 paint


def test_build_map_serializable_entry_shape():
    ir = _two_switch_vlan_ir()
    f = _f(affected_entities=("s1",))
    m = vm.build_visual_map(ir, ir, (f,))
    e = m["l2"]["device:s1"]
    assert (e.kind, e.id) == ("device", "s1")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/viz/test_visual_map.py -k build_map -q`
Expected: FAIL with `AttributeError: ... 'build_visual_map'`.

- [ ] **Step 3: Implement reconciliation**

Add to `src/digital_twin/viz/visual_map.py`:

```python
from collections.abc import Sequence

from digital_twin.contracts import Severity, VisualEntry, VisualMap, entity_key

_SEV_RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.ERROR: 2, Severity.CRITICAL: 3}
_TIER_RANK = {VisualTier.AFFECTED: 0, VisualTier.ORIGIN: 1}  # higher = more foreground


def build_visual_map(
    baseline_ir: IR, proposed_ir: IR, findings: Sequence[Finding]
) -> VisualMap:
    idx = _build_view_index(proposed_ir)
    contribs: list[_Contribution] = []
    for i, f in enumerate(findings):
        contribs.extend(_affected_contributions(f, i, proposed_ir, idx))
        contribs.extend(_origin_contributions(f, i, baseline_ir, proposed_ir, idx))

    # accumulate per (view, entity_key)
    acc: dict[str, dict[str, dict[str, object]]] = {}
    for c in contribs:
        key = entity_key(c.kind, c.id)
        view = acc.setdefault(c.view, {})
        cell = view.get(key)
        if cell is None:
            view[key] = {"kind": c.kind, "id": c.id, "tier": c.tier,
                         "sev_rank": -1, "sev": None, "refs": {}}
            cell = view[key]
        if _TIER_RANK[c.tier] > _TIER_RANK[cell["tier"]]:  # type: ignore[index]
            cell["tier"] = c.tier
        # severity from the finding the ref points at
        f = findings[c.ref.index]
        if _SEV_RANK[f.severity] > cell["sev_rank"]:  # type: ignore[operator]
            cell["sev_rank"] = _SEV_RANK[f.severity]
            cell["sev"] = f.severity
        cell["refs"][c.ref.index] = c.ref  # type: ignore[index]

    out: VisualMap = {}
    for view, cells in acc.items():
        out[view] = {}
        for key, cell in cells.items():
            refs = tuple(cell["refs"][i] for i in sorted(cell["refs"]))  # type: ignore[index]
            out[view][key] = VisualEntry(
                kind=cell["kind"], id=cell["id"], tier=cell["tier"],  # type: ignore[arg-type]
                severity=cell["sev"], findings=refs,  # type: ignore[arg-type]
            )
    return out
```

> Note: the `dict[str, object]` accumulator avoids a second dataclass; the `# type: ignore` lines are localized to the accumulation. If the reviewer prefers, replace the inner dict with a small mutable `@dataclass class _Cell` — functionally identical. Keep whichever passes `mypy src` cleanly.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/viz/test_visual_map.py -q`
Expected: PASS (all builder tests).

- [ ] **Step 5: Gate + commit**

```bash
uv run pytest tests -q && uv run ruff check . && uv run mypy src
git add src/digital_twin/viz/visual_map.py tests/viz/test_visual_map.py
git commit -m "feat(viz): build_visual_map reconciliation (tier precedence, worst severity, bleed regression)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Verdict field + pipeline wiring + serialization + invariance

**Files:**
- Modify: `src/digital_twin/verdict/verdict.py`
- Modify: `src/digital_twin/engine/pipeline.py:273`
- Test: `tests/verdict/test_visual_map_wiring.py`

**Interfaces:**
- Consumes: `build_visual_map`, `VisualMap`.
- Produces: `Verdict.visual_map: VisualMap` (default `{}` via `field(default_factory=dict)`).

- [ ] **Step 1: Write the failing test**

```python
# tests/verdict/test_visual_map_wiring.py
import dataclasses

from digital_twin.contracts import (
    Finding,
    FindingCategory,
    FindingSource,
    ObjectRef,
    Severity,
)
from digital_twin.drivers.render import verdict_to_dict
from digital_twin.ir import Confidence, ConfidenceLevel
from digital_twin.verdict.verdict import Verdict

_HIGH = Confidence(level=ConfidenceLevel.HIGH)


def _verdict_with_map():
    from digital_twin.contracts import VisualEntry, VisualTier
    f = Finding(source=FindingSource.CHECK, category=FindingCategory.NETWORK,
                code="t.x", severity=Severity.WARNING, confidence=_HIGH, message="m",
                subject=ObjectRef("device", "s1"))
    vmap = {"l2": {"device:s1": VisualEntry(
        kind="device", id="s1", tier=VisualTier.AFFECTED,
        severity=Severity.WARNING, findings=())}}
    # minimal Verdict — only fields needed for the test
    return Verdict(
        decision=__import__("digital_twin.verdict.decision", fromlist=["Decision"]).Decision.REVIEW,
        decision_reasons=(), overall_severity=Severity.WARNING, findings=(f,),
        check_results=(), coverage={}, confidence_summary=None, ir_diff=None,
        visual_map=vmap,
    )


def test_verdict_has_visual_map_field_default_empty():
    names = {f.name for f in dataclasses.fields(Verdict)}
    assert "visual_map" in names


def test_visual_map_serializes_to_nested_kind_id_shape():
    v = _verdict_with_map()
    d = verdict_to_dict(v)
    entry = d["visual_map"]["l2"]["device:s1"]
    assert entry["kind"] == "device" and entry["id"] == "s1"
    assert entry["tier"] == "affected" and entry["severity"] == "warning"
    assert entry["findings"] == []
```

> If `confidence_summary=None`/`ir_diff=None` trip validation, build the Verdict via `assemble(...)` with an empty `DecisionInputs` instead and then `dataclasses.replace(v, visual_map=vmap)` — mirror an existing verdict test in `tests/verdict/` for the exact minimal construction.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/verdict/test_visual_map_wiring.py -q`
Expected: FAIL — `visual_map` is not a field of `Verdict`.

- [ ] **Step 3: Add the field**

In `src/digital_twin/verdict/verdict.py`: import `VisualMap` (add to the existing `from digital_twin.contracts import ...` line) and add the field to the `Verdict` dataclass after `config_diffs`:

```python
from dataclasses import dataclass, field
```
```python
    config_diffs: tuple[ObjectConfigDiff, ...] = ()  # raw before→after (non-load-bearing)
    visual_map: VisualMap = field(default_factory=dict)  # presentational; decision.py ignores it
```

(`assemble()` does not set it — the pipeline attaches it via `replace`, exactly like `diagrams`.)

- [ ] **Step 4: Wire the pipeline (dual IR)**

In `src/digital_twin/engine/pipeline.py`, add the import near the existing mermaid import:

```python
from digital_twin.viz.visual_map import build_visual_map
```

Replace line 273:

```python
        return replace(verdict, diagrams=safe_build_diagrams(proposed.ir, verdict.findings))
```

with:

```python
        return replace(
            verdict,
            diagrams=safe_build_diagrams(baseline.ir, proposed.ir, verdict.findings),
            visual_map=build_visual_map(baseline.ir, proposed.ir, verdict.findings),
        )
```

> `safe_build_diagrams` becomes dual-IR in Task 8; until then this line will not type-check. To keep Task 7 independently green, in THIS task pass only the map and leave the diagrams call unchanged: `diagrams=safe_build_diagrams(proposed.ir, verdict.findings)`, add only `visual_map=build_visual_map(baseline.ir, proposed.ir, verdict.findings)`. Task 8 then upgrades the diagrams call. (Confirm `baseline` is in scope at line 273 — it is the same `baseline` used to build the diff earlier in the function; if the local is named differently, use that name.)

- [ ] **Step 5: Verdict-invariance test**

```python
# add to tests/verdict/test_visual_map_wiring.py
def test_visual_map_does_not_affect_decision_or_severity():
    # building the map is pure: a verdict's decision/severity are identical
    # whether or not visual_map is populated.
    v = _verdict_with_map()
    bare = dataclasses.replace(v, visual_map={})
    assert v.decision == bare.decision
    assert v.overall_severity == bare.overall_severity
    assert [f.severity for f in v.findings] == [f.severity for f in bare.findings]
```

- [ ] **Step 6: Run tests + gate**

Run: `uv run pytest tests/verdict/test_visual_map_wiring.py -q && uv run pytest tests -q && uv run ruff check . && uv run mypy src`
Expected: PASS; mypy clean.

- [ ] **Step 7: Commit**

```bash
git add src/digital_twin/verdict/verdict.py src/digital_twin/engine/pipeline.py tests/verdict/test_visual_map_wiring.py
git commit -m "feat(verdict): add Verdict.visual_map, populate in pipeline (dual IR), serialize, invariance test

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Refactor Mermaid to render from the map; delete `highlight.py`

**Files:**
- Modify: `src/digital_twin/viz/mermaid.py`
- Modify: `src/digital_twin/engine/pipeline.py:273` (diagrams call → dual IR)
- Delete: `src/digital_twin/viz/highlight.py`
- Delete: `tests/viz/test_highlight.py`
- Modify: `tests/viz/test_mermaid.py`

**Interfaces:**
- Consumes: `build_visual_map`, `VisualMap`, `VisualEntry`, `VisualTier`.
- Produces: `def build_diagrams(baseline_ir: IR, proposed_ir: IR, findings: tuple[Finding, ...]) -> tuple[Diagram, ...]` and `def safe_build_diagrams(baseline_ir, proposed_ir, findings) -> tuple[Diagram, ...]`.

The map is the single mechanism: each chart paints classes by querying its view sub-map. `origin` gets a distinct classDef; `affected` colors by severity. Captions/notes are resolved from `findings[ref.index]`. `unlocalized` = findings whose index never appears in any view.

- [ ] **Step 1: Write the failing test (bleed regression at the diagram level)**

Replace the body of `tests/viz/test_mermaid.py`'s construction helpers to call the new signature, and add:

```python
# tests/viz/test_mermaid.py (new bleed test; keep/adapt existing tests to new signature)
def test_vlan_chart_does_not_inherit_other_vlans_node_hits():
    ir = _two_switch_vlan_ir_for_mermaid()  # s1 in vlan10; also exists in vlan20 graph
    # a finding scoped to vlan 10 hitting s1
    f = _f(subject=ObjectRef("vlan", "10"),
           evidence={"vlan": 10, "component_nodes": ["s1"]})
    diagrams = build_diagrams(ir, ir, (f,))
    v20 = next(d for d in diagrams if d.view == "vlan:20")
    # s1 must NOT be classed on the vlan:20 chart
    assert " class " not in v20.mermaid or "warn" not in v20.mermaid.split("class", 1)[1]
    v10 = next(d for d in diagrams if d.view == "vlan:10")
    assert "warn" in v10.mermaid or "origin" in v10.mermaid
```

> Build `_two_switch_vlan_ir_for_mermaid()` so that s1 appears in BOTH vlan 10 and vlan 20 graphs (e.g. a trunk carrying 10 and 20 between s1 and s2, plus an access port for each VLAN), so a bleed bug would actually class s1 on vlan:20. Mirror `_two_switch_vlan_ir()` from `test_visual_map.py`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/viz/test_mermaid.py -k inherit -q`
Expected: FAIL — `build_diagrams()` takes 2 positional args / wrong signature, or s1 is classed on vlan:20.

- [ ] **Step 3: Refactor `mermaid.py`**

Replace the `highlight` import and the per-diagram painting. Key changes:

```python
# top of mermaid.py
from digital_twin.contracts import Diagram, Finding, Severity, VisualEntry, VisualMap, VisualTier
from digital_twin.viz.visual_map import build_visual_map
```

Add an `origin` classDef and a class selector:

```python
_CLASSDEFS = (
    "  classDef crit fill:#fdd,stroke:#c00,stroke-width:2px;",
    "  classDef warn fill:#fff3cd,stroke:#e0a800;",
    "  classDef info fill:#eef,stroke:#88a;",
    "  classDef origin fill:#fff,stroke:#06c,stroke-width:3px,stroke-dasharray:5 3;",
)
_SEV_CLASS = {
    Severity.CRITICAL: "crit", Severity.ERROR: "crit",
    Severity.WARNING: "warn", Severity.INFO: "info",
}
_SEV_RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.ERROR: 2, Severity.CRITICAL: 3}


def _class_for(entry: VisualEntry) -> str:
    return "origin" if entry.tier is VisualTier.ORIGIN else _SEV_CLASS[entry.severity]
```

Rework each `_*_diagram` to take `view_map: dict[str, VisualEntry]` (the sub-map for that view) and `findings` (for note text) instead of `hl: Highlight`. For a node drawn with raw id `raw_id`, look up `view_map.get(f"device:{raw_id}")`; if present, emit `f"  class {ids.get(raw_id)} {_class_for(entry)};"` and a caption from `findings[ref.index]` for each ref. For the vlan box, look up `view_map.get(f"vlan:{vid}")`; for interfaces, `view_map.get(f"intf:{ikey_id}")`. Compute `Diagram.severity` as the worst `entry.severity` among the view's entries that are actually on the chart.

`build_diagrams` becomes:

```python
def build_diagrams(
    baseline_ir: IR, proposed_ir: IR, findings: tuple[Finding, ...]
) -> tuple[Diagram, ...]:
    vmap: VisualMap = build_visual_map(baseline_ir, proposed_ir, findings)
    l2 = build_l2_graph(proposed_ir)
    out: list[Diagram] = [_l2_diagram(proposed_ir, vmap.get("l2", {}), findings)]
    vlan_diagrams = [
        _vlan_diagram(proposed_ir, l2, vid, vmap.get(f"vlan:{vid}", {}), findings)
        for vid in sorted(proposed_ir.vlans)
    ]
    # ... existing ordering by severity ...
    out += vlan_diagrams
    out.append(_l3_exits_diagram(proposed_ir, vmap.get("l3_exits", {}), findings))
    return tuple(out)


def safe_build_diagrams(
    baseline_ir: IR, proposed_ir: IR, findings: tuple[Finding, ...]
) -> tuple[Diagram, ...]:
    try:
        return build_diagrams(baseline_ir, proposed_ir, findings)
    except Exception:  # noqa: BLE001 — diagrams are presentational; never sink a verdict
        return ()
```

Note caption helper (replaces `_class_lines`): for a view sub-map, build caption strings as `f"{f.severity.value}: {f.code}: {f.message}"` from `findings[ref.index]` for each entry's refs (dedup), plus the existing cause lines derived directly from each finding's `caused_by`, plus `unlocalized`.

`unlocalized`: count findings whose `index` does not appear in ANY entry across the whole `vmap`. Compute once in `build_diagrams` and thread the count to each diagram's `notes` exactly as before.

- [ ] **Step 4: Update the diagrams call in the pipeline and the existing mermaid tests**

In `src/digital_twin/engine/pipeline.py:273`, finish the upgrade started in Task 7:

```python
            diagrams=safe_build_diagrams(baseline.ir, proposed.ir, verdict.findings),
```

In `tests/viz/test_mermaid.py`, mechanically update every `build_diagrams(_ir(), ...)` / `safe_build_diagrams(_ir(), ...)` call to pass the IR twice: `build_diagrams(_ir(), _ir(), ...)`. The embedded doctest-style strings near the bottom of the file (lines ~180, ~208) must be updated to the new signature too. Delete any assertions that reach into `highlight` internals.

- [ ] **Step 5: Delete `highlight.py` and its test**

```bash
git rm src/digital_twin/viz/highlight.py tests/viz/test_highlight.py
```

Grep to confirm nothing else imports it:

Run: `grep -rn "viz.highlight\|build_highlight\|from .highlight\|import Highlight" src tests`
Expected: no matches. (If `viz/markdown.py` or anything references it, update to the map or remove the dead reference.)

- [ ] **Step 6: Run tests + gate**

Run: `uv run pytest tests -q && uv run ruff check . && uv run mypy src`
Expected: PASS; mypy clean. Investigate any `tests/golden` failures — diagram `notes`/`severity` strings may shift; update goldens that legitimately changed (the bleed fix MEANS some previously-amber VLAN charts are now clean — that is the intended diff, not a regression).

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor(viz): render diagrams from VisualMap, delete highlight.py (single mechanism)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Worked-example golden + docs + full gate

**Files:**
- Modify: `tests/golden/` (add a scenario asserting the map shape on a disabled-uplink delta)
- Modify: `docs/ROADMAP.md`
- Modify: memory note (optional, outside repo) — skip in this task.

**Interfaces:**
- Consumes: the public simulate path / an existing golden harness. Find the existing golden builder pattern in `tests/golden/builders.py` and `tests/golden/test_golden_scenarios.py` and mirror it.

- [ ] **Step 1: Write the failing golden test**

Add a scenario that disables an uplink port and asserts on `verdict.visual_map`:

```python
# tests/golden/test_golden_scenarios.py (add)
def test_visual_map_origin_distinct_and_no_unrelated_vlan_paint(golden_env):
    verdict = golden_env.simulate(disabled_uplink_doc(), disable_uplink_op())
    vmap = verdict.visual_map
    # the disabled port's device is an ORIGIN on l2
    l2 = vmap["l2"]
    assert any(e.tier.value == "origin" for e in l2.values())
    # a VLAN not carried across the cut has no entry
    assert "vlan:999" not in vmap  # 999 = the untouched control VLAN in the fixture
```

> Use the existing golden fixture builders. If there is no disabled-uplink fixture, add `disabled_uplink_doc()` / `disable_uplink_op()` to `tests/golden/builders.py` modeled on the nearest existing port-mutation scenario. Keep the control VLAN (`999`) genuinely uninvolved in the cut so its absence proves scoping.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/golden/test_golden_scenarios.py -k visual_map -q`
Expected: FAIL (fixture/assertion missing).

- [ ] **Step 3: Implement the fixture + make it pass**

Add the builders and run until green. Do NOT weaken the scoping assertion to make it pass — if VLAN 999 appears, that's a real bug to fix in the builder, not the test.

- [ ] **Step 4: Update ROADMAP**

In `docs/ROADMAP.md`, add an entry recording the shipped feature and the two deferred fast-follows (primary/secondary cut-distance split; ghost baseline-node rendering for removed entities).

- [ ] **Step 5: Full gate**

Run: `uv run pytest tests -q && uv run ruff check . && uv run mypy src`
Expected: ALL PASS; mypy clean; ruff clean.

- [ ] **Step 6: Commit**

```bash
git add tests/golden docs/ROADMAP.md
git commit -m "test(golden): visual-map worked-example scenario; docs(roadmap): record feature + deferred

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review (run after writing; fix inline)

**Spec coverage:**
- VisualMap contract `{view:{entity:{kind,id,tier,severity,findings}}}` → Task 1 ✓
- Entity-key split-on-first-colon + structured kind/id → Task 1 (entry) + Task 8 (consumer) ✓
- IR-resolution of `affected_entities` (MAC not promoted) → Task 2/4 ✓
- Per-view membership / scoping rule → Task 3/4 ✓
- Paired-array (affected attachment + per-impact cause) → Task 4/5 ✓
- Origin from `caused_by` + port/link/l3intf owner expansion → Task 2/5 ✓
- Removed-entity no-self-entry + participation caveat → Task 5 ✓
- l3_exits interfaces only for referenced VLANs → Task 4 ✓
- Severance `l2`-only → Task 4 (no-vlan finding) ✓
- Reconciliation (origin>affected, severity worst-wins, FindingRef dedup) → Task 6 ✓
- Verdict field + dual-IR pipeline + serialization → Task 7 ✓
- Verdict invariance → Task 7 ✓
- Bleed fixed in our Mermaid + single mechanism (delete highlight.py) → Task 8 ✓
- Worked-example golden + ROADMAP → Task 9 ✓

**Type consistency:** `build_visual_map(baseline_ir, proposed_ir, findings)`, `_affected_contributions(f, index, proposed_ir, idx)`, `_origin_contributions(f, index, baseline_ir, proposed_ir, idx)`, `_Contribution(view, kind, id, tier, ref)`, `VisualEntry(kind, id, tier, severity, findings)`, `FindingRef(index, code, subject)` — consistent across Tasks 1–8.

**Placeholder scan:** none — every code step shows real code; fixture-specific builders in Task 9 are explicitly modeled on existing ones with a named control VLAN.
