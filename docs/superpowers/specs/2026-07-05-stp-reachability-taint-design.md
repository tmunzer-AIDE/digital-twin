# STP Blocked-Link Reachability Taint (Spec-5)

**Date:** 2026-07-05
**Status:** Approved for planning
**Predecessor:** Spec-4 (STP tree engine v1 — prediction core + validation rail).
This is the **first verdict-facing consumer** of `stp_tree()` and is bound by
**THE INVARIANT**: prediction alone never earns SAFE; a consumer must call
`compare_to_observed` and cap/degrade on component-level disagreement.

## Problem — the permanent false-SAFE

`wired.l2.blackhole` computes per-VLAN reachability on the cyclic `vlan_graph`,
where every VLAN-carrying edge counts as a usable path. STP blocks a *port*
(all VLANs on it) to break a physical cycle. The permanent false-SAFE lives at
the **intersection of per-VLAN pruning and STP blocking**: two segments are
physically connected by a forwarding link that does NOT carry VLAN 10, while
VLAN 10's only inter-segment link is one the spanning tree blocks. Today VLAN 10
reads connected → reaches its exit → SAFE; reality: VLAN 10 is stranded because
its only path is blocked. A change that severs the (VLAN-10-irrelevant)
forwarding path, or that this pruning-vs-block interaction already strands,
escapes as SAFE.

## Scope decisions (locked with the user)

1. **Confidence-gated mechanism** — soft floor + hard ceiling. A component that
   reaches its exit only via a blocking edge: HIGH-confidence block confirmed by
   observed telemetry → treat the edge as absent → the *existing* blackhole
   stranded finding fires (REVIEW/UNSAFE). LOW/MEDIUM or telemetry disagreement
   → do NOT remove the edge; floor the pass to REVIEW with an unverified-
   reachability note. Never manufacture a hard finding on a guess (mirror rule:
   never false-UNSAFE).
2. **Trust model** — baseline agreement licenses proposed taint. Run
   `compare_to_observed` on the BASELINE tree vs observed `stp_state`; a blocking
   edge whose baseline component agrees (matched evidence, no mismatch) makes the
   PROPOSED tree's blocks on that component hard-taint eligible. Disagreement or
   vacuity → soft only.
3. **blackhole only** — it is the per-VLAN reachability check, the precise owner
   of this false-SAFE. `l2_isolation` (device-level, and carrying the delicate
   PR #21/#24 over-severance guards) is a separate future slice.

## Architecture

New pure analysis module **`analysis/stp_reachability.py`** (the
`ospf_reachability.py` precedent), memoized on `AnalysisContext`. It joins
`stp_tree()` predictions to `vlan_graph` edges via `L2Edge.member_ports` and
produces STP-aware per-VLAN reachability views.

- **`vlan_components()` is UNCHANGED.** A new sibling method
  `AnalysisContext.stp_reachable_components(vid)` returns the same
  `VlanComponent` type computed on the vlan_graph with **hard-eligible blocked
  edges removed**. Only `wired.l2.blackhole` opts into it this slice; every
  other check keeps full-graph semantics until explicitly migrated.
- **Blocked-edge classification.** For a vlan_graph edge, join each
  `L2Edge.member_ports` entry to the proposed `stp_tree()` `PortPrediction`. The
  edge is **blocking** iff any member port's `PortPrediction.state == "blocking"`
  (use `state`, NOT `role == "alternate"` — `state` already folds
  alternate/backup/self-loop blocking and avoids role-taxonomy drift).
- **Hard vs soft** per blocking edge:
  - **hard-eligible** iff BOTH the edge's endpoint nodes lie in the SAME
    baseline `stp_tree()` component, that component's `ComponentAgreement` has
    `matched_count > 0 and not disagreement`, AND the proposed block's own
    `PortPrediction.confidence == HIGH`. A proposed edge whose endpoints fall in
    different baseline components or in none (newly-created topology), or whose
    licensing baseline component lacks matched evidence, is **soft-only** — the
    "baseline telemetry licenses prediction" doctrine stays honest.
  - **soft-only** otherwise (LOW/MEDIUM proposed confidence, no baseline matched
    evidence, baseline disagreement, or new topology).
- **Blocked-edge key**: `(vlan_id, frozenset(member_ports))` — stable and
  canonical across LAG bundles (all members in one key) and self-loop pairs.
  Used for the relevance gate and for deterministic dedupe.

## The three-way removal test (monotone; per populated component reaching exit)

Reachability is monotone in edges removed: `R_hard+soft ⟹ R_hard ⟹ R_full`.
`stp_reachability` exposes the hard-removed components (the blackhole source) and
a hard+soft-removed reachability query. Per populated component that reaches its
exit in the full graph:

| Condition | Outcome |
|---|---|
| `R_hard+soft` — a fully-forwarding path exists | no taint, SAFE-eligible |
| `R_hard` but not `R_hard+soft` — reach dies only once soft blocks also go (depends on a soft-only block) | **soft REVIEW floor + note**, never a hard finding; delta-relevance–gated |
| not `R_hard` — a hard-eligible block was load-bearing | **hard strand** via existing blackhole findings |

The soft test is exactly "all non-soft paths fail" (`R_hard and not R_hard+soft`)
— NOT the looser "some path traverses a soft edge."

**Realization** (so the implementer does not compute `R_full` explicitly): the
hard rung is realized by the component-source swap (§ next) — blackhole consumes
the hard-removed components, so a component that fails to reach its exit there
(when its baseline hard-removed counterpart did) flows through the existing
`exit_lost` path automatically. Only the soft rung needs an explicit query:
`R_hard and not R_hard+soft`, evaluated on the proposed side per populated
component that still reaches its exit in the hard-removed view.

## Symmetric baseline/proposed (the pre-existing doctrine)

`blackhole` swaps **every** `vlan_components(vid)` call — both `ctx.baseline.*`
and `ctx.proposed.*` — to `stp_reachable_components(vid)`. Enumerated sites in
`src/digital_twin/checks/wired/l2_blackhole.py` (all migrate; no other check
changes): the wireless-coverage guard (~L86), the baseline exit set (~L133), the
config-member-node set (~L146), the proposed strand scan (~L150), the per-side
coverage loop (~L184), the proposed components (~L205), the baseline components
(~L264), and the `_vlan_changed` delta helper (~L393).

Because both sides use the identical STP-aware (hard-removed) view, a pre-existing
hard block strands the VLAN in BOTH → `exit_lost` (which needs
baseline-reached-proposed-not) does not fire → it demotes to INFO/context through
the existing delta machinery, exactly as the pre-existing doctrine requires.
`l2_blackhole`'s finding vocabulary and severity logic are untouched; only the
component *source* becomes STP-aware.

Making `_vlan_changed` STP-aware IS the "blocked-edge set changed" relevance
extension: if a change alters the hard-removed component structure (including the
blocked set), `_vlan_changed` registers it and the existing delta gate lets the
finding through; an unrelated change leaves the STP-aware components identical →
suppressed. The soft REVIEW floor rides this same gate, so a pre-existing
soft-block dependence untouched by the change stays INFO, never REVIEW.

## Required Spec-4 comparator extension

`analysis/stp_agreement.py` `ComponentAgreement` gains **`matched_count: int`**
(count of ports in the component in the `matched` bucket) alongside the existing
`disagreement: bool`. The hard predicate is `matched_count > 0 and not
disagreement` — never "no mismatch because every port was `unvalidatable`." The
count is reported in evidence so non-vacuity is auditable. This is a pure
additive field; the comparator's buckets and existing tests are unchanged.

## Verdict wiring + THE INVARIANT

- Hard-taint requires baseline `compare_to_observed` agreement WITH matched
  evidence on the licensing component — prediction never moves the verdict alone.
  Disagreement or vacuity → soft at most. Recorded as a coverage note so the
  licensing decision is auditable.
- **Never-false-SAFE / never-false-UNSAFE**: the hard path only ever *removes*
  illusory reach (→ more stranded, closes the false-SAFE) and only on
  telemetry-confirmed HIGH blocks (guards the mirror rule); the soft path never
  manufactures a hard finding, only a REVIEW floor + note.
- No new check id, no new finding code, no verdict-precedence change. Hard
  strands flow through blackhole's existing `exit_lost` / `new_member_stranded`
  / `stranded` severities. The soft floor is realized by **capping the affected
  VLAN's blackhole `CheckResult` confidence below HIGH** (a coverage/confidence
  degrade) plus an explanatory note — `decide()` already floors REVIEW on any
  evaluated result below HIGH confidence. It is NOT an INFO finding (INFO is
  excluded from the confidence roll-up and would not floor). The implementer
  must confirm the confidence actually reaches the CheckResult and is not
  swallowed by a vacuous-HIGH default.

## Explicit limitations (declared)

- Single-tree-per-component (Spec-4 v1) — no per-VLAN STP; the block is a
  physical-port property applied to every VLAN the port carries.
- A change that heavily RESTRUCTURES a component weakens the baseline-agreement
  license. v1 leans on the confidence tiers (restructured components tend to
  predict lower-confidence — assumed roots, ties — and fall to soft) and
  documents this rather than modeling a restructure metric.
- LAG/self-loop edges inherit their bundle/pair predicted state via the shared
  member_ports key.
- Prediction is stable-state only (no convergence dynamics) — a transient
  reconvergence outage is not modeled; this slice reasons about the settled
  proposed tree.

## Testing

- **Motivating golden**: VLAN pruned off the forwarding inter-segment link,
  carried only on a tree-blocked link, baseline telemetry confirms the block
  (matched) → hard strand → UNSAFE.
- **Pre-existing symmetric**: the same block present in baseline and proposed,
  change unrelated → INFO, verdict unchanged (SAFE/REVIEW as before).
- **Soft (low confidence)**: a port-id-tie / defaulted-speed block on the
  load-bearing edge → REVIEW floor + note, NO hard finding.
- **Disagreement**: observed `stp_state` contradicts the predicted block on the
  component → soft only, never hard.
- **Vacuous agreement**: component all-`unvalidatable` (matched_count == 0) →
  soft only, never hard.
- **Relevance**: an unrelated change on a site with a pre-existing soft-block
  dependence → no REVIEW floor (STP-aware `_vlan_changed` suppresses).
- **Coverage-note parity**: the wireless/AP coverage paths use the same
  STP-aware components as the strand logic (no split-brain).
- Full gate: `uv run pytest tests -q && uv run ruff check . && uv run mypy src`.

## Live verification (finishing phase)

Simulate against the production org (Spec-4 live-verified at 10/10 agreement):
expect **no new UNSAFE** on unrelated plans (production had no pruned-onto-
blocked-link VLANs), and confirm a synthesized pruned-onto-block plan, if
constructible on a real redundant pair, produces the hard strand. Record the
agreement/coverage summary in the ledger.

## Deferred (explicitly not this slice)

- `l2_isolation` blocked-link taint (device-level; its own slice, stacks onto
  the PR #21/#24 over-severance guards).
- Per-VLAN / VSTP trees (Spec-4 v1 is single-tree).
- A restructure metric to license proposed prediction on heavily-changed
  components (v1 uses confidence tiers instead).
- Migrating other reachability consumers (segmentation, client_impact) to
  STP-aware components.
