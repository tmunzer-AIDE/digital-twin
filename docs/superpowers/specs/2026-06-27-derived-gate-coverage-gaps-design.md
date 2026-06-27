# Derived-gate coverage gaps: partial assessment instead of blanket UNKNOWN

**Status:** PROPOSED
**Date:** 2026-06-27
**Author:** brainstormed with the repo owner

Today an out-of-scope **effective** change makes the twin throw away *all* of its
modeled analysis and return a bare `UNKNOWN`. So deleting a template — the most
common "what will this break?" question — returns `UNKNOWN` even when the twin
can see perfectly well that it strands clients or removes a VLAN. This redesign
turns an out-of-scope effective change into a **non-fatal coverage gap**: the
modeled checks still run, and a confident modeled **UNSAFE rises above** the
coverage-gap UNKNOWN. It is the highest-leverage change available — it fixes
*every* partially-modeled change (deletes especially), not one object type.

## Problem

In `_simulate_site_state` (`engine/pipeline.py`), the derived gate runs **before**
the checks and **short-circuits**:

```python
with trace.stage("derived_gate"):
    rejection = check_derived(site_effective_base, site_effective_prop)
    if rejection:
        return _unknown(rejection, ...)        # ← STOPS; checks never run
    ... (per-device, per-gateway: same return _unknown) ...
with trace.stage("checks"):                    # ← unreachable on any out-of-scope leaf
    results = registry.run_all(...)
```

`check_derived` (`scope/derived_gate.py`) fails closed if **any** effective leaf
outside the allowlist differs. A template **delete** removes the template's whole
contribution, so unmodeled leaves (`radius_config`, `dns_servers`, `ntp_servers`,
`remote_syslog`, `additional_config_cmds`, `acl_policies`, `switch_matching`, …)
vanish from the effective config → the **first** such leaf trips the gate → the
op returns `UNKNOWN` and **no check ever runs**. The modeled impact (VLANs,
ports, clients the twin understands) is discarded.

Worse, even if a check *did* run and found breakage, it could not surface:
`decide()` precedence (`verdict/decision.py:3`) is **`UNKNOWN > UNSAFE`**, and
`decide()` returns `UNKNOWN` at step 1 on any `rejection` — before it inspects
UNSAFE findings at step 2. So a real breakage on a template delete is hidden
twice: never computed, and out-ranked.

## Goals

- **The derived gate stops short-circuiting.** An out-of-scope effective leaf
  change (site / device / gateway) becomes a **coverage gap** — a recorded
  signal, not an early `return`. The modeled checks **run** on the (valid)
  modeled IR.
- **A confident modeled UNSAFE outranks the coverage gap.** New `decide()`
  precedence (§3): **hard-UNKNOWN > UNSAFE > coverage-gap UNKNOWN > REVIEW >
  SAFE**. A coverage gap renders as `Decision.UNKNOWN` but sits *below* UNSAFE.
- **No-false-SAFE preserved.** A coverage gap can NEVER resolve SAFE; the modeled
  checks may only **escalate** (add UNSAFE/REVIEW), never certify SAFE over a gap.
- **The gap is visible.** Surface the unmodeled changed leaves as a finding/reason
  ("unmodeled effective config changed — not assessed: `radius_config`, …") so the
  operator sees exactly what the twin could not evaluate.
- **Result:** a template delete that strands clients/removes a depended-on VLAN →
  **UNSAFE** ("removes VLAN X / disconnects these clients", + "unmodeled config
  removed: … — unverified"). A delete that touches only inert unmodeled leaves →
  **UNKNOWN** (coverage gap) **with the modeled findings attached**, not a bare
  UNKNOWN.

## Non-goals (recorded, deferred)

- **Field gate (raw, pre-IR).** An out-of-scope *raw changed path* on an update
  still hard-UNKNOWNs. It fires before the IR is built, so "run the modeled
  checks anyway" is a separate, harder question. (Template deletes don't hit it —
  delete skips L0/field-gate — so this redesign already covers the reported pain.)
- *(In scope, was deferred)* `device_profile_rejection` is now folded in as a
  coverage gap (§1/§2) — otherwise profiled switch/gateway template deletes would
  still mask a modeled UNSAFE behind a bare UNKNOWN.
- **Modeling the unmodeled leaves themselves** (radius/dns/acl/switch_matching…).
  That's a scope-expansion effort; this spec makes their *removal* honestly
  reported, not modeled.

## §1 Two kinds of UNKNOWN

The redesign hinges on splitting today's single "UNKNOWN bucket" in two:

- **Hard-UNKNOWN — no trustworthy simulation exists.** L0-fatal payload, baseline
  fetch failure / ingest crash, and pre-simulation gate rejections (parse, scope,
  object-gate, apply, field-gate). The IR is absent or untrustworthy, so *no*
  finding it produced can be believed. **UNKNOWN dominates everything** (a
  computed UNSAFE here would be garbage). Unchanged from today.
- **Coverage-gap UNKNOWN — valid-enough modeled IR, partial blind spot.** Two
  gates produce this:
  - **derived gate** — an out-of-scope *effective* leaf changed; the modeled IR is
    correct for the leaves it models, only the unmodeled leaves are unseen.
  - **device-profile gate** (`device_profile_rejection`, already self-described as
    a "per-site coverage gate") — a profiled device's modeled leaves changed below
    an **unmodeled, higher-precedence device-profile layer** that could override
    them. The change may not take effect.

  In both, the modeled checks ran on the modeled IR and their UNSAFE is real (or,
  for the profile case where the profile actually overrides, a conservative
  *over-warn* — acceptable, since the doctrine forbids only false-SAFE). So a
  modeled UNSAFE **outranks** the gap, and a clean run is **floored to UNKNOWN**
  (never SAFE) while a gap is present.

## §2 The coverage gates become non-fatal

All feed the new `coverage_gaps` channel in `_simulate_site_state`; the pipeline's
*consumption* changes from fatal short-circuit to accumulation.

- **Derived gate (`check_derived`)** — replace the three
  `if rejection: return _unknown(...)` short-circuits with **accumulation**: run
  for site / each device / each gateway, collect every returned `Rejection` into
  `coverage_gaps`, do **not** return early, then run the checks. `check_derived`
  returns **two reason shapes, and BOTH become coverage gaps** (P2a decision):
  - *out-of-scope effective leaf gaps* — `stage="derived_gate"`, `reasons` = the
    offending leaf paths;
  - *DHCP semantic transitions* (`dhcp_row_rejection`) — `stage` ∈
    {`dhcp_mode_transition`, `dhcp_relay_target`, `dhcp_inert_servers`,
    `dhcp_scope_field`}, `reasons` = the semantic description. These are "valid IR,
    a DHCP transition the dhcp model can't fully assess" — the same coverage-gap
    class, so they accumulate too (a modeled UNSAFE still outranks; a clean run is
    still floored to UNKNOWN). `check_derived` is otherwise unchanged.
- **Device-profile gate (`device_profile_rejection`)** — its checks **already run**
  before `dp_rej` is computed (pipeline.py: `registry.run_all(...)` precedes it),
  so the verdict-side change is just **routing** (`dp_rej` → `coverage_gaps`, not
  `DecisionInputs.rejections`). One small **gate** change is also needed for
  evidence (P2b): today the rejection names only the device id; extend its
  `reasons` to **list the changed overridable leaves** it already computes
  (`[p for p in changed if allowed(p, patterns)]`), so the coverage-gap finding
  carries leaf evidence like the others.

Then `assemble` is called once with `coverage_gaps` + `check_results`.

## §3 `decide()` precedence change

Add `coverage_gaps: tuple[Rejection, ...] = ()` to `DecisionInputs`. New order
(first match wins):

1. **Hard-UNKNOWN** — `rejections` (pre-sim gates only: parse / scope / object-gate
   / apply / field-gate), `l0_fatal`, `baseline_unavailable` → `UNKNOWN`. (Today's
   step 1, minus the post-sim gates: ALL `check_derived` rejections — derived-gate
   leaf-gaps AND the `dhcp_*` stages — plus `dp_rej` now leave `rejections` and
   flow via `coverage_gaps`.)
2. **UNSAFE** — NETWORK ERROR/CRITICAL findings (now reachable under a gap).
3. **Coverage-gap UNKNOWN** — if `coverage_gaps` is non-empty → `UNKNOWN`, reasons
   list the unmodeled changed leaves. Sits below UNSAFE, above REVIEW/SAFE — so a
   gap can never be SAFE and never masks a real UNSAFE.
4. **REVIEW** — warnings / blind spots (today's step 3).
5. **SAFE** (today's step 4).

The module docstring's precedence line updates to:
`hard-UNKNOWN > UNSAFE > coverage-gap UNKNOWN > REVIEW > SAFE`.

### §3b Org rollup precedence (`decide_org`) — the org-level half

The reported case (gateway/network **template** deletes) is org-scoped, so the
per-site fix alone is not enough: `decide_org` rolls up per-site decisions via
`_PRECEDENCE = {SAFE:0, REVIEW:1, UNSAFE:2, UNKNOWN:3}` and takes the `max` — so
today a per-site `UNKNOWN` **outranks** a per-site `UNSAFE`. Change the rollup to
put **UNSAFE on top**: `_PRECEDENCE = {SAFE:0, REVIEW:1, UNKNOWN:2, UNSAFE:3}`.

**Why this is the correct asymmetry** (and still no-false-SAFE):
- *Per-site* `decide()` keeps **hard-UNKNOWN > UNSAFE**, because within one site a
  hard-UNKNOWN means the sim was untrustworthy, so a computed UNSAFE there would
  be garbage. (In practice the two never coexist: hard-UNKNOWN short-circuits
  before checks run.)
- *Across sites* `decide_org` uses **UNSAFE > UNKNOWN**, because a per-site
  `UNSAFE` is only ever produced at `decide()` step 2 — i.e. **after** that site
  passed its own hard-UNKNOWN gate. So a per-site UNSAFE always means "a *valid*
  site found a real breakage." That breakage is real regardless of another site
  being unknown, so the org headline should be UNSAFE, with the unknown site(s)
  still listed in the per-site breakdown.

For the single-site template delete in the screenshot this is what flips it from
`UNKNOWN` to `UNSAFE` once the one site computes a breakage; multi-site deletes
get the same correct headline.

## §4 Coverage-gap finding shape

Each accumulated coverage-gap `Rejection` becomes one **OPERATIONAL** finding
(category `OPERATIONAL`, so it never itself drives UNSAFE), code
`derived.coverage_gap`, severity `WARNING`, `confidence=HIGH`. Its message is
built from the rejection's own `stage` + `reasons` — which is **source-specific by
design**, so the operator always sees *what* couldn't be assessed:
- derived-gate leaf gap → the out-of-scope effective leaf paths;
- `dhcp_*` → the DHCP transition description (mode/relay-target/inert);
- device-profile → the changed overridable leaves (after the §2 P2b reason
  enhancement) + the device id.

The coverage-gap UNKNOWN's verdict reasons reuse those same strings. So the §
visibility promise is "list whatever the rejection names," not "always leaf
paths" — the three sources legitimately differ.

## §5 No-false-SAFE (why this is safe)

- A coverage gap **floors at UNKNOWN** (§3 step 3) — it can never reach SAFE.
- Raising UNSAFE above the gap can only make a verdict **more** conservative,
  never less — it cannot introduce a false-SAFE (the doctrine forbids false-SAFE,
  not false-UNSAFE).
- The modeled checks run on the genuinely-modeled IR; the unmodeled leaves they
  don't see are exactly the ones reported as the gap. So nothing the checks
  certify SAFE is contradicted by an unseen leaf *that the verdict claims is
  fine* — the verdict is UNKNOWN-or-worse whenever a gap exists.

## §6 Testing

- **Template delete, modeled breakage** (e.g. networktemplate delete removes a
  VLAN a client depends on, or strands a switch) → **UNSAFE**, with the modeled
  finding AND a `derived.coverage_gap` note for the removed unmodeled leaves.
- **Template delete, only inert unmodeled leaves removed** (dns/ntp/syslog) →
  **UNKNOWN** (coverage gap) with any modeled findings attached — not a bare
  UNKNOWN, and never SAFE.
- **Update touching one out-of-scope effective leaf + a modeled UNSAFE** → UNSAFE
  (precedence: UNSAFE over coverage-gap).
- **Device-profile coverage gap**: a profiled switch/gateway template delete/edit
  whose modeled change strands a client → **UNSAFE** (over the device-profile
  gap); one with no modeled breakage → **UNKNOWN** (coverage gap) with findings,
  never SAFE. The existing golden at `tests/golden/test_golden_scenarios.py:1403`
  (profiled gateway-template edit → bare `UNKNOWN`) flips to coverage-gap-UNKNOWN-
  with-findings (or UNSAFE) — update it deliberately, leaf-by-leaf. Assert the
  device-profile `coverage_gap` finding **names the changed overridable leaves**
  (P2b), not just the device id.
- **DHCP transition coverage gap** (P2a): a `dhcp_mode_transition` /
  `dhcp_relay_target` effective change with no modeled breakage → **UNKNOWN**
  (coverage gap), finding carries the DHCP semantic reason; the same change
  alongside a modeled NETWORK ERROR → **UNSAFE** (gap does not mask it). An inert
  DHCP row change (`dhcp_inert_servers`/`dhcp_scope_field`) → coverage-gap UNKNOWN,
  never SAFE.
- **Hard-UNKNOWN unchanged**: L0-fatal, baseline-unavailable, parse/object-gate
  rejection, field-gate rejection → still `UNKNOWN`, dominating any finding.
- **decide() unit tests**: the new 5-tier precedence, each tier; coverage-gap with
  no other findings → UNKNOWN; coverage-gap + NETWORK ERROR → UNSAFE; coverage-gap
  + only WARNING → UNKNOWN (gap outranks REVIEW).
- **decide_org() unit tests** (§3b): single per-site UNSAFE → org UNSAFE; per-site
  {UNSAFE, UNKNOWN} → org **UNSAFE** (was UNKNOWN); per-site all-UNKNOWN → org
  UNKNOWN; per-site {REVIEW, UNKNOWN} → org UNKNOWN (UNKNOWN still over REVIEW).
- **derived gate** still detects the same out-of-scope leaves (its detection is
  unchanged); only the pipeline's consumption is now non-fatal.
- goldens: template-delete goldens flip from UNKNOWN-bare to UNSAFE/UNKNOWN-with-
  findings — reviewed leaf-by-leaf, not blind-regenerated.
- `docs/ROADMAP.md`.

## Files touched (anchor map for the plan)

- `src/digital_twin/verdict/decision.py` — `DecisionInputs.coverage_gaps`; the
  5-tier precedence; docstring.
- `src/digital_twin/verdict/org_verdict.py` — `_PRECEDENCE` so UNSAFE outranks
  UNKNOWN at the org rollup (§3b); docstring.
- `src/digital_twin/engine/pipeline.py` — `_simulate_site_state`: accumulate
  derived-gate rejections into `coverage_gaps` instead of `return _unknown`;
  **route `dp_rej` into `coverage_gaps` not `rejections`**; pass into
  `assemble`/`DecisionInputs`; emit the `derived.coverage_gap` finding.
- `src/digital_twin/scope/device_profile_gate.py` — small change (P2b): extend the
  rejection `reasons` to list the changed overridable leaves it already computes
  (detection logic itself stays).
- `src/digital_twin/verdict/verdict.py` — `assemble` threads `coverage_gaps` (if
  it doesn't already pass `DecisionInputs` straight through).
- `src/digital_twin/scope/derived_gate.py` — unchanged (detection stays); note it
  returns BOTH `stage="derived_gate"` leaf-gap rejections and `dhcp_*` semantic
  rejections (via `dhcp_row_rejection`) — all consumed as coverage gaps.
- Tests: `tests/verdict/test_decision*.py`, `tests/engine/test_pipeline.py`,
  `tests/engine/test_org_plan.py` (template-delete e2e), goldens.
- `docs/ROADMAP.md`.

## Relationship to the WLAN work

This **supersedes the gating half** of the SP1 WLAN spec
(`2026-06-27-site-wlan-coverage-loss-design.md`): SP1's "fail-closed" instinct is
this model, generalized. SP1's *wireless client-impact check* (Client.ssid +
coverage-loss logic) remains its own modeled check and a **consumer** of this
precedence — it emits NETWORK/ERROR (UNSAFE) findings that now ride above any
coverage gap. Recommended sequencing: **this redesign first** (it's the core
fix and unblocks template deletes immediately), then SP1/SP2/SP3 on top.
