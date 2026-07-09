# STP policy SAFE grants (Spec-6) — design

**Date:** 2026-07-09
**Status:** Approved (brainstorm converged; user design adjustments + spec
review R1 [peer positive evidence P1, tree-representation scope P1,
telemetry-dark definition P2] baked in)
**Predecessors:** Spec-2 (`wired.stp.policy` — the REVIEW floor, SAFE explicitly
deferred), Spec-3 (live `stp_role`/`stp_state` telemetry), Spec-4 (tree engine +
`compare_to_observed` + THE INVARIANT), Spec-5 (`agreement_clean`, the
pair-aware license pattern, shared-memo precedent).

## Problem

Every STP policy change floors at REVIEW via `wired.stp.policy.policy_change`,
including provably-benign bulk hardening — the canonical case being
`stp_no_root_port` enabled on inter-switch **designated downlinks**
(distribution → access), exactly where root-protect best practice puts it.
Spec-2 deferred SAFE to "a future STP tree engine validated against live
stp_state". That engine exists (Spec-4) and is production-validated (Spec-5).
This slice grants SAFE — under a license so strict that the grant cannot
drift wider than the validated evidence.

## The central design fact

**The Spec-4 tree engine reads none of the four `StpPolicy` knobs.** Its
prediction is therefore the *unprotected/unrequired* behavior, and "baseline
tree == proposed tree" is **vacuously** true for any pure policy change — tree
identity alone can NEVER be the SAFE proof (a false-SAFE trap). The proof is
**knob-specific inertness against the validated tree**: the knob's semantics,
evaluated against a tree position that live telemetry has confirmed.

## Scope decisions (locked with the user)

1. **SAFE-eligible knobs: `stp_no_root_port` and `stp_required` only** (both
   directions each, under per-direction rules below). `stp_p2p` (RSTP
   proposal/agreement handshake — convergence dynamics, explicitly unmodeled
   by Spec-4) and `use_vstp` (protocol change — mixed-protocol interop
   explicitly out of Spec-4 scope) stay on the `.policy_change` floor.
2. **Grant shape: a new INFO code `wired.stp.policy.inert_change`** carrying
   the full license evidence. The change stays visible in the verdict document
   (audit trail of *why* the twin considered it inert); INFO is excluded from
   confidence roll-ups by existing convention; the port can resolve SAFE.
   Mirrors the `.preexisting` INFO-context convention.
3. **License strictness: component-clean AND port-matched.** The changed
   port's OWN baseline agreement row must be `matched` — not just "some port
   in the component matched". A telemetry-dark port can never earn SAFE.
4. **Architecture: pure pair-aware `analysis/stp_inertness.py`** (the Spec-5
   pattern), memoized on `CheckContext`; the check stays a consumer.
5. **Future-failure posture is OUT OF SCOPE for the SAFE claim** (user
   adjustment 3): the grant asserts "no current stable-state dataplane
   change", NOT "no change to future protection posture". Enabling
   root-protect on a designated port alters what happens in a future
   topology event — by design (the operator is hardening); the twin's SAFE
   speaks only to the stable state being provably unchanged. This boundary
   is stated in the module docstring and the finding's `severity_reason`.
6. **SAFE is scoped to ports REPRESENTED IN THE STP TREE** (review R1-P1-2):
   the Spec-4 engine predicts only switch-to-switch links and reciprocal
   self-loop pseudo-edges — ordinary client/AP-facing access ports and
   unlinked ports have NO `PortPrediction` and NO agreement row, so they
   fail license clauses (b)/(d) by construction and stay on the REVIEW
   floor, even when their observed `stp_role` is `designated`. This is the
   honest boundary, not an accident: the engine has earned no trust about
   ports it does not model, and an observed role with no prediction to
   agree with is exactly the "prediction alone / observation alone" territory
   THE INVARIANT forbids. Grants for non-tree ports require an engine
   expansion (deferred, listed below). The practical scope that remains is
   the canonical one: inter-switch tree ports — designated downlinks for
   root-protect, validated uplink pairs for `stp_required`.

## Architecture

```
CheckContext
  ├── stp_agreement        NEW memoized property: compare_to_observed(
  │                        baseline.stp_tree(), baseline.ir) — computed ONCE
  ├── stp_reachability     now constructed WITH the shared report
  └── stp_inertness        NEW memoized property: StpInertness(baseline,
                           proposed, agreement=ctx.stp_agreement)

analysis/stp_inertness.py  (pure — no I/O, no findings, no checks import)
  StpInertness(baseline, proposed, agreement=None)
    .decide(pid, knob, old_value, new_value) -> InertnessDecision

checks/wired/stp_policy.py (consumer)
  per changed port: risks first (unchanged) → else all-knobs-inert →
  .inert_change INFO — else .policy_change floor (unchanged) with the
  failure reasons folded into evidence
```

### Shared agreement memo (user adjustment 1)

`CheckContext.stp_agreement` is an explicit memoized property (same lazy
`object.__setattr__` idiom as `stp_reachability`) returning the baseline
`AgreementReport`. Both `StpReachability.__init__` and `StpInertness.__init__`
gain an optional `agreement: AgreementReport | None = None` parameter —
`None` → compute it themselves (purity and every existing test preserved);
`CheckContext` always passes the shared report so cache behavior lives in
exactly one place and the report is computed once per run, not per consumer.

### Contract

```python
@dataclass(frozen=True)
class InertnessDecision:
    inert: bool
    reasons: tuple[str, ...]   # the failing clause(s), or the granting facts
    evidence: dict[str, object]  # license + knob-specific facts, finding-ready
```

`decide(pid, knob, old_value, new_value)` where old/new are the EFFECTIVE knob
values the check already computes (`bool | str`; default False when the port
carries no `StpPolicy`). The module never diffes `StpPolicy` itself — the
check owns "what changed", the module owns "is that change provably inert".

Immediate `inert=False` (with reason) for: a non-eligible knob; a
non-`bool` old or new value (unresolved token); any license failure; any
knob-rule failure. Reasons are exact clause names so a floored finding's
evidence explains the near-miss.

## The license (all clauses required, evaluated before any knob rule)

- **(a) Baseline existence.** `pid` exists in the baseline IR. A port ADD has
  no earned trust — never inert.
- **(b) Port matched.** The port's own row in the baseline agreement report is
  bucket `matched` — per the Spec-4 comparator: observed role agrees, and a
  present-but-empty (`""`) `stp_state` skips the state cross-check but never
  rescues a role mismatch. An unvalidatable row fails; the grant never rests
  on prediction alone — THE INVARIANT. Note the `stp_required`-disable rule
  additionally requires a literal observed `"forwarding"`, so a state-dark
  (`""`) port can match here yet still cannot earn that specific grant.
- **(c) Component clean.** The port's baseline STP component has
  `agreement_clean` (≥1 matched, zero disagreement, zero bpdu-inconsistent —
  the Spec-5 property, unchanged).
- **(d) Tree position unchanged.** The port's baseline and proposed
  `PortPrediction`s are identical in `(role, state)` and BOTH have
  per-decision confidence HIGH. This guards the multi-op plan that also moves
  the tree (disables another port, changes a link): trust was earned on the
  BASELINE tree; if the delta reassigns this port's tree position, the
  proposed prediction is unvalidated extrapolation → floor.

## Knob rules (after the license)

### `stp_no_root_port` — both directions

**Inert iff the port's predicted role == `"designated"`.**

Deliberately narrower than "role != root": Juniper root-protect on an
*alternate* port (which receives superior BPDUs by definition — that is what
makes it alternate) enters root-inconsistent state — the dataplane is
unchanged NOW, but the failover path is silently removed. That deserves
REVIEW, not SAFE. A designated port never receives superior BPDUs in the
validated stable state, so protect provably never triggers. "Designated"
also covers every port of the root bridge (all its ports are designated).
`backup` (self-loop pseudo-edge) ports floor.

The enable-on-observed-root case never reaches the grant: the Spec-3
observed-root ERROR route fires `.root_protect_risk` first and risk always
wins (see Emission rules). Disable (True→False) on a designated port:
superior BPDUs do not arrive in the validated stable state, so protection was
never triggering — removing it is inert under the same rule.

### `stp_required` enable (False/absent → True)

**Inert iff the port has an effectively STP-participating peer** (user
adjustment 2 + review R1-P1 — every clause required, so "non-AP" can never be
read broadly, and the peer's STP participation is POSITIVELY evidenced, not
merely not-ruled-out):

- a modeled link with two-sided HIGH confidence (`lk.meta.confidence.level is
  HIGH` — the same bar as `_tie_confidence`),
- the peer DEVICE is a switch (`DeviceRole.SWITCH`) — an STP participant;
  gateway/AP/other roles never qualify,
- the peer PORT is present in BOTH baseline and proposed IR with effective
  `bpdu_filter is False` in BOTH states,
- **the peer port's own baseline agreement row is `matched`** — its observed
  `stp_role` is present and agrees with the prediction; live telemetry proves
  the peer is actually running STP on that port, not merely "a switch that
  should be" (a switch role + `bpdu_filter=False` only rules out a known
  filtering peer — it is enough to avoid `.blocking_risk`, NOT enough to
  prove SAFE; review R1-P1),
- **the peer port's baseline and proposed `PortPrediction`s are identical in
  `(role, state)` and both HIGH** — license clause (d) mirrored onto the
  peer, so a delta that moves the PEER's tree position defeats the grant too,
- plus the license on the changed port itself (matched baseline row,
  identical HIGH baseline/proposed prediction).

Because the link is modeled switch-to-switch and neither end is excluded, the
peer sits in the same baseline STP component as the changed port — license
clause (c) already vouches for that component's cleanliness; the two clauses
above add the peer-row and peer-position evidence on top. This is now
strictly stronger than the complement of `.blocking_risk`'s tiers: BPDUs are
demonstrably exchanged (the peer's validated role exists only because BPDUs
flow), the requirement is already satisfied, the knob is inert. The module
implements its own small peer scan (the family's established cloned-idiom
convention; `analysis/` must not import from `checks/`).

### `stp_required` disable (True → False)

**Inert iff the port's observed baseline `stp_state == "forwarding"`** — the
requirement is demonstrably not the operative constraint, so removing it
changes nothing now. An observed-blocking port floors (un-blocking a port is
never assumed benign: if the requirement was the thing holding the port down,
removing it can open a loop); a telemetry-dark port already failed license (b).
Per adjustment 3, the SAFE claim is stable-state-only: the port losing its
BPDU-requirement *guard* is a posture change, not a dataplane change.

## Emission rules (check wiring)

Per changed port, in order:

1. **Risk codes compute first, byte-identical to today** (`.blocking_risk`,
   `.root_protect_risk`, including the observed-root route and the liveness
   guard). If any fire, they are emitted and the grant is never consulted.
2. Otherwise, `ctx.stp_inertness.decide(...)` runs for EVERY changed knob on
   the port. ALL inert → provisionally one `.inert_change` finding: INFO,
   confidence HIGH, `subject` the port, `caused_by` populated from
   `delta_index`, evidence = license facts (matched row, component stats,
   predicted role/state both sides) + per-knob granting facts + the
   stable-state-only `severity_reason`.
3. Any knob non-eligible/unproven → `.policy_change` WARNING/MEDIUM exactly
   as today, with the `InertnessDecision.reasons` folded into its evidence
   under `"inertness"` (near-miss diagnosability; no new coverage note — the
   floor is not a blind spot, it is the honest verdict).
4. **`.inert_change` suppression (user adjustment 4):** the provisional grant
   is emitted ONLY if no WARNING-or-higher finding from THIS check names the
   port among its `affected_entities`. This makes risk/`link_mismatch`
   coexistence unambiguous — including the cross-end case: a delta that
   changes `use_vstp` on the PEER port and raises a WARNING `.link_mismatch`
   on the shared link blocks the grant on THIS port too, even though this
   port's own changed knobs are all licensed inert. An INFO `.link_mismatch`
   (pre-existing disagreement, merely touched) does NOT suppress — INFO is
   context, and Spec-2's rule that INFO never substitutes for the floor has a
   mirror here: INFO never blocks a grant either.
5. `.preexisting` INFO context: unchanged.

**The Spec-2 floor invariant is amended to:** every port with a changed
`stp_policy` yields at least one delta-caused finding — WARNING-or-above, OR
a fully-licensed `.inert_change` INFO. Never zero findings. (Spec-2's stronger
"never SAFE" clause is retired by this slice — that was its explicit purpose.)

Unresolved-token knobs keep today's behavior exactly: the coverage note, the
floor, and (new) a token on ANY changed knob of a port defeats that port's
grant via the non-`bool` rule.

## Never-false-SAFE argument

- The grant requires live telemetry to have validated the exact tree position
  being reasoned about (license b), the whole component to be clean (c), and
  the proposed tree to leave that position untouched (d) — prediction alone
  never earns SAFE (THE INVARIANT, discharged by construction).
- Each knob rule claims only stable-state inertness, and only where the knob's
  trigger condition (superior BPDUs; BPDU absence; an active block) is
  provably absent in that validated stable state.
- Vacuous tree identity is structurally excluded: identity is checked
  per-port as license (d), but the PROOF is always the knob rule — a knob the
  engine does not model can only pass through a rule written for its specific
  semantics, and non-eligible knobs hard-fail.
- Anything the twin cannot see (dark telemetry, one-sided ties, tokens,
  dirty components, moved tree positions) fails a named clause and lands on
  the unchanged REVIEW floor. Strictly: this slice can only ever REMOVE a
  WARNING that today fires on provably-inert changes — it can never demote a
  risk code, a mismatch, or a floored port it cannot prove.

## MIRROR-RULE note

No new WARNING/ERROR paths are introduced; false-UNSAFE exposure is unchanged.
The slice strictly reduces REVIEW noise.

## Existing-test impact

**"Telemetry-dark" is defined by `stp_role`, not `stp_state`** (review
R1-P2-3): per the Spec-4 comparator, a row is `unvalidatable` when the
observed ROLE is missing/unknown; a present role with absent state can still
land `matched` (role-only match). The existing-suite argument is therefore:

- fixtures with no observed `stp_role` on the changed port (the vast
  majority) → row unvalidatable → license (b) fails → floor, byte-identical;
- the Spec-3 observed-root fixtures DO carry `stp_role="root"` on the changed
  port → `.root_protect_risk` fires and risk always wins → the grant is never
  consulted → byte-identical;
- any fixture port that is role-matched must ALSO clear clauses (c)/(d) and
  a knob rule to change behavior — the full existing suite runs in the gate
  and pins byte-identical verdicts either way.

One deliberate amendment: Spec-2's never-SAFE e2e guard pins the retired
invariant and is updated to the new one — SAFE is permitted iff every changed
port carries `.inert_change` (and asserts at least one fixture actually
exercises the SAFE path so the guard is not vacuous).

## Testing

Unit — `StpInertness` (each on the bridge-id validated-fixture family from
Spec-5, extended with observed telemetry):

- License isolation, one clause at a time: port ADD (a); unvalidatable row
  (b); **target port matched but ANOTHER port in the same component
  mismatched → fail (c)** and **another port bpdu-inconsistent → fail (c)**
  (user adjustment 5 — component-dirty conservatism with a matched target);
  LOW/MEDIUM prediction (d); delta moves the tree so baseline≠proposed
  role/state (d).
- Knob rules: designated → inert (both root-protect directions); alternate →
  floor for root-protect; `stp_required` enable — switch peer two-sided HIGH
  no-filter with MATCHED peer row + identical HIGH peer prediction → inert;
  AP peer / bpdu_filter peer / gateway-role peer / one-sided tie / peer port
  absent in one state → floor; **telemetry-dark peer (switch peer, no-filter,
  but peer row unvalidatable) → floor** (R1-P1-1); **peer tree position moved
  by the delta → floor**; `stp_required` disable — observed forwarding →
  inert, observed blocking → floor.
- **Non-tree port (R1-P1-2): an access port with observed
  `stp_role="designated"` but NO `PortPrediction` (client-facing, not in the
  active topology) → floor**, proving the tree-representation boundary.
- Tokens; non-eligible knobs; multi-knob mixed (one inert + one not) → floor.

Check-level:

- `.inert_change` shape (INFO/HIGH, evidence, caused_by); risk-wins ordering
  (observed-root + would-be-grant → `.root_protect_risk` only); adjustment-4
  suppression: peer-end `use_vstp` change raises WARNING `.link_mismatch` →
  no grant on this port; INFO `.link_mismatch` does NOT suppress; floor
  evidence carries the failure reasons.

e2e goldens (real `CheckRegistry.run_all` → `assemble`/`decide`):

- Bulk root-protect hardening on the telemetry-validated fixture's
  **inter-switch designated downlinks** → **SAFE** (the headline, scoped per
  decision 6).
- Same fixture, plan additionally touching one telemetry-dark port → REVIEW.
- Plan touching an observed-designated but non-tree access port → REVIEW
  (the R1-P1-2 boundary, end-to-end).
- `stp_p2p` change → REVIEW, byte-identical to today.
- Amended Spec-2 guard as above.

Live verify: replay a bulk `stp_no_root_port` plan against the
production-validated site targeting matched inter-switch designated ports →
SAFE end-to-end; plus the full existing golden suite → zero verdict changes.

## Out of scope / deferred

- **Grants for non-tree ports** (ordinary client/AP-facing access ports):
  requires extending the engine to predict edge-port roles (a non-tree
  switch port with no bridge peer is trivially designated/forwarding in
  RSTP) AND a validation story for those rows — an engine-expansion slice,
  not a license tweak (R1-P1-2).
- `stp_p2p` and `use_vstp` grants (need convergence/protocol modeling the
  engine does not claim).
- Future-failure posture reasoning (resilience-degradation findings for
  root-protect on alternate ports — would be a new REVIEW refinement, not a
  SAFE grant).
- Per-VLAN/VSTP trees (Spec-4 declared limitation).
- `l2_isolation` blocked-link taint and `stp_root` tree-diff (separate
  roadmap slices).
