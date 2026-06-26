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
  kind:     "device" | "vlan" | "port" | "link" | "intf"   # structured — no string-parsing needed
  id:       str                            # the raw entity id (may contain colons, e.g. s1:mge-0/0/0)
  tier:     "origin" | "affected"          # v1 — see Deferred for primary/secondary
  severity: "info" | "warning" | "error" | "critical"
  findings: tuple[FindingRef, ...]         # finding instances touching this (view, entity)

FindingRef:
  index:   int                             # position in Verdict.findings — stable within a verdict
  code:    str                             # the finding code (display)
  subject: ObjectRef | None                # the finding's headline object, for human linkage
```

`findings` carries **instance refs, not bare codes**. Two findings can share a
code (e.g. `blackhole.exit_lost` on VLAN 10 and VLAN 20 both touching `device:s2`
on the `l2` view); code-only would collapse them, breaking back-links and counts.
`index` (the finding's position in `Verdict.findings`) uniquely identifies the
instance the UI links back to; `code`/`subject` are for display.

- Added as `Verdict.visual_map: VisualMap`. Serializes through
  `render.verdict_to_dict`'s existing `_plain` walk with no special-casing.
- Purely additive and **presentational**. `decision.py` never reads it.

### View vocabulary = existing `Diagram.view` ids

`l2`, `vlan:<vid>`, `l3_exits`. Reusing the exact ids the diagrams already emit
(not a parallel set) keeps the map and the rendered diagrams aligned 1:1, so
mistmcp can correlate a map entry to the chart it annotates.

### Entity keys

`device:<node>`, `vlan:<vid>`, `port:<id>`, `link:<id>`, `intf:<l3intf_id>`,
using the same `_mac` / `node_for` VC-folding normalization already in
`highlight.py`. The `intf:<l3intf_id>` key matches the synthetic `intf:<id>`
nodes the current `_l3_exits_diagram` already emits (`mermaid.py`), so an
interface highlighted on the `l3_exits` view has a stable key.

**Parsing rule (contract).** The id portion of a key may itself contain colons —
a port id is `s1:mge-0/0/0`, so its key is `port:s1:mge-0/0/0`, and a link key is
`link:<porta>__<portb>` where each port id contains colons. Consumers MUST split
on the **first** colon only: everything before it is `kind`, everything after is
the raw id. (This matches `highlight.py`'s existing `pid.split(":", 1)`.) If a
consumer prefers not to string-parse, the serialized form additionally carries
the split fields — see the `VisualEntry` note below — so `{kind, id}` is available
structurally and string-splitting is never required.

**Renderability rule (hard requirement).** Mermaid VLAN/L2 views render **device**
nodes, not port/link nodes. So whenever a `port:<id>` or `link:<id>` entity is
emitted (most importantly as an `origin` — see below), the builder **must also**
emit an entry for its endpoint **`device:<node>`** with the same tier, using the
existing `port_node` / link-split helpers in `highlight.py`. Without this, a
cut whose cause is a port/link would have no visible origin on any device-rendering
view and the "where did the change happen?" signal would silently disappear.

### Scoping rule (the bleed fix, made generic)

For each finding, derive:
- its **referenced VLANs** — `subject` (kind `vlan`), `evidence["vlan"]`,
  `evidence["affected_vlans"]`, `evidence["impacts"][].vlan`;
- its **referenced nodes/ports/links** — `subject` (device/port/link),
  `affected_entities`, the node/port/link evidence keys (`component_nodes`,
  `fragment_nodes`, `new_member_ports`, `link`, …), and **each
  `evidence["impacts"][].attachment`** (the client's attach port or AP — this is
  how `client.impact` findings localize to topology; omitting it would leave a
  client-impact finding painting only the VLAN box and the origin, losing the
  affected attachment node). The matching `evidence["impacts"][].vlan` feeds the
  referenced-VLANs set above.

**`affected_entities` disambiguation rule (resolve against the IR, never by
syntax).** `affected_entities` is an untyped id list and some findings put
non-topology ids there — `client.impact` puts client **MACs** there. A value is
promoted to an entity **only if it resolves against the IR**: `ent in ir.devices`
(or its VC-folded node), `int(ent) in ir.vlans`, or `ent in ir.ports` (the
existing `highlight.py:123-132` checks). A colon-bearing MAC must **never** be
treated as a `port:`-ish entity by string shape. Client-impact clients localize
via `impacts[].attachment` (a real port/AP id), not via their MACs in
`affected_entities`. Test: a `client.impact` finding's MAC does not produce any
device/port entry, while its `attachment` does.

Then project the finding onto views:

- **`l2`** ← every referenced node/port/link. (The L2 topology is the global
  physical view.)
- **`vlan:<vid>`**, for each referenced `vid` ← the referenced nodes **that exist
  in that VLAN's graph**, plus the `vlan:<vid>` box entry. A finding with **no**
  VLAN reference projects onto **no** VLAN view.

  **Paired-array exception (no finding-wide cross-product).** The bullet above
  takes the *finding-wide* node set × *finding-wide* VLAN set, which is correct
  only when a finding is single-VLAN. `wired.client.impact.active_clients` is a
  **single** finding carrying many `impacts[]`, each with its own `vlan` **and**
  `attachment`. Projecting the finding-wide cross-product would paint a VLAN-20
  client's attach node onto `vlan:10` whenever that switch also exists in VLAN
  10's graph — exactly the bleed we are removing. So structured arrays project
  **pairwise**: each `impacts[i].attachment` projects **only** onto
  `vlan:impacts[i].vlan` (and onto `l2`), never onto the other impacts' VLANs.
  General rule: when a finding carries paired `(vlan, node)` tuples, honor the
  pairing; only the finding-wide scalars (`subject`, top-level `evidence["vlan"]`,
  `affected_entities`) use the cross-product. Test: a client-impact finding with a
  VLAN-10 client on `s1` and a VLAN-20 client on `s1` yields `s1` under `vlan:10`
  and `vlan:20` but does **not** leak either onto an untouched `vlan:30` even
  though `s1` is in VLAN 30's graph.
- **`l3_exits`** ← referenced routed VLANs (`vlan:<vid>`), and **only the
  interfaces that serve those referenced VLANs** owned by hit nodes, emitted
  under the `intf:<l3intf_id>` key. A finding with **no** VLAN reference does
  **not** project onto `l3_exits` at all. This is deliberately tighter than the
  current `_l3_exits_diagram` "all interfaces owned by hit nodes" mapping, which
  preserves the bleed: a VLAN-10 hit on `s1` must not highlight `s1`'s VLAN-20
  interface. Filter candidate interfaces by `intf.vlan_id ∈ referenced VLANs`.

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
- **findings** = the union of `FindingRef`s (`{index, code, subject}`) that
  produced this entry (for tooltips / the findings list / back-links). Refs are
  deduplicated by `index`.

### Origin derivation

`origin` entities come from each finding's `caused_by` refs (the changed
entity), normalized to graph nodes/ports. An origin **inherits the view-set of
the finding it caused**, so on `vlan:10` the operator sees `origin s1 → affected
s2`, and on `l2` the origin device is distinct from the affected sea.

**Port/link/l3intf causes must surface as device origins.** Most cut causes are
ports or links (a disabled uplink, a removed link); blackhole causes can also be
`Cause(ref.kind="l3intf", …)` (a removed SVI/IRB that was the VLAN's exit). Per
the renderability rule above, when `caused_by` resolves to a non-device entity
the builder emits **both** the entity's own origin entry **and** an `origin`
entry for its owning `device:<node>`:

- `port:<id>` → endpoint `device:<node>` (existing `port_node` helper)
- `link:<id>` → both endpoint `device:<node>`s (existing link-split)
- `l3intf:<id>` → its owner `device:<node>` (resolve owner via `L3Intf.device_id`
  + `node_for` folding, the same path `_l3_exits_diagram` uses). The owner device
  is the guaranteed-renderable origin **on `l2`** (which contains every device).
  It also projects onto a referenced VLAN view **only if the owner still
  participates in that VLAN's proposed graph** — see the participation caveat
  below. The interface's own `intf:<l3intf_id>` entry is emitted **only when the
  interface still exists in the rendered (proposed) IR** — see the removed-entity
  rule below.

**Removed entities are not promised a self-entry (v1).** Diagrams render from the
**proposed** IR, so an entity the delta *removed* (a removed SVI/IRB, a removed
port, a removed link) has **no node in the rendered chart** to attach a class to.
For v1 we do **not** render "ghost"/baseline-only nodes. The contract therefore:
an origin always surfaces on its **owner `device:<node>`** (which still exists as
long as the device does), and the entity's own `intf:`/`port:`/`link:` self-entry
is emitted **only if that entity resolves in the proposed IR**. So a removed SVI's
origin shows on its owner device in `l2`, not as a dangling `intf:` on `l3_exits`.
(Rendering ghost baseline interfaces in `l3_exits` is a possible fast-follow,
listed in Deferred.) If the owner device itself was removed, the finding
contributes its origin as caption/unlocalized rather than a phantom node.

**Participation caveat (v1 promise scope).** The owner-device origin is
guaranteed only on **`l2`** (it contains every device). It appears on a
`vlan:<vid>` view **only if the owner still participates in that VLAN's proposed
graph**. Removing an SVI/IRB can drop its owner out of that VLAN's proposed graph
entirely (the IRB was the device's only presence in the VLAN), and v1 does not
render non-participating phantom nodes — so on that VLAN view the operator sees
the *affected* stranded members, and correlates the origin via `l2`. We do **not**
force-render a non-participating origin device onto a VLAN chart (that is the
ghost-node work, deferred). This is an honest, narrower promise than "origin shows
on `l2`/VLAN views"; the where-is-the-change signal is always present on `l2`.

This owner-device fallback is what keeps "origin s1" visible on the
device-rendering views — the single most important guard against re-introducing
the old "where did the change happen?" ambiguity. Dedicated tests: a
**port-caused** blackhole → `device:` origin on `l2` and the VLAN view (the port's
device still participates); an **l3intf-caused** blackhole where the SVI **still
exists** → `intf:` origin on `l3_exits` **and** owner `device:` origin; an
**l3intf-caused** blackhole where the SVI was **removed** and the owner no longer
participates in the VLAN → owner `device:` origin present on **`l2`**, **no**
dangling `intf:` entry, and **no** forced origin on the `vlan:` view.

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

- **`src/digital_twin/contracts/`** — new `VisualMap` / `VisualEntry` /
  `FindingRef` / tier enum (or typed dict); export from the contracts package.
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
  produces entries under `l2` only, none under any `vlan:*` **and none under
  `l3_exits`**.
- **`l3_exits` interface scoping (P1 guard)** — a VLAN-10 finding on a device
  that owns both a VLAN-10 and a VLAN-20 interface highlights the VLAN-10
  interface only; the VLAN-20 `intf:` entry is absent from `l3_exits`.
- **Client-impact attachment (P2 guard)** — a `client.impact.active_clients`
  finding produces an entry for each impact's `attachment` node/port on `l2` and
  the impact's `vlan` view, not merely the VLAN box. Conversely, the client
  **MAC** in `affected_entities` produces **no** device/port entry (it does not
  resolve against the IR) — the IR-resolution disambiguation rule.
- **Removed-entity origin (P2 guard)** — an `l3intf`-caused blackhole where the
  SVI **still exists** in proposed IR yields both an `intf:` origin on `l3_exits`
  and the owner `device:` origin; where the SVI was **removed** and the owner no
  longer participates in the VLAN, the owner `device:` origin appears on **`l2`**
  only — no dangling `intf:` entry and no forced origin on the `vlan:` view
  (diagrams render the proposed IR; v1 renders no phantom nodes).
- **Paired-array projection (P1 guard)** — a `client.impact` finding with a
  VLAN-10 client on `s1` and a VLAN-20 client on `s1` yields `s1` under `vlan:10`
  and `vlan:20`, but **not** under an untouched `vlan:30` even though `s1` is in
  VLAN 30's graph — each `impacts[i].attachment` projects only onto its own
  `impacts[i].vlan`, never the finding-wide cross-product.
- **Tier reconciliation** — an entity that is both `caused_by` (origin) and
  inside an affected fragment resolves to `origin`; severity still worst-wins
  independently.
- **Origin presentation** — `caused_by` entities appear as `origin` entries (a
  behavior change vs today's caption-only), distinct from `affected`.
- **Port/link → device origin (P1 guard)** — a blackhole whose `caused_by` is a
  **port** asserts an `origin` entry for the endpoint `device:<node>` on both the
  `l2` view and the relevant `vlan:<vid>` view (not just the `port:` entry). This
  is the test that prevents the origin signal from disappearing on
  device-rendering views.
- **Severity orthogonality** — two findings of different severity on the same
  `(view, entity)`: tier unchanged, severity = worst.
- **Verdict invariance** — for a representative set of goldens, building
  `visual_map` does not alter `decision` or any finding `severity` (compare
  verdict with/without the map populated).
- **Serialization** — `verdict_to_dict` round-trips `visual_map` to the nested
  `{view: {entity: {kind, id, tier, severity, findings}}}` shape mistmcp expects,
  where each entry carries structured `kind`/`id` (so no string-parsing is
  required) and each `findings` element is a `{index, code, subject}` ref (not a
  bare code).
- **Instance distinctness (P2 guard)** — an entity hit by two same-code findings
  (`blackhole.exit_lost` on VLAN 10 and VLAN 20) on the `l2` view carries **two**
  distinct `FindingRef`s with different `index` values, not one collapsed code.

## v1 scope and deferred work

**v1 ships:** `VisualMap` keyed by `(view, entity)`; tiers `origin` + `affected`;
severity independent from tier; Mermaid rendering from the map; `isolation.severed`
→ `l2` only; VLAN views receive VLAN-scoped findings only.

**Deferred (fast-follow):** split `affected` into `affected_primary`
(the segment/VLAN/client *at* the cut — first hop) and `affected_secondary`
(the transitive blast radius behind it). This needs **cut-distance analysis**
(graph adjacency to the cut edges) and is easy to make subtly wrong, so it is
intentionally out of v1.

The inputs are **partly** present and partly preparatory work:
`l2.isolation.severed` evidence today is `{fragment_nodes, lost_peers,
occupants}` — `lost_peers` gives the surviving side of the cut, but the **cut
edges themselves are not in evidence** (`severed_links` exists only as a local
confidence-computation variable, never written out). So the fast-follow must
either add a `severed_links` evidence field as preparatory work, or reconstruct
the cut edges from `fragment_nodes` + `lost_peers` against the IR's links. This
is called out here so the plan does not assume the cut edges are already
available. Origin-vs-affected plus view scoping already resolves the reported
operator confusion, so v1 ships without it.

**Deferred (fast-follow):** render "ghost" baseline-only nodes for removed
entities (a removed SVI/IRB in `l3_exits`, a removed port/link in `l2`) so a
removed entity can carry its own self-entry instead of falling back to the owner
device. v1 deliberately renders only proposed-IR nodes and surfaces removed-entity
origins on the owner `device:<node>`.

## Dependency: mistmcp (web)

This adds a new field; the engine emits it immediately. A **follow-up mistmcp
change** is required to consume `visual_map` (render `origin` distinctly from
`affected`, and key node/VLAN coloring by `(view, entity)` instead of unioning
findings). Until then, mistmcp's existing severity-based rendering keeps working
unchanged — the new field is purely additive. Sequence the web change after this
lands so the two can be verified together.
