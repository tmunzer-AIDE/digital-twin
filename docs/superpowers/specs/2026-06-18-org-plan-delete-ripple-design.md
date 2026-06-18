# Org-plan DELETE-ripple + multiple templates per plan

**Status:** design — pending user review
**Date:** 2026-06-18
**Author:** brainstormed with the repo owner

## Problem

Today a template **delete** (and any non-`update` action) is rejected pre-fetch by
`object_gate` → UNKNOWN (fails safe, test-pinned), and ORG mode allows **exactly one**
org op per plan. So the twin cannot answer two real questions:

1. *"What happens if I delete this networktemplate / gatewaytemplate / sitetemplate?"*
   — the assigned sites lose that inherited config layer (Mist allows the delete and the
   sites' effective config collapses to just their own `site_setting` + remaining layers).
2. *"What happens if my plan changes/deletes several org templates at once?"* — when a
   site is assigned to more than one of the changed templates, the combined effect must be
   simulated atomically, not as independent per-op runs.

Modify-ripple (single update) is DONE for all three typed templates (the prior
gatewaytemplate/sitetemplate work). This adds **delete** as a new op state and **multiple
org ops per plan** applied **atomically per affected site**.

**Confirmed with the repo owner (domain facts):**
- Deleting a template still assigned to sites **succeeds** in Mist; the assigned sites
  lose that layer (config collapse). It is NOT blocked, and it is NOT a dangling-reference
  fetch-miss. Baseline = template present, proposed = **layer absent**.
- A template op cannot change site→template *assignment* (assignment lives on the **site**
  object's `<type>_id` field, not in the template), so for these ops the affected sites are
  the baseline-assigned sites.

## Scope (MVP)

In: **DELETE-ripple** for the three typed templates (networktemplate / gatewaytemplate /
sitetemplate) + **multiple org ops per plan**, combined per affected site.

Out (separate future cycles, per the ROADMAP cluster): org_networks and WLAN/RF templates
(not modeled as compile layers yet — a gateway-sized gap); site-level reassignment ops;
the apply (write) path.

## Core model — Approach A: org overlays

An org plan is a set of changes to org-shared layers. We resolve each change once, take the
**union of affected sites**, and simulate each affected site **once** with **all applicable
overlays applied together**. This is the only model that preserves the combined effect of
multi-op deletes/updates (independent per-op fan-out cannot — a site assigned to two deleted
templates would get two separate verdicts and never show the simultaneous collapse).

### `OrgOverlay` and `OrgChange` (frozen dataclasses, not tuples)

```python
@dataclass(frozen=True)
class OrgOverlay:
    object_type: str                          # networktemplate | gatewaytemplate | sitetemplate
    object_id: str
    name: str | None                          # for verdict / rendering
    action: Literal["update", "delete"]       # explicit, even though proposed is None ⇔ delete
    assigned_site_ids: frozenset[str]          # canonical, from resolve_org_template — the overlay filter
    baseline: Mapping[str, Any]                # the resolved current template (snapshot)
    proposed: Mapping[str, Any] | None         # edited snapshot (update) — or None == REMOVED (layer absent)


@dataclass(frozen=True)
class OrgChange:
    ref: ObjectRef                             # kind=object_type, id, name
    action: Literal["update", "delete"]
```

**`proposed is None` is the REMOVED state and means *layer absent*** — the pin sets the
`RawSiteState.<type>` field to `None`, which the compile already folds as "no layer." It is
deliberately distinct from `{}` (an empty-but-present template). This keeps delete behavior
honest and lets the existing compile collapse do the real work.

**`assigned_site_ids` lives on the overlay** because the per-site filter depends on this
canonical resolver output, not on re-reading the raw `site.<type>_id` field (avoids a
naming-mismatch trap).

## `object_gate` relaxation

In ORG mode (every op is an `ORG_OBJECT_TYPE` and `scope.site_id` is absent):
- **Allow `delete` and `update`** — but only for the three org template types. Site/device
  `delete` (and any other non-`update`) stays rejected, unchanged.
- **Delete payload must be empty.** `ChangeOp.payload` is a required `Mapping` and the
  envelope stays action-agnostic (its contract keeps action semantics in `object_gate`), so
  a delete op carries `"payload": {}`. A `delete` op whose payload is **non-empty** is
  **rejected loudly** by `object_gate` — a delete has no proposed object (it skips
  apply/L0/field-gate), so a payload is meaningless and must never be silently ignored. The
  empty `{}` is the canonical delete shape; an update keeps its full-object payload.
- **Allow multiple org ops** — lift the current exactly-one-op rule (which was itself a
  prior review fix; it is now superseded by the dedup rule below).
- **Reject duplicate `(object_type, object_id)`** ops (MVP — two ops on the same template is
  an authoring ambiguity → loud rejection).
- Mixed delete + update of *different* templates in one plan is allowed.
- The SITE-mode branch and all its existing per-op diagnostics are unchanged.

## The fan-out — `simulate_org_plan`

Generalizes `simulate_org_template` (which becomes a thin single-op alias):

1. **Resolve each op → `OrgOverlay`** (before any fan-out). `resolve_org_template(scope,
   object_id, object_type)` → `baseline` snapshot + `assigned_site_ids`.
   - **delete** → `proposed=None`; **no L0, no field-gate, no apply** on a proposed object
     (there is none) — but baseline object resolution is still REQUIRED (we need the
     baseline snapshot + the assigned sites).
   - **update** → `proposed = apply_template(baseline, payload)`, and **L0 + field-gate run
     on the edited proposed snapshot** exactly as today; a fatal L0 short-circuits to
     org-level UNKNOWN.
   - **Any resolve failure → org-level UNKNOWN BEFORE fan-out** (you cannot reliably know
     the affected sites for that op, so the whole org run is UNKNOWN, as today).
2. **Affected sites = union of each overlay's baseline `assigned_site_ids`.** Computed by a
   helper `affected_sites(overlays)` so a future site-reassignment op can feed a
   baseline∪proposed union; for MVP it is the baseline union.
3. **Per affected site**, fetch its raw, then build `baseline_raw` / `proposed_raw` by
   pinning **every overlay whose `assigned_site_ids` contains this site_id** onto its layer
   slot: baseline pins `overlay.baseline`, proposed pins `overlay.proposed` (`None` for a
   delete). A site **not** assigned to a given overlay does **not** get that overlay pinned,
   even if it is in the affected union because of another op. Untouched layers keep the
   site's fetched copy (the fetch-race guard, as in today's `override_template`).
4. **Overlays apply by layer precedence, not op order.** Each overlay targets a distinct
   layer field (`networktemplate` / `gatewaytemplate` / `sitetemplate`), and a site is
   assigned to at most one template per layer, so there is exactly ≤1 overlay per slot per
   site → no ordering ambiguity. Then the existing `_simulate_site_state(baseline_raw,
   proposed_raw)` runs once per site → per-site verdict → roll-up.
5. **Unfetchable affected site** → that one site is a `FetchError`/UNKNOWN recorded in
   `site_failures`; the org run continues (does not fail the whole simulation).

## `OrgVerdict` — multi-object-native

- Replace the single `template_id` with `changes: tuple[OrgChange, ...]` — names every org
  object the plan touches, each with its action.
- `decide_org` rollup unchanged in spirit: the org decision is the precedence-max over the
  per-site verdicts (UNKNOWN > UNSAFE > REVIEW > SAFE), `driving_sites` / `site_failures` /
  `per_site` retained.
- `template_findings` (non-fatal L0 on a proposed) → a flat tuple across the **update** ops,
  each already stamped with its object subject; deletes contribute none.
- **0-site delete:** SAFE, but the verdict still includes the `changes` entry and a
  decision reason like `"<object_type> <id>: no assigned sites — nothing ripples"`, so it is
  auditable (not a silent empty SAFE).
- **Back-compat:** today's single-op update simulation is just a 1-op org plan;
  `simulate_org_template` is kept as a thin alias over `simulate_org_plan`. CLI/MCP dispatch
  and `render_org_human` / `org_verdict_to_dict` are updated to show the **set** of changed
  objects + actions. The existing single-update output is equivalent **except** for the
  intentional `template_id → changes` shape change.

## Edge cases (all honest, never silent)

- **0-site delete** → SAFE + auditable `changes`/reason (above).
- **Resolve failure** for any op → org-level UNKNOWN before fan-out.
- **Unfetchable affected site** → per-site FetchError/UNKNOWN in `site_failures`; org run
  continues.
- **Site in the affected union but not assigned to an overlay** → that overlay not pinned.
- **A deleted layer that contributed nothing** to a particular site (fully overridden at
  `site_setting`) → ~no diff → SAFE for that site. Honest.
- **The collapse is deliberately dramatic where real:** deleting a networktemplate that
  defines the VLANs strands them across every assigned site → segmentation / blackhole /
  client_impact fire → UNSAFE naming the sites.
- **Duplicate `(object_type, object_id)`** → loud `object_gate` rejection (MVP).

## Testing

- **The motivating golden (the proof A does what B cannot):** one site assigned to TWO
  templates; two org ops where *each alone* is harmless/incomplete but *together* collapses
  effective config into a finding. Assert the per-site finding appears ONLY when both
  overlays are applied, and the org verdict's `changes` names BOTH objects. (A
  one-overlay-at-a-time control must NOT produce the finding.)
- **Goldens:** a single delete per template type → collapse UNSAFE naming the affected
  sites; a 0-site delete → SAFE with the auditable `changes`/reason; a mixed delete+update
  in one plan → combined per-site verdict.
- **Unit:** `object_gate` (delete allowed for org types; multiple ops allowed; duplicate
  `(type,id)` rejected; site/device delete still rejected; mixed delete+update ok; a
  `delete` op with a **non-empty payload → rejected**, with `"payload": {}` accepted); the
  `affected_sites` union helper; per-site overlay pinning (only assigned overlays applied,
  unfetchable site → per-site failure); `OrgOverlay`/`OrgChange` construction;
  `OrgVerdict.changes`.
- **Equivalence:** the `simulate_org_template` single-update alias produces output
  equivalent to today's for the existing path, **except** the `template_id → changes` shape
  change (pin this explicitly so the migration is provably non-regressive).
- **Live (read-only / simulate-only):** simulate a real template's delete against the demo
  org → confirm the honest collapse verdict (UNSAFE naming the affected sites); re-run the
  8 single-site plans → verdicts unchanged. `.env` MUST NOT be committed; runs are
  read-only.
- Gate unchanged: `uv run pytest tests -q && uv run ruff check . && uv run mypy src`.

## Resolved decisions (from brainstorming)

- **Approach A (org overlays)** over B (independent per-op — under-reports interactions,
  ruled out by the combined-per-site requirement) and C (synthetic "delete every attribute"
  update — muddies delete semantics, does not generalize to atomic multi-op).
- **`proposed=None` ⇔ layer absent**, distinct from `{}` (empty-but-present).
- **`OrgOverlay` and `OrgChange` are frozen dataclasses**, not tuples;
  `OrgOverlay.action` is explicit; `OrgOverlay.assigned_site_ids` carries the canonical
  resolver assignment used by the per-site filter.
- **Affected sites = baseline assignment** (template ops can't change assignment); the
  `affected_sites` helper is structured to allow a baseline∪proposed union later.
- **Overlays apply by layer precedence, not op order** (≤1 overlay per layer per site).
- **Deletes skip L0/field-gate/apply** but still require baseline resolution; updates keep
  them.
- **0-site delete is SAFE + auditable**; resolve failure is org-UNKNOWN before fan-out;
  unfetchable affected site is a per-site failure, not a whole-run failure.
- **MVP rejects duplicate `(object_type, object_id)`** ops.
- **Delete payload must be empty** (`{}`): the envelope stays action-agnostic (payload
  always a required `Mapping`), and `object_gate` rejects a non-empty delete payload loudly
  — a delete has no proposed object, so a payload would be silently ignored otherwise
  (review P2).

## Out of scope (recorded, deferred)

- org_networks and WLAN/RF template deletes/changes (need new IR/compile surface).
- Site-level reassignment ops (would feed the proposed side of `affected_sites`).
- The apply (write) path.
