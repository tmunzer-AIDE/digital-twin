# Always surface the config diff (`config_diffs` on `UNKNOWN`)

**Status:** PROPOSED
**Date:** 2026-06-27
**Author:** brainstormed with the repo owner

The configuration diff (`ObjectConfigDiff`, added 2026-06-23) is today withheld
from every `UNKNOWN` verdict. So the most common "why did this fail?" case — an
out-of-scope edit the field gate rejected — returns a verdict with **no diff at
all**, even though the admin most wants to see *what they changed*. This spec
surfaces the before→after diff **regardless of `Decision.UNKNOWN`**, for every op
where a `before → after` is computable, across all three simulate paths. The diff
stays exactly what it is today: redacted, structural, and strictly
non-load-bearing.

## Problem

The diff is dropped on `UNKNOWN` by **two** mechanisms, present identically in
`simulate` (site), `simulate_org_plan` (org-template), and `simulate_org_nac`
(org-NAC) in `src/digital_twin/engine/pipeline.py`:

- **A — built late, not threaded through early exits.** Each path appends its
  per-op `object_config_diff(...)` only *after* the field gate (`screen_op`)
  passes — site `pipeline.py:411`, org `:558`, NAC `:722`. Every early-exit
  helper that builds the `UNKNOWN` verdict — `_unknown`, `org_unknown`,
  `_org_nac_unknown` — constructs it with no diffs. The NAC L0-fatal branch
  (`pipeline.py:714`) is worse: it builds `OrgNacVerdict(...)` **directly**,
  not via the helper, so even threading the helper would miss it.
- **B — final suppression.** Where a diff *was* accumulated, the attach is
  guarded `if decision is not Decision.UNKNOWN` — site `pipeline.py:463-464`,
  org `:574` / `:618`, NAC `:762`.

For the screenshot case (a site `updateSiteDevice` whose change touched an
out-of-scope leaf such as `port_config.ge-0/0/4.description`), mechanism A fires:
`screen_op` rejects at `:403`, the loop returns `_unknown(...)` at `:405` —
*before* the diff is built at `:411` — so the verdict carries `config_diffs=()`.

### Superseding the prior non-goal

The original config-diff spec
(`docs/superpowers/specs/2026-06-23-config-diff-in-results-design.md`, Non-goals)
deliberately excluded "Config diffs on UNKNOWN/rejected plans," reasoning *"a
plan rejected by parse/scope/L0 has no coherent after."* That rationale is only
true for a **subset** of UNKNOWN exits. For a field-gate rejection, an L0-fatal,
or a site apply rejection, the `after` (`effective` / `proposed_t`) **has already
been computed** — we simply could not *prove the change safe*. This spec draws
the line precisely at **computability of `before → after`** (§1), surfaces the
diff wherever that holds, and keeps the genuinely-uncomputable exits diff-less.
"We couldn't prove it safe" and "there is nothing to show" are different states;
only the latter justifies an empty diff.

## Goals

- `config_diffs` is surfaced **regardless of `Decision.UNKNOWN`** on all three
  paths, for every op whose `before → after` is computable — **including the op
  that triggered the UNKNOWN** (e.g. the field-gate-rejected op).
- **Earlier passed ops** in a multi-op plan keep their diffs even when a later op
  forces an early exit.
- **Strictly non-load-bearing — unchanged invariant.** The diff MUST NOT alter
  any decision, severity, confidence, coverage, finding, or reason. `decide()` /
  `decide_org()` take it as no input today and must continue to. (Same contract
  as the 2026-06-23 spec.)
- **Redaction unchanged.** `object_config_diff` already redacts every leaf via
  `redact_leaf(full_path, …)`, path-agnostically. Surfacing previously-withheld
  out-of-scope leaves introduces **no new redaction surface** — they pass through
  the identical masking.
- **No new source of truth.** Reuse the existing rolling pre-op state and the
  existing `object_config_diff`; do not add a second diff pass.

## Non-goals (recorded, deferred)

- **Diffs for genuinely-uncomputable exits.** Where no coherent `before → after`
  exists — plan parse / scope-pre / `check_objects` rejections, object-not-found,
  baseline fetch failure, set+delete conflict, org `apply_template` rejection —
  `config_diffs` stays `()` for that op (or `()` overall when nothing resolved).
  The rejection *reasons* explain those. This is the refined remainder of the
  2026-06-23 non-goal, not a regression. Enumerated exhaustively in §1.
- **Per-site rippled config diffs under an org-template plan.** Unchanged from
  2026-06-23: the template-object diff shows once on the `OrgVerdict`; per-site
  `Verdict.config_diffs` stays `()` under the org path.
- **Any renderer semantic change.** `_render_config_diffs` already consumes
  `verdict.config_diffs` unconditionally (`drivers/render.py:143`, `:200`,
  `:224`), so the data simply appears. An optional "proposed — not validated"
  header on UNKNOWN verdicts is deferred unless the owner requests it (§7).
- **Changing what a diff contains.** Leaf granularity, `null == absent`, and
  full-object-replacement semantics stay exactly as the field gate evaluates
  them.

## §1 The computability boundary (the decisive distinction)

A diff requires a real `before` **and** a real `after`. The table below is the
authoritative enumeration of every exit per path and whether it carries a diff.
"DIFF" = the op's `object_config_diff` is included; "—" = `()` for that op
(earlier passed ops still carry theirs).

### Site — `simulate`

| Exit | `pipeline.py` | `before`/`after` state | Result |
|---|---|---|---|
| object-not-found | ~353 (`:357`) | no `before` (object absent) | — |
| set+delete conflict | ~367 (`:371`) | `effective` not yet computed | — |
| L0-fatal | ~396 (`:395`) | `effective` computed (`:380`) | **DIFF** |
| field-gate reject | ~405 (`:403`) | `effective` computed | **DIFF** ← screenshot |
| `adapter.apply` reject | ~417 (`:416`) | `effective` computed (real apply, post-effective) | **DIFF** |
| derived-gate / check `UNKNOWN` (final) | `:463` | all ops' diffs already built | **DIFF** |

### Org-template — `simulate_org_plan`

| Exit | `pipeline.py` | `before`/`after` state | Result |
|---|---|---|---|
| parse / `check_objects` / not-org | ~494 / ~510 / ~512 | no resolved ops | — |
| org-template lookup failed | ~526 (`:527`) | no `snapshot` (`before` absent) | — |
| `apply_template` reject | ~538 (`:537`) | **this step computes `proposed_t`**; it failed → no `after` | — |
| L0-fatal | ~543 (`:542`) | `proposed_t` computed (`:536`) | **DIFF** |
| field-gate reject | ~550 (`:548`) | `proposed_t` computed | **DIFF** |
| per-site fetch failure | ~590 | site baseline unavailable | — (that site) |
| final `decide_org` `UNKNOWN` | `:574` / `:618` | `org_diffs` built in loop | **DIFF** |

### Org-NAC — `simulate_org_nac`

| Exit | `pipeline.py` | `before`/`after` state | Result |
|---|---|---|---|
| parse / `check_objects` | ~659 / ~662 | no resolved ops | — |
| `FetchError` (no baseline) | ~665 | no baseline rules | — |
| no-such-rule (update/delete) | ~687 (`:688`) | no `before` | — |
| already-exists (create) | ~691 (`:692`) | malformed create | — |
| set+delete conflict | ~702 (`:703`) | `effective` not yet computed | — |
| L0-fatal (**direct return**) | `:714` | `effective` computed (`:706`) | **DIFF** ← refinement #1 |
| field-gate reject | ~721 (`:719`) | `effective` computed | **DIFF** |
| final `decide` `UNKNOWN` | `:762` | `nac_diffs` built in loop | **DIFF** |

The two stages both colloquially called "apply rejection" are **different** and
resolve oppositely: the **site** `adapter.apply` runs *after* `effective` exists
(→ DIFF); the **org** `apply_template` *is* the step that computes `proposed_t`,
so its failure means there is no `after` (→ —).

## §2 Build the diff early (reuse the rolling state)

In each path, move the per-op `object_config_diff(...)` build to **immediately
after `effective` / `proposed_t` is computed and known-good**, *before* L0 and
`screen_op`, appending to the existing accumulator (`site_diffs` / `org_diffs` /
`nac_diffs`). The diff is pure structural data and does not depend on
validation, so building it earlier is safe and makes it available to the
L0-fatal / field-gate / apply-reject early exits.

- **Site:** build right after `:380` (`effective = effective_update(...)`),
  before the L0 validate at `:388`. (The existing build at `:411` is removed —
  one build per op, not two.)
- **Org:** in the loop body, build for the non-delete branch right after
  `proposed_t` is confirmed not a `Rejection` (after `:539`), and for the delete
  branch where `proposed = None` is set (after `:534`). (Replaces the single
  end-of-loop build at `:558`.)
- **NAC:** the delete build stays where it is (`:695`); for create/update, build
  right after `effective` is finalized (after the `effective["id"]` set at
  `:708`), before L0 at `:711`. (Replaces the build at `:722`.)

Exactly **one** `object_config_diff` per op. The rolling pre-op state the site
loop already maintains (`proposed_raw`, each op diffing against the post-prior-op
object) is the correct and only diff source — no separate pre-pass.

## §3 Thread accumulated diffs through every exit

Two return shapes exist, and they need different handling:

- **Fall-through returns** reach a path's *final* attach (site `:463`, org
  `:574`/`:618`, NAC `:762`). Making that attach unconditional is sufficient —
  no per-call-site threading needed. On the site path this covers **all** of
  `_simulate_site_state`'s internal `_unknown` exits (baseline/proposed ingest
  crash, etc.), because `simulate` wraps that function's result at `:463`.
- **Direct early returns** return *before* the final attach and therefore must
  carry the accumulator themselves.

1. **Drop the three suppression guards (the primary mechanism).** Replace each
   `if decision is not Decision.UNKNOWN … else ()` with an unconditional attach:
   site `:463-464` → always `replace(verdict, config_diffs=tuple(site_diffs))`;
   org `:574` and `:618` → `config_diffs=tuple(org_diffs)`; NAC `:762` →
   `tuple(nac_diffs)`. This alone fixes every fall-through `UNKNOWN`.

2. **`_unknown`** (site): add `config_diffs: tuple[ObjectConfigDiff, ...] = ()`
   (**default `()`** — it is shared with `_simulate_site_state`, whose exits carry
   no accumulator and are already covered by the unconditional `:463`). Implement
   as `return replace(assemble(...), config_diffs=config_diffs)` — `assemble`'s
   shared signature is untouched. Pass `tuple(site_diffs)` at **every direct early
   return inside the op loop** that bypasses `:463` — including the
   uncomputable-current-op exits **object-not-found `:353` and conflict `:367`**:
   the current op has no diff there (both precede the build at `:380`), but
   `site_diffs` already holds **prior** ops' diffs, which MUST survive — as well as
   the computable exits L0-fatal `:396`, field-gate `:405`, `adapter.apply` reject
   `:417`, and the post-loop below-profile ingest-crash exit `:451` (full
   `site_diffs`). Only the **pre-loop** exits (parse/fetch/state, before any op is
   processed) pass `()`. This matches §1: `—` means no diff for *that op*, never
   for earlier ones.

3. **`org_unknown`** (org): add a **required** `config_diffs` keyword (no default
   — this helper is narrow, always has `org_diffs` in scope, and always bypasses
   the final attach, so forcing explicitness is pure upside). Pass it into the
   `OrgVerdict(...)` it builds. Pass `tuple(org_diffs)` at **every in-loop exit**,
   including the uncomputable-current-op exits **template-lookup-failed `:526` and
   `apply_template` reject `:538`** (current op has no diff; **prior ops survive**)
   as well as the computable L0-fatal `:543` and field-gate `:550`. Only the
   **pre-loop** exits (`:494` / `:510` / `:512`) pass `()`.

4. **`_org_nac_unknown`** (NAC): add **two** keywords — a **required**
   `config_diffs` and `l0_fatal: bool = False` — and make `rej` optional. Build
   `decide(DecisionInputs(rejections=(rej,) if rej else (), l0_fatal=l0_fatal,
   …))` and `org_rejections=(rej,) if rej else ()`. **Route the direct L0-fatal
   branch at `:714-718` through this helper** with `l0_fatal=True`, `rej=None`,
   `config_diffs=tuple(nac_diffs)`, so *every* NAC `UNKNOWN` exit funnels through
   one place (refinement #1). Pass `tuple(nac_diffs)` at the other exits too
   (`:687`/`:691`/`:702` carry earlier ops' diffs, `()` for a single-op plan).
   The `FetchError` branch (`:665`) stays a direct diff-less return (no baseline;
   §1).

The genuine safety net against a re-introduced drop is the **per-exit test
matrix** (§6), not the parameter defaults: each computable exit on each path is
asserted to carry its diff.

## §4 Non-load-bearing invariant (unchanged, now explicitly tested)

`config_diffs` is never read by `decide` / `decide_org` / `decide_org_nac` today
(verified: no reference in `verdict/decision.py`, `verdict/org_verdict.py`,
`verdict/org_nac_verdict.py` outside the dataclass field). This spec preserves
that and adds an explicit guard test (§6): an `UNKNOWN` verdict and a non-empty
`config_diffs` coexist, proving population does not perturb the decision.

## §5 Security — no new exposure

`object_config_diff` (`config_diff.py`) diffs **all** leaves via
`leaf_changes(before, after, ignore_top=IGNORED_RAW_FIELDS)` and redacts **every**
value through `redact_leaf(d.path, …)` on the full path, by key-segment match.
This is independent of whether a leaf is in the field-gate allowlist, so the
now-surfaced out-of-scope leaves are masked by the *same* policy the in-scope
diffs already use. No code change in `config_diff.py` or `redaction.py`. Pinned
by a test (§6) that asserts an out-of-scope, secret-keyed leaf appears redacted.

## §6 Testing

**Per-path × per-exit (the safety net against a missed thread).** Drive each
path to each exit in §1 and assert the §1 result:

- *Site* (`simulate`): field-gate reject → diff present (the screenshot case);
  L0-fatal → present; `adapter.apply` reject → present; derived-gate `UNKNOWN`
  (final) → present; object-not-found → `()`; set+delete conflict → `()`.
- *Org* (`simulate_org_plan`): field-gate reject → present; L0-fatal → present;
  final `decide_org` `UNKNOWN` → present; `apply_template` reject → `()` for that
  op (earlier ops present); template-lookup-failed → `()`.
- *NAC* (`simulate_org_nac`): field-gate reject → present; **L0-fatal (direct
  return) → present** (refinement #1); final `decide` `UNKNOWN` → present;
  no-such-rule / already-exists / conflict / `FetchError` → `()`.

**Multi-op partial plan (computable later op).** A 2-op plan where op 2 fails the
field gate: assert both op 1's diff (passed) and op 2's diff (the rejected op,
`effective` computable) are present.

**Multi-op partial plan (uncomputable later op).** A 2-op plan where op 1 passes
and op 2 hits an *uncomputable* in-loop exit (site object-not-found `:353` or
conflict `:367`; org template-lookup-failed `:526`): assert op 1's diff
**survives** and op 2 contributes nothing. This pins the §3 rule that an in-loop
uncomputable exit still carries the accumulator — guarding against re-suppressing
earlier ops.

**Non-load-bearing invariant.** `decision == UNKNOWN` and `config_diffs`
non-empty coexist in the same verdict.

**Security.** An out-of-scope changed leaf with a secret-bearing key (e.g. a
`*_key` / `password` field on an unmodeled attribute) appears **redacted** in the
surfaced diff.

**Golden churn (expected, reviewed).** Existing goldens that assert
`config_diffs=()` on `UNKNOWN` will change. Updates are part of this change and
must be reviewed leaf-by-leaf — never blindly regenerated — to confirm each new
diff is correct and redacted.

## §7 Rendering (no change; optional header deferred)

The renderer already prints `verdict.config_diffs` unconditionally, so the data
appears on `UNKNOWN` verdicts with no change. If the owner later wants the UI to
signal these are *proposed, unvalidated* changes on an `UNKNOWN` verdict, a
one-line header in `_render_config_diffs` is a small additive follow-up — out of
scope here unless requested.

## Files touched (anchor map for the plan)

- `src/digital_twin/engine/pipeline.py` — build-early (§2) in all three paths;
  `_unknown` / `org_unknown` / `_org_nac_unknown` signatures + threading (§3);
  drop the three suppression guards (§3.4); route NAC L0-fatal through the helper.
- `src/digital_twin/config_diff.py`, `src/digital_twin/redaction.py` — **no
  change** (§5).
- `src/digital_twin/drivers/render.py` — **no change** (§7).
- Tests: site / org / NAC pipeline e2e suites + goldens (§6).
- `docs/ROADMAP.md` — record the feature and the superseded 2026-06-23 non-goal.
