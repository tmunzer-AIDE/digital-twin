# `INFERRED_UPLINK` exit from `Port.is_uplink` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Locate a VLAN's exit "up an `is_uplink` port" at a capped LOW confidence when no IRB and no modeled gateway exists — replacing alarming `exit_unlocatable` noise with a quiet inferred exit, and sharpening severance into `exit_lost`, while never certifying SAFE.

**Architecture:** One new `ExitKind.INFERRED_UPLINK` and one new precedence rule (rule 3) in `resolve_exit`, which gains an explicit `vlan_id` parameter (two callers updated). `l2_blackhole` needs **no logic change** — the inferred exit flows through its existing stranded/`exit_lost` logic and taints the check confidence to LOW automatically.

**Tech Stack:** Python 3.14, uv, pytest, ruff (100-col), mypy (strict on `src`, not tests). networkx.

## Global Constraints

- **Precedence:** IRB (HIGH) > modeled gateway edge (BOUNDARY_UPLINK) > **inferred uplink (INFERRED_UPLINK, LOW)** > NONE. Rule 3 runs only after rules 1 and 2 miss.
- **Rule 3 qualification (all required):** `port.is_uplink is True` (identity, not truthiness) **and** `not port.disabled` **and** (`vid in port.tagged_vlans` or `port.native_vlan == vid`). Scan `ir.ports` directly (no `Link` edge in the unmodeled-gateway case). Exit nodes = `sorted(set(node_for(vc_root, p.device_id) for qualifying p) & set(vlan_graph.nodes))`. Confidence `ConfidenceLevel.LOW`, reason `"exit inferred from Mist uplink flag; upstream gateway unmodeled"`.
- **Never-false-SAFE:** LOW confidence can never certify SAFE — `decision.py:94-101` floors REVIEW on any evaluated check whose result confidence is below HIGH. Rule 3 creates no new SAFE; it only sharpens findings within REVIEW/UNSAFE. Rules 1/2 untouched.
- **Case 3 stays `exit_unlocatable`:** rule 3 is a proposed-state locator; if the only qualifying uplink disappears in proposed, no inferred exit exists, so `l2_blackhole` correctly stays `exit_unlocatable`. Do NOT add a baseline-inferred-exit path (deferred, distinct semantic).
- **Gate (before each commit):** `uv run pytest tests -q && uv run ruff check . && uv run mypy src`. Pyright/IDE diagnostics (networkx unresolved) are noise.
- **Commit trailer:** `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

## File Structure

- `src/digital_twin/analysis/exits.py` **(modify)** — `ExitKind.INFERRED_UPLINK`; `resolve_exit(ir, vlan_id, vlan_graph)` + rule 3; docstring.
- `src/digital_twin/analysis/context.py` **(modify)** — `exit_for` passes `vlan_id` to `resolve_exit`.
- `src/digital_twin/viz/mermaid.py` **(modify)** — `_vlan_diagram` passes `vid` to `resolve_exit`.
- `src/digital_twin/checks/wired/l2_blackhole.py` **(modify)** — module-docstring exit-precedence line only (no logic).
- `tests/analysis/test_exits.py`, `tests/checks/test_l2_blackhole.py` **(modify)**.

---

## Task 1: `ExitKind.INFERRED_UPLINK` + `resolve_exit` rule 3

**Files:**
- Modify: `src/digital_twin/analysis/exits.py`, `src/digital_twin/analysis/context.py`, `src/digital_twin/viz/mermaid.py`
- Test: `tests/analysis/test_exits.py`

- [ ] **Step 1: Write the failing tests**

In `tests/analysis/test_exits.py`, add `from dataclasses import replace` to the imports, and extend the existing `_base` helper with uplink knobs (keep all current params and behavior; add the four new keyword-only params and the `with_uplink` block before `return b.build()`):

```python
def _base(
    with_irb: bool,
    with_gateway: bool = False,
    gw_one_sided: bool = False,
    *,
    with_uplink: bool = False,
    uplink_flag: bool | None = True,
    uplink_disabled: bool = False,
    uplink_carries: bool = True,
):
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
    if with_uplink:
        b.add_port(
            replace(
                trunk_port("A", "up2", tagged=(10,) if uplink_carries else ()),
                is_uplink=uplink_flag,
                disabled=uplink_disabled,
            )
        )
    return b.build()
```

Then add the rule-3 tests:

```python
def test_rule3_inferred_uplink_is_low_confidence_exit():
    res = AnalysisContext(_base(with_irb=False, with_uplink=True)).exit_for(10)
    assert res.kind is ExitKind.INFERRED_UPLINK
    assert res.nodes == ("A",)
    assert res.confidence is not None and res.confidence.level is ConfidenceLevel.LOW
    assert res.confidence.reasons == (
        "exit inferred from Mist uplink flag; upstream gateway unmodeled",
    )


def test_rule3_disqualifies_non_true_disabled_or_vlan_blind():
    # is_uplink None / False, a disabled uplink, and a VLAN-blind uplink each
    # leave the exit unlocatable (NONE) when nothing else locates it.
    assert AnalysisContext(_base(False, with_uplink=True, uplink_flag=None)).exit_for(10).kind is ExitKind.NONE
    assert AnalysisContext(_base(False, with_uplink=True, uplink_flag=False)).exit_for(10).kind is ExitKind.NONE
    assert AnalysisContext(_base(False, with_uplink=True, uplink_disabled=True)).exit_for(10).kind is ExitKind.NONE
    assert AnalysisContext(_base(False, with_uplink=True, uplink_carries=False)).exit_for(10).kind is ExitKind.NONE


def test_rule3_yields_to_irb_and_gateway():
    # precedence: a stronger exit always wins over the inferred uplink
    assert AnalysisContext(_base(with_irb=True, with_uplink=True)).exit_for(10).kind is ExitKind.IRB
    assert (
        AnalysisContext(_base(False, with_gateway=True, with_uplink=True)).exit_for(10).kind
        is ExitKind.BOUNDARY_UPLINK
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/analysis/test_exits.py -k "rule3" -v`
Expected: FAIL — `ExitKind.INFERRED_UPLINK` does not exist yet (AttributeError) / the inferred cases resolve to `NONE`.

- [ ] **Step 3: Add the enum member**

In `src/digital_twin/analysis/exits.py`, add to `ExitKind`:

```python
class ExitKind(StrEnum):
    IRB = "irb"
    BOUNDARY_UPLINK = "boundary_uplink"
    INFERRED_UPLINK = "inferred_uplink"
    NONE = "none"
```

- [ ] **Step 4: Add the `vlan_id` parameter + rule 3 to `resolve_exit`**

Change the signature to `def resolve_exit(ir: IR, vlan_id: int, vlan_graph: nx.MultiGraph) -> ExitResolution:`. Leave rules 1 and 2 unchanged. Insert rule 3 immediately before the final `return ExitResolution(kind=ExitKind.NONE, ...)`:

```python
    # rule 3: VLAN carried on a qualifying is_uplink port — an inferred exit "up
    # the uplink" when the upstream gateway is unmodeled. LOW confidence so it can
    # never certify SAFE (decision.py floors REVIEW on a sub-HIGH result conf).
    # vc_root is already computed above for rule 2.
    graph_nodes = set(vlan_graph.nodes)
    uplink_nodes = sorted(
        {
            node_for(vc_root, p.device_id)
            for p in ir.ports.values()
            if p.is_uplink is True
            and not p.disabled
            and (vlan_id in p.tagged_vlans or p.native_vlan == vlan_id)
        }
        & graph_nodes
    )
    if uplink_nodes:
        return ExitResolution(
            kind=ExitKind.INFERRED_UPLINK,
            nodes=tuple(uplink_nodes),
            confidence=Confidence(
                level=ConfidenceLevel.LOW,
                reasons=("exit inferred from Mist uplink flag; upstream gateway unmodeled",),
            ),
        )

    return ExitResolution(kind=ExitKind.NONE, nodes=(), confidence=None)
```

> `vc_root = vc_root_map(ir)` is already assigned inside rule 2's block. If rule 1 returns early it is never needed; if execution reaches rule 3, rule 2's block has run and `vc_root` is in scope. Confirm `vc_root` is assigned before rule 3 in the final code (rule 2 assigns it unconditionally before its `hits` loop); if a linter flags possibly-unbound, hoist `vc_root = vc_root_map(ir)` to just above rule 2.

- [ ] **Step 5: Update the two callers**

`src/digital_twin/analysis/context.py` (in `exit_for`):

```python
            self._exits[vlan_id] = resolve_exit(self._ir, vlan_id, self.vlan_graph(vlan_id))
```

`src/digital_twin/viz/mermaid.py` (in `_vlan_diagram`, the `resolve_exit(ir, g)` call):

```python
    exit_nodes = set(resolve_exit(ir, vid, g).nodes)
```

- [ ] **Step 6: Update the module docstring**

In `exits.py`, update the precedence list in the module docstring to insert rule 3 (inferred uplink, LOW) between the gateway-uplink rule and NONE, noting it scans `is_uplink` ports directly and is capped LOW so it never certifies SAFE.

- [ ] **Step 7: Run tests + gate**

Run: `uv run pytest tests/analysis/test_exits.py -q && uv run pytest tests -q && uv run ruff check . && uv run mypy src`
Expected: PASS. Existing fixtures do not set `is_uplink` (default `None`), so rule 3 never fires for them — no regression. If a test breaks because a fixture incidentally had an `is_uplink` uplink, investigate before "fixing".

- [ ] **Step 8: Commit**

```bash
git add src/digital_twin/analysis/exits.py src/digital_twin/analysis/context.py src/digital_twin/viz/mermaid.py tests/analysis/test_exits.py
git commit -m "feat(analysis): resolve_exit rule 3 — INFERRED_UPLINK exit from Port.is_uplink (LOW)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `l2_blackhole` downstream — the three cases (tests + docstring)

`l2_blackhole` has **no logic change**; this task pins the three locked downstream behaviors and updates its docstring.

**Files:**
- Modify: `src/digital_twin/checks/wired/l2_blackhole.py` (docstring only)
- Test: `tests/checks/test_l2_blackhole.py`

- [ ] **Step 1: Write the three failing/locking tests**

In `tests/checks/test_l2_blackhole.py`, add `from dataclasses import replace` (if absent) and the `decide`/`DecisionInputs`/`Decision` imports for the case-1 decision assertion. Add a small uplink-port helper and the three topology builders, then the tests. Use the file's existing `_ctx(baseline, proposed)` and `L2BlackholeCheck`:

```python
from dataclasses import replace
from digital_twin.verdict.decision import Decision, DecisionInputs, decide
from tests.factories import access_port, link, sw, trunk_port


def _uplink(did, name, vid):  # an is_uplink trunk toward an UNMODELED core
    return replace(trunk_port(did, name, tagged=(vid,)), is_uplink=True)


def _ab(*, extra_member=False, uplink_disabled=False):
    # A(member access) -- B(is_uplink toward unmodeled core); no IRB, no gateway
    b = IRBuilder()
    b.add_device(sw("A")).add_device(sw("B"))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    b.add_port(access_port("A", "acc", 10))
    if extra_member:
        b.add_port(access_port("A", "acc2", 10))  # the delta: a new vlan-10 member
    b.add_port(trunk_port("A", "up", tagged=(10,)))
    b.add_port(trunk_port("B", "down", tagged=(10,)))
    core = _uplink("B", "core", 10)
    if uplink_disabled:
        core = replace(core, disabled=True)
    b.add_port(core)
    b.add_link(link("A:up", "B:down"))
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def _abc(*, sever=False):
    # A(member) -- B -- C(is_uplink toward unmodeled core); delta severs A's uplink
    b = IRBuilder()
    for d in ("A", "B", "C"):
        b.add_device(sw(d))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    b.add_port(access_port("A", "acc", 10))
    a_up = trunk_port("A", "up", tagged=(10,))
    if sever:
        a_up = replace(a_up, disabled=True)  # the delta: A loses its path upstream
    b.add_port(a_up)
    b.add_port(trunk_port("B", "da", tagged=(10,)))
    b.add_port(trunk_port("B", "uc", tagged=(10,)))
    b.add_port(trunk_port("C", "dc", tagged=(10,)))
    b.add_port(_uplink("C", "core", 10))  # untouched -> exit survives in proposed
    b.add_link(link("A:up", "B:da"))
    b.add_link(link("B:uc", "C:dc"))
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def test_inferred_uplink_intact_is_review_not_safe():
    # case 1: a changed vlan still reaches its inferred uplink -> structural PASS,
    # but result confidence LOW -> decision floors REVIEW (never SAFE), and NO
    # exit_unlocatable noise is emitted.
    result = L2BlackholeCheck().run(_ctx(_ab(), _ab(extra_member=True)))
    assert result.status is Status.PASS
    assert result.confidence is not None and result.confidence.level is ConfidenceLevel.LOW
    assert not any("unlocatable" in f.code for f in result.findings)
    decision, _ = decide(
        DecisionInputs(rejections=(), l0_fatal=False, baseline_unavailable=False,
                       check_results=(result,))
    )
    assert decision is Decision.REVIEW  # the LOW result confidence floors it; never SAFE


def test_inferred_uplink_severed_is_exit_lost_warning():
    # case 2: the delta cuts A off from the SURVIVING inferred uplink at C ->
    # sharper exit_lost (WARNING at LOW exit confidence), not vague unlocatable.
    result = L2BlackholeCheck().run(_ctx(_abc(sever=False), _abc(sever=True)))
    f = next(f for f in result.findings if f.code == "wired.l2.blackhole.exit_lost")
    assert f.severity is Severity.WARNING
    assert not any("unlocatable" in x.code for x in result.findings)


def test_inferred_uplink_last_uplink_removed_stays_unlocatable():
    # case 3: disabling the sole qualifying uplink removes the inferred exit in
    # proposed -> NONE -> exit_unlocatable (unchanged; the exit genuinely vanished)
    result = L2BlackholeCheck().run(_ctx(_ab(), _ab(uplink_disabled=True)))
    assert any(f.code == "wired.l2.blackhole.exit_unlocatable" for f in result.findings)
```

> If `_ctx`, `IRBuilder`, `Vlan`, `Status`, `Severity`, `ConfidenceLevel`, or `IRCapability` are not already imported in this file, add them (mirror the imports already present at the top of `test_l2_blackhole.py`).

- [ ] **Step 2: Run the tests**

Run: `uv run pytest tests/checks/test_l2_blackhole.py -k "inferred_uplink" -v`
Expected: PASS (Task 1 supplies the behavior; `l2_blackhole` already routes `INFERRED_UPLINK` through its stranded/`exit_lost` logic).
- If `test_..._intact` shows `result.confidence` is HIGH (not LOW), the delta did not register as a vlan-10 change, so the exit confidence was not appended. `_vlan_changed` compares `vlan_components` (which capture member ports), so `extra_member` should suffice; if it does not, make the delta unambiguous by adding the new vlan-10 member on a *new* device linked to `B` (changing the node set) instead of a second port on `A`.
- If `test_..._severed` returns `exit_unlocatable` instead of `exit_lost`, the proposed exit at C is not resolving — verify C's `_uplink` core port is enabled and carries vid 10 in the proposed IR (it must be untouched by `sever`).

- [ ] **Step 3: Update the `l2_blackhole` docstring**

In `l2_blackhole.py`'s module docstring, update the exit-resolution line ("IRB HIGH > boundary uplink edge-confidence > NONE") to include the inferred uplink: `IRB HIGH > boundary uplink edge-confidence > inferred uplink LOW (is_uplink, gateway unmodeled) > NONE`. No logic change.

- [ ] **Step 4: Run tests + full gate**

Run: `uv run pytest tests -q && uv run ruff check . && uv run mypy src`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/checks/wired/l2_blackhole.py tests/checks/test_l2_blackhole.py
git commit -m "test(checks): pin l2_blackhole's three INFERRED_UPLINK downstream cases; docstring

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3 (controller-run): live verification — read-only

Not a subagent task. After Tasks 1-2, the controller runs a **read-only** simulate against the authorized `dev` MCP org/site (the `mge`-disable scenario): confirm the backbone `is_uplink` resolves `INFERRED_UPLINK`, the previously-`exit_unlocatable` VLANs that reach it become quiet LOW-confidence REVIEW (no `exit_unlocatable`), and a severed leaf yields `exit_lost`. No writes. Record the observation in the PR/memory.

---

## Self-Review

**Spec coverage:**
- `ExitKind.INFERRED_UPLINK` + rule 3 (port-direct scan, qualification matrix, LOW + reason, owner∩graph) → Task 1 Steps 3-4 ✓
- precedence IRB > gateway > inferred > NONE → Task 1 rule order + `test_rule3_yields_to_irb_and_gateway` ✓
- `vlan_id` param + both callers → Task 1 Steps 4-5 ✓
- never-false-SAFE (case 1 REVIEW not SAFE via decision) → Task 2 `test_inferred_uplink_intact_is_review_not_safe` ✓
- case 2 exit_lost WARNING, case 3 exit_unlocatable unchanged → Task 2 ✓
- docstrings (exits + blackhole) → Task 1 Step 6, Task 2 Step 3 ✓
- live verify → Task 3 ✓

**Type/name consistency:** `resolve_exit(ir, vlan_id, vlan_graph)` updated at both call sites (`context.py`, `mermaid.py`); `node_for`/`vc_root_map`/`Confidence`/`ConfidenceLevel` already imported in `exits.py`; `replace`/`access_port`/`_uplink` used consistently; `decide`/`DecisionInputs`/`Decision` exist in `verdict/decision.py`.

**Placeholder scan:** none — full code for every code step; the only conditionals name the concrete expected outcome.
