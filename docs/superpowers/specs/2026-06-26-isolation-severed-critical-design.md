# `l2_isolation.severed` → CRITICAL on confirmed exit-anchor loss — Design

**Status:** Approved (design); plan pending
**Date:** 2026-06-26
**Author:** Thomas Munzer (with Claude)

## Problem

PR [#23](https://github.com/tmunzer-AIDE/digital-twin/pull/23) made `wired.l2.blackhole.exit_lost` `CRITICAL` when a populated segment loses a confidently-located exit, and explicitly deferred the symmetric move on `wired.l2.isolation.severed`. This spec delivers that follow-up: when a *physically severed* occupied fragment is cut off from an exit anchor that survives on the other side of the cut, the severance is the top-severity wired event and should render `CRITICAL`.

**This is a salience/consistency change, not a new detection path.** On the *verdict* it is redundant: whenever a fragment is severed from a still-present, per-VLAN-locatable exit, `blackhole.exit_lost` already fires `CRITICAL`, and (under the current visual-map design) `blackhole.exit_lost` already carries the affected device nodes in `affected_entities`, so it can already paint those nodes `CRITICAL`. The value of escalating `isolation.severed` is:

1. **Consistency** — the device-severance finding agrees in severity with the per-VLAN finding for the same event, instead of reading `ERROR` while `blackhole` reads `CRITICAL`.
2. **The physical-carrier case** — where `isolation.severed` is the *visible* finding and `blackhole` emits no per-VLAN blackhole/exit-loss finding for it (isolation still requires occupants, but the home it was cut from holds a gateway-role / routed-IRB anchor without a per-VLAN exit-loss that `blackhole` would report).

It is **not** a verdict change (`CRITICAL` and `ERROR` both gate UNSAFE in `decision.py`) and **not** a coverage change.

## Design

### Locked contract

`wired.l2.isolation.severed` severity, for a fragment already determined to be a severed occupied strict-subset (the existing candidate selection — the `fragment & anchors` suppression and the `if not occupied: continue` guard around [l2_isolation.py:95-98](src/digital_twin/checks/wired/l2_isolation.py) — is unchanged):

- **`CRITICAL`** when `high` **and** `lost_anchor_nodes` is non-empty.
- **`ERROR`** when `high` but `lost_anchor_nodes` is empty (exit-less domain, or the delta removed the only exit — the conservative direction where we must not invent CRITICAL).
- **`WARNING`** when not `high` (severance confidence below HIGH), *even if* an anchor exists — a low-confidence severance does not earn the top tier.

where, reusing the `anchors = exit_anchor_nodes(ctx.proposed.ir)` already computed in `run()`:

```python
lost_anchor_nodes = (baseline_home - fragment) & anchors
critical = high and bool(lost_anchor_nodes)
```

`lost_anchor_nodes` is written explicitly as "anchors on the *other side* of the cut" rather than the today-equivalent `baseline_home & anchors`. The equivalence holds only because the existing suppression already `continue`s when `fragment & anchors` is non-empty (so a flagged fragment never contains an anchor). The explicit `(baseline_home - fragment)` form states the real claim — *the severed fragment lost reach to an exit anchor that remains on the far side* — and stays correct if that suppression ever changes.

### Severity / status

`CRITICAL` only occurs on the `high` branch, so the existing aggregation `worst = Status.FAIL if high else (...)` is **unchanged**: `CRITICAL` maps to `FAIL` exactly like `ERROR`. No status, verdict, or coverage logic changes.

### Capability — keep `WIRED_L2` only

`requires()` stays `frozenset({IRCapability.WIRED_L2})`. Do **not** add `L3_EXITS` as required: `isolation` must keep firing on its original motivating scenario (a switch's only uplink disabled, with *no* modeled L3 exit at all) — that case has no anchors, correctly stays `ERROR`, and would be lost if the check gated on an L3-exit capability. `exit_anchor_nodes(ctx.proposed.ir)` is already called unconditionally today and returns an empty set when there is no exit model; that behavior is relied upon and unchanged.

### Evidence

Add to the finding's `evidence` dict (fail-soft, secret-free, sorted for determinism):

- `"exit_anchor_nodes": sorted(anchors)` — the full proposed exit-anchor set that drove the decision (lets a reader see whether *any* anchor existed, i.e. why a fragment did or didn't escalate).
- `"lost_anchor_nodes": sorted(lost_anchor_nodes)` — the anchors this fragment was severed from.
- `"severity_reason": <str>` — a short literal explaining the tier: e.g. `"severed from a surviving exit anchor"` (CRITICAL), `"physical severance, no surviving exit anchor"` (ERROR), `"physical severance, severance confidence below HIGH"` (WARNING).

The existing evidence keys (`fragment_nodes`, `lost_peers`, `occupants`) and the human `message` are **unchanged** (the message already names the severed segment and its occupants; the tier distinction is carried by severity + `severity_reason`). `default_severity` stays `Severity.ERROR`.

## Files touched

- `src/digital_twin/checks/wired/l2_isolation.py` — compute `lost_anchor_nodes`; tier the severity (CRITICAL/ERROR/WARNING) and `severity_reason`; add the two anchor evidence keys; update the module docstring's severity line.
- Tests in `tests/checks/test_l2_isolation*.py`; any golden asserting a severed-from-gateway fragment's old `ERROR`.

## Testing

- **CRITICAL — severed from a surviving anchor:** a fragment with occupants is cut (HIGH-confidence severed links) from a `baseline_home` that retains a proposed exit anchor on the far side → `severity is Severity.CRITICAL`, `evidence["lost_anchor_nodes"]` non-empty, status FAIL.
- **ERROR — exit-less severance (unchanged):** the original motivating scenario — an occupied fragment severed at HIGH confidence with *no* exit anchor anywhere in the home → `severity is Severity.ERROR`, `evidence["lost_anchor_nodes"] == []`.
- **WARNING — below-HIGH (unchanged):** the same severed-from-anchor topology but with a one-sided-LLDP (non-HIGH) severed boundary link → `severity is Severity.WARNING` even though an anchor exists (the `high` gate dominates).
- **Verdict invariance:** a confident CRITICAL severance still yields `UNSAFE` (CRITICAL gates UNSAFE exactly like ERROR) — label-only escalation.
- **Evidence shape:** the finding carries `exit_anchor_nodes`, `lost_anchor_nodes`, and `severity_reason`; existing keys unchanged.
- **Existing isolation goldens / tests:** the motivating exit-less goldens stay `ERROR` (no anchor). Any test/golden that asserted a severed-from-gateway fragment as `ERROR` legitimately becomes `CRITICAL` — update it and note why; a confident-severance verdict stays UNSAFE.

## Scope and deferred

In scope: the `isolation.severed` severity tier + evidence only. No change to candidate selection, occupants, confidence, status aggregation, verdict, or coverage.

Still deferred (separate threads): `blackhole.exit_unlocatable`'s exit-location coverage gap; the mistmcp VisualMap consumer that renders origin≠affected and colors by `(view, entity)`.
