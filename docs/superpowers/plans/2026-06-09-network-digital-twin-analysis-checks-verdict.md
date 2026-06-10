# Plan 4 — Analysis + Checks + Verdict/Decision Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the reasoning half of the twin — the pure analysis layer (memoized per-IR computations carrying confidence), the four M1 wired checks (the ONLY layer assigning severity), and verdict assembly with the deterministic `SAFE|REVIEW|UNSAFE|UNKNOWN` decision — so two IRs + an IRDiff produce an agent-actionable verdict document.

**Architecture:** Strict layer discipline per spec: `analysis` computes properties over representations (confidence, NO severity); `checks` interpret analysis into `contracts.Finding`s (severity is terminal here); `verdict` aggregates findings/coverage/confidence into one decision (pure, imports checks but is never imported by them). The registry enforces the spec's gating order (`applies_to` → `requires` → `run`) and crash isolation (`CHECK_ERROR` → operational finding, never `UNSAFE`). Confidence composition is MIN-rule everywhere via the existing `min_confidence`.

**Tech Stack:** Python 3.14, networkx (cycle/component algorithms), frozen dataclasses + StrEnum, pytest/ruff/mypy(strict). Builds on Plan 1's `representations/` + `ir.indexes`/`ir.diff`, Plan 3's `contracts.Finding`/`Rejection`, and `tests/factories.py`.

**Pinned as-built facts (verified):**
- `IR.vlans: Mapping[int, Vlan]`, `IR.devices: Mapping[str, Device]`, `ir.capabilities: frozenset[Capability]`; `IRCapability` values: `wired.l2`, `stp.state`, `clients.active`, `l3.exits`.
- `Port.stp_enabled: bool | None` (None = UNKNOWN → the WARN/LOW loop case; False = disabled → FAIL).
- `build_vlan_graph(ir, l2, vlan_id)` nodes carry `VlanNode(access_ports, exits)` under `data`; edges carry `L2Edge(vlans, member_ports, link_ids, confidence, ...)` under `data`.
- `Client(mac, kind, attach_kind, attach_id, vlan, ip, active, meta)`; indexes: `clients_by_port`, `clients_by_ap`, `clients_by_vlan`.
- `IRDiff(added, removed, modified).touches(kind)`; `Modified(ref, changed_fields)`.
- `Finding` (Plan 3) carries `source/category/code/severity/confidence/message/affected_entities/evidence/remediation`; `Severity` = info|warning|error|critical.
- `tests/factories.py` provides `sw/ap/trunk_port/access_port/link/irb/wired_client/wireless_client`.

**Documented design decisions (resolve spec to code):**
- `requires()` is checked against the **intersection** of baseline and proposed IR capabilities (a check needs its facts on both sides to compare them).
- Boundary-uplink exits (blackhole rule 2): in M1, device roles come from Mist inventory (authoritative), so the "config-inferred role → MEDIUM" row has no M1 source; an edge carrying the VLAN to a `DeviceRole.GATEWAY` node is the boundary uplink, confidence = that edge's confidence (two-sided → HIGH, one-sided → LOW). MEDIUM is documented as a non-M1 slot.
- A cycle in a MultiGraph has two forms: parallel edges between one node pair (≥2 standalone links), and simple cycles of ≥3 nodes (`nx.cycle_basis` on the simple projection). Both are detected.
- Decision inputs arrive as one `DecisionInputs` value so Plan 5's engine maps `Rejection`/L0-fatal/FetchError/IngestReport-not-ok into it without rewiring.

---

### Task 1: `analysis/context.py` — AnalysisContext (memoization)

**Files:**
- Create: `src/digital_twin/analysis/__init__.py`
- Create: `src/digital_twin/analysis/context.py`
- Test: `tests/analysis/__init__.py`, `tests/analysis/test_context.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/analysis/__init__.py  (empty)
```

```python
# tests/analysis/test_context.py
from digital_twin.analysis.context import AnalysisContext
from digital_twin.ir import IRBuilder, Vlan
from tests.factories import access_port, irb, link, sw, trunk_port


def _ir():
    b = IRBuilder()
    b.add_device(sw("A")).add_device(sw("B"))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    b.add_port(trunk_port("A", "ge-0/0/0", tagged=(10,)))
    b.add_port(trunk_port("B", "ge-0/0/0", tagged=(10,)))
    b.add_port(access_port("A", "ge-0/0/1", 10))
    b.add_l3intf(irb("B", 10))
    b.add_link(link("A:ge-0/0/0", "B:ge-0/0/0"))
    return b.build()


def test_l2_graph_is_memoized():
    ctx = AnalysisContext(_ir())
    assert ctx.l2_graph() is ctx.l2_graph()  # same object, built once


def test_vlan_graph_memoized_per_vlan():
    ctx = AnalysisContext(_ir())
    assert ctx.vlan_graph(10) is ctx.vlan_graph(10)
    assert set(ctx.vlan_graph(10).nodes) == {"A", "B"}


def test_ir_and_capabilities_exposed():
    ir = _ir()
    ctx = AnalysisContext(ir)
    assert ctx.ir is ir
    assert ctx.capabilities == ir.capabilities
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/analysis/ -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'digital_twin.analysis'`

- [ ] **Step 3: Write the implementation**

```python
# src/digital_twin/analysis/__init__.py
"""Property COMPUTATIONS over representations — PURE, carry confidence, no severity."""
```

```python
# src/digital_twin/analysis/context.py
"""AnalysisContext: one immutable IR + lazily-memoized representations/analysis.

Checks receive two of these (baseline + proposed) and NEVER rebuild shared work:
the L2 graph is built once, each per-VLAN graph once, each analysis result once.
Pure reads only — the IR is frozen and graphs are built fresh per context.
"""

from __future__ import annotations

from functools import cached_property

import networkx as nx

from digital_twin.ir import IR, Capability
from digital_twin.representations.l2_graph import build_l2_graph
from digital_twin.representations.vlan_graph import build_vlan_graph


class AnalysisContext:
    def __init__(self, ir: IR) -> None:
        self._ir = ir
        self._vlan_graphs: dict[int, nx.MultiGraph] = {}

    @property
    def ir(self) -> IR:
        return self._ir

    @property
    def capabilities(self) -> frozenset[Capability]:
        return self._ir.capabilities

    @cached_property
    def _l2(self) -> nx.MultiGraph:
        return build_l2_graph(self._ir)

    def l2_graph(self) -> nx.MultiGraph:
        return self._l2

    def vlan_graph(self, vlan_id: int) -> nx.MultiGraph:
        if vlan_id not in self._vlan_graphs:
            self._vlan_graphs[vlan_id] = build_vlan_graph(self._ir, self._l2, vlan_id)
        return self._vlan_graphs[vlan_id]
```

- [ ] **Step 4: Run tests + quality gate**

Run: `uv run pytest tests/analysis/ -q && uv run ruff check . && uv run mypy`
Expected: PASS, clean

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/analysis tests/analysis
git commit -m "Plan 4: AnalysisContext (per-IR memoized representations)"
```

---

### Task 2: `analysis/cycles.py` — cycle detection on the per-VLAN graph

**Files:**
- Create: `src/digital_twin/analysis/cycles.py`
- Modify: `src/digital_twin/analysis/context.py` (add `cycles(vlan_id)` memo)
- Test: `tests/analysis/test_cycles.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/analysis/test_cycles.py
from digital_twin.analysis.context import AnalysisContext
from digital_twin.ir import ConfidenceLevel, IRBuilder, Vlan
from digital_twin.ir.entities import LinkKind
from digital_twin.ir.provenance import Provenance
from tests.factories import link, sw, trunk_port


def _builder(*devs):
    b = IRBuilder()
    for d in devs:
        b.add_device(sw(d))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    return b


def _trunk(b, dev, name):
    b.add_port(trunk_port(dev, name, tagged=(10,)))


def test_triangle_is_one_cycle():
    b = _builder("A", "B", "C")
    for dev, peers in (("A", ("B", "C")), ("B", ("A", "C")), ("C", ("A", "B"))):
        for p in peers:
            _trunk(b, dev, f"to-{p}")
    b.add_link(link("A:to-B", "B:to-A"))
    b.add_link(link("B:to-C", "C:to-B"))
    b.add_link(link("A:to-C", "C:to-A"))
    cycles = AnalysisContext(b.build()).cycles(10)
    assert len(cycles) == 1
    assert set(cycles[0].nodes) == {"A", "B", "C"}
    assert len(cycles[0].member_ports) == 6  # every port on the ring


def test_parallel_standalone_links_are_a_two_node_cycle():
    b = _builder("A", "B")
    for dev, peer in (("A", "B"), ("B", "A")):
        _trunk(b, dev, f"to-{peer}-1")
        _trunk(b, dev, f"to-{peer}-2")
    b.add_link(link("A:to-B-1", "B:to-A-1"))
    b.add_link(link("A:to-B-2", "B:to-A-2"))
    cycles = AnalysisContext(b.build()).cycles(10)
    assert len(cycles) == 1
    assert set(cycles[0].nodes) == {"A", "B"}


def test_lag_bundle_is_not_a_cycle():
    # two LAG members collapse to ONE logical edge (Plan 1 contract)
    b = _builder("A", "B")
    for dev, peer in (("A", "B"), ("B", "A")):
        _trunk(b, dev, f"to-{peer}-1")
        _trunk(b, dev, f"to-{peer}-2")
    b.add_link(link("A:to-B-1", "B:to-A-1", kind=LinkKind.LAG, bundle="ae0"))
    b.add_link(link("A:to-B-2", "B:to-A-2", kind=LinkKind.LAG, bundle="ae0"))
    assert AnalysisContext(b.build()).cycles(10) == ()


def test_cycle_confidence_is_min_of_edge_confidences():
    b = _builder("A", "B")
    for dev, peer in (("A", "B"), ("B", "A")):
        _trunk(b, dev, f"to-{peer}-1")
        _trunk(b, dev, f"to-{peer}-2")
    b.add_link(link("A:to-B-1", "B:to-A-1"))  # two-sided -> HIGH
    b.add_link(link("A:to-B-2", "B:to-A-2", prov=Provenance.LLDP_ONE_SIDED))  # LOW
    (cycle,) = AnalysisContext(b.build()).cycles(10)
    assert cycle.confidence.level is ConfidenceLevel.LOW  # weakest input governs


def test_no_cycle_on_a_tree():
    b = _builder("A", "B")
    _trunk(b, "A", "to-B")
    _trunk(b, "B", "to-A")
    b.add_link(link("A:to-B", "B:to-A"))
    assert AnalysisContext(b.build()).cycles(10) == ()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/analysis/test_cycles.py -q`
Expected: FAIL — ImportError / AttributeError (cycles)

- [ ] **Step 3: Write the implementation**

```python
# src/digital_twin/analysis/cycles.py
"""Cycle detection on the normalized per-VLAN graph (NO severity — that is
l2.loop's job; this only reports structure + confidence).

Two cycle forms in a MultiGraph:
- PARALLEL edges between one node pair (>=2 standalone links; LAG members were
  already collapsed by the representation, so parallel = real redundancy).
- Simple cycles of >=3 nodes (cycle_basis on the simple projection).

Cycle confidence = MIN over its edges' confidences (method itself is exact ->
contributes HIGH, i.e. never lowers).
"""

from __future__ import annotations

from dataclasses import dataclass

import networkx as nx

from digital_twin.ir import Confidence, min_confidence

from digital_twin.representations.graph_data import L2Edge


@dataclass(frozen=True)
class Cycle:
    nodes: tuple[str, ...]
    member_ports: tuple[str, ...]  # every port on the cycle's edges
    link_ids: tuple[str, ...]
    confidence: Confidence


def _edges_between(g: nx.MultiGraph, u: str, v: str) -> list[L2Edge]:
    return [d["data"] for d in g[u][v].values()] if g.has_edge(u, v) else []


def find_cycles(g: nx.MultiGraph) -> tuple[Cycle, ...]:
    out: list[Cycle] = []
    seen_pairs: set[frozenset[str]] = set()

    # form 1: parallel logical edges between one pair
    for u, v in {(u, v) for u, v in g.edges() if u != v}:
        pair = frozenset((u, v))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        edges = _edges_between(g, u, v)
        if len(edges) >= 2:
            out.append(_cycle((u, v), edges))

    # form 2: simple cycles >=3 nodes on the deduplicated projection
    simple = nx.Graph(g)
    for nodes in nx.cycle_basis(simple):
        if len(nodes) < 3:
            continue
        ring = [*nodes, nodes[0]]
        edges = [e for a, b in zip(ring, ring[1:], strict=False) for e in _edges_between(g, a, b)]
        out.append(_cycle(tuple(nodes), edges))

    return tuple(sorted(out, key=lambda c: c.nodes))


def _cycle(nodes: tuple[str, ...], edges: list[L2Edge]) -> Cycle:
    return Cycle(
        nodes=tuple(sorted(nodes)),
        member_ports=tuple(sorted({p for e in edges for p in e.member_ports})),
        link_ids=tuple(sorted({lid for e in edges for lid in e.link_ids})),
        confidence=min_confidence(*(e.confidence for e in edges)),
    )
```

Add to `AnalysisContext` (in `context.py`):

```python
    def cycles(self, vlan_id: int) -> tuple[Cycle, ...]:
        if vlan_id not in self._cycles:
            self._cycles[vlan_id] = find_cycles(self.vlan_graph(vlan_id))
        return self._cycles[vlan_id]
```

with `self._cycles: dict[int, tuple[Cycle, ...]] = {}` in `__init__` and
`from digital_twin.analysis.cycles import Cycle, find_cycles` at module top.

- [ ] **Step 4: Run tests + quality gate**

Run: `uv run pytest tests/analysis/ -q && uv run ruff check . && uv run mypy`
Expected: PASS, clean

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/analysis tests/analysis
git commit -m "Plan 4: cycle analysis (parallel-edge + simple cycles, MIN confidence)"
```

---

### Task 3: `analysis/exits.py` — VLAN-exit resolution (the blackhole contract's rule 1-3)

**Files:**
- Create: `src/digital_twin/analysis/exits.py`
- Modify: `src/digital_twin/analysis/context.py` (add `exit_for(vlan_id)` memo)
- Test: `tests/analysis/test_exits.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/analysis/test_exits.py
from digital_twin.analysis.context import AnalysisContext
from digital_twin.analysis.exits import ExitKind
from digital_twin.ir import ConfidenceLevel, IRBuilder, Vlan
from digital_twin.ir.entities import Device, DeviceRole
from digital_twin.ir.provenance import Provenance
from tests.factories import irb, link, sw, trunk_port


def _base(with_irb: bool, with_gateway: bool = False, gw_one_sided: bool = False):
    b = IRBuilder()
    b.add_device(sw("A"))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    b.add_port(trunk_port("A", "down", tagged=(10,)))
    if with_irb:
        b.add_l3intf(irb("A", 10))
    if with_gateway:
        b.add_device(Device(id="GW", role=DeviceRole.GATEWAY, site="s1"))
        b.add_port(trunk_port("A", "up", tagged=(10,)))
        b.add_port(trunk_port("GW", "down", tagged=(10,)))
        prov = Provenance.LLDP_ONE_SIDED if gw_one_sided else Provenance.LLDP_TWO_SIDED
        b.add_link(link("A:up", "GW:down", prov=prov))
    return b.build()


def test_rule1_irb_is_high_confidence_exit():
    res = AnalysisContext(_base(with_irb=True)).exit_for(10)
    assert res.kind is ExitKind.IRB
    assert res.nodes == ("A",)
    assert res.confidence is not None and res.confidence.level is ConfidenceLevel.HIGH


def test_rule2_boundary_uplink_two_sided_is_high():
    res = AnalysisContext(_base(with_irb=False, with_gateway=True)).exit_for(10)
    assert res.kind is ExitKind.BOUNDARY_UPLINK
    assert res.nodes == ("GW",)
    assert res.confidence is not None and res.confidence.level is ConfidenceLevel.HIGH


def test_rule2_one_sided_uplink_is_low():
    res = AnalysisContext(
        _base(with_irb=False, with_gateway=True, gw_one_sided=True)
    ).exit_for(10)
    assert res.kind is ExitKind.BOUNDARY_UPLINK
    assert res.confidence is not None and res.confidence.level is ConfidenceLevel.LOW


def test_rule1_wins_over_rule2():
    res = AnalysisContext(_base(with_irb=True, with_gateway=True)).exit_for(10)
    assert res.kind is ExitKind.IRB


def test_rule3_no_exit_found():
    res = AnalysisContext(_base(with_irb=False)).exit_for(10)
    assert res.kind is ExitKind.NONE
    assert res.confidence is None  # absent -> INSUFFICIENT_DATA at the check
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/analysis/test_exits.py -q`
Expected: FAIL — ImportError

- [ ] **Step 3: Write the implementation**

```python
# src/digital_twin/analysis/exits.py
"""VLAN-exit resolution — the spec's core blackhole contract (precedence 1-3).

1. IRB/SVI on a compiled device (VlanNode.exits non-empty)      -> IRB, HIGH.
2. No IRB, but the VLAN is carried on an edge to a GATEWAY-role
   node (out-of-scope upstream in M1)                            -> BOUNDARY_UPLINK,
   confidence = that edge's confidence (two-sided HIGH / one-sided LOW; the
   spec's MEDIUM row — config-inferred role — has no M1 source: device roles
   come from Mist inventory, which is authoritative).
3. Neither                                                       -> NONE, no
   confidence (the check maps this to INSUFFICIENT_DATA, never PASS).

NO severity here; the check interprets.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import networkx as nx

from digital_twin.ir import IR, Confidence, ConfidenceLevel, min_confidence
from digital_twin.ir.entities import DeviceRole
from digital_twin.ir.indexes import node_for, vc_root_map

from digital_twin.representations.graph_data import VlanNode


class ExitKind(StrEnum):
    IRB = "irb"
    BOUNDARY_UPLINK = "boundary_uplink"
    NONE = "none"


@dataclass(frozen=True)
class ExitResolution:
    kind: ExitKind
    nodes: tuple[str, ...]  # graph nodes that ARE the exit (empty for NONE)
    confidence: Confidence | None  # None only for NONE


def resolve_exit(ir: IR, vlan_graph: nx.MultiGraph) -> ExitResolution:
    # rule 1: in-scope IRB/SVI (the representation already indexed them)
    irb_nodes = tuple(
        sorted(n for n, d in vlan_graph.nodes(data=True) if d["data"].is_exit)
    )
    if irb_nodes:
        return ExitResolution(
            kind=ExitKind.IRB,
            nodes=irb_nodes,
            confidence=Confidence(level=ConfidenceLevel.HIGH),
        )

    # rule 2: an edge carrying the VLAN to a gateway-role node
    vc_root = vc_root_map(ir)
    gateway_nodes = {
        node_for(vc_root, d.id) for d in ir.devices.values() if d.role is DeviceRole.GATEWAY
    }
    hits: dict[str, list[Confidence]] = {}
    for u, v, data in vlan_graph.edges(data=True):
        for node in (u, v):
            if node in gateway_nodes:
                hits.setdefault(node, []).append(data["data"].confidence)
    if hits:
        return ExitResolution(
            kind=ExitKind.BOUNDARY_UPLINK,
            nodes=tuple(sorted(hits)),
            confidence=min_confidence(*(c for confs in hits.values() for c in confs)),
        )

    return ExitResolution(kind=ExitKind.NONE, nodes=(), confidence=None)
```

Add to `AnalysisContext`: memoized `exit_for(vlan_id)` calling
`resolve_exit(self._ir, self.vlan_graph(vlan_id))` (same dict-memo pattern as `cycles`).

- [ ] **Step 4: Run tests + quality gate**

Run: `uv run pytest tests/analysis/ -q && uv run ruff check . && uv run mypy`
Expected: PASS, clean

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/analysis tests/analysis
git commit -m "Plan 4: VLAN-exit resolution (IRB > boundary uplink > none)"
```

---

### Task 4: `analysis/vlan_reachability.py` — components + membership + exit reachability

**Files:**
- Create: `src/digital_twin/analysis/vlan_reachability.py`
- Modify: `src/digital_twin/analysis/context.py` (add `vlan_components(vlan_id)` memo)
- Test: `tests/analysis/test_vlan_reachability.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/analysis/test_vlan_reachability.py
from digital_twin.analysis.context import AnalysisContext
from digital_twin.ir import IRBuilder, Vlan
from tests.factories import access_port, irb, link, sw, trunk_port


def _split_ir(connected: bool):
    """A--B (carrying vlan 10) and C isolated-with-member; IRB on B."""
    b = IRBuilder()
    for d in ("A", "B", "C"):
        b.add_device(sw(d))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    b.add_port(trunk_port("A", "to-B", tagged=(10,)))
    b.add_port(trunk_port("B", "to-A", tagged=(10,)))
    b.add_link(link("A:to-B", "B:to-A"))
    b.add_port(access_port("A", "acc", 10))
    b.add_port(access_port("C", "acc", 10))
    b.add_l3intf(irb("B", 10))
    if connected:
        b.add_port(trunk_port("B", "to-C", tagged=(10,)))
        b.add_port(trunk_port("C", "to-B", tagged=(10,)))
        b.add_link(link("B:to-C", "C:to-B"))
    return b.build()


def test_components_partition_the_vlan_graph():
    comps = AnalysisContext(_split_ir(connected=False)).vlan_components(10)
    assert sorted(sorted(c.nodes) for c in comps) == [["A", "B"], ["C"]]


def test_membership_and_exit_reachability_per_component():
    comps = AnalysisContext(_split_ir(connected=False)).vlan_components(10)
    by_nodes = {tuple(sorted(c.nodes)): c for c in comps}
    ab, c = by_nodes[("A", "B")], by_nodes[("C",)]
    assert ab.has_members and ab.reaches_exit  # access port on A, IRB on B
    assert c.has_members and not c.reaches_exit  # member but stranded


def test_single_component_when_connected():
    comps = AnalysisContext(_split_ir(connected=True)).vlan_components(10)
    assert len(comps) == 1 and comps[0].reaches_exit
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/analysis/test_vlan_reachability.py -q`
Expected: FAIL — ImportError

- [ ] **Step 3: Write the implementation**

```python
# src/digital_twin/analysis/vlan_reachability.py
"""Connected components of a per-VLAN graph, with membership + exit reachability.

A component HAS MEMBERS if any node holds an access port for the VLAN
(config-based switched membership — a configured-but-empty port still counts;
the AP/wireless observation-based side is layered on by the checks, which have
client data). It REACHES THE EXIT if any of the resolved exit nodes is inside
the component. NO severity here.
"""

from __future__ import annotations

from dataclasses import dataclass

import networkx as nx

from .exits import ExitResolution


@dataclass(frozen=True)
class VlanComponent:
    nodes: frozenset[str]
    has_members: bool
    reaches_exit: bool


def vlan_components(vlan_graph: nx.MultiGraph, exit_res: ExitResolution) -> tuple[VlanComponent, ...]:
    exit_nodes = set(exit_res.nodes)
    out: list[VlanComponent] = []
    for nodes in nx.connected_components(vlan_graph):
        members = any(vlan_graph.nodes[n]["data"].is_member for n in nodes)
        out.append(
            VlanComponent(
                nodes=frozenset(nodes),
                has_members=members,
                reaches_exit=bool(nodes & exit_nodes),
            )
        )
    return tuple(sorted(out, key=lambda c: sorted(c.nodes)))
```

Add to `AnalysisContext`: memoized `vlan_components(vlan_id)` calling
`vlan_components(self.vlan_graph(vlan_id), self.exit_for(vlan_id))` (dict-memo as before;
import the function as `compute_vlan_components` to avoid the name clash).

- [ ] **Step 4: Run tests + quality gate**

Run: `uv run pytest tests/analysis/ -q && uv run ruff check . && uv run mypy`
Expected: PASS, clean

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/analysis tests/analysis
git commit -m "Plan 4: VLAN reachability (components, membership, exit reach)"
```

---

### Task 5: `checks/base.py` — the check contract

**Files:**
- Create: `src/digital_twin/checks/__init__.py`
- Create: `src/digital_twin/checks/base.py`
- Test: `tests/checks/__init__.py`, `tests/checks/test_base.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/checks/__init__.py  (empty)
```

```python
# tests/checks/test_base.py
from digital_twin.checks.base import CheckResult, Coverage, CoverageState, Status


def test_status_vocabulary_matches_spec():
    assert {s.value for s in Status} == {
        "pass", "warn", "fail", "not_applicable", "insufficient_data", "check_error"
    }


def test_coverage_states():
    assert {s.value for s in CoverageState} == {
        "complete", "partial", "insufficient", "not_applicable"
    }


def test_check_result_constructs():
    r = CheckResult(
        check_id="wired.l2.loop",
        status=Status.PASS,
        findings=(),
        coverage=Coverage(state=CoverageState.COMPLETE),
        confidence=None,
        reasoning="no cycles found",
    )
    assert r.status is Status.PASS and r.coverage.notes == ()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/checks/ -q`
Expected: FAIL — ModuleNotFoundError

- [ ] **Step 3: Write the implementation**

```python
# src/digital_twin/checks/__init__.py
"""Checks INTERPRET analysis -> findings — the ONLY layer with severity."""
```

```python
# src/digital_twin/checks/base.py
"""The check-plugin contract (spec): Status vs Severity are distinct vocabularies.

A check receives ONLY the two AnalysisContexts + the neutral IRDiff — never raw
vendor payload. Severity is assigned in checks and nowhere else. A check emits
FAIL only at HIGH confidence (otherwise it degrades to WARN/INSUFFICIENT_DATA);
that invariant is what keeps FAIL -> UNSAFE an always-confident assertion.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from digital_twin.analysis.context import AnalysisContext
from digital_twin.contracts import Finding, Severity
from digital_twin.ir import Capability, Confidence, IRDiff


class Status(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"
    INSUFFICIENT_DATA = "insufficient_data"
    CHECK_ERROR = "check_error"


class CoverageState(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class Coverage:
    state: CoverageState
    notes: tuple[str, ...] = ()  # e.g. ("AP vlan membership is observation-based",)


@dataclass(frozen=True)
class CheckContext:
    baseline: AnalysisContext
    proposed: AnalysisContext
    diff: IRDiff


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    status: Status
    findings: tuple[Finding, ...]
    coverage: Coverage
    confidence: Confidence | None  # None when nothing was evaluated (N_A / error)
    reasoning: str


class Check(Protocol):
    id: str
    title: str
    domain: str  # groups in the verdict, e.g. "wired.l2"
    default_severity: Severity

    def requires(self) -> frozenset[Capability]: ...

    def applies_to(self, diff: IRDiff) -> bool: ...

    def run(self, ctx: CheckContext) -> CheckResult: ...
```

- [ ] **Step 4: Run tests + quality gate**

Run: `uv run pytest tests/checks/ -q && uv run ruff check . && uv run mypy`
Expected: PASS, clean

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/checks tests/checks
git commit -m "Plan 4: check contract (Status/Coverage/CheckContext/CheckResult)"
```

---

### Task 6: `checks/registry.py` — gating order + crash isolation

**Files:**
- Create: `src/digital_twin/checks/registry.py`
- Test: `tests/checks/test_registry.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/checks/test_registry.py
from dataclasses import dataclass, field

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState, Status
from digital_twin.checks.registry import CheckRegistry
from digital_twin.contracts import FindingCategory, Severity
from digital_twin.ir import IRBuilder, IRCapability, diff_ir
from tests.factories import sw


def _ctx(*caps: str) -> CheckContext:
    b1, b2 = IRBuilder(), IRBuilder()
    b1.add_device(sw("A"))
    b2.add_device(sw("A")).add_device(sw("B"))  # diff: device B added
    for c in caps:
        b1.with_capability(c)
        b2.with_capability(c)
    ir1, ir2 = b1.build(), b2.build()
    return CheckContext(
        baseline=AnalysisContext(ir1), proposed=AnalysisContext(ir2), diff=diff_ir(ir1, ir2)
    )


@dataclass
class FakeCheck:
    id: str = "test.fake"
    title: str = "fake"
    domain: str = "test"
    default_severity: Severity = Severity.ERROR
    applies: bool = True
    needs: frozenset = field(default_factory=frozenset)
    boom: bool = False
    ran: bool = False

    def requires(self):
        return self.needs

    def applies_to(self, diff):
        return self.applies

    def run(self, ctx):
        self.ran = True
        if self.boom:
            raise RuntimeError("kaboom")
        return CheckResult(
            check_id=self.id, status=Status.PASS, findings=(),
            coverage=Coverage(state=CoverageState.COMPLETE), confidence=None, reasoning="ok",
        )


def test_not_applicable_short_circuits_before_requires():
    # gating order: applies_to FIRST — a non-applicable check with missing caps
    # is NOT_APPLICABLE, never INSUFFICIENT_DATA
    check = FakeCheck(applies=False, needs=frozenset({IRCapability.STP_STATE}))
    (result,) = CheckRegistry([check]).run_all(_ctx())
    assert result.status is Status.NOT_APPLICABLE
    assert check.ran is False


def test_missing_capability_is_insufficient_data():
    check = FakeCheck(needs=frozenset({IRCapability.STP_STATE}))
    (result,) = CheckRegistry([check]).run_all(_ctx())  # ctx has NO capabilities
    assert result.status is Status.INSUFFICIENT_DATA
    assert check.ran is False
    assert result.coverage.state is CoverageState.INSUFFICIENT


def test_capability_present_runs_the_check():
    check = FakeCheck(needs=frozenset({IRCapability.WIRED_L2}))
    (result,) = CheckRegistry([check]).run_all(_ctx(IRCapability.WIRED_L2))
    assert result.status is Status.PASS and check.ran


def test_crash_is_isolated_to_check_error_with_operational_finding():
    boom, ok = FakeCheck(id="test.boom", boom=True), FakeCheck(id="test.ok")
    results = CheckRegistry([boom, ok]).run_all(_ctx())
    by_id = {r.check_id: r for r in results}
    assert by_id["test.boom"].status is Status.CHECK_ERROR
    f = by_id["test.boom"].findings[0]
    assert f.category is FindingCategory.OPERATIONAL  # crash != network breakage
    assert "kaboom" in str(f.evidence.get("error"))
    assert by_id["test.ok"].status is Status.PASS  # one bad check cannot sink the rest
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/checks/test_registry.py -q`
Expected: FAIL — ImportError

- [ ] **Step 3: Write the implementation**

```python
# src/digital_twin/checks/registry.py
"""Run checks under the spec's STRICT gating order, with crash isolation.

Per check: (1) applies_to(diff) -> NOT_APPLICABLE and stop (checked FIRST, so a
cosmetic change is never INSUFFICIENT_DATA); (2) requires() vs the IRs'
capabilities (INTERSECTION of baseline+proposed — a comparison needs facts on
both sides) -> INSUFFICIENT_DATA; (3) run, exceptions isolated to CHECK_ERROR +
an OPERATIONAL finding (a crash is never network breakage -> REVIEW, not UNSAFE).
"""

from __future__ import annotations

from digital_twin.contracts import Finding, FindingCategory, FindingSource, Severity
from digital_twin.ir import Confidence, ConfidenceLevel

from .base import Check, CheckContext, CheckResult, Coverage, CoverageState, Status


class CheckRegistry:
    def __init__(self, checks: list[Check]) -> None:
        self._checks = list(checks)

    def run_all(self, ctx: CheckContext) -> tuple[CheckResult, ...]:
        capabilities = ctx.baseline.capabilities & ctx.proposed.capabilities
        results: list[CheckResult] = []
        for check in self._checks:
            results.append(self._run_one(check, ctx, capabilities))
        return tuple(results)

    def _run_one(
        self, check: Check, ctx: CheckContext, capabilities: frozenset[str]
    ) -> CheckResult:
        if not check.applies_to(ctx.diff):
            return CheckResult(
                check_id=check.id,
                status=Status.NOT_APPLICABLE,
                findings=(),
                coverage=Coverage(state=CoverageState.NOT_APPLICABLE),
                confidence=None,
                reasoning="delta does not touch this check's domain",
            )
        missing = check.requires() - capabilities
        if missing:
            return CheckResult(
                check_id=check.id,
                status=Status.INSUFFICIENT_DATA,
                findings=(),
                coverage=Coverage(
                    state=CoverageState.INSUFFICIENT,
                    notes=tuple(f"missing capability: {m}" for m in sorted(missing)),
                ),
                confidence=None,
                reasoning=f"applicable but lacking capabilities: {sorted(missing)}",
            )
        try:
            return check.run(ctx)
        except Exception as e:  # noqa: BLE001 — isolated per the spec's component contract
            return CheckResult(
                check_id=check.id,
                status=Status.CHECK_ERROR,
                findings=(
                    Finding(
                        source=FindingSource.CHECK,
                        category=FindingCategory.OPERATIONAL,
                        code=f"{check.id}.check_error",
                        severity=Severity.ERROR,
                        confidence=Confidence(level=ConfidenceLevel.HIGH),
                        message=f"check {check.id} crashed; result unavailable",
                        evidence={"error": str(e)},
                    ),
                ),
                coverage=Coverage(state=CoverageState.INSUFFICIENT, notes=("check crashed",)),
                confidence=None,
                reasoning=f"crashed: {e}",
            )
```

- [ ] **Step 4: Run tests + quality gate**

Run: `uv run pytest tests/checks/ -q && uv run ruff check . && uv run mypy`
Expected: PASS, clean

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/checks tests/checks
git commit -m "Plan 4: check registry (gating order + crash isolation)"
```

---

### Task 7: `checks/wired/l2_loop.py`

**Files:**
- Create: `src/digital_twin/checks/wired/__init__.py`
- Create: `src/digital_twin/checks/wired/l2_loop.py`
- Test: `tests/checks/test_l2_loop.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/checks/test_l2_loop.py
"""l2.loop spec table: cycle + all-STP = PASS; + STP disabled = FAIL(HIGH);
+ STP unknown = WARN(LOW). Only NEW cycles are attributed to the delta."""

from dataclasses import replace

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.wired.l2_loop import L2LoopCheck
from digital_twin.contracts import FindingCategory, Severity
from digital_twin.ir import ConfidenceLevel, IRBuilder, IRCapability, Vlan, diff_ir
from tests.factories import link, sw, trunk_port


def _ring_ir(stp: bool | None, parallel: bool):
    """A-B with one link (tree) or two standalone links (cycle)."""
    b = IRBuilder()
    b.add_device(sw("A")).add_device(sw("B"))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    for dev, peer in (("A", "B"), ("B", "A")):
        p1 = trunk_port(dev, f"to-{peer}-1", tagged=(10,))
        b.add_port(replace(p1, stp_enabled=stp))
        if parallel:
            p2 = trunk_port(dev, f"to-{peer}-2", tagged=(10,))
            b.add_port(replace(p2, stp_enabled=stp))
    b.add_link(link("A:to-B-1", "B:to-A-1"))
    if parallel:
        b.add_link(link("A:to-B-2", "B:to-A-2"))
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def _ctx(baseline, proposed) -> CheckContext:
    return CheckContext(
        baseline=AnalysisContext(baseline),
        proposed=AnalysisContext(proposed),
        diff=diff_ir(baseline, proposed),
    )


def test_new_cycle_with_stp_everywhere_passes():
    ctx = _ctx(_ring_ir(stp=True, parallel=False), _ring_ir(stp=True, parallel=True))
    result = L2LoopCheck().run(ctx)
    assert result.status is Status.PASS  # protected redundancy, not a loop


def test_new_cycle_with_stp_disabled_fails_high():
    ctx = _ctx(_ring_ir(stp=False, parallel=False), _ring_ir(stp=False, parallel=True))
    result = L2LoopCheck().run(ctx)
    assert result.status is Status.FAIL
    f = result.findings[0]
    assert f.severity is Severity.ERROR and f.category is FindingCategory.NETWORK
    assert f.confidence.level is ConfidenceLevel.HIGH


def test_new_cycle_with_stp_unknown_warns_low():
    ctx = _ctx(_ring_ir(stp=None, parallel=False), _ring_ir(stp=None, parallel=True))
    result = L2LoopCheck().run(ctx)
    assert result.status is Status.WARN
    assert result.findings[0].confidence.level is ConfidenceLevel.LOW


def test_preexisting_cycle_is_context_not_failure():
    same = _ring_ir(stp=False, parallel=True)
    ctx = _ctx(same, _ring_ir(stp=False, parallel=True))
    result = L2LoopCheck().run(ctx)
    assert result.status is Status.PASS  # not introduced by the delta
    assert any(f.severity is Severity.INFO for f in result.findings)  # reported as context


def test_applies_to_link_and_port_changes_only():
    check = L2LoopCheck()
    base, prop = _ring_ir(True, False), _ring_ir(True, True)
    assert check.applies_to(diff_ir(base, prop)) is True
    assert check.applies_to(diff_ir(base, base)) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/checks/test_l2_loop.py -q`
Expected: FAIL — ImportError

- [ ] **Step 3: Write the implementation**

```python
# src/digital_twin/checks/wired/__init__.py
"""The four M1 wired checks."""
```

```python
# src/digital_twin/checks/wired/l2_loop.py
"""wired.l2.loop — a cycle is NOT a loop by itself (spec table):

all cycle ports STP-running -> protected redundancy (PASS); any port STP
DISABLED -> FAIL (ERROR, network, HIGH); STP UNKNOWN on any port -> WARN with
LOW confidence (floors the decision to REVIEW). Only cycles newly introduced by
the delta are attributed to it; pre-existing cycles are INFO context.
requires() is wired.l2 only — STP_STATE absence degrades to the UNKNOWN row,
which is exactly the honest answer (not INSUFFICIENT_DATA).
"""

from __future__ import annotations

from digital_twin.analysis.cycles import Cycle
from digital_twin.checks.base import (
    CheckContext,
    CheckResult,
    Coverage,
    CoverageState,
    Status,
)
from digital_twin.contracts import Finding, FindingCategory, FindingSource, Severity
from digital_twin.ir import Capability, Confidence, ConfidenceLevel, IRCapability, IRDiff
from digital_twin.ir import min_confidence


class L2LoopCheck:
    id = "wired.l2.loop"
    title = "L2 loop risk (cycle without STP protection)"
    domain = "wired.l2"
    default_severity = Severity.ERROR

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2})

    def applies_to(self, diff: IRDiff) -> bool:
        return any(diff.touches(k) for k in ("link", "port", "vlan", "device"))

    def run(self, ctx: CheckContext) -> CheckResult:
        findings: list[Finding] = []
        worst = Status.PASS
        confidences: list[Confidence] = []
        vlan_ids = sorted(set(ctx.baseline.ir.vlans) | set(ctx.proposed.ir.vlans))
        for vid in vlan_ids:
            baseline_keys = {c.nodes for c in ctx.baseline.cycles(vid)}
            for cycle in ctx.proposed.cycles(vid):
                is_new = cycle.nodes not in baseline_keys
                finding, status = self._judge(ctx, vid, cycle, is_new)
                if finding:
                    findings.append(finding)
                    confidences.append(finding.confidence)
                worst = _worse(worst, status)
        confidence = min_confidence(*confidences) if confidences else Confidence(
            level=ConfidenceLevel.HIGH
        )
        return CheckResult(
            check_id=self.id,
            status=worst,
            findings=tuple(findings),
            coverage=Coverage(state=CoverageState.COMPLETE),
            confidence=confidence,
            reasoning=f"examined {len(vlan_ids)} vlan graphs for cycles",
        )

    def _judge(
        self, ctx: CheckContext, vid: int, cycle: Cycle, is_new: bool
    ) -> tuple[Finding | None, Status]:
        ports = [ctx.proposed.ir.port(p) for p in cycle.member_ports]
        disabled = [p.id for p in ports if p.stp_enabled is False]
        unknown = [p.id for p in ports if p.stp_enabled is None]
        if not is_new:  # pre-existing: context only, never attributed to the delta
            return (
                self._finding(
                    code="wired.l2.loop.preexisting",
                    severity=Severity.INFO,
                    confidence=cycle.confidence,
                    message=f"pre-existing cycle on vlan {vid} (context, not caused by delta)",
                    cycle=cycle,
                    vid=vid,
                ),
                Status.PASS,
            )
        if disabled:
            return (
                self._finding(
                    code="wired.l2.loop.unprotected",
                    severity=Severity.ERROR,
                    confidence=min_confidence(
                        cycle.confidence, Confidence(level=ConfidenceLevel.HIGH)
                    ),
                    message=(
                        f"new cycle on vlan {vid} with STP DISABLED on "
                        f"{len(disabled)} port(s) — unprotected redundant path"
                    ),
                    cycle=cycle,
                    vid=vid,
                    extra={"stp_disabled_ports": disabled},
                ),
                Status.FAIL,
            )
        if unknown:
            return (
                self._finding(
                    code="wired.l2.loop.unverified",
                    severity=Severity.WARNING,
                    confidence=Confidence(
                        level=ConfidenceLevel.LOW,
                        reasons=tuple(f"STP state unknown on {p}" for p in unknown[:5]),
                    ),
                    message=f"new cycle on vlan {vid}; STP state unverified — potential loop",
                    cycle=cycle,
                    vid=vid,
                    extra={"stp_unknown_ports": unknown},
                ),
                Status.WARN,
            )
        return (
            self._finding(
                code="wired.l2.loop.protected",
                severity=Severity.INFO,
                confidence=min_confidence(
                    cycle.confidence, Confidence(level=ConfidenceLevel.HIGH)
                ),
                message=f"new cycle on vlan {vid} fully STP-protected (redundancy, not a loop)",
                cycle=cycle,
                vid=vid,
            ),
            Status.PASS,
        )

    def _finding(
        self,
        *,
        code: str,
        severity: Severity,
        confidence: Confidence,
        message: str,
        cycle: Cycle,
        vid: int,
        extra: dict[str, object] | None = None,
    ) -> Finding:
        return Finding(
            source=FindingSource.CHECK,
            category=FindingCategory.NETWORK,
            code=code,
            severity=severity,
            confidence=confidence,
            message=message,
            affected_entities=cycle.member_ports,
            evidence={
                "vlan": vid,
                "cycle_nodes": list(cycle.nodes),
                "link_ids": list(cycle.link_ids),
                **(extra or {}),
            },
        )


_ORDER = [Status.PASS, Status.WARN, Status.FAIL]


def _worse(a: Status, b: Status) -> Status:
    return a if _ORDER.index(a) >= _ORDER.index(b) else b
```

- [ ] **Step 4: Run tests + quality gate**

Run: `uv run pytest tests/checks/ -q && uv run ruff check . && uv run mypy`
Expected: PASS, clean

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/checks tests/checks
git commit -m "Plan 4: wired.l2.loop check (cycle + STP state, spec table)"
```

---

### Task 8: `checks/wired/l2_blackhole.py`

**Files:**
- Create: `src/digital_twin/checks/wired/l2_blackhole.py`
- Test: `tests/checks/test_l2_blackhole.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/checks/test_l2_blackhole.py
"""l2.blackhole: FAIL only when a member component HAD a HIGH-confidence exit
path in IR and LOSES it in IR'; MEDIUM/LOW exit -> WARN; no locatable exit ->
INSUFFICIENT_DATA for that vlan (never PASS); pre-existing strands = context."""

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.wired.l2_blackhole import L2BlackholeCheck
from digital_twin.contracts import Severity
from digital_twin.ir import IRBuilder, IRCapability, Vlan, diff_ir
from tests.factories import access_port, irb, link, sw, trunk_port


def _ir(*, connected: bool, with_irb: bool = True):
    """A(member)--B(IRB). connected=False cuts the link (the delta's effect)."""
    b = IRBuilder()
    b.add_device(sw("A")).add_device(sw("B"))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    b.add_port(access_port("A", "acc", 10))
    b.add_port(trunk_port("A", "up", tagged=(10,)))
    b.add_port(trunk_port("B", "down", tagged=(10,)))
    if connected:
        b.add_link(link("A:up", "B:down"))
    if with_irb:
        b.add_l3intf(irb("B", 10))
    b.with_capability(IRCapability.WIRED_L2).with_capability(IRCapability.L3_EXITS)
    return b.build()


def _ctx(baseline, proposed):
    return CheckContext(
        baseline=AnalysisContext(baseline),
        proposed=AnalysisContext(proposed),
        diff=diff_ir(baseline, proposed),
    )


def test_losing_a_high_confidence_exit_fails():
    result = L2BlackholeCheck().run(_ctx(_ir(connected=True), _ir(connected=False)))
    assert result.status is Status.FAIL
    f = result.findings[0]
    assert f.severity is Severity.ERROR
    assert "10" in f.message  # names the vlan


def test_still_connected_passes():
    result = L2BlackholeCheck().run(_ctx(_ir(connected=True), _ir(connected=True)))
    assert result.status is Status.PASS


def test_no_locatable_exit_is_insufficient_data():
    base = _ir(connected=True, with_irb=False)
    prop = _ir(connected=False, with_irb=False)
    result = L2BlackholeCheck().run(_ctx(base, prop))
    assert result.status is Status.INSUFFICIENT_DATA  # exit unlocatable, never PASS


def test_preexisting_strand_is_context_not_failure():
    # already disconnected in baseline -> not attributed to the delta
    base = _ir(connected=False)
    prop = _ir(connected=False)
    result = L2BlackholeCheck().run(_ctx(base, prop))
    assert result.status is Status.PASS
    assert any(f.severity is Severity.INFO for f in result.findings)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/checks/test_l2_blackhole.py -q`
Expected: FAIL — ImportError

- [ ] **Step 3: Write the implementation**

```python
# src/digital_twin/checks/wired/l2_blackhole.py
"""wired.l2.blackhole — a member component that loses its path to the VLAN exit.

Per VLAN (spec contract):
- exit resolved by analysis/exits (IRB HIGH > boundary uplink edge-confidence >
  NONE). NONE while members exist -> INSUFFICIENT_DATA for that vlan.
- FAIL only when the component reached the exit in IR, loses it in IR', AND the
  exit is HIGH confidence; a MEDIUM/LOW exit downgrades to WARN ("FAIL only at
  HIGH confidence").
- Components stranded in BOTH IRs are pre-existing -> INFO context.
- Switched membership is configuration-based (access ports — empty ports count).
  AP/wireless membership is observation-based; when client data is absent the
  coverage is PARTIAL (noted), never silently complete.
"""

from __future__ import annotations

from digital_twin.checks.base import (
    CheckContext,
    CheckResult,
    Coverage,
    CoverageState,
    Status,
)
from digital_twin.contracts import Finding, FindingCategory, FindingSource, Severity
from digital_twin.ir import (
    Capability,
    Confidence,
    ConfidenceLevel,
    IRCapability,
    IRDiff,
    min_confidence,
)
from digital_twin.analysis.exits import ExitKind


class L2BlackholeCheck:
    id = "wired.l2.blackhole"
    title = "VLAN segment loses its exit"
    domain = "wired.l2"
    default_severity = Severity.ERROR

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2, IRCapability.L3_EXITS})

    def applies_to(self, diff: IRDiff) -> bool:
        return any(diff.touches(k) for k in ("link", "port", "vlan", "l3intf", "device"))

    def run(self, ctx: CheckContext) -> CheckResult:
        findings: list[Finding] = []
        statuses: list[Status] = []
        confidences: list[Confidence] = []
        notes: list[str] = []
        if IRCapability.CLIENTS_ACTIVE not in ctx.proposed.capabilities:
            notes.append(
                "AP/wireless VLAN membership is observation-based and client data "
                "is absent — wireless membership not evaluated"
            )
        for vid in sorted(set(ctx.baseline.ir.vlans) | set(ctx.proposed.ir.vlans)):
            statuses.append(self._check_vlan(ctx, vid, findings, confidences))
        status = _aggregate(statuses)
        coverage_state = CoverageState.PARTIAL if notes else CoverageState.COMPLETE
        if status is Status.INSUFFICIENT_DATA:
            coverage_state = CoverageState.INSUFFICIENT
        return CheckResult(
            check_id=self.id,
            status=status,
            findings=tuple(findings),
            coverage=Coverage(state=coverage_state, notes=tuple(notes)),
            confidence=min_confidence(*confidences) if confidences else None,
            reasoning="compared member-component exit reachability per vlan",
        )

    def _check_vlan(
        self,
        ctx: CheckContext,
        vid: int,
        findings: list[Finding],
        confidences: list[Confidence],
    ) -> Status:
        proposed_exit = ctx.proposed.exit_for(vid)
        stranded = [
            c for c in ctx.proposed.vlan_components(vid) if c.has_members and not c.reaches_exit
        ]
        if not stranded:
            return Status.PASS
        if proposed_exit.kind is ExitKind.NONE:
            findings.append(
                self._finding(
                    code="wired.l2.blackhole.exit_unlocatable",
                    severity=Severity.WARNING,
                    category=FindingCategory.OPERATIONAL,
                    confidence=Confidence(
                        level=ConfidenceLevel.LOW,
                        reasons=(f"no IRB and no boundary uplink found for vlan {vid}",),
                    ),
                    message=f"vlan {vid} has members but its exit cannot be located",
                    vid=vid,
                    nodes=sorted(n for c in stranded for n in c.nodes),
                )
            )
            return Status.INSUFFICIENT_DATA
        baseline_reaching = {
            frozenset(c.nodes)
            for c in ctx.baseline.vlan_components(vid)
            if c.has_members and c.reaches_exit
        }
        exit_conf = proposed_exit.confidence
        assert exit_conf is not None  # kind != NONE guarantees it
        confidences.append(exit_conf)
        worst = Status.PASS
        for comp in stranded:
            newly = any(comp.nodes & prev for prev in baseline_reaching)
            if not newly:
                findings.append(
                    self._finding(
                        code="wired.l2.blackhole.preexisting",
                        severity=Severity.INFO,
                        category=FindingCategory.NETWORK,
                        confidence=exit_conf,
                        message=(
                            f"vlan {vid}: component already had no exit path before the "
                            "delta (context)"
                        ),
                        vid=vid,
                        nodes=sorted(comp.nodes),
                    )
                )
                continue
            high = exit_conf.level is ConfidenceLevel.HIGH
            findings.append(
                self._finding(
                    code="wired.l2.blackhole.exit_lost",
                    severity=Severity.ERROR if high else Severity.WARNING,
                    category=FindingCategory.NETWORK,
                    confidence=exit_conf,
                    message=(
                        f"vlan {vid}: member segment loses its path to the "
                        f"{proposed_exit.kind} exit"
                    ),
                    vid=vid,
                    nodes=sorted(comp.nodes),
                )
            )
            worst = _aggregate([worst, Status.FAIL if high else Status.WARN])
        return worst

    def _finding(
        self,
        *,
        code: str,
        severity: Severity,
        category: FindingCategory,
        confidence: Confidence,
        message: str,
        vid: int,
        nodes: list[str],
    ) -> Finding:
        return Finding(
            source=FindingSource.CHECK,
            category=category,
            code=code,
            severity=severity,
            confidence=confidence,
            message=message,
            affected_entities=tuple(nodes),
            evidence={"vlan": vid, "component_nodes": nodes},
        )


_ORDER = [Status.PASS, Status.INSUFFICIENT_DATA, Status.WARN, Status.FAIL]


def _aggregate(statuses: list[Status]) -> Status:
    return max(statuses, key=_ORDER.index) if statuses else Status.PASS
```

- [ ] **Step 4: Run tests + quality gate**

Run: `uv run pytest tests/checks/ -q && uv run ruff check . && uv run mypy`
Expected: PASS, clean

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/checks tests/checks
git commit -m "Plan 4: wired.l2.blackhole check (exit contract, FAIL only at HIGH)"
```

---

### Task 9: `checks/wired/l2_vlan_segmentation.py`

**Files:**
- Create: `src/digital_twin/checks/wired/l2_vlan_segmentation.py`
- Test: `tests/checks/test_l2_vlan_segmentation.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/checks/test_l2_vlan_segmentation.py
"""l2.vlan_segmentation: split -> WARN(WARNING, HIGH); expansion/contraction
-> PASS with INFO. Purely structural — no intent judged."""

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.wired.l2_vlan_segmentation import L2VlanSegmentationCheck
from digital_twin.contracts import Severity
from digital_twin.ir import ConfidenceLevel, IRBuilder, IRCapability, Vlan, diff_ir
from tests.factories import access_port, link, sw, trunk_port


def _chain_ir(*links: tuple[str, str], devs=("A", "B", "C")):
    b = IRBuilder()
    for d in devs:
        b.add_device(sw(d))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    for d in devs:
        b.add_port(access_port(d, "acc", 10))
    for a, c in links:
        b.add_port(trunk_port(a, f"to-{c}", tagged=(10,)))
        b.add_port(trunk_port(c, f"to-{a}", tagged=(10,)))
        b.add_link(link(f"{a}:to-{c}", f"{c}:to-{a}"))
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def _ctx(baseline, proposed):
    return CheckContext(
        baseline=AnalysisContext(baseline),
        proposed=AnalysisContext(proposed),
        diff=diff_ir(baseline, proposed),
    )


def test_split_warns_high_confidence():
    base = _chain_ir(("A", "B"), ("B", "C"))  # one domain A-B-C
    prop = _chain_ir(("A", "B"))  # C cut off -> 2 components
    result = L2VlanSegmentationCheck().run(_ctx(base, prop))
    assert result.status is Status.WARN
    f = result.findings[0]
    assert f.severity is Severity.WARNING
    assert f.confidence.level is ConfidenceLevel.HIGH


def test_contraction_without_split_is_info_pass():
    base = _chain_ir(("A", "B"))
    prop = _chain_ir(("A", "B"), devs=("A", "B"))  # C (isolated member) gone entirely
    result = L2VlanSegmentationCheck().run(_ctx(base, prop))
    assert result.status is Status.PASS
    assert all(f.severity is Severity.INFO for f in result.findings)


def test_no_structural_change_passes_quietly():
    base = _chain_ir(("A", "B"), ("B", "C"))
    result = L2VlanSegmentationCheck().run(_ctx(base, _chain_ir(("A", "B"), ("B", "C"))))
    assert result.status is Status.PASS and result.findings == ()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/checks/test_l2_vlan_segmentation.py -q`
Expected: FAIL — ImportError

- [ ] **Step 3: Write the implementation**

```python
# src/digital_twin/checks/wired/l2_vlan_segmentation.py
"""wired.l2.vlan_segmentation — structural broadcast-domain change, no intent.

Per VLAN, compares the per-VLAN graph partition between IR and IR':
- SPLIT (a baseline component fragments into >=2 proposed components) ->
  WARN + WARNING finding, HIGH confidence.
- expansion/contraction (node set grows/shrinks without a split) -> PASS +
  INFO finding, HIGH confidence.
Deliberately does NOT judge whether the change is allowed (that needs intent).
Distinct from blackhole: segmentation = shape changed; blackhole = lost exit.
"""

from __future__ import annotations

from digital_twin.checks.base import (
    CheckContext,
    CheckResult,
    Coverage,
    CoverageState,
    Status,
)
from digital_twin.contracts import Finding, FindingCategory, FindingSource, Severity
from digital_twin.ir import Capability, Confidence, ConfidenceLevel, IRCapability, IRDiff

_HIGH = Confidence(level=ConfidenceLevel.HIGH)


class L2VlanSegmentationCheck:
    id = "wired.l2.vlan_segmentation"
    title = "Broadcast-domain shape change"
    domain = "wired.l2"
    default_severity = Severity.WARNING

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2})

    def applies_to(self, diff: IRDiff) -> bool:
        return any(diff.touches(k) for k in ("link", "port", "vlan", "device"))

    def run(self, ctx: CheckContext) -> CheckResult:
        findings: list[Finding] = []
        status = Status.PASS
        for vid in sorted(set(ctx.baseline.ir.vlans) | set(ctx.proposed.ir.vlans)):
            base_comps = [set(c.nodes) for c in ctx.baseline.vlan_components(vid)]
            prop_comps = [set(c.nodes) for c in ctx.proposed.vlan_components(vid)]
            split = any(
                len([p for p in prop_comps if p & b]) >= 2 for b in base_comps
            )
            if split:
                status = Status.WARN
                findings.append(
                    self._finding(
                        code="wired.l2.vlan_segmentation.split",
                        severity=Severity.WARNING,
                        message=f"vlan {vid}: broadcast domain partitioned by the delta",
                        vid=vid,
                        base=base_comps,
                        prop=prop_comps,
                    )
                )
                continue
            base_nodes = set().union(*base_comps) if base_comps else set()
            prop_nodes = set().union(*prop_comps) if prop_comps else set()
            if base_nodes != prop_nodes:
                grew, shrank = prop_nodes - base_nodes, base_nodes - prop_nodes
                findings.append(
                    self._finding(
                        code="wired.l2.vlan_segmentation.reshape",
                        severity=Severity.INFO,
                        message=(
                            f"vlan {vid}: domain "
                            f"{'expands to ' + str(sorted(grew)) if grew else ''}"
                            f"{' and ' if grew and shrank else ''}"
                            f"{'stops reaching ' + str(sorted(shrank)) if shrank else ''}"
                        ),
                        vid=vid,
                        base=base_comps,
                        prop=prop_comps,
                    )
                )
        return CheckResult(
            check_id=self.id,
            status=status,
            findings=tuple(findings),
            coverage=Coverage(state=CoverageState.COMPLETE),
            confidence=_HIGH,
            reasoning="compared per-vlan graph partitions baseline vs proposed",
        )

    def _finding(
        self,
        *,
        code: str,
        severity: Severity,
        message: str,
        vid: int,
        base: list[set[str]],
        prop: list[set[str]],
    ) -> Finding:
        return Finding(
            source=FindingSource.CHECK,
            category=FindingCategory.NETWORK,
            code=code,
            severity=severity,
            confidence=_HIGH,
            message=message,
            evidence={
                "vlan": vid,
                "baseline_components": [sorted(c) for c in base],
                "proposed_components": [sorted(c) for c in prop],
            },
        )
```

- [ ] **Step 4: Run tests + quality gate**

Run: `uv run pytest tests/checks/ -q && uv run ruff check . && uv run mypy`
Expected: PASS, clean

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/checks tests/checks
git commit -m "Plan 4: wired.l2.vlan_segmentation check (split=WARN, reshape=INFO)"
```

---

### Task 10: `checks/wired/client_impact.py`

**Files:**
- Create: `src/digital_twin/checks/wired/client_impact.py`
- Test: `tests/checks/test_client_impact.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/checks/test_client_impact.py
"""client.impact: enumerate CURRENTLY-CONNECTED clients whose connectivity the
delta changes (vlan_move / disconnect / blackhole), WARN when >=1 affected,
HIGH confidence (observed clients), currently-connected-only caveat in coverage."""

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.wired.client_impact import ClientImpactCheck
from digital_twin.ir import IRBuilder, IRCapability, Vlan, diff_ir
from tests.factories import access_port, irb, link, sw, trunk_port, wired_client


def _ir(*, acc_vlan: int = 10, with_client: bool = True, connected: bool = True):
    b = IRBuilder()
    b.add_device(sw("A")).add_device(sw("B"))
    for vid in (10, 20):
        b.add_vlan(Vlan(vlan_id=vid, name=f"v{vid}", scope="s1"))
    b.add_port(access_port("A", "acc", acc_vlan))
    b.add_port(trunk_port("A", "up", tagged=(10, 20)))
    b.add_port(trunk_port("B", "down", tagged=(10, 20)))
    if connected:
        b.add_link(link("A:up", "B:down"))
    b.add_l3intf(irb("B", 10))
    b.add_l3intf(irb("B", 20))
    if with_client:
        b.add_client(wired_client("aa:aa", "A:acc", vlan=acc_vlan))
    b.with_capability(IRCapability.WIRED_L2).with_capability(IRCapability.CLIENTS_ACTIVE)
    b.with_capability(IRCapability.L3_EXITS)
    return b.build()


def _ctx(baseline, proposed):
    return CheckContext(
        baseline=AnalysisContext(baseline),
        proposed=AnalysisContext(proposed),
        diff=diff_ir(baseline, proposed),
    )


def test_access_vlan_change_flags_vlan_move():
    result = ClientImpactCheck().run(_ctx(_ir(acc_vlan=10), _ir(acc_vlan=20)))
    assert result.status is Status.WARN
    f = result.findings[0]
    assert f.evidence["impacts"][0]["impact"] == "vlan_move"
    assert f.evidence["impacts"][0]["mac"] == "aa:aa"


def test_client_in_blackholed_segment_flags_blackhole():
    result = ClientImpactCheck().run(
        _ctx(_ir(connected=True), _ir(connected=False))
    )
    assert result.status is Status.WARN
    impacts = result.findings[0].evidence["impacts"]
    assert any(i["impact"] == "blackhole" and i["mac"] == "aa:aa" for i in impacts)


def test_no_clients_affected_passes_with_caveat():
    result = ClientImpactCheck().run(_ctx(_ir(with_client=False), _ir(with_client=False)))
    assert result.status is Status.PASS
    assert any("currently-connected" in n for n in result.coverage.notes)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/checks/test_client_impact.py -q`
Expected: FAIL — ImportError

- [ ] **Step 3: Write the implementation**

```python
# src/digital_twin/checks/wired/client_impact.py
"""wired.client.impact — who is affected, RIGHT NOW (enrichment over IRDiff).

Enumerates currently-connected clients whose connectivity the delta changes:
- vlan_move:   the client's access port changes native VLAN (still up).
- disconnect:  the client's attach port disappears from IR'.
- blackhole:   the client's VLAN component loses exit reach in IR'.
WARN (WARNING, network) when >=1 client is affected; HIGH confidence (devices
report their own clients). The currently-connected-only caveat is a COVERAGE
note — not-yet-connected clients are out of observational reach (spec).
"""

from __future__ import annotations

from typing import Any

from digital_twin.checks.base import (
    CheckContext,
    CheckResult,
    Coverage,
    CoverageState,
    Status,
)
from digital_twin.contracts import Finding, FindingCategory, FindingSource, Severity
from digital_twin.ir import Capability, Confidence, ConfidenceLevel, IRCapability, IRDiff
from digital_twin.ir.entities import AttachKind
from digital_twin.ir.indexes import node_for, vc_root_map

_HIGH = Confidence(level=ConfidenceLevel.HIGH)
_CAVEAT = "currently-connected clients only (not-yet-connected clients are unobservable)"


class ClientImpactCheck:
    id = "wired.client.impact"
    title = "Active-client impact"
    domain = "wired.client"
    default_severity = Severity.WARNING

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2, IRCapability.CLIENTS_ACTIVE})

    def applies_to(self, diff: IRDiff) -> bool:
        return any(diff.touches(k) for k in ("port", "link", "vlan", "client", "l3intf"))

    def run(self, ctx: CheckContext) -> CheckResult:
        impacts: list[dict[str, Any]] = []
        for client in ctx.baseline.ir.clients:
            impact = self._impact_of(ctx, client)
            if impact is not None:
                impacts.append(impact)
        findings: tuple[Finding, ...] = ()
        if impacts:
            findings = (
                Finding(
                    source=FindingSource.CHECK,
                    category=FindingCategory.NETWORK,
                    code="wired.client.impact.active_clients",
                    severity=Severity.WARNING,
                    confidence=_HIGH,
                    message=f"{len(impacts)} currently-connected client(s) affected by the delta",
                    affected_entities=tuple(i["mac"] for i in impacts),
                    evidence={"impacts": impacts},
                ),
            )
        return CheckResult(
            check_id=self.id,
            status=Status.WARN if impacts else Status.PASS,
            findings=findings,
            coverage=Coverage(state=CoverageState.COMPLETE, notes=(_CAVEAT,)),
            confidence=_HIGH,
            reasoning=f"evaluated {len(ctx.baseline.ir.clients)} observed clients",
        )

    def _impact_of(self, ctx: CheckContext, client: Any) -> dict[str, Any] | None:
        base_ir, prop_ir = ctx.baseline.ir, ctx.proposed.ir
        if client.attach_kind is AttachKind.PORT:
            base_port = base_ir.ports.get(client.attach_id)
            prop_port = prop_ir.ports.get(client.attach_id)
            if base_port is None:
                return None
            if prop_port is None:
                return self._entry(client, "disconnect", "attach port removed")
            if (
                base_port.native_vlan is not None
                and prop_port.native_vlan != base_port.native_vlan
            ):
                return self._entry(
                    client,
                    "vlan_move",
                    f"access vlan {base_port.native_vlan} -> {prop_port.native_vlan}",
                )
        vlan = client.vlan
        if vlan is not None and vlan in prop_ir.vlans:
            node = self._attach_node(ctx, client)
            if node is not None:
                for comp in ctx.proposed.vlan_components(vlan):
                    if node in comp.nodes and comp.has_members and not comp.reaches_exit:
                        for base_comp in ctx.baseline.vlan_components(vlan):
                            if node in base_comp.nodes and base_comp.reaches_exit:
                                return self._entry(
                                    client, "blackhole", f"vlan {vlan} segment loses its exit"
                                )
        return None

    def _attach_node(self, ctx: CheckContext, client: Any) -> str | None:
        ir = ctx.baseline.ir
        vc_root = vc_root_map(ir)
        if client.attach_kind is AttachKind.PORT:
            port = ir.ports.get(client.attach_id)
            return node_for(vc_root, port.device_id) if port else None
        if client.attach_kind is AttachKind.AP:
            return node_for(vc_root, client.attach_id)
        return None

    def _entry(self, client: Any, impact: str, detail: str) -> dict[str, Any]:
        return {
            "mac": client.mac,
            "vlan": client.vlan,
            "attachment": client.attach_id,
            "impact": impact,
            "detail": detail,
        }
```

- [ ] **Step 4: Run tests + quality gate**

Run: `uv run pytest tests/checks/ -q && uv run ruff check . && uv run mypy`
Expected: PASS, clean. *Note: `IR.ports` is a `Mapping[str, Port]` (verify `.get` exists —
the lldp tests use `ir.port(pid)` which raises on missing; `.ports.get` is the safe form here).*

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/checks tests/checks
git commit -m "Plan 4: wired.client.impact check (vlan_move/disconnect/blackhole)"
```

---

### Task 11: `verdict/` — decision + assembly

**Files:**
- Create: `src/digital_twin/verdict/__init__.py`
- Create: `src/digital_twin/verdict/decision.py`
- Create: `src/digital_twin/verdict/coverage.py`
- Create: `src/digital_twin/verdict/confidence_summary.py`
- Create: `src/digital_twin/verdict/verdict.py`
- Test: `tests/verdict/__init__.py`, `tests/verdict/test_decision.py`, `tests/verdict/test_assembly.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/verdict/__init__.py  (empty)
```

```python
# tests/verdict/test_decision.py
"""The decision table (spec): UNKNOWN > UNSAFE > REVIEW > SAFE, first match wins.
A blind spot can NEVER resolve to SAFE."""

from digital_twin.checks.base import CheckResult, Coverage, CoverageState, Status
from digital_twin.contracts import (
    Finding,
    FindingCategory,
    FindingSource,
    Rejection,
    Severity,
)
from digital_twin.ir import Confidence, ConfidenceLevel
from digital_twin.verdict.decision import Decision, DecisionInputs, decide


def _finding(severity, category=FindingCategory.NETWORK, level=ConfidenceLevel.HIGH):
    return Finding(
        source=FindingSource.CHECK,
        category=category,
        code="t",
        severity=severity,
        confidence=Confidence(level=level),
        message="m",
    )


def _result(status, findings=(), coverage_state=CoverageState.COMPLETE):
    return CheckResult(
        check_id="c",
        status=status,
        findings=tuple(findings),
        coverage=Coverage(state=coverage_state),
        confidence=None,
        reasoning="",
    )


def _inputs(**kw):
    defaults = dict(
        rejections=(), l0_fatal=False, baseline_unavailable=False, check_results=()
    )
    return DecisionInputs(**{**defaults, **kw})


def test_rejection_is_unknown():
    d, reasons = decide(_inputs(rejections=(Rejection(stage="object_gate", reasons=("x",)),)))
    assert d is Decision.UNKNOWN and "object_gate" in reasons[0]


def test_no_baseline_is_unknown():
    d, _ = decide(_inputs(baseline_unavailable=True))
    assert d is Decision.UNKNOWN


def test_network_error_finding_is_unsafe():
    res = _result(Status.FAIL, [_finding(Severity.ERROR)])
    d, _ = decide(_inputs(check_results=(res,)))
    assert d is Decision.UNSAFE


def test_operational_error_finding_is_not_unsafe():
    res = _result(Status.CHECK_ERROR, [_finding(Severity.ERROR, FindingCategory.OPERATIONAL)])
    d, _ = decide(_inputs(check_results=(res,)))
    assert d is Decision.REVIEW  # crash floors at REVIEW, never UNSAFE


def test_warning_finding_is_review():
    res = _result(Status.WARN, [_finding(Severity.WARNING)])
    assert decide(_inputs(check_results=(res,)))[0] is Decision.REVIEW


def test_insufficient_data_is_review():
    res = _result(Status.INSUFFICIENT_DATA, coverage_state=CoverageState.INSUFFICIENT)
    assert decide(_inputs(check_results=(res,)))[0] is Decision.REVIEW


def test_low_confidence_finding_floors_review():
    res = _result(Status.PASS, [_finding(Severity.INFO, level=ConfidenceLevel.LOW)])
    assert decide(_inputs(check_results=(res,)))[0] is Decision.REVIEW


def test_partial_coverage_floors_review():
    res = _result(Status.PASS, coverage_state=CoverageState.PARTIAL)
    assert decide(_inputs(check_results=(res,)))[0] is Decision.REVIEW


def test_all_clean_is_safe():
    res = _result(Status.PASS, [_finding(Severity.INFO)])
    na = _result(Status.NOT_APPLICABLE, coverage_state=CoverageState.NOT_APPLICABLE)
    d, reasons = decide(_inputs(check_results=(res, na)))
    assert d is Decision.SAFE and reasons


def test_precedence_unknown_beats_unsafe():
    res = _result(Status.FAIL, [_finding(Severity.ERROR)])
    d, _ = decide(
        _inputs(
            rejections=(Rejection(stage="envelope", reasons=("bad",)),), check_results=(res,)
        )
    )
    assert d is Decision.UNKNOWN
```

```python
# tests/verdict/test_assembly.py
from digital_twin.checks.base import CheckResult, Coverage, CoverageState, Status
from digital_twin.contracts import Finding, FindingCategory, FindingSource, Severity
from digital_twin.ir import Confidence, ConfidenceLevel, IRDiff
from digital_twin.verdict.decision import Decision, DecisionInputs
from digital_twin.verdict.verdict import assemble


def test_assemble_flattens_findings_and_rolls_up():
    f = Finding(
        source=FindingSource.CHECK,
        category=FindingCategory.NETWORK,
        code="x",
        severity=Severity.WARNING,
        confidence=Confidence(level=ConfidenceLevel.LOW, reasons=("one-sided",)),
        message="m",
    )
    res = CheckResult(
        check_id="wired.l2.loop",
        status=Status.WARN,
        findings=(f,),
        coverage=Coverage(state=CoverageState.COMPLETE),
        confidence=Confidence(level=ConfidenceLevel.LOW),
        reasoning="",
    )
    l0 = Finding(
        source=FindingSource.ADAPTER,
        category=FindingCategory.OPERATIONAL,
        code="l0.schema.violation",
        severity=Severity.ERROR,
        confidence=Confidence(level=ConfidenceLevel.HIGH),
        message="bad type",
    )
    verdict = assemble(
        inputs=DecisionInputs(
            rejections=(), l0_fatal=False, baseline_unavailable=False, check_results=(res,)
        ),
        adapter_findings=(l0,),
        ir_diff=IRDiff((), (), ()),
    )
    assert verdict.decision is Decision.REVIEW
    assert {x.code for x in verdict.findings} == {"x", "l0.schema.violation"}  # two sources, one list
    assert verdict.overall_severity is Severity.ERROR
    assert verdict.confidence_summary.low == 1 and verdict.confidence_summary.high == 1
    assert verdict.coverage["wired.l2"].complete == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/verdict/ -q`
Expected: FAIL — ModuleNotFoundError

- [ ] **Step 3: Write the implementation**

```python
# src/digital_twin/verdict/__init__.py
"""Findings -> decision — PURE; imports contracts + checks, never the reverse."""
```

```python
# src/digital_twin/verdict/decision.py
"""The agent-facing decision: SAFE | REVIEW | UNSAFE | UNKNOWN.

Deterministic precedence (first match wins): UNKNOWN > UNSAFE > REVIEW > SAFE.
Key invariant (spec): a blind spot — INSUFFICIENT_DATA, partial coverage,
non-HIGH confidence, or a crashed check — can NEVER resolve to SAFE; it floors
at REVIEW. Operational findings never drive UNSAFE.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from digital_twin.checks.base import CheckResult, CoverageState, Status
from digital_twin.contracts import FindingCategory, Rejection, Severity
from digital_twin.ir import ConfidenceLevel


class Decision(StrEnum):
    SAFE = "safe"
    REVIEW = "review"
    UNSAFE = "unsafe"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DecisionInputs:
    rejections: tuple[Rejection, ...]  # gates/apply (any -> UNKNOWN)
    l0_fatal: bool  # structurally-fatal L0 short-circuit
    baseline_unavailable: bool  # FetchError / ingest not ok
    check_results: tuple[CheckResult, ...]


def decide(inputs: DecisionInputs) -> tuple[Decision, tuple[str, ...]]:
    # 1) UNKNOWN — could not simulate
    unknown: list[str] = []
    for r in inputs.rejections:
        unknown.extend(f"UNSUPPORTED [{r.stage}]: {reason}" for reason in r.reasons)
    if inputs.l0_fatal:
        unknown.append("structurally-fatal L0 violation short-circuited the run")
    if inputs.baseline_unavailable:
        unknown.append("no usable baseline state (fetch/ingest failed)")
    if unknown:
        return Decision.UNKNOWN, tuple(unknown)

    findings = [f for res in inputs.check_results for f in res.findings]

    # 2) UNSAFE — confident network breakage only
    unsafe = [
        f"{f.code}: {f.message}"
        for f in findings
        if f.category is FindingCategory.NETWORK
        and f.severity in (Severity.ERROR, Severity.CRITICAL)
    ]
    if unsafe:
        return Decision.UNSAFE, tuple(unsafe)

    # 3) REVIEW — any warning or blind spot
    review: list[str] = []
    review.extend(
        f"{f.code}: {f.message}" for f in findings if f.severity is Severity.WARNING
    )
    review.extend(
        f"{res.check_id}: {res.status}"
        for res in inputs.check_results
        if res.status in (Status.INSUFFICIENT_DATA, Status.CHECK_ERROR)
    )
    review.extend(
        f"{f.code}: confidence {f.confidence.level.name}"
        for f in findings
        if f.confidence.level is not ConfidenceLevel.HIGH
    )
    review.extend(
        f"{res.check_id}: coverage {res.coverage.state}"
        for res in inputs.check_results
        if res.coverage.state in (CoverageState.PARTIAL, CoverageState.INSUFFICIENT)
    )
    if review:
        return Decision.REVIEW, tuple(review)

    # 4) SAFE — evaluated, covered, high confidence, clean
    evaluated = [
        r.check_id for r in inputs.check_results if r.status is not Status.NOT_APPLICABLE
    ]
    return Decision.SAFE, (
        f"all applicable checks passed ({', '.join(evaluated) or 'none applicable'}); "
        "coverage complete; confidence HIGH",
    )
```

```python
# src/digital_twin/verdict/coverage.py
"""Per-domain coverage rollup: domain -> counts of check coverage states."""

from __future__ import annotations

from dataclasses import dataclass

from digital_twin.checks.base import CheckResult


@dataclass(frozen=True)
class DomainCoverage:
    complete: int = 0
    partial: int = 0
    insufficient: int = 0
    not_applicable: int = 0


def rollup(results: tuple[CheckResult, ...], domains: dict[str, str]) -> dict[str, DomainCoverage]:
    """domains: check_id -> domain (from the registered checks)."""
    counts: dict[str, dict[str, int]] = {}
    for res in results:
        domain = domains.get(res.check_id, "unknown")
        c = counts.setdefault(
            domain, {"complete": 0, "partial": 0, "insufficient": 0, "not_applicable": 0}
        )
        c[res.coverage.state.value] += 1
    return {d: DomainCoverage(**c) for d, c in counts.items()}
```

```python
# src/digital_twin/verdict/confidence_summary.py
"""Confidence rollup across every finding (counts + the LOW/MEDIUM reasons)."""

from __future__ import annotations

from dataclasses import dataclass

from digital_twin.contracts import Finding
from digital_twin.ir import ConfidenceLevel


@dataclass(frozen=True)
class ConfidenceSummary:
    high: int
    medium: int
    low: int
    reasons: tuple[str, ...]  # why anything is below HIGH


def summarize(findings: tuple[Finding, ...]) -> ConfidenceSummary:
    high = sum(1 for f in findings if f.confidence.level is ConfidenceLevel.HIGH)
    medium = sum(1 for f in findings if f.confidence.level is ConfidenceLevel.MEDIUM)
    low = sum(1 for f in findings if f.confidence.level is ConfidenceLevel.LOW)
    reasons = tuple(
        r
        for f in findings
        if f.confidence.level is not ConfidenceLevel.HIGH
        for r in f.confidence.reasons
    )
    return ConfidenceSummary(high=high, medium=medium, low=low, reasons=reasons)
```

```python
# src/digital_twin/verdict/verdict.py
"""Verdict assembly: one document, three independent axes (findings, coverage,
confidence) + the single agent-facing decision. Two finding sources (adapter L0
+ checks) flatten into ONE list; check_results stay as the per-check audit."""

from __future__ import annotations

from dataclasses import dataclass

from digital_twin.checks.base import CheckResult
from digital_twin.contracts import Finding, Severity
from digital_twin.ir import IRDiff

from .confidence_summary import ConfidenceSummary, summarize
from .coverage import DomainCoverage, rollup
from .decision import Decision, DecisionInputs, decide

_SEVERITY_ORDER = [Severity.INFO, Severity.WARNING, Severity.ERROR, Severity.CRITICAL]


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    decision_reasons: tuple[str, ...]
    overall_severity: Severity | None  # None when there are no findings
    findings: tuple[Finding, ...]
    check_results: tuple[CheckResult, ...]
    coverage: dict[str, DomainCoverage]
    confidence_summary: ConfidenceSummary
    ir_diff: IRDiff


def assemble(
    *,
    inputs: DecisionInputs,
    adapter_findings: tuple[Finding, ...] = (),
    ir_diff: IRDiff,
    domains: dict[str, str] | None = None,
) -> Verdict:
    decision, reasons = decide(inputs)
    findings = (*adapter_findings, *(f for r in inputs.check_results for f in r.findings))
    overall = (
        max((f.severity for f in findings), key=_SEVERITY_ORDER.index) if findings else None
    )
    check_domains = domains or {
        r.check_id: r.check_id.rsplit(".", 1)[0] for r in inputs.check_results
    }
    return Verdict(
        decision=decision,
        decision_reasons=reasons,
        overall_severity=overall,
        findings=findings,
        check_results=inputs.check_results,
        coverage=rollup(inputs.check_results, check_domains),
        confidence_summary=summarize(findings),
        ir_diff=ir_diff,
    )
```

- [ ] **Step 4: Run tests + quality gate**

Run: `uv run pytest tests/verdict/ -q && uv run ruff check . && uv run mypy`
Expected: PASS, clean.

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/verdict tests/verdict
git commit -m "Plan 4: verdict (decision precedence, coverage/confidence rollups, assembly)"
```

---

### Task 12: End-to-end Plan-4 slice + the four checks registered together

**Files:**
- Create: `src/digital_twin/checks/wired/__init__.py` (modify: export ALL_WIRED_CHECKS)
- Test: `tests/test_plan4_flow.py`

- [ ] **Step 1: Update `checks/wired/__init__.py`**

```python
# src/digital_twin/checks/wired/__init__.py
"""The four M1 wired checks. ALL_WIRED_CHECKS is the default registry payload."""

from .client_impact import ClientImpactCheck
from .l2_blackhole import L2BlackholeCheck
from .l2_loop import L2LoopCheck
from .l2_vlan_segmentation import L2VlanSegmentationCheck

ALL_WIRED_CHECKS = [
    L2LoopCheck(),
    L2BlackholeCheck(),
    L2VlanSegmentationCheck(),
    ClientImpactCheck(),
]

__all__ = [
    "ALL_WIRED_CHECKS",
    "ClientImpactCheck",
    "L2BlackholeCheck",
    "L2LoopCheck",
    "L2VlanSegmentationCheck",
]
```

- [ ] **Step 2: Write the end-to-end test**

```python
# tests/test_plan4_flow.py
"""Plan-4 slice: two IRs -> diff -> registry (all four checks, gating order) ->
verdict/decision. The full ChangePlan->verdict pipeline is Plan 5; this proves
the reasoning half composes: a cut uplink with an active client yields UNSAFE
with blackhole + segmentation + client findings; a cosmetic no-op yields SAFE."""

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.registry import CheckRegistry
from digital_twin.checks.wired import ALL_WIRED_CHECKS
from digital_twin.ir import IRBuilder, IRCapability, Vlan, diff_ir
from digital_twin.verdict.decision import Decision, DecisionInputs
from digital_twin.verdict.verdict import assemble
from tests.factories import access_port, irb, link, sw, trunk_port, wired_client

ALL_CAPS = (
    IRCapability.WIRED_L2,
    IRCapability.L3_EXITS,
    IRCapability.CLIENTS_ACTIVE,
    IRCapability.STP_STATE,
)


def _site(*, connected: bool):
    b = IRBuilder()
    b.add_device(sw("ACCESS")).add_device(sw("CORE"))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    b.add_port(access_port("ACCESS", "acc", 10))
    b.add_port(trunk_port("ACCESS", "up", tagged=(10,)))
    b.add_port(trunk_port("CORE", "down", tagged=(10,)))
    if connected:
        b.add_link(link("ACCESS:up", "CORE:down"))
    b.add_l3intf(irb("CORE", 10))
    b.add_client(wired_client("aa:bb", "ACCESS:acc", vlan=10))
    for cap in ALL_CAPS:
        b.with_capability(cap)
    return b.build()


def _verdict(baseline, proposed):
    diff = diff_ir(baseline, proposed)
    ctx = CheckContext(
        baseline=AnalysisContext(baseline), proposed=AnalysisContext(proposed), diff=diff
    )
    results = CheckRegistry(ALL_WIRED_CHECKS).run_all(ctx)
    return assemble(
        inputs=DecisionInputs(
            rejections=(), l0_fatal=False, baseline_unavailable=False, check_results=results
        ),
        ir_diff=diff,
    )


def test_cutting_the_uplink_is_unsafe_with_three_findings():
    verdict = _verdict(_site(connected=True), _site(connected=False))
    assert verdict.decision is Decision.UNSAFE
    codes = {f.code for f in verdict.findings}
    assert "wired.l2.blackhole.exit_lost" in codes
    assert "wired.l2.vlan_segmentation.split" in codes
    assert "wired.client.impact.active_clients" in codes
    by_id = {r.check_id: r for r in verdict.check_results}
    assert by_id["wired.l2.blackhole"].status is Status.FAIL


def test_identical_irs_yield_safe_via_not_applicable():
    verdict = _verdict(_site(connected=True), _site(connected=True))
    assert verdict.decision is Decision.SAFE
    assert all(r.status is Status.NOT_APPLICABLE for r in verdict.check_results)


def test_missing_client_capability_floors_review_not_safe():
    def _site_no_clients(connected: bool):
        b = IRBuilder()
        b.add_device(sw("ACCESS")).add_device(sw("CORE"))
        b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
        b.add_port(access_port("ACCESS", "acc", 10))
        b.add_port(trunk_port("ACCESS", "up", tagged=(10,)))
        b.add_port(trunk_port("CORE", "down", tagged=(10,)))
        if connected:
            b.add_link(link("ACCESS:up", "CORE:down"))
        b.add_l3intf(irb("CORE", 10))
        b.with_capability(IRCapability.WIRED_L2).with_capability(IRCapability.L3_EXITS)
        return b.build()

    # an in-domain change with client data missing: client.impact INSUFFICIENT_DATA
    base, prop = _site_no_clients(True), _site_no_clients(False)
    verdict = _verdict(base, prop)
    by_id = {r.check_id: r for r in verdict.check_results}
    assert by_id["wired.client.impact"].status is Status.INSUFFICIENT_DATA
    assert verdict.decision is not Decision.SAFE  # blind spot can never be SAFE
```

- [ ] **Step 3: Run the slice + the FULL quality gate**

Run: `uv run pytest tests/test_plan4_flow.py -q`
Expected: PASS
Run: `uv run ruff format . && uv run ruff check . && uv run mypy && uv run pytest -q`
Expected: all clean, all tests pass

- [ ] **Step 4: Commit**

```bash
git add src/digital_twin/checks tests/test_plan4_flow.py
git commit -m "Plan 4: e2e slice (four checks + registry + verdict compose)"
```

---

### Task 13: Public API surface + plan sync

**Files:**
- Modify: `tests/test_public_api.py`
- Modify: `docs/superpowers/plans/2026-06-09-network-digital-twin-analysis-checks-verdict.md` (check boxes)

- [ ] **Step 1: Extend the public-API test** (follow the existing style)

```python
def test_plan4_public_api():
    from digital_twin.analysis.context import AnalysisContext
    from digital_twin.analysis.cycles import Cycle, find_cycles
    from digital_twin.analysis.exits import ExitKind, ExitResolution, resolve_exit
    from digital_twin.analysis.vlan_reachability import VlanComponent
    from digital_twin.checks.base import (
        Check,
        CheckContext,
        CheckResult,
        Coverage,
        CoverageState,
        Status,
    )
    from digital_twin.checks.registry import CheckRegistry
    from digital_twin.checks.wired import ALL_WIRED_CHECKS
    from digital_twin.verdict.decision import Decision, DecisionInputs, decide
    from digital_twin.verdict.verdict import Verdict, assemble

    assert len(ALL_WIRED_CHECKS) == 4
    assert all(callable(f) for f in (find_cycles, resolve_exit, decide, assemble))
    assert all(
        x is not None
        for x in (
            AnalysisContext, Cycle, ExitKind, ExitResolution, VlanComponent,
            Check, CheckContext, CheckResult, Coverage, CoverageState, Status,
            CheckRegistry, Decision, DecisionInputs, Verdict,
        )
    )
```

- [ ] **Step 2: Full gate**

Run: `uv run ruff format . && uv run ruff check . && uv run mypy && uv run pytest -q`
Expected: all clean

- [ ] **Step 3: Mark all checkboxes in this plan document, then commit**

```bash
git add tests/test_public_api.py docs/superpowers/plans/2026-06-09-network-digital-twin-analysis-checks-verdict.md
git commit -m "Plan 4: public API surface + plan doc synced"
```

---

## Acceptance (Plan 4 exit)

1. `uv run pytest -q` — every test green (existing 191 plus the new analysis/checks/verdict suites).
2. `uv run ruff check .` + `uv run mypy` — clean (strict).
3. The e2e slice proves on synthetic IRs: cutting an uplink with an active client → `UNSAFE` with blackhole(FAIL) + segmentation(WARN) + client-impact(WARN) findings; identical IRs → `SAFE` via `NOT_APPLICABLE`; missing client capability on an in-domain change → `INSUFFICIENT_DATA` → never `SAFE`.
4. Spec invariants hold and are test-pinned: gating order (`applies_to` before `requires`), crash isolation (`CHECK_ERROR` → operational → `REVIEW`), FAIL-only-at-HIGH (blackhole + loop), MIN-confidence composition, pre-existing conditions = context not cause, decision precedence `UNKNOWN > UNSAFE > REVIEW > SAFE`.
5. Everything is pure — no I/O anywhere in `analysis/`, `checks/`, `verdict/`.

**Explicitly deferred (per spec, not this plan):** `engine/pipeline.py` wiring ChangePlan→verdict end to end, `verdict/state_meta.py` freshness view (needs the pipeline's RawSiteState plumbing — Plan 5), drivers/observability/replay, golden scenarios GS1–GS8 against real org data (Plan 5), `rules/` declarative layer (deferred beyond M1).
