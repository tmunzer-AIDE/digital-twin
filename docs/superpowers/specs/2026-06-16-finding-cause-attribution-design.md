# Finding cause attribution (`Finding.caused_by`)

**Status:** design — pending user review
**Date:** 2026-06-16
**Author:** brainstormed with the repo owner

## Problem

When a `ChangePlan` touches several targets in one op, the verdict lists the
*symptoms* but never the *cause*. Real example — one `updateSiteDevice` PUT that
re-profiles three ports:

```json
{"port_config": {
  "ge-0/0/10": {"usage": "srv", ...},
  "mge-0/0/0": {"usage": "default"},
  "mge-0/0/1": {"usage": "default"}}}
```

produces:

```
WARNING wired.l2.vlan_segmentation.split ×4
vlan 7:  broadcast domain partitioned by the delta
vlan 8:  broadcast domain partitioned by the delta
vlan 10: broadcast domain partitioned by the delta
vlan 20: broadcast domain partitioned by the delta
WARNING wired.l2.blackhole.exit_lost ×3
vlan 7/8/10: member segment loses its path to the boundary_uplink exit
WARNING wired.l2.blackhole.exit_unlocatable ×3
vlan 12/20/192 has members but its exit cannot be located
WARNING wired.client.impact.active_clients
3 currently-connected client(s) affected by the delta
```

Every finding names the affected **vlan** (the symptom). None names the changed
**port** (the cause). With three ports changed at once, the admin cannot tell
which edit produced which warning — and cannot tell which warnings are *their
doing* versus *pre-existing conditions the change merely surfaced*.

The information already exists: `IRDiff` records exactly which ports/links/leaves
changed, and each graph check already computes the affected component's member
ports and nodes. It is simply never threaded from the diff into the finding.

## Goals

- Every **delta-attributed** finding names the changed entity (or entities)
  responsible for it, with the field(s) that changed.
- **Pre-existing / context** findings explicitly carry *no* cause — telling the
  admin "this was not caused by your change."
- Uniform across all 16 wired checks and the adapter/dynamic-gate findings, with
  one shared mechanism and one rendering path.
- **Strictly additive and non-load-bearing**: attribution is evidence only. It
  MUST NOT change any severity, confidence, coverage, decision, or decision
  reason. A run with attribution disabled and one with it enabled produce
  identical verdicts apart from the new evidence field.

## Non-goals (recorded, deferred)

- **Cause-first rendering / grouping** (group output by changed port instead of
  by vlan). A pure presentation reframe on top of this data; valuable for
  many-vlans-one-port pushes, but separate. Deferred.
- **Per-leaf differential simulation** (`--explain` mode: re-simulate each port
  change in isolation and diff verdicts). Gold-standard precision incl.
  interaction effects, but N× cost and a much larger feature. Deferred.
- No change to which findings are *emitted*, only to the evidence they carry.

## Core mechanism

### The `Cause` contract

A new frozen dataclass in `contracts/`, and a new trailing-defaulted field on
`Finding`:

```python
@dataclass(frozen=True)
class Cause:
    """A changed entity responsible for a finding. `ref` locates the changed
    object (the same ObjectRef vocabulary as Finding.subject); `fields` are the
    NORMALIZED IR field name(s) that changed on it (e.g. ("poe",), ("stp_priority",),
    ("dhcp_snooping",), ("native_vlan",)) — empty for pure add/remove deltas."""
    ref: ObjectRef                 # kind/id/name of the CHANGED entity
    fields: tuple[str, ...] = ()   # which IR field(s) changed (empty for add/remove)

@dataclass(frozen=True)
class Finding:
    ...
    caused_by: tuple[Cause, ...] = ()   # changed entities responsible (delta-attributed only)
```

**`fields` vocabulary — IR-normalized, NOT raw config paths.** The single source
of truth (below) is `IRDiff.changed_fields`, which compares IR dataclass
attributes (`Port.poe`, `Port.mtu`, `Device.stp_priority`, `Device.dhcp_snooping`,
`Vlan.dhcp_sources`, …). The raw config leaves the admin literally edited
(`port_config.*.usage`, `stp_config.bridge_priority`, `dhcp_snooping.enabled`,
`servers`, …) are **not recoverable from `IRDiff` alone**, so `fields` exposes the
IR field name, not the raw path. This still answers "which entity and roughly
what about it changed," and the admin already holds the raw `ChangePlan` they
pushed. Raw-path `fields` (and the field-gate's `changed_leaf_paths` index that
would feed them) are a documented future enhancement that the deferred per-leaf
differential mode would also consume — explicitly out of scope here to keep the
source of truth single and the feature bounded.

`caused_by` is distinct from the existing fields:
- `subject` — the **symptom** object (the affected vlan/device); unchanged.
- `affected_entities` — *all* involved IR ids; unchanged.
- `caused_by` — the **changed** subset that *produced* this finding. New.

`Cause.ref` reuses `ObjectRef(kind, id, name)`. Name resolution is centralized
exactly like subject names: `checks/subjects.py:name_findings` is extended to
resolve friendly names for `caused_by` refs too (ports/vlans/links have IR names;
devices resolve to `name=None` → renderers show the id/MAC, per the PR #3
decision that `Device.model` is not a name). Concretely, `name_findings` (which
today only rewrites `f.subject`) gains two more passes, reusing the same
`resolve_subject`/`_name_for` machinery so there is one naming code path:

1. every `cause.ref` in `f.caused_by` (top-level), and
2. the **nested** per-impact causes — `evidence["impacts"][i]["caused_by"]` —
   which `client_impact` carries (the single known nested shape; the traversal is
   guarded so other findings' evidence is untouched).

So a `Cause.ref` is named identically wherever it appears; there is no
"named at top, id-only nested" split.

### The single source of truth: `IRDiff`

Every check already gates on `diff.touches(...)`, so the cause set has one clean
input: `IRDiff.modified / added / removed` (each an `EntityRef(kind, id)`, and
`Modified` also carries `changed_fields`). A small shared helper turns the diff
into a per-entity-kind index the checks query:

```python
# analysis/delta_cause.py (new, pure)
def delta_index(diff: IRDiff) -> DeltaIndex: ...
# DeltaIndex answers: is port/link/device/l3intf/dhcp_scope/vlan X in the delta,
# and with which changed_fields? Plus reverse lookups used by Family 2 below.
```

The helper lives in `analysis/` (pure), not in any single check, so all checks
share one definition of "what changed." It is built **once per run from the
diff** and carried on `CheckContext` — which is the context that owns `baseline`,
`proposed`, AND `diff` (`AnalysisContext` wraps a single IR and has no diff, so it
is the wrong home). The registry/pipeline constructs the `DeltaIndex` from
`ctx.diff` when it builds the `CheckContext`, and every check reads
`ctx.delta_index`. (`CheckContext` gains one field: `delta_index: DeltaIndex`.)

### The honesty rule (mirrors "never false-SAFE")

Attribution is non-load-bearing, so the bar is: **never name the wrong cause.**
If the helper cannot confidently attribute a finding to a delta entity (empty
intersection, genuinely ambiguous), the check emits `caused_by=()` rather than a
guess. Naming nothing is always acceptable; naming the wrong port is not. This is
the attribution analog of the project's cardinal verdict rule.

## Per-check attribution

The 16 wired checks split into two families plus one exclusion. Attribution is
applied **only to delta-attributed findings**; every `preexisting`/context row
carries `caused_by=()` by construction (the checks already compute that boolean).

### Family 1 — leaf/port/device-local: direct

These checks already iterate the changed entity and emit a finding whose subject
is (or maps 1:1 to) that entity. The cause is the same entity + its changed IR
field — built directly from the `DeltaIndex`, no graph analysis. `fields` below
are **IR field names** (per the vocabulary note above). (`stp_root` is NOT here —
its root election runs over a connected component, so a topology change can move
the root with no priority change; it is a Family-2 hybrid rule, #5 below.)

| Check | Cause `ref.kind` | IR `fields` (examples) |
|---|---|---|
| `mtu_mismatch` | port / link | `mtu` |
| `native_mismatch` | port / link | `native_vlan` |
| `stp_edge` | port | `stp_edge` |
| `poe_disconnect` | port | `poe` |
| `ospf_withdrawal` | ospf_intf / device | ospf_intf changed fields |
| `dhcp_path` | vlan / dhcp_scope | `dhcp_sources` |
| `snooping` | device | `dhcp_snooping` |
| `scope_lint` | dhcp_scope | dhcp_scope changed fields |
| `gateway_gap` | l3intf / vlan | removed l3intf → empty `fields` |
| `link_boundary` | link / port | link/port changed fields |

### Family 2 — graph-effect, many-to-one: five mapping rules

`l2_blackhole`, `l2_vlan_segmentation`, `l2_isolation`, `l2_loop`, `stp_root`,
`client_impact`. The finding is about a vlan/component/cycle/client; the cause is
a **set** of changed ports/links/devices. There is **no single mapping** — each
condition needs its own rule, all computed by the shared helper:

1. **VLAN-graph cut** (`l2_vlan_segmentation.split`, `l2_blackhole.exit_lost`):
   cause = delta-changed ports/links **incident to the affected component whose
   change removed `vid`'s carriage** — edges that carried `vid` in the baseline
   vlan-graph and no longer do, mapped to their backing port(s) (L2 edges are
   port-derived), intersected with the `DeltaIndex`.
2. **Physical L2 severance** (`l2_isolation`): cause = the delta-changed *links*
   (removed/disabled) on the boundary between the isolated island and its former
   domain — a physical-topology cut, independent of any single vlan's carriage.
3. **Added-member stranding** (`l2_blackhole.new_member_stranded` /
   `new_member_ports`): the cause is **directly** the added/changed access
   port(s) that introduced the stranded membership — these are already in the
   delta (the check computes `new_member_ports`), so the mapping is a direct
   `new_member_ports ∩ delta`, not a cut analysis.
4. **Loop arming** (`l2_loop`): cause = the delta entity that *armed* the cycle —
   an added edge in the cycle (`added ∩ cycle.member_ports`' links), or a port
   whose `stp_enabled` flipped to unblock it (`cycle.member_ports ∩ delta`).
5. **STP root move** (`stp_root.moved`): the check elects the root over a
   *connected component*, so the move can be driven by **either** a priority
   change or a topology change — it is NOT device-only. Dual rule, restricted to
   the affected component: (a) a device in the component whose `stp_priority`
   changed → that **device** is the cause; (b) otherwise, a delta-changed
   **link/port** that altered the component's connectivity (added/removed edge) →
   that link/port is the cause. Both may apply (cause = the union); if neither is
   in the delta, `caused_by=()` (honesty rule).

`client_impact` is handled separately (next section) — it aggregates clients, so
its causes are per-impact, not a single component cut.

Because a cut can legitimately require more than one removed edge, and one trunk
going to `default` can drop several vlans at once, `caused_by` for Family 2 is a
**set** and may name multiple ports — that is correct, not a defect. (The
many-vlans-one-port readability problem is what the deferred cause-first
rendering solves; it does not block this spec.)

### `client_impact` — aggregate finding, per-impact causes

`ClientImpactCheck` emits ONE aggregate finding (`evidence["impacts"]` is a list,
one entry per affected client) and deliberately has no single `subject`. A single
top-level `caused_by` would conflate clients impacted by *different* ports. The
spec's non-goal forbids changing which findings are emitted, so the finding stays
aggregate; attribution is two-layer:

- Each `evidence["impacts"][i]` entry gains its own `caused_by` — the changed
  port/link on **that** client's attachment path (the entry already records the
  client's attachment).
- The finding's top-level `Finding.caused_by` is the **deduplicated union** of
  the per-impact causes — a faithful "all ports that affected some client,"
  with the precise per-client mapping preserved in evidence.

### The exclusion — pre-existing / context findings

Nearly every check emits `preexisting` INFO context (e.g. `gateway_gap`
preexisting, `mtu` preexisting, blackhole pre-existing strand, `scope_lint` /
`snooping` / `stp_edge` pre-existing, loop pre-existing context, isolation
pre-existing island). By definition these have **no delta cause**:
`caused_by=()`, and the existing "(pre-existing, unchanged)" wording stands. This
is a feature — it tells the admin which warnings are *not* their change's doing
(e.g. the `exit_unlocatable` vlans in the motivating example may be pre-existing).

### Adapter / dynamic-gate findings

`scope.dynamic_ports.unverifiable`, `scope.stp.bridge_priority_invalid`,
`scope.dhcp.range_unresolved` carry a device/dhcp_scope subject and set
`caused_by` inline at construction (they bypass the registry resolver, same as
they already do for subject names) — **but only under the same delta/parity rule
as the checks.** These findings fire on a malformed value present in *either*
baseline or proposed: e.g. `invalid_bridge_priority_findings` sets `invalid` when
the baseline OR the proposed effective is uninterpretable, so an **unchanged
malformed baseline** fires it. That row MUST carry `caused_by=()` — the change did
not introduce it. The inline construction therefore compares the offending value
baseline-vs-proposed and attributes a cause only when it actually changed (the
findings already receive both effective maps, so the parity is local).

### L0 schema findings — explicitly out of scope

L0/`schema.py` validation findings are **excluded** from `caused_by` in this
work. They are structural validity of the pushed object, not a baseline→proposed
condition (there is no meaningful parity: an invalid shape is invalid regardless
of baseline), and their `subject` already names the edited object. Including them
would invite exactly the pre-existing-blame trap P1b warns about under
full-object L0 mode. They keep `caused_by=()`; revisiting them belongs with the
future raw-path `fields` work, where the field-gate change-paths are available.

### Parity note

The UNKNOWN/rejection paths (`derived_gate`, `dhcp_screen`,
`device_profile_gate`) already name the offending leaf path in their reasons.
This work brings the SAFE/REVIEW/UNSAFE *check* findings up to the same
leaf-level attributability the UNKNOWN path already has — making the whole output
uniformly "here is the thing you changed that caused this."

## Rendering

- **Human (`render_human` / `render_org_human`):** append a cause clause to the
  existing line. Today: `on <kind> "<name>" at <path>: <msg>`. With cause:
  `… <msg> (caused by port "mge-0/0/0" [native_vlan])`; multiple →
  `(caused by mge-0/0/0 [native_vlan], mge-0/0/1 [native_vlan])`. The bracketed
  names are IR fields (per the vocabulary note), not raw config leaves. No clause
  when `caused_by=()` (pre-existing or unattributable).
- **Dict (`verdict_to_dict`):** each finding gains a `caused_by` array of
  `{ref: {kind,id,name}, fields: [...]}`. Additive; existing keys unchanged. For
  `client_impact`, the top-level array is the union and each
  `evidence["impacts"][i]` also carries its own `caused_by`.

## Impact on verdict / contracts

- `Finding` gains one trailing-defaulted field → back-compatible; all existing
  construction sites compile unchanged.
- `CheckContext` gains one field, `delta_index: DeltaIndex`, built once by the
  registry/pipeline from `ctx.diff`. (`AnalysisContext` is unchanged — it wraps a
  single IR and has no diff.)
- `decide()` / coverage / confidence are untouched: `caused_by` is never read by
  the verdict layer. The non-load-bearing invariant is test-pinned (a golden
  asserting identical decision/severity/coverage with and without attribution).

## Testing

- **Unit, per check:** a delta-attributed finding names the correct cause(s) with
  the right `fields`; a pre-existing finding carries `caused_by=()`.
- **`delta_index` / cut-mapping unit tests:** Family-2 mapping returns the
  incident changed ports for a constructed partition; returns `()` (not a guess)
  when attribution is ambiguous.
- **Golden — the motivating scenario:** one device op sets `mge-0/0/0` and
  `mge-0/0/1` to `default`; assert each affected vlan's segmentation/blackhole
  finding names the responsible port, and any pre-existing `exit_unlocatable`
  carries no cause.
- **Non-load-bearing golden:** same plan, assert decision/severity/coverage are
  byte-identical to the pre-feature verdict (attribution changed only evidence).
- **Live (read-only):** re-run the existing 8 single-site plans; verdicts
  unchanged, and the multi-target plan now shows port-level causes.
- Gate unchanged: `uv run pytest tests -q && uv run ruff check . && uv run mypy src`.

## Phasing (for the implementation plan)

1. **Contract + plumbing:** `Cause`, `Finding.caused_by`, the `CheckContext.delta_index`
   field, extend `subjects.py:name_findings` (incl. nested `client_impact`
   causes), render (human + dict), the non-load-bearing golden. Lands with empty
   `caused_by` everywhere (no behavior change yet).
2. **Shared helper:** `analysis/delta_cause.py` (`delta_index` + the five Family-2
   mapping rules), built once from `ctx.diff` and carried on `CheckContext`; each
   rule unit-tested in isolation against constructed graphs (incl. the
   "ambiguous → `()`" cases).
3. **Family 1 wiring:** the leaf-local checks set `caused_by` from the index
   (mechanical, near-free).
4. **Family 2 wiring:** the five graph-effect rules (vlan-cut, physical severance,
   added-member, loop, stp-root) + `client_impact`'s per-impact/union shape; the
   motivating golden.
5. **Adapter/dynamic-gate findings** (with the delta/parity rule) + docs/roadmap/
   memory + live verify.

## Resolved decisions (from spec review)

- **`fields` are IR-normalized, not raw config paths** (P1a). The Family-1 table
  and `Cause` docstring use IR field names; raw-path `fields` are deferred (needs
  a field-gate change-path index, shared with the future differential mode).
- **Adapter findings obey the same delta/parity rule** (P1b): attribute only when
  the offending value changed; an unchanged malformed baseline → `caused_by=()`.
- **L0 schema findings are excluded** from `caused_by` in this work (P1b).
- **`client_impact` stays one aggregate finding** (P2a): per-impact causes nested
  in `evidence["impacts"]`, top-level `caused_by` = their deduplicated union;
  `name_findings` resolves the nested refs too (review round 2).
- **Family 2 has five distinct mapping rules** (P2b + round 2): vlan-graph cut,
  physical L2 severance, added-member stranding, loop arming, **and STP root
  move** — not one carriage rule.
- **`stp_root` is a hybrid, not device-only** (round 2): its root election runs
  over a connected component, so attribution is priority-change → device,
  else topology-change → port/link.
- **`DeltaIndex` lives on `CheckContext`, not `AnalysisContext`** (round 2):
  `CheckContext` is the only context that owns the `diff`.

## Open questions / risks

- **Family-2 mapping precision.** The five cut/arm/root mappings are the only
  non-trivial pieces; the honesty rule (emit `()` when unsure) bounds the risk to
  "sometimes silent," never "wrong." The plan must TDD each mapping against
  constructed graphs before wiring the checks.
- **`fields` for add/remove deltas.** An added/removed port or l3intf has no
  "changed field"; `fields=()` is acceptable (the `ref` alone is the cause).
- **Name resolution for device causes.** Devices have no IR name → cause shows
  the MAC/id, consistent with subject handling; acceptable.
