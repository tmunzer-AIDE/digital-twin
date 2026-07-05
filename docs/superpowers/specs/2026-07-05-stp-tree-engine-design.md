# STP Tree Engine v1 — Prediction Core + Validation Rail (Spec-4)

**Date:** 2026-07-05
**Status:** Implemented (branch feat/stp-tree-engine; live-verified 2026-07-05 — production org 10 matched/0 mismatched; TM-LAB self-loop pair via redacted replay fixture, golden committed)
**Predecessors:** Spec-2 (`wired.stp.policy`, policy floor — SAFE deferred to a
validated tree engine), Spec-3 (STP telemetry escalation — live-confirmed
per-port `stp_role`/`stp_state` ground truth)

## Problem

Every STP policy change today floors at REVIEW (`wired.stp.policy.policy_change`)
because the twin cannot predict what the spanning tree actually does. The
prerequisite for ever granting SAFE — and for making blackhole/isolation
reachability aware of blocked links — is an engine that predicts the stable-state
tree (root, per-port roles, blocked set) and has **earned trust against live
telemetry**. This slice builds that engine and its validation rail. It changes
no verdict, no check, no finding.

## Scope decisions (locked with the user)

1. **V1 = engine + validation rail only.** No check/verdict changes. Consumers
   (SAFE grants, reachability taint) come in later slices, gated on proven
   agreement.
2. **Single tree per physical L2 component.** No per-VLAN/VSTP correctness
   claim — Mist config cannot express per-VLAN priorities, and Mist telemetry
   carries ONE `stp_role`/`stp_state` per port, so a per-VLAN prediction could
   not be validated anyway. Declared limitation, not a silent assumption.
3. **IEEE speed-derived costs + per-decision confidence.** Decisions carried by
   cost margin or bridge ID are HIGH; decisions that would fall to port-number
   tie-breaking (ungrounded — Mist exposes no ifindex/port-priority) are
   predicted deterministically but capped LOW.
4. **Validation = live gate + replay goldens.** A Tier-2-equivalence-style
   read-only gate script (strict on HIGH-confidence predictions) plus committed
   redacted replay-fixture goldens pinning agreement in CI.

## Architecture

New pure module **`analysis/stp_tree.py`** (the `ospf_reachability.py`
precedent). Memoized on `AnalysisContext` as `stp_tree()` alongside
`l2_graph()`/`cycles()`. Pure function of the IR; no I/O, no findings.

### Election reuse — one election rule

`_root_of` MOVES from `checks/wired/stp_root.py` to `analysis/stp_tree.py`
(public name `root_of`), semantics byte-identical: `None` when < 2 switches,
`_ABSTAIN` sentinel when any elector's `stp_priority_invalid`, else
`(root_device_id, any_default_assumed)` by min `(stp_priority ?? 32768, device_id)`.
**Both existing importers switch to the new home**: `stp_root.py` (its own
election) and `stp_policy.py:76` (`_root_protect_risk` graph route). Behavior
pinned by the existing suites — the move must be a pure relocation
(re-export or direct import; no logic edit).

### Result contract (structured data, never findings)

```python
@dataclass(frozen=True)
class PortPrediction:
    port_id: str
    role: str            # "root" | "designated" | "alternate" | "backup"
    state: str           # "forwarding" | "blocking" (derived from role)
    confidence: ConfidenceLevel
    deciding_factor: str # "cost" | "bridge_id" | "port_id_tie" | "sole_path"
                         # | "root_bridge"
    notes: tuple[str, ...] = ()

@dataclass(frozen=True)
class ComponentTree:
    nodes: frozenset[str]           # VC-folded switch device ids
    root: str | None                # None = no election (see below)
    root_assumed_default: bool
    ports: Mapping[str, PortPrediction]  # keyed by port_id; participating ends only

@dataclass(frozen=True)
class StpTreePrediction:
    components: tuple[ComponentTree, ...]
    notes: tuple[str, ...]          # IR-wide abstention notes
```

`root=None` has two distinct causes, both VALID analysis results (not errors):
fewer than two active switches AND no pseudo-edges (nothing to elect, nothing
to classify — no port predictions, no note), or `_ABSTAIN` on an
uninterpretable priority (no port predictions + an explicit note; never guess
past bad input). **Single-switch component WITH a reciprocal self-loop (P2
decision): still predicted** — the switch is the TRIVIAL root (no competing
bridge exists; no priority comparison is made, so `root_assumed_default` is
False and `root_of` is not consulted — engine-local rule, the moved helper's
`< 2 switches → None` semantics stay untouched for its check consumers), and
the pseudo-edge classifies designated/backup as usual. This keeps a
standalone lab-loop switch validatable instead of silently unpredicted. The *live gate* treats
"zero participating ports across the whole org" as FAIL (vacuous green), but
the pure module returning an empty/rootless component is correct behavior —
these are different layers' contracts.

## Topology preparation (before any election)

Build the STP-active subgraph per component:

- **Ports excluded:** `disabled`, `bpdu_filter`. NOTHING else — `Port` has no
  up/down field, and `observed_speed is None` must NOT be read as down (it
  also means "no telemetry"). Participation is defined by config intent +
  modeled links only.
- **Links excluded:** any link with an excluded end; any link whose either end
  is not a SWITCH-role device (APs, unmanaged neighbors — the tree spans
  switches only).
- **`stp_edge` ports stay in and are elected NORMALLY**: edge is a role hint,
  not non-participation — an edge port receiving a BPDU self-heals into a
  participant, so on a modeled switch↔switch link the election (not edge
  fiat) decides its role; the prediction carries a note that the port is
  edge-configured (that misconfiguration itself is `link_mismatch`'s job,
  Spec-2). An edge port with no modeled switch link has no link end in the
  active subgraph and gets no prediction at all — "participating ends" ≡
  ends of links in the active subgraph.
- **Self-loop pseudo-edges are SYNTHESIZED, not read from links** (P1):
  ingest deliberately mints NO `Link` for same-device LLDP (Spec-3
  `_emit_links` same-device skip) and `build_l2_graph()` drops self edges —
  so building from `ir.links`/`l2_graph` alone would silently lose every
  self-loop. Construction rule: for each RECIPROCAL claim pair
  (`a.self_loop_peer == b.port_id and b.self_loop_peer == a.port_id`, both
  ends passing the port-exclusion rules above), synthesize one same-bridge
  pseudo-edge, deduped per pair (frozenset key — the Spec-3 `l2_loop`
  precedent). One-sided claims synthesize NOTHING (unconfirmed physical
  loop) and add a component note. Pseudo-edges are classified in role
  assignment but are NEVER part of the RPC/Dijkstra graph.
  "Participating ends" ≡ ends of active-subgraph links ∪ ends of
  synthesized pseudo-edges.

## Election + role assignment

Per component of the active subgraph:

1. **Root:** `root_of(ir, component)` (moved helper). ABSTAIN → rootless
   component + note, skip roles.
2. **Bridge RPC:** Dijkstra from the root over LINK costs gives each bridge's
   root-path-cost. **Same-bridge pseudo-edges never enter the Dijkstra
   graph — they never contribute to RPC** (they exist only for role
   classification in step 3).
3. **Role assignment walks EVERY graph edge / member port, not the node-level
   SPT.** Node-level shortest paths cannot distinguish parallel links between
   the same bridge pair, LAG members, or self-loop ends — each physical port
   end gets its own decision:
   - **Same-bridge pseudo-edge (the EXCEPTION — wins over every rule below,
     including on the root bridge itself):** deterministic port tie-break
     picks one end `designated`, the other `backup` — always
     `deciding_factor="port_id_tie"`, always LOW. A self-loop on the elected
     root is still designated/backup, NOT designated/designated (P2).
   - Root bridge: every OTHER active port → `designated`
     (`deciding_factor="root_bridge"`).
   - Per non-root bridge, **root port** = min over its link ends of the IEEE
     total-order key (below). Sole candidate → `deciding_factor="sole_path"`.
   - Per link, **designated end** = the side whose bridge has lower
     `(RPC, bridge_id)`; ties within one bridge (parallel links) fall to the
     port-id component of the key.
   - Every remaining participating end → `alternate` (blocking).
4. **State derivation:** `alternate`/`backup` → `blocking`; `root`/`designated`
   → `forwarding`.

**The IEEE total-order key is ONE explicit comparison tuple** implementing
root bridge ID → root path cost → sender bridge ID → sender port ID →
receiver port ID. The port-ID components are supplied by a deterministic
port-name sort (stable output) but are marked UNGROUNDED: any decision whose
outcome the port-ID components determine is capped LOW.

## Cost model + confidence

- **Cost is PER PORT END and DIRECTIONAL, never a symmetric link value**
  (review P1): IEEE 802.1t value from that end's own speed ladder
  (`observed_speed` → `speed` (config) → None → 1G default + LOW cap on
  every decision whose margin the default could flip). RPC accumulates the
  RECEIVING port's cost — `RPC(B via p) = RPC(neighbor) + path_cost(p)`
  where `p` is B's own port on the link — so Dijkstra runs over a directed
  view in which edge `u→v` weighs `path_cost(v's port)`. Asymmetric
  negotiated/configured end speeds therefore produce asymmetric candidate
  costs, exactly as the protocol does; a speed disagreement between two
  known ends is still noted + capped MEDIUM (it usually signals telemetry
  inconsistency), but the algorithm NEVER collapses it to `min`.
- **Per-decision confidence:**
  - HIGH: decided by cost margin or bridge ID, all contributing link costs
    known, link confidence HIGH.
  - Capped at the minimum LINK confidence along the deciding comparison (a
    MEDIUM one-sided-LLDP link cannot carry a HIGH prediction).
  - LOW: decided by port-ID tie-break, or resting on an unknown/defaulted
    speed.
  - Component-wide cap MEDIUM when `root_assumed_default` (matches
    `stp_root`'s existing stance on assumed 32768).

## Validation rail

### Pure comparator (`analysis/stp_tree.py` or sibling; pure)

`compare_to_observed(prediction, ir) -> StpAgreementReport` joins
per-port predictions against observed `Port.stp_role`/`Port.stp_state`:

- Observed `None` (absent or `""`-normalized) → **unvalidatable** (excluded
  from agreement math).
- **Unknown/variant observed role strings → unvalidatable, NOT mismatch** —
  the live vocabulary is `root/designated/backup/alternate`, but an
  unrecognized token must never count against the engine (nor for it).
- `disabled-bpdu-inconsistent` → reported in a separate bucket (protection
  state, not a role).
- Role compared exactly; state cross-checked independently (a role match with
  a state mismatch is still a mismatch).
- Report: `matched / mismatched_high / mismatched_medium / mismatched_low /
  unvalidatable` totals + per-port detail rows, and per-component rollups
  (consumers cap confidence at component granularity). Mismatch buckets key
  on the PREDICTION's confidence tier exactly (review P2) — MEDIUM
  predictions (assumed-default root, link-confidence caps, speed
  disagreement) land in `mismatched_medium`, never silently up- or
  down-classified into the HIGH/LOW buckets.

### Live gate script (read-only; Tier-2-equivalence precedent)

Fetch via SDK → ingest → predict → compare, over TM-LAB (self-loops cabled —
real ground truth for `backup`/`blocking`) and the production org:

- **FAIL on ANY `mismatched_high`** (a HIGH-confidence prediction the network
  contradicts is an engine bug, full stop).
- `mismatched_medium` and `mismatched_low` → report-only for now (MEDIUM
  predictions rest on a declared assumption — assumed-default priority,
  degraded link, speed disagreement; LOW are declared tie-break guesses).
  Tightening MEDIUM into the gate is a later decision, taken on real
  agreement data, not now.
- **Zero participating ports org-wide → FAIL** (no vacuous green).
- Prints the full per-port table for mismatches + the agreement summary.

### Replay goldens

A committed fixture captured from TM-LAB via `ReplayStore.save_raw` pins the
agreement offline in CI. **Fixtures are redacted on write** (org/site UUIDs +
MACs pseudonymized) — goldens target the fixture's REDACTED ids (Spec-3
lesson, recorded in the ledger).

## THE INVARIANT (binding on all future consumer slices)

> **Every future verdict-facing consumer of `stp_tree()` MUST call
> `compare_to_observed` and cap/degrade its confidence on component-level
> disagreement. Prediction alone NEVER earns SAFE; agreement with observed
> telemetry is what allows confidence.**

This spec's slice stays pure; this invariant prevents the next slice from
treating the engine as an oracle. It is stated here so the consumer spec
inherits it as a requirement, not a suggestion.

## Explicit limitations (v1, declared)

- Single tree per component — no per-VLAN/VSTP claim (config can't express
  it; telemetry can't validate it).
- Port-priority/port-ID inputs ungrounded → those tie-breaks are permanently
  ≤ LOW in v1.
- LAG/ESI-LAG bundles are ONE logical STP port pair (the l2_graph already
  collapses members, matching `ae` semantics — per-member STP edges would
  wrongly predict blocking on members LACP keeps forwarding). Aggregate cost
  approximated from the best (min-cost) member end → capped MEDIUM + note;
  each member port carries the bundle's role.
- Stable state only — no convergence dynamics, timers, or transient states.
- Mixed-protocol (VSTP↔RSTP) interop out of scope.
- **Zero verdict-facing change:** `stp_root`, `stp_policy`, `l2_loop`,
  blackhole/isolation byte-identical (the `_root_of` relocation is proven
  behavior-preserving by their existing suites).

## Testing

TDD throughout:

- Unit tests per classification rule: each role, each `deciding_factor`, each
  confidence cap (unknown speed, link-confidence cap, assumed-default cap,
  port-id tie).
- Parallel-links and self-loop member-port granularity pinned explicitly
  (node-level SPT shortcuts must fail these).
- Self-loop construction pinned (P1): a reciprocal pair with NO `Link` in
  `ir.links` still yields predictions (the synthesis rule, not the link
  table, is load-bearing); one-sided claim → no pseudo-edge + note.
- Self-loop exception ordering pinned (P2): a reciprocal pair ON the elected
  root bridge → designated/backup (never designated/designated); a
  single-switch component with a reciprocal pair → trivial root +
  designated/backup; a single-switch component without one → no predictions.
- Determinism: same IR → identical prediction (ordering-independent).
- `_root_of` relocation pinned by the untouched `stp_root` + `stp_policy`
  suites.
- Comparator: agreement/disagreement/unvalidatable/unknown-token/
  bpdu-inconsistent buckets over synthetic IRs.
- Goldens from the redacted TM-LAB fixture.
- Full gate: `uv run pytest tests -q && uv run ruff check . && uv run mypy src`.

## Live verification (mandated, run while the lab loops are still cabled)

1. Run the live gate against TM-LAB: expect the backbone root port
   (`xe-0/1/3`-side) and the self-loop designated/backup pairs
   (`ge-0/0/8↔9` test_pvstp, `ge-0/0/10↔11` test_stp) in the per-port table;
   self-loop pair predictions are LOW (port-id ties) so mismatches there are
   report-only — but role agreement is the expected outcome.
2. Run the live gate against the production org: expect zero
   `mismatched_high`; record the agreement summary in the ledger.
3. Capture the TM-LAB fixture for the committed goldens in the same session.

## Deferred (explicitly not this slice)

- Any consumer: SAFE grants in `wired.stp.policy`, blocked-link reachability
  taint in blackhole/isolation, `stp_root` upgrade to tree-diff.
- Per-VLAN trees; port-priority grounding if Mist ever exposes it;
  LAG-aware costs; convergence dynamics; VSTP↔RSTP interop.
- `stp_role`-based root-direction triangulation (Spec-3 deferral) as an
  additional election cross-check.
