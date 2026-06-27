# `INFERRED_UPLINK` exit from `Port.is_uplink` — Design

**Status:** Approved (design); plan pending
**Date:** 2026-06-26
**Author:** Thomas Munzer (with Claude)

## Problem

`resolve_exit` locates a VLAN's exit by two rules: an in-scope IRB (HIGH), or the VLAN carried on a graph edge to a **modeled** GATEWAY-role node (BOUNDARY_UPLINK, edge confidence). When the gateway/SRX is **out of scope** — not present in the L2 graph, the common real case and the original `l2_isolation` motivating scenario — both rules miss, the exit resolves to `ExitKind.NONE`, and `l2_blackhole` emits `exit_unlocatable` (REVIEW) for every changed member VLAN, *even when an obvious uplink toward the core carries the VLAN*. Mist exposes `Port.is_uplink` (PR #22), the authoritative "this port faces the core" bit, which lets us **locate an inferred exit** in exactly this case.

The intent is **sharper attribution, never a cleared REVIEW**. We cannot confirm reachability to a gateway we cannot see, so the inferred exit carries a capped, sub-HIGH confidence and can *never* certify a VLAN as SAFE — it only improves findings *within* REVIEW/UNSAFE.

## Design

### New exit kind + precedence

Add `ExitKind.INFERRED_UPLINK` (distinct from `BOUNDARY_UPLINK` — a modeled gateway edge and an unmodeled "uplink points somewhere upstream" are operationally different). Precedence in `resolve_exit`:

1. in-scope IRB/SVI → `IRB`, HIGH
2. VLAN on a graph edge to a modeled GATEWAY node → `BOUNDARY_UPLINK`, edge confidence
3. **(new)** VLAN carried on a qualifying `is_uplink` port → `INFERRED_UPLINK`, **LOW**
4. neither → `NONE`

### Rule 3 — inferred uplink

`resolve_exit` inspects **IR ports directly** (not VLAN-graph edges — the unmodeled-gateway case may have no `Link` edge at all). A port *qualifies* iff **all** hold:

- `port.is_uplink is True` (identity, not truthiness — `None`/`False` never infer)
- `not port.disabled`
- the port offers the VLAN: `vid in port.tagged_vlans` **or** `port.native_vlan == vid`

For each qualifying port, the exit node is its owner node, VC-folded: `node_for(vc_root, port.device_id)`. The resolved exit nodes are `sorted(set(owner_nodes) & set(vlan_graph.nodes))`. In the meaningful member/downstream cases the owner already carries the VID and is a VLAN-graph node, so the intersection is a no-op there; it *does* correctly drop a lone uplink-only node that never entered the VLAN graph (no VLAN edge or member) — locating it would be pointless, since no component could ever reach it. If any qualify:

```python
ExitResolution(
    kind=ExitKind.INFERRED_UPLINK,
    nodes=<sorted owner nodes>,
    confidence=Confidence(
        level=ConfidenceLevel.LOW,
        reasons=("exit inferred from Mist uplink flag; upstream gateway unmodeled",),
    ),
)
```

Rule 3 runs **only after** rules 1 and 2 miss, so a modeled IRB or gateway edge always wins (a stronger, higher-confidence exit is never downgraded to an inference).

### `l2_blackhole` needs no logic change

`INFERRED_UPLINK` is a non-`NONE` exit with a non-`None` LOW confidence, so it flows through the existing `_check_vlan` logic unchanged:

- It is **not** `ExitKind.NONE`, so the `exit_unlocatable` / `preexisting_unlocatable` branch is skipped.
- Its LOW confidence is appended to the check's `confidences` for any changed, member-bearing VLAN (the existing taint rule), so the check result confidence becomes LOW.
- `reaches_exit` (component contains an exit node) and the `stranded` / `exit_lost` logic operate normally on the inferred exit node.

Only documentation changes in `l2_blackhole.py` (the exit-precedence line in the module docstring). The existing `exit_lost` message interpolates `{proposed_exit.kind}`, so it will read "…loses its path to the inferred_uplink exit" — acceptable, no change needed.

## Downstream behavior — three locked cases

1. **Component still reaches the inferred uplink** → not stranded → structural `Status.PASS`, but the appended LOW confidence makes the check result LOW, so `decision.py` floors **REVIEW** (SAFE requires HIGH result-confidence). The operator sees a located-but-low-confidence exit instead of alarming "exit cannot be located." **Never SAFE.**
2. **Component severed from a *surviving* inferred-uplink owner** (e.g. a leaf cut off from a backbone switch whose `is_uplink` port is untouched — the live `mge`-disable scenario) → the inferred exit still resolves in proposed, the stranded leaf no longer contains it → `exit_lost` **WARNING** (sharp), alongside `isolation.severed`. The real win.
3. **The last qualifying uplink is disabled/removed in proposed** → no inferred exit exists in the proposed state → exit resolves to `NONE` → `exit_unlocatable` (unchanged), with `admin_disable` / `isolation.severed` carrying the physical harm.

**Case-3 rationale (explicit, do not "fix" later):** Rule 3 is a **proposed-state locator** and never uses baseline-only uplinks. If the only qualifying uplink disappears in proposed, no inferred exit exists in proposed, so `l2_blackhole` correctly remains `exit_unlocatable`. Reporting this as `exit_lost` would require a *new* "the only inferred exit vanished" semantic in `l2_blackhole` — a different finding ("the sole inferred exit was removed," not "a component lost its path to a surviving exit") needing its own code, wording, and evidence. That is deliberately **out of scope** here and may be a later check.

## Never-false-SAFE

Strictly safe: rule 3 creates **no** new SAFE verdict. An inferred exit is LOW confidence, and `decision.py` requires HIGH result-confidence for SAFE, so any changed member VLAN relying on an inferred uplink floors REVIEW via confidence. The rule only ever *sharpens* findings within REVIEW/UNSAFE (a quieter located exit, or a precise `exit_lost`), never relaxes the verdict. Rules 1 and 2 are untouched, so no stronger exit is weakened.

## Files touched

- `src/digital_twin/analysis/exits.py` — add `ExitKind.INFERRED_UPLINK`; add rule 3 to `resolve_exit`; update the module docstring's precedence list. (`exit_anchor_nodes` is **not** changed — inferred uplinks are not L3 anchors for `l2_isolation`; that consistency question is deferred.)
- `src/digital_twin/checks/wired/l2_blackhole.py` — module-docstring exit-precedence line only (no logic change).
- Tests: `tests/analysis/test_exits*.py` (rule 3 qualification matrix + precedence), `tests/checks/test_l2_blackhole*.py` (the three downstream cases), and a decision-level assertion that an intact inferred uplink yields REVIEW, not SAFE.

## Testing

- **Rule 3 qualifies:** an IR with no IRB and no modeled gateway, a member VLAN carried on an `is_uplink=True`, enabled port → `resolve_exit` returns `INFERRED_UPLINK`, the owner node, `ConfidenceLevel.LOW`, and the documented reason string.
- **Rule 3 disqualifies (each independently):** `is_uplink is None`, `is_uplink is False`, `port.disabled is True`, and a VLAN-blind port (VID neither tagged nor native) each → the inferred rule does **not** fire (exit stays `NONE` when nothing else locates it).
- **Precedence:** an IRB *and* a qualifying uplink → `IRB`/HIGH (rule 1 wins); a modeled gateway edge *and* a qualifying uplink → `BOUNDARY_UPLINK` (rule 2 wins); only the uplink → `INFERRED_UPLINK`.
- **Case 1 (intact → REVIEW, not SAFE):** a changed member VLAN whose component reaches an inferred uplink → `l2_blackhole` emits no stranded finding (structural PASS) but the check result confidence is LOW; at the decision layer the verdict is REVIEW, never SAFE, and no `exit_unlocatable` finding is emitted.
- **Case 2 (severed → exit_lost WARNING):** the delta cuts a member component off from a surviving inferred-uplink owner → `wired.l2.blackhole.exit_lost`, `severity is Severity.WARNING` (LOW exit confidence, not CRITICAL), and `isolation.severed` also fires.
- **Case 3 (last uplink removed → exit_unlocatable):** the delta disables the sole qualifying uplink port → proposed exit `NONE` → `wired.l2.blackhole.exit_unlocatable` (unchanged behavior).
- **No regression:** existing fixtures that do not set `is_uplink` (default `None`) are unaffected — rule 3 never fires for them.

## Live verification

Read-only simulate against the authorized `dev` MCP org/site (the `mge`-disable scenario): confirm the inferred uplink at the backbone switch resolves `INFERRED_UPLINK`, the previously-`exit_unlocatable` VLANs that reach it become quiet LOW-confidence REVIEW, and the severed leaf produces `exit_lost`. No writes.

## Scope and deferred

In scope: `ExitKind.INFERRED_UPLINK` + rule 3 in `resolve_exit` + docs/tests. Deferred: the "sole inferred exit removed → `exit_lost`-style finding" (case-3 upgrade) with its own code/evidence; whether `exit_anchor_nodes` (and thus `l2_isolation`'s CRITICAL predicate) should also count inferred uplinks as anchors; the mistmcp VisualMap consumer.
