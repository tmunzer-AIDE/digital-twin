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
    changed leaf field name(s) on it (e.g. ("usage",), ("vlan_id",), ("enabled",))."""
    ref: ObjectRef                 # kind/id/name of the CHANGED entity
    fields: tuple[str, ...] = ()   # which leaf field(s) changed (may be empty for add/remove)

@dataclass(frozen=True)
class Finding:
    ...
    caused_by: tuple[Cause, ...] = ()   # changed entities responsible (delta-attributed only)
```

`caused_by` is distinct from the existing fields:
- `subject` — the **symptom** object (the affected vlan/device); unchanged.
- `affected_entities` — *all* involved IR ids; unchanged.
- `caused_by` — the **changed** subset that *produced* this finding. New.

`Cause.ref` reuses `ObjectRef(kind, id, name)`. Name resolution is centralized
exactly like subject names: `checks/subjects.py:name_findings` is extended to
resolve friendly names for `caused_by` refs too (ports/vlans/links have IR names;
devices resolve to `name=None` → renderers show the id/MAC, per the PR #3
decision that `Device.model` is not a name).

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

The helper lives in `analysis/` (pure, memoized per run on `AnalysisContext`),
not in any single check, so all checks share one definition of "what changed."

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

### Family 1 — leaf/port/device-local (≈11 checks): direct

These checks already iterate the changed entity and emit a finding whose subject
is (or maps 1:1 to) that entity. The cause is the same entity + its changed
field — built directly from the `DeltaIndex`, no graph analysis:

| Check | Cause `ref.kind` | typical `fields` |
|---|---|---|
| `mtu_mismatch` | link / port | `mtu` |
| `native_mismatch` | link / port | `native_vlan` / port usage leaf |
| `stp_edge` | port | `stp_edge` |
| `poe_disconnect` | port | `poe_disabled` / `usage` |
| `stp_root` | device | `stp_config.bridge_priority` |
| `ospf_withdrawal` | device / l3intf | `ospf_config.enabled`, `…passive` |
| `dhcp_path` | dhcp_scope / device | `type`, `servers` |
| `snooping` | device | `dhcp_snooping.*` |
| `scope_lint` | dhcp_scope | `ip_start` / `gateway` / … |
| `gateway_gap` | l3intf / vlan | removed l3intf (add/remove → empty `fields`) |
| `link_boundary` | link / port | usage leaf |

### Family 2 — graph-effect, many-to-one (5 checks): cut mapping

`l2_blackhole`, `l2_vlan_segmentation`, `l2_isolation`, `l2_loop`,
`client_impact`. The finding is about a vlan/component/cycle/client; the cause is
a **set** of changed ports/links. Mapping rule, computed by the shared helper:

- For a partitioned / exit-losing vlan `vid` over affected component `C`: the
  cause set is the delta-changed ports and links **incident to C's nodes whose
  change altered `vid`'s carriage** — i.e. edges that carried `vid` in the
  baseline vlan-graph and no longer do, mapped to their backing port(s) (L2
  edges are port-derived), filtered to those present in the `DeltaIndex`.
- For `l2_loop`: the cause is the delta entity that *armed* the cycle — an added
  edge in the cycle, or a port whose `stp_*` change unblocked it — drawn from
  `cycle.member_ports ∩ delta`.
- For `client_impact`: per affected client, the cause is the changed port/link on
  the client's own attachment path (the check already enumerates per-client
  impacts; attribution attaches to each).

Because a cut can legitimately require more than one removed edge, and one trunk
going to `default` can drop several vlans at once, `caused_by` for Family 2 is a
**set** and may name multiple ports — that is correct, not a defect. (The
many-vlans-one-port readability problem is what the deferred cause-first
rendering solves; it does not block this spec.)

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
`scope.dhcp.range_unresolved` already carry a device/dhcp_scope subject and are
inherently single-leaf; they set `caused_by` inline at construction (they bypass
the registry resolver, same as they already do for subject names).

### Parity note

The UNKNOWN/rejection paths (`derived_gate`, `dhcp_screen`,
`device_profile_gate`) already name the offending leaf path in their reasons.
This work brings the SAFE/REVIEW/UNSAFE *check* findings up to the same
leaf-level attributability the UNKNOWN path already has — making the whole output
uniformly "here is the thing you changed that caused this."

## Rendering

- **Human (`render_human` / `render_org_human`):** append a cause clause to the
  existing line. Today: `on <kind> "<name>" at <path>: <msg>`. With cause:
  `… <msg> (caused by port "mge-0/0/0" [usage])`; multiple →
  `(caused by mge-0/0/0 [usage], mge-0/0/1 [usage])`. No clause when
  `caused_by=()` (pre-existing or unattributable).
- **Dict (`verdict_to_dict`):** each finding gains a `caused_by` array of
  `{ref: {kind,id,name}, fields: [...]}`. Additive; existing keys unchanged.

## Impact on verdict / contracts

- `Finding` gains one trailing-defaulted field → back-compatible; all existing
  construction sites compile unchanged.
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

1. **Contract + plumbing:** `Cause`, `Finding.caused_by`, extend
   `subjects.py:name_findings`, render (human + dict), the non-load-bearing
   golden. Lands with empty `caused_by` everywhere (no behavior change yet).
2. **Shared helper:** `analysis/delta_cause.py` (`delta_index` + the Family-2 cut
   mapping), memoized on `AnalysisContext`, fully unit-tested in isolation.
3. **Family 1 wiring:** the ≈11 leaf-local checks set `caused_by` from the index
   (mechanical, near-free).
4. **Family 2 wiring:** the 5 graph-effect checks set `caused_by` via the cut
   mapping; the motivating golden.
5. **Adapter/dynamic-gate findings** + docs/roadmap/memory + live verify.

## Open questions / risks

- **Family-2 cut precision.** The cut mapping is the only non-trivial piece; the
  honesty rule (emit `()` when unsure) bounds the risk to "sometimes silent,"
  never "wrong." The plan should TDD the mapping against constructed graphs
  before wiring the checks.
- **`fields` for add/remove deltas.** An added/removed port or l3intf has no
  "changed field"; `fields=()` is acceptable (the `ref` alone is the cause).
- **Name resolution for device causes.** Devices have no IR name → cause shows
  the MAC/id, consistent with subject handling; acceptable.
