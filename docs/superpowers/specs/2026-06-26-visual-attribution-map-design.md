# Visual Attribution Map — Design

**Status:** Approved (design); plan pending
**Date:** 2026-06-26
**Author:** Thomas Munzer (with Claude)

## Problem

In the mistmcp config-change approval UI, the topology view paints **every** node
amber ("warning") and the findings list is wall-to-wall `warning:`. A single
real delta (one disabled uplink port, `mge-0/0/0`) cascades warning-severity
findings across the whole L2 domain, and VLAN views that the change does **not**
touch also render amber. The severity color stops discriminating anything, so an
operator cannot tell **where the change is** from **what is merely caught behind
it**.

This was initially scoped as a severity-model problem. It is not. Severity is
the verdict gate (`decision.py`) and must stay conservative; the missing thing is
a **visual attribution layer** — a way for renderers to distinguish the cut/origin
from the blast radius, scoped to the view being drawn.

## Root cause (verified)

Two independent mechanisms combine:

1. **No origin/blast-radius distinction in what gets painted.**
   `viz/highlight.py` paints *anything localizable* in a finding — `subject`,
   selected `evidence` keys (`fragment_nodes`, `component_nodes`, `baseline_root`,
   …), and `affected_entities` — with the finding's severity. The single
   exception is `caused_by`, which is rendered as caption text only
   (`highlight.py:134`, "cause != blast radius"). So the device where the change
   happened looks identical to every device transitively cut off behind it.
   When a high uplink is severed, the legitimate blast radius is the whole
   downstream subtree, and all of it paints the same amber.

2. **VLAN scope bleed (confirmed bug).** `viz/mermaid.py:124` renders each
   per-VLAN diagram by reusing the **global** `hl.nodes` set
   (`_class_lines(ids, hl.nodes)`). A node hit by a VLAN-10 finding therefore
   appears amber on VLAN-20's chart whenever that node also exists in VLAN 20's
   graph — even though VLAN 20 is untouched. The chart's own `severity`
   (`mermaid.py:129`) inherits the bleed.

The checks themselves are **correct**: they accurately report which subtree/VLAN
loses service. We do not change them.

## Design

A new verdict-level **`VisualMap`** keyed by `(view, entity)`. It is the single
source of truth for highlighting: our Mermaid renderer draws from it, and mistmcp
consumes the serialized form directly — no consumer-side inference over
`subject`/`evidence`/`affected_entities`/`caused_by`.

### Contract

```
VisualMap = { view_id -> { entity_key -> VisualEntry } }

VisualEntry:
  tier:     "origin" | "affected"          # v1 — see Deferred for primary/secondary
  severity: "info" | "warning" | "error" | "critical"
  findings: tuple[str, ...]                # finding codes touching this (view, entity)
```

- Added as `Verdict.visual_map: VisualMap`. Serializes through
  `render.verdict_to_dict`'s existing `_plain` walk with no special-casing.
- Purely additive and **presentational**. `decision.py` never reads it.

### View vocabulary = existing `Diagram.view` ids

`l2`, `vlan:<vid>`, `l3_exits`. Reusing the exact ids the diagrams already emit
(not a parallel set) keeps the map and the rendered diagrams aligned 1:1, so
mistmcp can correlate a map entry to the chart it annotates.

### Entity keys

`device:<node>`, `vlan:<vid>`, `port:<id>`, `link:<id>`, using the same
`_mac` / `node_for` VC-folding normalization already in `highlight.py`.

### Scoping rule (the bleed fix, made generic)

For each finding, derive:
- its **referenced VLANs** — `subject` (kind `vlan`), `evidence["vlan"]`,
  `evidence["affected_vlans"]`, `evidence["impacts"][].vlan`;
- its **referenced nodes/ports/links** — `subject` (device/port/link),
  `affected_entities`, and the node/port/link evidence keys
  (`component_nodes`, `fragment_nodes`, `new_member_ports`, `link`, …).

Then project the finding onto views:

- **`l2`** ← every referenced node/port/link. (The L2 topology is the global
  physical view.)
- **`vlan:<vid>`**, for each referenced `vid` ← the referenced nodes **that exist
  in that VLAN's graph**, plus the `vlan:<vid>` box entry. A finding with **no**
  VLAN reference projects onto **no** VLAN view.
- **`l3_exits`** ← referenced routed VLANs and the interfaces owned by hit nodes
  (mirrors the current `_l3_exits_diagram` mapping).

Consequence: a blackhole-on-VLAN-10 finding reaches `vlan:10` only — it cannot
appear on `vlan:20` because it never references VLAN 20. **The bleed is
structurally impossible**, not filtered after the fact.

#### Physical severance is `l2`-only (v1 decision)

`l2.isolation.severed` carries **no VLAN** — it is an L2-topology fact — so under
the generic rule it projects onto `l2` only. Its per-VLAN consequences are
already represented on the VLAN views by the VLAN-scoped checks the severance
causes (`blackhole`, `vlan_segmentation`, `client.impact`). We deliberately do
**not** synthesize "every VLAN carried across this uplink" from a severance:
that would re-broaden under a new name. If a physical severance is the *only*
finding and no per-VLAN check fires, that is acceptable for v1 — the operator
sees the cut on `l2`; VLAN scope is not synthesized from it until the
primary/secondary fast-follow.

**Invariant:** a finding appears in a VLAN view **iff** the finding itself is
VLAN-scoped (carries an explicit VLAN reference).

### Reconciliation

Within a single `(view, entity)`:

- **tier** by precedence: `origin > affected`. The most-foreground role wins
  (the changed device is `origin` even if it also sits inside an affected
  fragment).
- **severity** = worst-wins (`INFO < WARNING < ERROR < CRITICAL`), computed
  **independently** of tier. The two axes never interfere: tier answers "how
  central to the change," severity answers "how bad."
- **findings** = the union of codes that produced this entry (for tooltips /
  the findings list).

### Origin derivation

`origin` entities come from each finding's `caused_by` refs (the changed
entity), normalized to graph nodes/ports. An origin **inherits the view-set of
the finding it caused**, so on `vlan:10` the operator sees `origin s1 → affected
s2`, and on `l2` the origin device is distinct from the affected sea.

### Doctrine: cause is still not blast radius

`caused_by` remains semantically "cause, not blast radius." We change only its
**presentation** — from caption-only to painted as the `origin` tier, visually
distinct from `affected`. This is a deliberate, documented reversal of the
"never highlight cause" stance at `highlight.py:134`, justified because operators
need to see where the change originates. mistmcp must render `origin` distinctly
from `affected` (e.g. a different border style / accent), not merely a different
severity color.

### Mechanism: one builder, not two

`highlight.py` is refactored into the per-view `VisualMap` builder. Mermaid's
`_class_lines` (and the per-diagram `severity` computation) render from the
scoped map instead of the global `hl.nodes`, which fixes the bleed in our own
output as a side effect. There is **no** throwaway Mermaid-only patch — the map
is the single mechanism (the user's "fold the bleed into the map" choice).

## Worked example — one disabled uplink (`mge-0/0/0`)

Delta: disable the uplink port on `s1` that carries VLANs 10 and 20 toward the
L3 exit. `s2`/`s3` sit behind `s1`. VLAN 30 lives only on `s4`, nowhere near the
cut.

Findings (illustrative, matching current emitters):
- `l2.isolation.severed` — fragment `{s1,s2,s3}`, caused_by port `s1:mge-0/0/0`
- `l2.blackhole.exit_lost` — VLAN 10, component `{s1,s2,s3}`
- `l2.blackhole.exit_lost` — VLAN 20, component `{s1,s2,s3}`
- `wired.client.impact.active_clients` — clients on VLANs 10/20

Resulting `VisualMap`:

| view | entity | tier | severity | from |
|------|--------|------|----------|------|
| `l2` | `port:s1:mge-0/0/0` | origin | warning | isolation.caused_by |
| `l2` | `device:s1` | origin | warning | isolation.caused_by |
| `l2` | `device:s2` | affected | warning | isolation, blackhole×2 |
| `l2` | `device:s3` | affected | warning | isolation, blackhole×2 |
| `vlan:10` | `device:s1` | origin | warning | blackhole-10.caused_by |
| `vlan:10` | `device:s2` | affected | warning | blackhole-10 |
| `vlan:10` | `device:s3` | affected | warning | blackhole-10 |
| `vlan:10` | `vlan:10` | affected | warning | blackhole-10 |
| `vlan:20` | `device:s1` | origin | warning | blackhole-20.caused_by |
| `vlan:20` | `device:s2` | affected | warning | blackhole-20 |
| `vlan:20` | `device:s3` | affected | warning | blackhole-20 |
| `vlan:20` | `vlan:20` | affected | warning | blackhole-20 |

What changed for the operator:
- **VLAN 30's view has no entries** — `s4` is no longer painted, because no
  VLAN-30-scoped finding exists. (Today it bleeds amber via the global node set.)
- On every view, **`s1` is `origin`**, visually distinct from the `s2`/`s3`
  `affected` sea — the operator immediately sees the cut.
- The physical severance contributes to `l2` only; the VLAN views carry the
  per-VLAN blackhole consequences. No double-painting.

## Verdict impact

**None.** `decision.py` does not read `visual_map`; SAFE / REVIEW / UNSAFE /
UNKNOWN are unchanged for every delta. Locked by an explicit invariance test
(below). This is the entire reason the change is safe to ship without verdict
sign-off.

## Files touched

- **`src/digital_twin/contracts/`** — new `VisualMap` / `VisualEntry` / tier
  enum (or typed dict); export from the contracts package.
- **`src/digital_twin/viz/highlight.py`** — refactor `build_highlight` into the
  per-`(view, entity)` `VisualMap` builder: per-finding view projection +
  scoping rule + tier/severity reconciliation + origin-from-`caused_by`.
- **`src/digital_twin/viz/mermaid.py`** — render node/VLAN classes and per-chart
  `severity` from the scoped map for the chart's `view` id; remove the global
  `hl.nodes` reuse that caused the bleed. Origin vs affected distinguishable in
  the rendered class (e.g. a dedicated `origin` classDef).
- **`src/digital_twin/verdict/verdict.py`** — add `visual_map` to `Verdict`;
  populate it where diagrams/highlights are currently built.
- **`src/digital_twin/drivers/render.py`** — ensure `visual_map` is serialized
  (verify the `_plain` walk; add a typed projection only if needed). Optionally
  surface tier in `render_human` finding lines.
- **`docs/ROADMAP.md`** — record the feature and the deferred primary/secondary
  fast-follow.

## Test plan

- **Bleed regression** — a delta touching VLAN 10 only: assert `vlan:20` and an
  untouched `vlan:N` have **no** entry for nodes that exist in their graphs but
  were hit only by the VLAN-10 finding. This is the headline fix.
- **Scoping invariant** — a finding with no VLAN reference (`isolation.severed`)
  produces entries under `l2` only, none under any `vlan:*`.
- **Tier reconciliation** — an entity that is both `caused_by` (origin) and
  inside an affected fragment resolves to `origin`; severity still worst-wins
  independently.
- **Origin presentation** — `caused_by` entities appear as `origin` entries (a
  behavior change vs today's caption-only), distinct from `affected`.
- **Severity orthogonality** — two findings of different severity on the same
  `(view, entity)`: tier unchanged, severity = worst.
- **Verdict invariance** — for a representative set of goldens, building
  `visual_map` does not alter `decision` or any finding `severity` (compare
  verdict with/without the map populated).
- **Serialization** — `verdict_to_dict` round-trips `visual_map` to the nested
  `{view: {entity: {tier, severity, findings}}}` shape mistmcp expects.

## v1 scope and deferred work

**v1 ships:** `VisualMap` keyed by `(view, entity)`; tiers `origin` + `affected`;
severity independent from tier; Mermaid rendering from the map; `isolation.severed`
→ `l2` only; VLAN views receive VLAN-scoped findings only.

**Deferred (fast-follow):** split `affected` into `affected_primary`
(the segment/VLAN/client *at* the cut — first hop) and `affected_secondary`
(the transitive blast radius behind it). This needs **cut-distance analysis**
(graph adjacency to the severed links — inputs already exist in
`isolation.severed` evidence: `severed_links`, `lost_peers`) and is easy to make
subtly wrong, so it is intentionally out of v1. Origin-vs-affected plus view
scoping already resolves the reported operator confusion.

## Dependency: mistmcp (web)

This adds a new field; the engine emits it immediately. A **follow-up mistmcp
change** is required to consume `visual_map` (render `origin` distinctly from
`affected`, and key node/VLAN coloring by `(view, entity)` instead of unioning
findings). Until then, mistmcp's existing severity-based rendering keeps working
unchanged — the new field is purely additive. Sequence the web change after this
lands so the two can be verified together.
