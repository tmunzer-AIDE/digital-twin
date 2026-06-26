# L2 Isolation Over-Severance Fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop `wired.l2.isolation` from flagging the surviving majority of an L2 domain as "severed" when a few leaf ports are disabled.

**Architecture:** Add a physical-graph exit-anchor helper (`exit_anchor_nodes`) that reuses `resolve_exit`'s two exit kinds, then add one guard to the isolation loop: a proposed fragment that still contains an exit anchor is not L2-isolated, so it is skipped. Suppression is grounded-only — when no fragment keeps an anchor, every occupied strict-subset is still flagged (today's conservative, never-false-SAFE behavior).

**Tech Stack:** Python 3.14, uv, pytest, ruff (100-col), mypy (strict on `src`, not tests). networkx.

## Global Constraints

- **Never-false-SAFE:** the only suppression is "this fragment itself still contains a proposed exit anchor." No size/majority/"pick a survivor" heuristic. When no fragment retains an anchor, flag every occupied strict-subset (current behavior).
- **Anchors are computed on the PROPOSED IR** (`ctx.proposed.ir`) — an exit the delta removes must drop out of the anchor set.
- **Exit anchor = gateway-role device ∪ device owning a routed `IRB`/`SVI` L3 interface.** `WAN`/`LOOPBACK` L3 roles are NOT exits.
- **No change** to occupant counting, the severed-link confidence calc, severity (`ERROR if HIGH else WARNING`), subject, message, or evidence — only *which* fragments are eligible.
- **Gate (run before every commit that touches `src`):** `uv run pytest tests -q && uv run ruff check . && uv run mypy src`. Pyright/IDE diagnostics are noise.
- **Commit trailer:** `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

## File Structure

- `src/digital_twin/analysis/exits.py` **(modify)** — add `exit_anchor_nodes(ir) -> set[str]`; add the `L3Role` import.
- `src/digital_twin/checks/wired/l2_isolation.py` **(modify)** — compute anchors once, add the one-line skip guard, update the module docstring.
- `tests/analysis/test_exits.py` **(modify)** — unit tests for `exit_anchor_nodes`.
- `tests/checks/test_l2_isolation.py` **(modify)** — new behavior tests; the existing three stay green.
- `tests/golden/builders.py` **(modify)** + `tests/golden/test_golden_scenarios.py` **(modify)** — a headline golden reproducing the reported topology end-to-end (backbone-connected L3 switch + leaf AP/client ports; disable the leaf ports while the backbone stays up), asserting `isolation.severed` flags only the leaves and the survivors are absent.

---

## Task 1: `exit_anchor_nodes` helper

**Files:**
- Modify: `src/digital_twin/analysis/exits.py`
- Test: `tests/analysis/test_exits.py`

**Interfaces:**
- Produces: `def exit_anchor_nodes(ir: IR) -> set[str]` — the set of VC-folded graph nodes that are network exits (gateway-role devices ∪ devices owning a routed IRB/SVI).

- [ ] **Step 1: Write the failing tests**

Append to `tests/analysis/test_exits.py`:

```python
from digital_twin.analysis.exits import exit_anchor_nodes
from digital_twin.ir import IRBuilder
from digital_twin.ir.entities import Device, DeviceRole, L3Intf, L3Role, Vlan


def test_exit_anchor_nodes_collects_gateway_and_irb_svi():
    b = IRBuilder()
    b.add_device(Device(id="gw", role=DeviceRole.GATEWAY, site="s1"))
    b.add_device(Device(id="core", role=DeviceRole.SWITCH, site="s1"))
    b.add_device(Device(id="acc", role=DeviceRole.SWITCH, site="s1"))
    b.add_vlan(Vlan(vlan_id=10, name="a", scope="s1"))
    b.add_vlan(Vlan(vlan_id=20, name="b", scope="s1"))
    b.add_l3intf(L3Intf(device_id="core", role=L3Role.IRB, vlan_id=10))
    b.add_l3intf(L3Intf(device_id="acc", role=L3Role.SVI, vlan_id=20))
    assert exit_anchor_nodes(b.build()) == {"gw", "core", "acc"}


def test_exit_anchor_nodes_excludes_wan_loopback_and_plain_switch():
    b = IRBuilder()
    b.add_device(Device(id="sw1", role=DeviceRole.SWITCH, site="s1"))
    b.add_device(Device(id="gwdev", role=DeviceRole.GATEWAY, site="s1"))
    # WAN / LOOPBACK L3 interfaces are NOT exits; gwdev is an anchor by ROLE only
    b.add_l3intf(L3Intf(device_id="gwdev", role=L3Role.WAN, port="ge-0/0/0"))
    b.add_l3intf(L3Intf(device_id="sw1", role=L3Role.LOOPBACK, port="lo0"))
    assert exit_anchor_nodes(b.build()) == {"gwdev"}


def test_exit_anchor_nodes_ignores_unresolved_irb_without_vlan():
    # an IRB/SVI not tied to a concrete VLAN is unresolved/malformed -> not an exit
    b = IRBuilder()
    b.add_device(Device(id="sw", role=DeviceRole.SWITCH, site="s1"))
    b.add_l3intf(L3Intf(device_id="sw", role=L3Role.IRB, vlan_id=None, port="irb"))
    assert exit_anchor_nodes(b.build()) == set()


def test_exit_anchor_nodes_folds_vc_members_to_root():
    b = IRBuilder()
    # member1 must exist as a device (IRBuilder._validate_l3intfs rejects unknown
    # devices); it is also declared a VC member of vcroot, so it folds to the root.
    b.add_device(Device(id="vcroot", role=DeviceRole.SWITCH, site="s1", vc_members=("member1",)))
    b.add_device(Device(id="member1", role=DeviceRole.SWITCH, site="s1"))
    b.add_vlan(Vlan(vlan_id=10, name="a", scope="s1"))
    b.add_l3intf(L3Intf(device_id="member1", role=L3Role.IRB, vlan_id=10))
    # the IRB lives on a VC member -> its anchor node is the VC root
    assert exit_anchor_nodes(b.build()) == {"vcroot"}
```

> The `add_vlan(10)` lines satisfy any builder validation that an IRB's VLAN
> exist. If the VC-member fixture trips a builder rule about a device being both
> a top-level device and a `vc_members` entry, mirror the exact construction
> `tests/factories.py`'s `sw(..., vc_members=...)` uses elsewhere — but keep both
> the `member1` device and the fold-to-root assertion.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/analysis/test_exits.py -k exit_anchor -q`
Expected: FAIL with `ImportError: cannot import name 'exit_anchor_nodes'`.

- [ ] **Step 3: Implement the helper**

In `src/digital_twin/analysis/exits.py`, change the entities import to add `L3Role`:

```python
from digital_twin.ir.entities import DeviceRole, L3Role
```

Append the function at module end:

```python
def exit_anchor_nodes(ir: IR) -> set[str]:
    """VC-folded graph nodes that ARE a network exit on the PHYSICAL graph:
    gateway-role devices, or devices owning a routed IRB/SVI that is tied to a
    concrete VLAN. A fragment that contains one of these still reaches an L3 exit
    and is therefore NOT L2-isolated. (WAN/LOOPBACK L3 interfaces are not exits; a
    gateway's own L3 interface already belongs to a DeviceRole.GATEWAY device
    counted here. An IRB/SVI with vlan_id=None is unresolved/malformed and is NOT
    an exit, matching resolve_exit, which only treats concrete-VLAN IRBs as exits.)

    This lifts resolve_exit's two exit kinds (rule 1: IRB; rule 2: gateway node)
    from the per-VLAN graph to the vlan-agnostic physical graph, for callers that
    ask 'does this physical fragment retain any exit'."""
    vc = vc_root_map(ir)
    anchors = {
        node_for(vc, d.id) for d in ir.devices.values() if d.role is DeviceRole.GATEWAY
    }
    anchors |= {
        node_for(vc, i.device_id)
        for i in ir.l3intfs
        if i.role in (L3Role.IRB, L3Role.SVI) and i.vlan_id is not None
    }
    return anchors
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/analysis/test_exits.py -k exit_anchor -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Gate + commit**

```bash
uv run pytest tests -q && uv run ruff check . && uv run mypy src
git add src/digital_twin/analysis/exits.py tests/analysis/test_exits.py
git commit -m "feat(analysis): exit_anchor_nodes — physical-graph exit anchors (gateway + IRB/SVI)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Apply the anchor guard in `l2_isolation`

**Files:**
- Modify: `src/digital_twin/checks/wired/l2_isolation.py`
- Test: `tests/checks/test_l2_isolation.py`

**Interfaces:**
- Consumes: `exit_anchor_nodes(ir)` from `digital_twin.analysis.exits` (Task 1).

The fix is a single guard plus the anchor computation. The home rule from the spec reduces to "skip a fragment iff it itself retains a proposed exit anchor": a fragment is suppressed exactly when it is in the home set, and a fragment is in the home set exactly when it contains an anchor; when no fragment contains an anchor (home empty) nothing is skipped, so every occupied strict-subset is flagged.

- [ ] **Step 1: Write the failing tests**

Append to `tests/checks/test_l2_isolation.py`. (It already imports `replace`, `AnalysisContext`, `CheckContext`, `Status`, `L2IsolationCheck`, `Severity`, `IRBuilder`, `Port`, `PortMode`, `Vlan`, `diff_ir`, and from `tests.factories`: `access_port, link, sw, trunk_port, wired_client`. Add `irb` to the factories import and `L3Intf`, `L3Role` are not needed since `irb()` wraps them.)

Update the factories import line to include `irb`:

```python
from tests.factories import access_port, irb, link, sw, trunk_port, wired_client
```

Add these tests:

```python
def _anchored_ir(*, link_disabled: bool):
    """core(IRB vlan10, member+client) --trunk link-- leaf(member+client).
    Both sides are OCCUPIED; only `core` holds an exit anchor (its IRB)."""
    b = IRBuilder()
    b.add_device(sw("core")).add_device(sw("leaf"))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    b.add_l3intf(irb("core", 10, subnet="10.0.10.0/24"))  # core is an exit anchor
    c_acc = access_port("core", "cacc", 10)
    l_acc = access_port("leaf", "lacc", 10)
    b.add_port(c_acc).add_port(l_acc)
    b.add_client(wired_client("cc:core", c_acc.id, vlan=10))
    b.add_client(wired_client("cc:leaf", l_acc.id, vlan=10))
    c_up = trunk_port("core", "up", tagged=(10,))
    if link_disabled:
        c_up = replace(c_up, disabled=True)
    b.add_port(c_up)
    b.add_port(trunk_port("leaf", "down", tagged=(10,)))
    b.add_link(link("core:up", "leaf:down"))
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def test_exit_anchored_survivor_not_flagged_only_the_leaf():
    # disabling the link severs leaf from core. core is occupied AND a strict
    # subset of the baseline domain, but it keeps its IRB -> NOT flagged.
    result = _run(_anchored_ir(link_disabled=False), _anchored_ir(link_disabled=True))
    flagged = {n for f in result.findings for n in f.affected_entities}
    assert "leaf" in flagged       # the cut-off, anchor-less side IS flagged
    assert "core" not in flagged   # the survivor keeps an exit -> NOT flagged


def test_both_sides_keep_an_exit_neither_flagged():
    def _ir(*, link_disabled: bool):
        b = IRBuilder()
        b.add_device(sw("a")).add_device(sw("b"))
        b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
        b.add_l3intf(irb("a", 10, subnet="10.0.10.0/24"))
        b.add_l3intf(irb("b", 10, subnet="10.0.11.0/24"))
        a_acc, b_acc = access_port("a", "aacc", 10), access_port("b", "bacc", 10)
        b.add_port(a_acc).add_port(b_acc)
        b.add_client(wired_client("cc:a", a_acc.id, vlan=10))
        b.add_client(wired_client("cc:b", b_acc.id, vlan=10))
        a_up = trunk_port("a", "up", tagged=(10,))
        if link_disabled:
            a_up = replace(a_up, disabled=True)
        b.add_port(a_up).add_port(trunk_port("b", "down", tagged=(10,)))
        b.add_link(link("a:up", "b:down"))
        b.with_capability(IRCapability.WIRED_L2)
        return b.build()

    result = _run(_ir(link_disabled=False), _ir(link_disabled=True))
    assert result.status is Status.PASS
    assert result.findings == ()


def test_exit_removed_by_delta_flags_the_fragment():
    # baseline: core keeps leaf reachable AND core has an IRB. proposed: the link
    # is cut AND core's IRB is removed -> core retains NO proposed anchor ->
    # core's occupied fragment is flagged (proposed-state anchors only).
    baseline = _anchored_ir(link_disabled=False)
    proposed = _anchored_ir(link_disabled=True)
    # rebuild `proposed` WITHOUT core's IRB
    pb = IRBuilder()
    pb.add_device(sw("core")).add_device(sw("leaf"))
    pb.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    c_acc, l_acc = access_port("core", "cacc", 10), access_port("leaf", "lacc", 10)
    pb.add_port(c_acc).add_port(l_acc)
    pb.add_client(wired_client("cc:core", c_acc.id, vlan=10))
    pb.add_client(wired_client("cc:leaf", l_acc.id, vlan=10))
    pb.add_port(replace(trunk_port("core", "up", tagged=(10,)), disabled=True))
    pb.add_port(trunk_port("leaf", "down", tagged=(10,)))
    pb.add_link(link("core:up", "leaf:down"))
    pb.with_capability(IRCapability.WIRED_L2)
    result = _run(baseline, pb.build())
    flagged = {n for f in result.findings for n in f.affected_entities}
    assert "core" in flagged   # IRB gone in proposed -> no anchor -> flagged
    assert "leaf" in flagged


def test_exitless_only_uplink_still_severs_member_side():
    # P1 guard / regression: NO exits modeled anywhere; the stranded member side
    # (with all the occupants) is still flagged even though the upstream stub is
    # empty. A size/majority heuristic would have false-SAFE'd this.
    result = _run(_ir(uplink_disabled=False), _ir(uplink_disabled=True))
    flagged = {n for f in result.findings for n in f.affected_entities}
    assert "A" in flagged
```

> `irb(did, vlan, subnet)` is `tests/factories.py:71` — returns `L3Intf(device_id=did, role=L3Role.IRB, vlan_id=vlan, subnet=subnet)`. `_run` and `_ir` already exist in this file (`_ir` is the exit-less A—B fixture). If `IRBuilder.build()` rejects an IRB whose VLAN is missing, the `add_vlan(10)` lines cover it.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/checks/test_l2_isolation.py -q`
Expected: `test_exit_anchored_survivor_not_flagged_only_the_leaf` and `test_both_sides_keep_an_exit_neither_flagged` FAIL (today the survivor/both sides ARE flagged). `test_exit_removed_by_delta_flags_the_fragment` and `test_exitless_only_uplink_still_severs_member_side` may already PASS (current behavior flags everything) — that is fine; they lock the never-false-SAFE direction so it survives the change.

- [ ] **Step 3: Implement the guard**

In `src/digital_twin/checks/wired/l2_isolation.py`, add the import near the other `digital_twin` imports:

```python
from digital_twin.analysis.exits import exit_anchor_nodes
```

In `run()`, compute the anchor set once (after `vc_root = vc_root_map(...)`):

```python
        anchors = exit_anchor_nodes(ctx.proposed.ir)
```

Add the skip guard immediately after the existing strict-subset check, before `occupied = ...`:

```python
            if baseline_home is None or not (fragment < baseline_home):
                continue  # new/unchanged/merged reach — nothing severed
            if fragment & anchors:
                continue  # fragment still holds a real L3 exit — not L2-isolated
            occupied = {n: occupants[n] for n in sorted(fragment) if occupants.get(n)}
```

- [ ] **Step 4: Update the module docstring**

Replace the contract sentence in the module docstring (top of `l2_isolation.py`). The current text says the check is exit-agnostic and flags any member-bearing strict-subset. Update to:

```python
"""wired.l2.isolation — PHYSICAL severance of a member-bearing segment.

Found in real use (2026-06-10): disabling a switch's only uplink blackholes the
switch and everything on it, yet no per-vlan check could say so.

Per baseline connected component: a proposed component that is a STRICT subset of
its baseline component (its reach shrank) and holds occupants — config member
access ports, observed clients (wired or wireless), or WLAN-requiring APs — is a
candidate. It is reported as severed UNLESS it still contains an exit anchor
(`exit_anchor_nodes`: a gateway-role device or a routed IRB/SVI in the proposed
state) — such a fragment keeps a real L3 exit and is not L2-isolated. When NO
fragment of a split component retains an anchor (an exit-less domain, or one whose
only exit the delta removed), every occupied strict-subset is flagged — the
conservative, never-false-SAFE direction. Suppression is grounded-only: there is
no size/majority heuristic, so the surviving majority is dropped only when it
demonstrably keeps an exit.

- Severity is terminal here (this layer only): ERROR at HIGH confidence, WARNING
  below — confidence = MIN over the baseline boundary links the delta severed.
- A pre-existing island (proposed nodes == baseline nodes) is unchanged context.
- Redundancy is respected by construction: graph components, not "an uplink died".
"""
```

(Preserve whatever the current docstring's surrounding lines say about confidence/redundancy; the key edit is replacing the "exit-agnostic / flags any strict-subset" claim with the anchor rule above.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/checks/test_l2_isolation.py -q`
Expected: PASS (all — the 3 pre-existing tests + the 4 new ones). In particular the original `test_disabling_the_only_uplink_severs_the_member_fragment` still passes (its A—B topology has no anchor, so the guard never fires).

- [ ] **Step 6: Full gate + commit**

```bash
uv run pytest tests -q && uv run ruff check . && uv run mypy src
git add src/digital_twin/checks/wired/l2_isolation.py tests/checks/test_l2_isolation.py
git commit -m "fix(checks): l2_isolation suppresses only exit-anchored survivors

A fragment that still holds a proposed exit anchor (gateway / IRB / SVI) keeps a
real L3 exit and is not L2-isolated, so it is no longer flagged as severed.
Grounded-only: when no fragment retains an anchor, every occupied strict-subset
is still flagged (exit-less domains and exit-removed deltas) — never-false-SAFE.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

> If any *other* existing test or golden scenario fails at the full-suite gate, investigate before committing: a golden that asserted the old whole-domain severance is a legitimately-changed expectation (update it and say why); any non-isolation failure is a real regression to fix, not to paper over.

---

## Task 3: Headline golden (end-to-end, the reported topology)

**Files:**
- Modify: `tests/golden/builders.py`
- Modify: `tests/golden/test_golden_scenarios.py`

**Interfaces:**
- Consumes: the existing golden harness. Before writing anything, read
  `tests/golden/builders.py` and `tests/golden/test_golden_scenarios.py` to learn
  the exact construction pattern (how a Mist doc is assembled, how a device-level
  port change is applied, and how a scenario is simulated to a `verdict`). Mirror
  the nearest existing multi-device / port-disable scenario — do not invent a new
  harness.

This task proves the fix end-to-end through the real simulate path (which the
Task 2 unit tests do not exercise): a backbone-connected L3 switch keeps its
uplink while leaf ports are disabled, and only the leaves are reported severed.

- [ ] **Step 1: Write the failing golden test**

Add a scenario to `tests/golden/test_golden_scenarios.py` that reproduces the
reported shape and asserts on the simulated `verdict`:

- Topology (built via `tests/golden/builders.py`, extending it with a helper if
  the existing ones don't cover it):
  - a **core** switch that is an exit anchor — it owns an IRB/SVI on a routed
    VLAN (and/or a gateway device is present),
  - the changed **access** switch, connected to the core by a **backbone uplink
    that is NOT disabled**,
  - on the access switch, **leaf ports**: at least one AP uplink (an AP with an
    observed client) and one wired-client access port.
- Delta (a `device_op`-style change on the access switch): disable the leaf ports
  only; leave the backbone uplink up.
- Assertions on the resulting `verdict`:

```python
    isolation = [f for f in verdict.findings if f.code == "wired.l2.isolation.severed"]
    severed_nodes = {n for f in isolation for n in f.affected_entities}
    # the cut-off leaf AP (and/or wired-client device) IS reported severed
    assert LEAF_AP_NODE in severed_nodes
    # the survivors keep the backbone + an exit anchor -> NOT reported severed
    assert CORE_NODE not in severed_nodes
    assert ACCESS_NODE not in severed_nodes
```

Use the concrete node ids your fixture mints for `LEAF_AP_NODE`, `CORE_NODE`,
`ACCESS_NODE` (match how neighbouring golden assertions reference nodes).

- [ ] **Step 2: Run it to confirm it fails on the pre-fix tree**

If implementing Task 3 after Tasks 1-2 are merged (the fix is already in place),
this test will PASS immediately — that is acceptable; it then serves as the
end-to-end regression lock. To see it fail first, you can temporarily `git stash`
the Task 2 guard, observe the survivors flagged, then restore. Either way, run:

Run: `uv run pytest tests/golden/test_golden_scenarios.py -k isolation -q`
Expected (post-fix): PASS — only the leaf nodes in `severed_nodes`.

- [ ] **Step 3: Do NOT weaken the assertion**

If `CORE_NODE`/`ACCESS_NODE` appear in `severed_nodes`, that is a real failure of
the fix (or the fixture doesn't actually give the core an exit anchor / keep the
backbone up) — fix the fixture or the code, never relax the assertion.

- [ ] **Step 4: Full gate + commit**

```bash
uv run pytest tests -q && uv run ruff check . && uv run mypy src
git add tests/golden/builders.py tests/golden/test_golden_scenarios.py
git commit -m "test(golden): leaf-port-disable on a backbone-anchored L3 switch flags only the leaves

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- `exit_anchor_nodes` (gateway ∪ IRB/SVI **tied to a concrete VLAN**, WAN/LOOPBACK excluded, VC-folded) → Task 1 ✓; unresolved `vlan_id=None` IRB does not anchor → `test_exit_anchor_nodes_ignores_unresolved_irb_without_vlan` ✓
- Anchors on the proposed IR → Task 2 (`exit_anchor_nodes(ctx.proposed.ir)`) ✓
- Grounded-only suppression / no majority heuristic / empty-home flags-all → Task 2 guard (`if fragment & anchors: continue`) ✓
- Never-false-SAFE (exit-less still flags; exit-removed still flags) → `test_exitless_only_uplink_still_severs_member_side`, `test_exit_removed_by_delta_flags_the_fragment` ✓
- Headline case (survivor quiet, leaf flagged) → unit `test_exit_anchored_survivor_not_flagged_only_the_leaf` (Task 2) **and** the end-to-end golden (Task 3) ✓
- Both-sides-keep-exit → `test_both_sides_keep_an_exit_neither_flagged` ✓
- Spec's "Tests + a golden fixture" / headline golden → Task 3 ✓
- Module docstring contract update → Task 2 Step 4 ✓
- No change to occupants/confidence/severity/subject/message → guard is additive; nothing else touched ✓

**Type consistency:** `exit_anchor_nodes(ir: IR) -> set[str]` defined in Task 1, consumed in Task 2 as `exit_anchor_nodes(ctx.proposed.ir)`; `fragment` is `frozenset[str]`, `anchors` is `set[str]`, `fragment & anchors` is well-typed.

**Placeholder scan:** none — every step shows real code; the two "if the builder rejects…" notes give a concrete fallback, not a TBD.
