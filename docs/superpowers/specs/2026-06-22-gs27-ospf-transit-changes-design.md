# GS27 — OSPF transit changes

**Status:** design — pending user review
**Date:** 2026-06-22
**Author:** brainstormed with the repo owner
**Builds on:** GS26 (`wired.l3.ospf_withdrawal`, the `OspfIntf` IR entity) — see
`docs/superpowers/specs/2026-06-13-gs26-ospf-exit-withdrawal-design.md`.

## Goal

Turn **mutation of retained OSPF transit config** into precise verdicts, and
add a **live-telemetry adjacency-break** layer. Two halves:

1. **Structural** (config-delta, no RIB, like GS26): a metric change, a
   non-collapsing active↔passive flip, an area move, a *newly added* routed OSPF
   participation, or a *changed advertised connected prefix* (the subnet of an
   OSPF-participating vlan) → **REVIEW**. Replaces GS26's coarse `.transit_mutation`
   placeholder, and models `metric` (today denied → blunt UNKNOWN).
2. **Telemetry** (`site_ospf` neighbor stats): a live, established OSPF peer
   that the proposed config no longer covers within its predicted connected
   subnet → **UNSAFE**. **Escalate-only** — telemetry can only add/raise
   findings, never produce SAFE.

## Hard constraint — the telemetry layer is built blind

The reachable org (`9777c1a0-…`) has **zero OSPF** — no `ospf_areas` config and
`org_ospf` returns `count: 0`. So the telemetry subnet-prediction + peer-join
model cannot be grounded on real records nor live-verified; it is exercised only
by synthetic fixtures (exactly as GS26's structural model was). The design
contains this risk with one rule, stated up front and relied on throughout:

> **Escalate-only invariant.** Telemetry may only *add* a finding or *raise* an
> existing structural finding's severity (REVIEW→UNSAFE). It may never downgrade,
> remove a finding, or produce PASS/SAFE. Therefore any error in the blind-built
> subnet/peer model can only *over*-escalate (spurious UNSAFE — safe-side, noisy)
> or *fail* to escalate (the structural REVIEW floor stands — safe-side). **There
> is no path from a telemetry error to a false-SAFE.**

Exact `site_ospf` field names are confirmed against the Mist OAS at build; the
ingester is fail-soft (unknown shape → that row is telemetry-blind, never a
crash, never a downgrade).

## Architecture (Approach A)

Extend the existing `wired.l3.ospf_withdrawal` check in place (one OSPF check,
one `applies_to`, shared participation model), and quarantine the blind-built
reachability logic in a focused pure module the check consults.

```
ir/entities.py            OspfIntf.metric; new OspfNeighbor
ir/model.py               IR.ospf_neighbors + ospf_telemetry_unparsed_count + setters
ir/diff.py                OspfNeighbor NOT in _ENTITY_KINDS (non-diff-bearing)
ir/__init__.py            export OspfNeighbor + IRCapability.OSPF_TELEMETRY
adapters/mist/ingest/switch.py            _metric_int parser; _ospf reads metric
adapters/mist/ingest/ospf_neighbors.py    NEW self-isolating OspfNeighborIngester
scope/allowlist.py        + ospf_areas.*.networks.*.metric
providers/base.py         RawSiteState.ospf_neighbors
providers/mist_api.py     _ospf_neighbors (site_ospf), fail-soft, meta.fetched
observability/replay/store.py + FixtureProvider   ospf_neighbors save/load + defaults
analysis/ospf_reachability.py             NEW pure module (predict / cover / broken_peers)
checks/wired/ospf_withdrawal.py           per-area participation, new codes, escalation
```

## Section 1 — data layer

### `OspfIntf.metric`
Add `metric: int | None = None` (after `passive`). The `_ospf` ingest reads
`_metric_int(ncfg.get("metric"))` — a small parser returning `int` or `None`
(templated/unparseable/absent → `None`; there is no generic `_int` in `switch.py`
today, only `_vlan_int`). `metric` is **not** part of `OspfIntf.id`; it is a
mutable attribute compared in the check, and it is diff-bearing automatically
(a metric edit yields an `ospf_intf` diff → `applies_to` fires).

Allowlist: add `ospf_areas.*.networks.*.metric` beside the existing
`…networks.*.passive` leaf. This is the one-leaf opening that turns a metric
edit from today's blunt UNKNOWN into a modeled REVIEW. No false-SAFE hole:
before this change any metric edit was denied → UNKNOWN; GS27 now consumes it.

### `OspfNeighbor` (live telemetry, observational)
```python
@dataclass(frozen=True)
class OspfNeighbor:
    device_id: str
    peer_ip: str
    area: str | None = None            # None = telemetry omitted area -> subnet-only match
    state: str = ""                    # raw Mist state, e.g. "Full"
    vrf: str | None = None
    neighbor_router_id: str | None = None
    meta: FactMeta = OBSERVED_META     # repo has OBSERVED_META (no TELEMETRY_META)
    id: str = ""                       # f"{device_id}:ospfnbr:{area or '*'}:{peer_ip}"
```
`area` is `str | None`, **not** defaulted to `"0"`: a neighbor whose telemetry omits
the area must stay distinguishable from a genuine area-0 neighbor, so the lenient
subnet-only match (Section 3 `covers`) fires for it regardless of which area its
interface is in. The id renders absent area as `*` for stability.
- Minted by a **self-isolating** `OspfNeighborIngester` (mirrors
  `ClientEnrichmentIngester`): whole-body + per-row `try/except`; it never
  reaches the registry's `failures`, so malformed telemetry can never flip
  `report.ok → ir=None → UNKNOWN`.
- **Non-load-bearing all the way down:** like `ClientEnrichment`, not `OspfIntf`
  — no strict IR validation; `IRBuilder.build()` can never fail on a neighbor row.
- **Not diff-bearing** (excluded from `diff._ENTITY_KINDS`): telemetry is baseline
  observation, not config — a telemetry change must never create a diff.

### Capability `OSPF_TELEMETRY` (fetch-success ≠ parse-usable)
- Earned when the `site_ospf` fetch **succeeds** (shape reachable), including the
  genuinely-empty case (a site with OSPF but no adjacencies up → zero peers,
  shape known). This is the `telemetry_known` gate, mirroring `clients_known`.
- The ingester also carries a soft `IR.ospf_telemetry_unparsed_count` — the number
  of raw rows it **skipped** (no usable `peer_ip`/state). This counts **partial**
  loss, not only the all-rows-failed case: if some rows parse and others are
  dropped, the dropped ones must not vanish silently — for a subnet-only change
  that could turn "there may be an affected peer we could not parse" into PASS.
  The check honors `count > 0`: telemetry-blind for the unparsed portion + a
  relevance-scoped coverage note (same scoping as Section 3), **never** "no Full
  neighbors exist." (Under the escalate-only invariant this is a coverage-honesty
  refinement, not a false-SAFE fix — a wrongly-empty neighbor set only means "no
  escalation, structural floor stands"; the count makes the *partial* case honest.)
- `OSPF_TELEMETRY` is **never** a hard `requires()` of the check (that would skip
  the whole check — and the structural REVIEW floor — when telemetry is absent).
  Escalation is conditional *inside* `run()`, exactly like `clients_known`.

### Fetch plumbing
`RawSiteState.ospf_neighbors: tuple[Json, …] = ()`; `MistApiProvider._ospf_neighbors`
fetches `site_ospf` in `_fetch_one` (fail-soft: error → empty + `meta.failures`,
never crash); `meta.fetched` lists `"ospf_neighbors"` on success; `ReplayStore`
save/load and `FixtureProvider` defaults carry the field (else golden/live replay
silently drops it). In **replay** fixtures redaction scrubs `peer_ip` → the subnet
join can't match → telemetry-blind (safe-side); live runs use real IPs; goldens
use synthetic non-redacted test IPs.

## Section 2 — structural mutation detection

Extend the participation model: `_Seg.by_area: dict[area, _Row(passive: bool,
metric: int | None)]`, with `active = any(not r.passive)` and `areas = set(by_area)`
derived. `_participation(ir)` still keys `by_dev_vlan[(device, vlan)]`.

**Per-area collision guard.** If two OSPF network entries resolve to the same
`(device, vlan, area)` with **differing** `(passive, metric)`, that area is
**ambiguous**: emit a PARTIAL coverage note and **skip** precise metric/passive
comparison for it — never last-win. Agreeing duplicates collapse to one. The
area-*set* comparison (`.area_changed`) is unaffected. (Mirrors the GS30
VLAN-name-collision philosophy.)

For each **retained** `(device, vlan)` present in both IRs and **not** owned by an
egress-collapse (`egress_owned_pairs` subsumption carries over), diff `by_area`
and emit precise REVIEW findings (replacing GS26's `.transit_mutation`):

| code | source | trigger | message gist |
|---|---|---|---|
| `.metric_changed` | `ospf_intf` diff | retained `(device,vlan,area)` metric `X→Y` | "path selection may shift (no RIB computed)" |
| `.passive_flip` | `ospf_intf` diff | retained `(device,vlan,area)` flipped active↔passive, non-collapsing | "transit role changed" |
| `.area_changed` | `ospf_intf` diff | the `(device,vlan)` area **set** changed | "adjacency / LSA-scope may shift" |
| `.participation_added` | `ospf_intf` diff | a `(device,vlan)` newly present in OSPF (absent in baseline) | "new advertisement / possible transit — review" |
| `.advertised_prefix_changed` | `Vlan.subnet` diff ∩ OSPF | a retained OSPF `(device,vlan)` whose canonical `Vlan.subnet` changed | "the connected prefix advertised into OSPF changed" |

All five: **WARNING → REVIEW**, MEDIUM confidence (reuse `_UNVERIFIED`). Never
UNSAFE structurally. `caused_by` names the changed `ospf_intf` rows (or, for
`.advertised_prefix_changed`, the changed `vlan`) via `delta_index`. The first
four are gated on `_routed(prop_ir, vid)` like the GS26 codes; an *unresolved*
added/changed `ospf_intf` row rides the existing unresolved-row PARTIAL note.

`.advertised_prefix_changed` has a **distinct source**: it is not an `ospf_intf`
attribute diff but a join of the OSPF participation set with the `Vlan.subnet`
diff. For each `(device, vlan)` in OSPF participation in **both** IRs (active OR
passive — a stub still advertises its connected prefix), if both subnets resolve
and **differ canonically** (`ip_network`, GS31-style), emit it. If either side is
unresolved/`None`, the prefix can't be compared → the relevance-scoped telemetry/
unresolved note, not a precise finding. This closes the false-SAFE where a subnet
edit on an OSPF-active VLAN silently advertised a new prefix while the adjacency
survived.

Notes:
- `.participation_added` is required for safety, not just usefulness: a bare-`{}`
  OSPF network add produces **no raw-leaf diff** but **does** produce an added
  `ospf_intf` — silently treating additions as SAFE would be a false-SAFE. Both
  passive (advertises a prefix) and active (can transit) additions → REVIEW.
  Additions do **not** telemetry-escalate (they cannot break a pre-existing peer).
  `.participation_added` is a `(device,vlan)` **wholly new** to OSPF; adding a new
  *area* to a `(device,vlan)` already in OSPF is `.area_changed` (area-set grew) —
  the two never double-report.
- An area-move reads as `.area_changed` only (the moved area has no retained
  overlap). A pure rename leaves the semantic tuple unchanged → silent.
- `metric` on a passive stub still changes the advertised cost → `.metric_changed`
  REVIEW (no adjacency, so never escalates).
- The GS26 golden asserting `.transit_mutation` (non-collapsing flip) is updated
  to `.passive_flip`.

## Section 3 — telemetry reachability + escalation

### Pure module `analysis/ospf_reachability.py` (IR-only, no I/O)
1. **predict_subnet(intf)** — for an *active* `OspfIntf(device, vlan, area)`, the
   predicted connected subnet = that vlan's `Vlan.subnet`. Unresolved/no subnet →
   no prediction → blind for that interface.
2. **covers(neighbor, intfs, vlans)** — a live `OspfNeighbor(device, peer_ip,
   area)` is *covered* by an active OspfIntf on the **same device** whose predicted
   subnet contains `peer_ip` and whose area matches (telemetry area absent → match
   on subnet only — the lenient/safe direction).
3. **broken_peers(base_ir, prop_ir)** — baseline neighbors that **were covered in
   baseline but are not covered in proposed** (covering interface removed / went
   passive / area-moved / subnet changed to exclude). A peer *not* covered in
   baseline → the model is blind for it → coverage note, **never** escalation.

### Escalation (in `run()`, gated on `telemetry_known`)
Only baseline neighbors whose **normalized state is established** (`Full`, pending
OAS confirmation; case/whitespace-normalized; unrecognized state → *not*
established → conservative no-escalate) drive escalation.

General rule: attribute each broken established peer to the structural finding that
**owns its `(device, vlan)`**, and escalate that finding to **ERROR / UNSAFE, HIGH
confidence**, naming the `peer_ip`, instead of its REVIEW/`_UNVERIFIED` default.

- The owning finding may be any adjacency-affecting code: GS26's `.egress_lost`
  (collapse) or `.advertised_removed` (this device's interface withdrawn while it
  keeps adjacency elsewhere — the peer that was adjacent over *that* interface
  breaks); or GS27's `.passive_flip` / `.area_changed`; or
  `.advertised_prefix_changed` when the subnet edit that broke the peer is on a
  retained OSPF `(device, vlan)`. (This also sharpens `.egress_lost`'s "no observed
  clients → REVIEW" case to UNSAFE when a live peer confirms the break.)
- **`.metric_changed` and `.participation_added` never own a break** — metric
  affects path cost not adjacency; an addition cannot break a pre-existing peer.
- A broken peer with **no owning structural finding** (e.g. the covering `(device,
  vlan)` left OSPF in a shape that produced no other code) → standalone
  **`.peer_unreachable`**, ERROR / UNSAFE / HIGH.

HIGH is defensible: escalation fires only for peers **covered in baseline**, so
`peer_ip ∈ a real predicted subnet` — the prediction is validated for that peer —
and the breaking change is config-certain. The finding's claim is precisely "this
known OSPF peer is no longer covered by the predicted connected subnet," not "all
downstream routing is broken."

### Precise applicability
`applies_to(diff) = diff.touches("ospf_intf") or _touches_vlan_subnet(diff)`,
where `_touches_vlan_subnet` matches **vlan add/remove** and **modified-vlan**
entries whose changed fields intersect `{subnet, subnet_unresolved}` — never
`name` / `collisions` / `dhcp_sources` / etc. (The diff carries per-`Modified`
changed-field tuples.) The vlan-subnet path does **both** the structural
`.advertised_prefix_changed` detection (join with OSPF participation) **and**
telemetry peer-break work; the other four structural codes key on `ospf_intf`
participation diffs.

### Relevance-scoped telemetry-blind note (closes the subnet-edit false-SAFE)
Telemetry is **blind** when it was not fetched (no `OSPF_TELEMETRY`) **or**
`ospf_telemetry_unparsed_count > 0` (some/all rows dropped). When blind, emit a
PARTIAL note (→ REVIEW) **iff the delta is OSPF-relevant**:
- a structural OSPF finding exists, **OR**
- a delta-touched subnet belongs to an active OSPF `(device, vlan)` in baseline
  or proposed (even with zero baseline peers — we cannot prove no peer would break).

When telemetry is usable but a baseline-covered peer's proposed coverage could not
be evaluated on a delta-touched device → note too. No note for an unrelated subnet
edit (mirrors the `touched_ids` discipline — PARTIAL floors to REVIEW, so never
taint an unrelated change).

## Section 4 — verdict matrix & edge cases

| condition | severity | conf | status |
|---|---|---|---|
| `.metric_changed` (retained) | WARNING | MEDIUM | REVIEW |
| `.passive_flip` / `.area_changed` / `.participation_added` / `.advertised_prefix_changed`, no peer break | WARNING | MEDIUM | REVIEW |
| `.passive_flip` / `.area_changed` / `.advertised_prefix_changed` breaks an established peer | ERROR | HIGH | UNSAFE |
| GS26 `.egress_lost` / `.advertised_removed` breaks an established peer | ERROR | HIGH | UNSAFE (even with 0 observed clients) |
| `.peer_unreachable` (break with no structural owner) | ERROR | HIGH | UNSAFE |
| ambiguous `(device,vlan,area)` | — | — | PARTIAL note, skip precise compare |
| telemetry absent/unparsed **and OSPF-relevant** | — | — | PARTIAL note |
| OSPF addition (routed) | WARNING | MEDIUM | REVIEW (`.participation_added`) |

Edge cases: an egress-collapsed `(device,vlan)` subsumes mutation findings and
its peer-breaks escalate the egress finding; a subnet edit on an **OSPF-participating**
vlan that *still* covers the peer → `.advertised_prefix_changed` REVIEW (the
advertised prefix changed even though the adjacency survived) — **not** a PASS; a
subnet edit on a **non-OSPF** vlan → check runs (applies_to fires on the subnet
diff), finds no OSPF participation → clean PASS (no finding, no spurious note via
relevance-scoping); empty `site_ospf` = success → `OSPF_TELEMETRY` earned with
genuinely-zero peers.

### Never-false-SAFE consolidation
The structural REVIEW floor is independent of telemetry; telemetry is
escalate-only; opening the `metric` leaf is safe because GS27 consumes it (no
UNKNOWN→SAFE regression); ambiguous / unparsed / absent-but-relevant / unresolved
→ REVIEW floor + note, never SAFE; additions → REVIEW (no silent-add false-SAFE);
a subnet edit on an OSPF-participating vlan → `.advertised_prefix_changed` REVIEW
(prefix change is real even when the adjacency survives), independent of telemetry.

## Testing

- **Pure `ospf_reachability` unit suite** (adversarial — it is blind-built):
  cover in/out of subnet; area match / mismatch / absent; each break cause
  (remove / passive / area-move / subnet-exclude); malformed neighbor → no match;
  vlan-without-subnet → blind; non-established state → not broken.
- **Check unit tests:** each structural code (incl. `.participation_added` + the
  bare-`{}` add, and `.advertised_prefix_changed` on an OSPF vlan whose subnet
  changed — adjacency-surviving REVIEW, *not* PASS); subnet edit on a **non-OSPF**
  vlan → PASS; ambiguous-area note; egress subsumption; per-code escalation (incl.
  `.advertised_prefix_changed` → UNSAFE when the subnet edit also breaks a peer);
  `.peer_unreachable`; metric-never-escalates; `applies_to` precision (vlan **name**
  change does *not* fire; **subnet** change does); relevance-scoped telemetry-blind
  note (incl. subnet-edit-without-telemetry → REVIEW, and unrelated subnet edit →
  no note); non-established peer does not escalate.
- **Self-isolating ingester test:** malformed telemetry → `report.ok` stays True,
  `OSPF_TELEMETRY` earned, `ospf_telemetry_unparsed_count` reflects dropped rows
  (incl. the **partial** case: some rows parse, some skipped → count > 0).
- **Goldens** (end-to-end via `simulate`, GS26-style): metric / passive / area /
  participation_added REVIEW; ambiguous PARTIAL; passive_flip + live established
  peer → UNSAFE; OSPF-vlan subnet edit, adjacency survives → `.advertised_prefix_changed`
  REVIEW; OSPF-vlan subnet edit that excludes a live peer → escalates that finding
  to UNSAFE; non-OSPF-vlan subnet edit → SAFE; telemetry-absent on OSPF-active
  subnet edit → REVIEW + relevance note; egress + live peer → UNSAFE. Update the
  GS26 `.transit_mutation` golden → `.passive_flip`.

## Live-verify (honest about the constraint)

The live org has zero OSPF, so live-verify is a **regression check only**:
confirm the new `site_ospf` fetch is clean (empty → `OSPF_TELEMETRY` earned, zero
peers), ingest stays `ok=True`, and the 8 existing live plans have **decisions /
findings / check statuses unchanged, aside from the expected `state_meta.fetched`
gaining `ospf_neighbors`** (not literal byte-equality). The feature itself is
proven by the synthetic fixtures above — as GS26 was.

## Out of scope / deferred

- Gateway OSPF (device-level gateway ops are out of M1 field-gate scope, per GS26).
- OSPF area-type (stub/NSSA), timers (hello/dead/bfd), auth — stay denied → UNKNOWN.
- A real RIB / SPF computation. The twin models participation + connected-subnet
  coverage, never full reachability.
- Grounding/validating the telemetry model on real `org_ospf` records — revisit
  when an OSPF-bearing org is available.
