# Network Digital Twin — Design (Milestone 1)

## Context

We want a **Digital Twin** of a Mist customer's network that lets an AI agent propose a
configuration change and learn, *before* anything is applied, whether that change would break
the network — and **why**, with explicit evidence and confidence.

A prior implementation exists (`/Users/tmunzer/4_dev/mist_automation`, specs under
`docs/superpowers/specs/2026-04-*`). It had good ideas but was **fused to its host platform**:
checks read Mist JSON directly and depended on platform internals (backup DB, telemetry cache,
IA sessions). That coupling made every check carry both Mist quirks and platform complexity,
and is the most likely reason it did not work as expected.

This project is a **fresh, standalone, modular** twin. Two ideas are carried over deliberately:

1. **Config compiler** — derive the effective per-device config through Mist's template
   inheritance chain. This is where most real conflicts live.
2. **Layered checks with explicit severity + remediation hints** returning structured results.

Everything else is redesigned around a **vendor-neutral intermediate representation (IR)** so the
system is modular and can ingest additional sources (e.g. Aruba Central) later.

### Goal (product)

An AI agent proposes a change as a **`ChangePlan`** — an envelope around the ordered ops, not a bare
array. The envelope carries `source` (which vendor adapter owns these ops), `scope` (so `device`
ops don't need their site guessed), an optional `intent`, and the `ops` list:

```python
ChangePlan = {
  "source": "mist",                       # selects the VendorAdapter; "aruba" later
  "scope":  { "org_id": "...", "site_id": "..." },   # explicit — no guessing a device's site
  "intent": "optional free-text rationale, for logging/explanation only",
  "ops": [ ChangeOp, ... ],
}
ChangeOp = { "action": "update", "order": 0, "object_type": "...", "object_id": "...", "payload": {...} }
```

**Product-vision example** (multi-object, org templates — *not* all supported in M1; see
*Supported delta types*):

```json
{ "source": "mist", "scope": {"org_id": "o1"}, "ops": [
  {"action": "update", "order": 0, "object_type": "switchtemplate", "object_id": "xxxx", "payload": { } },
  {"action": "update", "order": 1, "object_type": "gatewaytemplate", "object_id": "yyyy", "payload": { } }
]}
```

**M1-valid example** (single site, switch-relevant — what the MVP actually simulates):

```json
{ "source": "mist", "scope": {"org_id": "o1", "site_id": "s1"}, "ops": [
  {"action": "update", "order": 0, "object_type": "site_setting", "object_id": "s1", "payload": { } },
  {"action": "update", "order": 1, "object_type": "device", "object_id": "<switch_device_id>", "payload": { } }
]}
```

The twin **simulates** it against the current network state and returns a **verdict document**: a
top-level agent-facing **`decision`** (`SAFE | REVIEW | UNSAFE | UNKNOWN`), plus per-check evidence,
overall severity, a coverage map, a confidence summary, and the IR diff the change produced. The
twin has **no side effects** — applying changes is a separate module, out of scope here.

---

## Scope

### Milestone 1 (this spec)

The narrowest slice that proves the full loop end-to-end and is still genuinely useful.

- **Single site.** Deltas scoped to one site. **No org-template fan-out** yet.
- **Switch-only compile.** We compile switch effective config. We do **not** compile gateway or
  AP/WLAN config.
- **Wi-Fi-aware impact, not Wi-Fi simulation.** APs are modeled as L2 leaf nodes (attached to a
  switch port via LLDP); wireless clients are modeled as leaf clients whose VLAN is read from
  Mist **live client data** (no WLAN compile needed). The delta is still a wired/switch change;
  we extend only the *impact surface* to include downstream wireless clients.
- **Fully-managed sites are the target, but completeness is verified at runtime — never assumed.**
  M1 is designed for sites where all forwarding devices are Mist/Junos with two-sided LLDP. We do
  **not** assume that holds: ingest records per-fact provenance, and any data gap (one-sided LLDP,
  missing neighbor, unfetched device) lowers confidence/coverage and pushes affected checks to
  `INSUFFICIENT_DATA` → `REVIEW`, never a silent `SAFE` (this is exactly GS6).
- **On-demand data.** Each simulate call fetches current Mist state for the affected scope,
  builds the IR fresh, applies the delta in memory, runs checks, discards.
- **Drivers:** CLI (for testing) + MCP tool (for the agent loop).
- **~4 topology checks (L2):** `l2.loop`, `l2.blackhole`, `l2.vlan_segmentation`, `client.impact`.
- **Thin schema validation (L0):** basic structural validation of the delta payload against the
  Mist OAS (types, enums, required, machine-readable conditionals), plus the two-source verdict
  plumbing. Deterministic, HIGH confidence.

### Supported delta types (M1) — the honest-decision boundary

The product vision allows any Mist `object_type` (see the example in *Goal*). **M1 supports only a
whitelisted subset, gated at two levels** — object type *and* the specific fields changed — and
anything outside it is rejected loudly rather than silently passed.

**Supported object types (exact strings, with `object_id` meaning):**

| `object_type` | `object_id` is | Mist endpoint | M1 |
|---|---|---|---|
| `site_setting` | the `site_id` | `PUT /sites/{site_id}/setting` | ✅ (subject to field gate below) |
| `device` (role=`switch` only) | the switch `device_id` | `PUT /sites/{site_id}/devices/{device_id}` | ✅ (subject to field gate below) |
| `switchtemplate`, `gatewaytemplate`, `aptemplate`, `networktemplate`, `rftemplate`, `sitetemplate` | org template id | org-level | ⛔ `UNSUPPORTED` (multi-site fan-out) |
| any gateway/AP `device`, WLAN, or other object | — | — | ⛔ `UNSUPPORTED` (not compiled/modeled in M1) |

**Field gate — two default-deny stages (raw pre-screen + post-compile derived-impact).** Because a
delta is a *full-object replacement*, a single op can change fields M1 cannot reason about — either
directly, or *indirectly* via `vars` substitution that ripples through the compiler into an
out-of-scope effective field. So the field gate runs at **two** points:

1. **Raw pre-screen** (`ScopeResolver.post`, pipeline step 4) — diff `payload` vs current raw and
   match changed paths against the raw allowlist below. Cheap; catches obvious out-of-scope edits
   before we bother compiling.
2. **Derived-impact gate** (post-compile) — diff the **compiler's full effective config**
   (`effective` vs `effective'`), **not** the IR. This matters: the IR is a *projection* of only the
   in-scope fields, so an out-of-scope effective field (e.g. `dhcpd_config`) **never enters the IR**
   and `IRDiff` can't see it. The compiler therefore retains the *full* effective config alongside
   the IR projection; the gate diffs that full config and **if any effective field outside the
   in-scope set differs → `UNKNOWN`**, even when the raw change looked in-scope. This is what makes
   `vars` safe to allow: a var edit that compiles into a `dhcpd_config` change is caught here.

   *(Two artifacts from compile: the **full effective config** — all fields, consumed by this gate —
   and the **IR** — the in-scope projection, consumed by representations/analysis/checks.)*

**In-scope raw paths (authoritative M1 allowlist — named subtrees, leaf-tightened):**

| `object_type` | In-scope changed paths | Notes |
|---|---|---|
| `site_setting` | `networks.*`, `port_usages.*`, `vars.*` | `vars` allowed *only* because stage-2 catches ripple |
| `device` (switch) | `port_config.*`, `networks.*`, `port_usages.*`, `name`, `notes` | `ip_config` **excluded** in M1 (mgmt/L3 fields out of scope); the switch IRB exit is read from `networks` |

**In-scope effective fields (stage-2 gate):** the compiled `networks`/`vlans`, `port_usages`, and
switch `port_config` that feed the L2 / per-VLAN / exit model. Any other effective field differing
between `IR` and `IR'` → `UNKNOWN`. (Exact leaf paths are pinned against the OAS during build — see
Open items.)

**Explicitly out-of-scope → `UNKNOWN`** (default-deny catches the rest): `*.dhcpd_config.*` (DHCP),
`wlan*`, `*radio*`/`*rf*`, `gateway*`/`vpn*`/`tunnel*` (WAN), `*.acl*`/`*nac*`/`*auth*` (security),
`ip_config.*`, and any path/effective-field absent from the allowlists above.

**Gating outcomes (all carry an `UNSUPPORTED` reason; never a green verdict):**
- Unwhitelisted `object_type`, or fan-out beyond the one in-scope site → `UNKNOWN` (**pre-fetch**).
- A `device` whose **actual role** is gateway/AP → `UNKNOWN` (**post-fetch** — role is only known
  once the device is fetched from inventory).
- Any changed raw path outside the allowlist → `UNKNOWN` (**post-fetch** raw pre-screen).
- Any out-of-scope **effective** field changed → `UNKNOWN` (**post-compile** derived-impact gate).

### Delta semantics (resolve before `apply` and L0)

A delta op is **a full-object replacement**, matching Mist's `PUT` semantics: `payload` is the
*complete* new object, not a JSON-merge-patch or partial. Consequences:
- `apply` **replaces** the raw object in state with `payload` (it does not merge fields).
- L0 `required`/conditional validation runs against the **full** object.
- **Do not conflate this with inheritance merge.** Object-replacement (`apply`) and
  template-inheritance derivation (`compiler`) are separate steps: `apply` swaps the raw object,
  then `compiler` re-derives the effective config from the changed raw state.
- `action` values in M1: `update` (replace existing object). `create`/`delete` are out of M1.

**Multi-op semantics (ordered).** `ops` apply in strictly increasing `order` against a **rolling raw
state**: op *N* sees the raw state as already modified by ops `0..N-1`, and its changed-field gate
diffs against that rolling pre-op state (not the original fetched state). Constraints:
- `order` must be a **total order with unique values**; gaps are fine, duplicates → `UNKNOWN`.
- **Two ops targeting the same `(object_type, object_id)` → `UNKNOWN`** (ambiguous: because each op
  is a *full-object replacement*, a later op would silently make an earlier one dead — almost
  certainly an authoring error). One op per object in M1.
- The proposed IR is compiled once from the final rolling `raw'` (after all ops applied).

### Explicit non-goals (deferred, behind existing seams)

- Apply/actuation of changes (separate module).
- Gateway and AP/WLAN config compilation.
- Simulating Wi-Fi config deltas (SSID→VLAN change, PSK rotation, RF/airtime).
- Org-template fan-out across many sites.
- Deep L3 / route computation (OSPF/BGP/forwarding). **No Batfish, no Junos-CLI translation in
  Milestone 1** — loop/blackhole/client-impact are pure graph reasoning on the IR.
- WAN/SDWAN/SASE, NAC, firewall/ACL, DHCP/RadSec service checks.
- **L1 semantic / conditional config rules** ("xyz required if abc==true" when the constraint is
  prose rather than machine-readable in the OAS). Deferred — assessing how much of Mist's OAS
  encodes conditionals machine-readably vs in prose is its own investigation, kept off the M1
  critical path. The *declarative rule layer* that will host these is designed-for below.
- **L3 requirement/reachability rules** (e.g. "AP↔Mist Edge on UDP 500/4500 if the Mx tunnel uses
  IPSec"). Deferred — these need IR domains (WAN, Mist Edge, security, L3 reachability) that M1
  does not populate. Such a rule is *expressible* today and self-reports `INSUFFICIENT_DATA`.
- Snapshot/persisted state store, web UI.
- A second vendor adapter (Aruba). Only the *seams* for it are built now.

---

## Architecture

### The twin is one pure function

`simulate(ChangePlan) → verdict`, no side effects on Mist. Internally, a fixed pipeline where every
stage is a swappable seam:

```
                  ┌──────────────────────────────────────────────────────────────┐
  ChangePlan ──▶  │                     SIMULATION ENGINE                         │ ──▶ verdict doc
 {source, scope,  │  1. ScopeResolver.pre   envelope + object_type + single-site  │   (decision +
  intent, ops:[   │       └─ unsupported type / fan-out → decision: UNKNOWN        │    severity +
   {action,order, │  2. Adapter.validate    L0 payload validation (vendor)        │    findings +
    object_type,  │       └─ structurally-fatal → short-circuit verdict           │    check_results +
    object_id,    │  3. StateProvider       fetch raw vendor state for scope      │    coverage map +
    payload}]}    │       └─ total failure → UNKNOWN; partial → state_meta        │    confidence +
                  │  4. ScopeResolver.post  raw field pre-screen + device-role     │    state_meta +
                  │       └─ out-of-scope raw path / wrong role → UNKNOWN          │    ir_diff + trace)
                  │  5. Adapter.ingest      raw → effective + IR (baseline)        │
                  │  6. Adapter.apply       raw + δ → raw'                         │
                  │  7. Adapter.ingest      raw' → effective' + IR' (proposed)     │
                  │  8. derived-impact gate diff(effective, effective')           │
                  │       └─ out-of-scope EFFECTIVE field changed → UNKNOWN        │
                  │  9. CheckRegistry       diff_ir(IR,IR'); run checks on ctx     │
                  │ 10. VerdictBuilder      aggregate findings → decision          │
                  └──────────────────────────────────────────────────────────────┘
```

`ScopeResolver` runs in **three checkpoints**: a **pre-fetch** gate (envelope shape, object-type
whitelist, single-site — no state needed); a **post-fetch** raw pre-screen + device-role check
(needs the fetched device/raw); and a **post-compile** derived-impact gate (step 8) that catches
changes which only become out-of-scope *after* template/var substitution. The raw field gate must
be post-fetch because "which fields changed" needs current state; the derived-impact gate must be
post-compile because `vars`/template ripple is only visible in the effective config.

The verdict has **two finding sources**, both using the same `Finding` model: the vendor adapter's
payload validation (L0, step 2) and the neutral check registry (L2/L3, step 9). Schema validation —
vendor-specific, reads raw payload — stays out of the neutral check layer, while still surfacing in
one unified verdict. **Checks at step 9 receive only the two `AnalysisContext`s (baseline +
proposed, each wrapping an IR with memoized representations/analysis) and the `IRDiff` — never the
raw Mist payload** (see the check contract).

### Seams (this is where modularity lives)

| Seam | Responsibility | Milestone 1 impl | Swappable to |
|---|---|---|---|
| `StateProvider` | raw vendor state for a scope | `MistApiProvider` (on-demand) | `SnapshotProvider` |
| `VendorAdapter` | `validate(δ)`, `ingest(raw)→IR`, `apply(raw,δ)→raw'` | `MistAdapter` (facade) | `ArubaAdapter` |
| `Ingester` | one domain: raw → IR slice; declares produced capabilities | switch ingester | gateway/wlan/wan ingesters |
| `Check` | inspect IR + analysis → findings | wired/Wi-Fi-aware checks | any new "test scope" |
| `Driver` | drive the engine | `cli` + `mcp` | `http` / `ui` |

### Two hard rules that keep it modular

1. **Checks only ever see the IR + the `IRDiff` + analysis results** (all vendor-neutral). They
   **never** see the raw Mist payload and never import vendor code. This is what lets a second
   vendor reuse every check, and lets a new check be added without touching the engine.
2. **Vendor specifics live only in the adapter.** `validate`, the per-domain ingesters, the
   compiler, and `apply` are the *only* places that know Mist's JSON shape and inheritance rules.

### The four pure layers: standardize → represent → analyze → check

Between the vendor adapter and the verdict sit four layers with a strict, one-directional flow.
Keeping them separate is what stops checks from re-deriving shared work and stops expensive,
reusable computation (reachability, forwarding) from being buried inside a single check.

```
vendor data ─► ingest+compile ─► IR ─► REPRESENTATIONS ─► ANALYSIS ─► CHECKS ─► findings
                (adapter)         │      (structural views)  (computed   (interpret
                                  │                            properties)  → severity)
                                  └─ indexes (ir/indexes.py)
```

- **Representations** (`representations/`) — pure structural *views* of the IR: the L2 graph, the
  per-VLAN graph; plus `ir/indexes.py` lookups (ports-by-device, access-ports-by-vlan,
  exits-by-vlan, clients-by-vlan). Construction only — **no algorithms, no severity.** *(Later:
  route graph, tunnel model, policy model.)*
- **Analysis** (`analysis/`) — pure *computations* over representations that produce reusable
  results: cycle detection, VLAN connected-components, path-to-exit, exit resolution. *(Later:
  reachability matrix, forwarding, Batfish output, policy evaluation.)* This is the home for a
  future reachability/forwarding engine — it is analysis, **not** a check.
- **Checks** (`checks/`) — the *only* layer that assigns severity. They read IR + `IRDiff` +
  analysis results and emit `Finding`s.

**Confidence is data and propagates; severity is interpretation and is terminal.** Representations
and analysis results both *carry* confidence (a fact about the data — e.g. a path-to-exit that
crossed a one-sided LLDP link is `LOW`). They **never** carry severity. Only a check turns
`(analysis result + confidence + diff)` into `WARN`/`FAIL`.

**`AnalysisContext` enforces "compute once, share."** Because the IR is immutable, one
`AnalysisContext(ir)` per simulation run lazily builds and **memoizes** every representation and
analysis result, and is handed to every check. Three checks asking for `vlan_graph(30)` get one
computed graph. This is the mechanism that makes "pure, cached, reusable" real rather than
aspirational.

```python
ctx = AnalysisContext(ir)
ctx.l2_graph(); ctx.vlan_graph(30)          # representations (cached)
ctx.vlan_components(30); ctx.path_to_exit(30, node)  # analysis (cached)
# l2.loop / l2.blackhole / client.impact read from ctx — never rebuild
```

### Capability supply/demand is a DAG (closes the silent-blind-spot wall)

**Capabilities are one unified, namespaced vocabulary** (`Capability` — namespaced strings). M1's
set is the IR-domain subset (`IRCapability`: `wired.l2`, `stp.state`, `clients.active`, `l3.exits`);
*producers* of analysis-derived capabilities (e.g. `analysis.reachability`) slot into the **same**
vocabulary later with no type change. Every producer (ingester/compiler, and later an analyzer) and
every consumer (representation, analyzer, check) declares `produces()` / `requires()` over this one
type — so the supply/demand graph is uniform across layers.

Capabilities flow up the layers: **ingesters/compilers *produce*** them → **representations/
analyzers *require*** them → **checks *require*** them (directly, or transitively via the analysis
they read). A check that `requires()` a capability the IR didn't populate auto-resolves to
`INSUFFICIENT_DATA` (per the gating order). `engine/capability_check.py` validates the whole chain:
**every required capability must have a producer somewhere, or be explicitly marked
not-yet-supported** — a test turns a "silently INSUFFICIENT_DATA forever" gap into a loud build
failure. Adding a domain means adding an ingester that *produces* the capability and a check that
*requires* it; the validator keeps them honest.

### Component contracts (inputs / outputs / errors)

Each component has one job and a defined failure mode. Errors are **values, not exceptions** unless
noted — they become verdict outcomes (`UNKNOWN`/`REVIEW`), never an unhandled crash.

| Component | Input | Output | On error |
|---|---|---|---|
| `scope/object_gate` (pre-fetch) | `ChangePlan` | scope + op metadata, or reject | unsupported type / fan-out → `UNKNOWN` |
| `StateProvider` | scope | raw vendor state + `state_meta` | total fetch fail → `UNKNOWN`; partial → recorded in `state_meta` |
| `scope/field_gate` (post-fetch) | `payload`, current raw | changed-path set, in/out verdict | out-of-scope raw path → `UNKNOWN` |
| `adapters/mist/validate` (L0) | `ChangeOp.payload`, OAS | L0 `Finding`s | fatal schema error → short-circuit verdict |
| `compile/registry` (+per-domain compilers) | raw + templates | **full effective config** | compile failure → `UNKNOWN` (named in reasons) |
| `ingest/registry` (+per-domain ingesters) | effective config | IR slices (in-scope projection) + produced capabilities | a domain ingester failure → `UNKNOWN` (named) |
| `adapters/mist/apply` | raw, `δ` (ordered ops) | `raw'` | unknown object/id, duplicate op → `UNKNOWN` |
| `ir/diff.diff_ir` | `IR`, `IR'` | neutral `IRDiff` (for checks) | — (pure) |
| `scope/derived_gate` (post-compile) | `effective`, `effective'`, effective allowlist | in/out verdict | out-of-scope *effective* field → `UNKNOWN` |
| `analysis/AnalysisContext` | `IR` | memoized representations + analysis (carry confidence) | missing capability → analysis absent → check `INSUFFICIENT_DATA` |
| `checks/registry` | `IRDiff` + baseline/proposed `AnalysisContext` | `list[CheckResult]` | per-check crash isolated → `CHECK_ERROR` |
| `verdict/verdict` (+`decision`) | all findings + coverage + `state_meta` | `verdict` (+ `decision`) | — (pure aggregation) |
| `ReplayStore` | `(raw, ChangePlan, verdict)` | redacted fixture on disk | write failure logged; never blocks a run |
| CLI driver | `ChangePlan` (file/stdin) | verdict JSON / human summary | distinct exit code per decision (below) |
| MCP tool | `ChangePlan` (tool args) | verdict JSON | returns verdict; tool itself never throws to the agent |

**CLI exit codes** — only `SAFE` is success, because `REVIEW`/`UNSAFE`/`UNKNOWN` all mean "do not
apply automatically": `SAFE` → `0`, `REVIEW` → `10`, `UNSAFE` → `20`, `UNKNOWN` → `30`. Distinct
codes let scripts/CI branch precisely.

### Compositional ingest (cross-vendor enabler, cheap now)

Ingest is compositional, not monolithic. Each adapter contributes entities into the same IR; a
`merge/reconcile` step stitches them on **vendor-neutral identity** (MAC, LLDP system-name,
subnet — never a vendor `object_id`). In Milestone 1 there is one adapter, so reconcile is a
pass-through. The `ChangePlan.source` field (explicit, e.g. `"mist"`) selects the owning adapter, so
a delta-router can later route each op to the right adapter's `apply`. This is the only "future"
work done now, and it is one field plus a no-op step.

### Module layout

Decomposed deliberately into small, single-responsibility modules — **no god-modules.** Each module
has one reason to change; protocols sit at every seam; pure layers never import effectful code; data
(allowlists, rule catalogs, OAS) is separated from logic. `*later*` = slot only, not built in M1.

```
src/digital_twin/
├── contracts/                  # cross-cutting value types (pure) — imported by everyone, imports nobody
│   ├── change_plan.py          # ChangePlan, ChangeOp
│   └── finding.py              # Finding, Severity, category(network|operational)  ← shared result DTO
├── ir/                         # vendor-neutral model — PURE, depends on nothing
│   ├── confidence.py           # Confidence, min_confidence
│   ├── provenance.py           # Provenance, FactMeta, canonical provenance→confidence table
│   ├── capabilities.py         # IRCapability enum (vocabulary only)
│   ├── entities.py             # Device/Port/Link/Vlan/L3Intf/Client + id helpers
│   ├── model.py                # IR, validating IRBuilder
│   ├── indexes.py              # ports-by-device, access-ports-by-vlan, exits-by-vlan, clients-by-*
│   └── diff.py                 # EntityRef, IRDiff, diff_ir
├── representations/            # structural VIEWS over IR — PURE, no algorithms, no severity
│   ├── l2_graph.py             # build_l2_graph (port-derived edges, bundle collapse, VC fold)
│   └── vlan_graph.py           # build_vlan_graph (participating nodes)   (later: route_graph, tunnel_model, policy_model)
├── analysis/                   # property COMPUTATIONS over representations — PURE, carry confidence, no severity
│   ├── context.py              # AnalysisContext (memoizes representations + analysis per run)
│   ├── cycles.py               # cycle detection on per-VLAN graph
│   ├── vlan_reachability.py    # connected components, path-to-exit
│   └── exits.py                # VLAN-exit resolution (IRB → boundary uplink → none)   (later: reachability, forwarding, batfish, policy_eval)
├── checks/                     # INTERPRET analysis → findings — the ONLY layer with severity
│   ├── base.py                 # Check protocol, CheckContext, CheckResult, Coverage (imports contracts.finding)
│   ├── registry.py             # discovery, gating order (applies_to→requires), isolation
│   └── wired/                  # l2_loop.py, l2_blackhole.py, l2_vlan_segmentation.py, client_impact.py
├── rules/                      # declarative L1/L3 (later) — data-driven, not code-per-rule
│   ├── engine.py               # evaluate "when <cond> then <require>"
│   ├── primitives.py           # require/reachable primitives (query analysis layer)
│   └── catalog/                # rule DEFINITIONS (data)
├── verdict/                    # findings → decision — PURE (imports contracts.finding + checks.base; never imported by them)
│   ├── coverage.py             # per-domain coverage rollup
│   ├── confidence_summary.py   # confidence rollup
│   ├── state_meta.py           # freshness (acquired_at, per-source age, fetch_failures, region)
│   ├── decision.py             # SAFE|REVIEW|UNSAFE|UNKNOWN + precedence (pure function)
│   └── verdict.py              # Verdict assembly
├── scope/                      # the gates — one module per gate
│   ├── envelope.py             # ChangePlan shape validation
│   ├── object_gate.py          # pre-fetch: object_type whitelist + fan-out
│   ├── field_gate.py           # post-fetch: raw changed-path allowlist
│   ├── derived_gate.py         # post-compile: effective-field allowlist
│   └── allowlist.py            # the allowlist DATA (raw + effective paths)
├── providers/                  # state fetching — EFFECTFUL
│   ├── base.py                 # StateProvider protocol, RawState, StateMeta
│   └── mist_api.py             # MistApiProvider (on-demand)   (later: snapshot.py)
├── adapters/                   # vendor specifics — split hard (the #1 god-module risk)
│   ├── base.py                 # VendorAdapter protocol (validate, ingest, apply)
│   └── mist/
│       ├── adapter.py          # thin FACADE wiring the pieces below
│       ├── validate/           # oas.py (load/cache OAS), schema.py (L0 structural → findings)
│       ├── ingest/             # base.py (Ingester protocol), registry.py, switch.py, lldp.py, clients.py  (later: gateway/wlan/wan)
│       ├── compile/            # registry.py, switch.py, merge.py, vars.py, equivalence.py
│       └── apply/              # apply.py (full-object replace, ordered rolling state), objects.py (per-object_type targeting)
├── engine/                     # ORCHESTRATION ONLY — no business logic
│   ├── pipeline.py             # the 10-stage sequence (thin; delegates to every module)
│   ├── run_context.py          # run_id, trace handle, state_meta accumulation
│   └── capability_check.py     # supply/demand DAG validation across registries
├── observability/              # EFFECTFUL
│   ├── trace.py                # per-run structured trace
│   ├── logging.py              # structured logging setup
│   └── replay/                 # store.py (raw, ChangePlan, verdict), redaction.py (pseudonymize + strip secrets)
└── drivers/
    ├── render.py               # verdict → human/JSON (shared)
    ├── cli.py                  # ChangePlan in → verdict + exit codes
    └── mcp_server.py           # MCP tool
```

**Dependency direction (a clean DAG, no cycles):**
`ir → representations → analysis → checks → verdict`; `scope → {ir, contracts}`;
`checks → {analysis, contracts}` (emit `contracts.Finding`); `verdict → {contracts, checks}` (one
direction — verdict is never imported by checks, so no cycle); `scope → {ir, contracts}`;
`adapters → {ir, contracts}` (L0 findings are `contracts.Finding`); `providers → contracts`;
`rules → {analysis, contracts}`; `engine → everything (wires only)`; `observability/drivers` consume
`{contracts, verdict}`. The pure core (`ir/representations/analysis/checks/verdict`) never imports
effectful code; `contracts` imports nobody.

The central lesson from the prior project: the vendor-specific hard 80% (`validate`/per-domain
`ingest`/`compile`/`apply`) is split into many small modules behind the `MistAdapter` facade — adding
a *gateway* domain is dropping in `ingest/gateway.py` + `compile/gateway.py`, nothing else changes.

### Stack

Python. The difficulty is the network modeling; `networkx` (graph/VLAN/loop analysis) and
`netaddr` (IP/subnet) are the best fit, plus a solid MCP Python SDK and existing Mist tooling.

---

## The Intermediate Representation (IR)

A **typed domain model** (source of truth) plus **derived graphs** (computed views the checks
consume). Snapshots are **immutable**: `ingest` yields a frozen baseline IR, `apply`+`ingest`
yields a frozen proposed IR. Nothing mutates in place — a run is reproducible from `(raw, δ)`.

### Entities (Milestone 1 — namespaced so new domains bolt on without touching these)

Every entity carries a `FactMeta` (provenance + confidence). The entity `meta` covers the entity's
facts when they share a source; a field that comes from a *different* source than the entity's
config gets its **own** `*_meta` field (M1's one such case: `Port.stp_meta`, a live fact).

```
core domain (Milestone 1):
  Device    id, role(switch|gateway|ap|mistedge), model, site, vc_members, meta
  Port      id, device_id, name, mode(access|trunk), speed, poe, profile, native_vlan,
            tagged_vlans, stp_enabled, stp_mode, stp_state, stp_meta(FactMeta|None), meta
  Link      id, a_port, b_port, kind(physical|lag|mclag|vc), bundle_id, meta
            #  ^ no source/bidirectional: provenance (incl. one-sided LLDP) lives in meta;
            #    bundle_id identifies a LAG/MCLAG bundle for correct edge collapse
  Vlan      vlan_id, name, scope, meta
  L3Intf    id(auto), device_id, role(irb|svi|wan|loopback), vlan_id|port, subnet, ip, meta
  Client    id(=mac), mac, kind(wired|wireless), attach_kind(port|ap), attach_id, vlan, ip, active, meta

future domains (added later — additive; existing entities/checks untouched, see Extensibility):
  wan/    Tunnel, Peer, Path        policy/  Acl, FwRule, Service
  nac/    AuthPolicy, AuthServer    svc/     DhcpServer, RadSec
  routing/ OspfAdj, BgpPeer, StpInfo, IpsecSa
```

### Derived graphs / representations (built once per IR, memoized by `AnalysisContext`)

These are **representations** (layer 2). M1 uses a **device-level** L2 graph whose **edges are
derived from specific ports** (see *L2 topology check semantics*) — so a port-level config change
changes its edge and is detected.

- **L2 graph** — **device nodes**; edges are logical links carrying `member_ports`/`bundle_id`/
  carried-VLANs/confidence. LAG/MCLAG collapse by `bundle_id`; independent links (and independent
  bundles) stay **parallel** edges (redundancy = cycle); VC fabric folds into one node.
- **Per-VLAN graph** — subgraph restricted to edges that *carry* that VLAN, including only
  participating nodes (carrying / member access-port / exit), annotated `access_ports`/`exits`.
  Input to **loop** and **blackhole** *analysis* (layer 3); a graph cycle is **not** a loop by itself.
- **VLAN-exit / index lookups** (`ir/indexes.py`) — access-ports-by-vlan, exits-by-vlan, etc.

### Three properties that make the IR trustworthy and diffable

1. **Every fact carries provenance + confidence** via `FactMeta`. A `Link` whose `meta.provenance`
   is one-sided LLDP is `LOW`; a `Port.stp_meta` of `None` means STP unknown. Analysis composes
   confidence (MIN) over the specific facts it relied on; only checks turn it into severity.
2. **Stable identity.** Every entity has a stable `id` from stable keys (`device.mac + port.name`,
   etc.) — never a vendor `object_id` — so baseline/proposed entities line up, `IRDiff` is a clean
   structural delta, and `IRBuilder` rejects duplicate ids and dangling refs.
3. **Immutable snapshots.** Frozen IRs make runs reproducible and replayable.

### Extensibility (additive, not zero-touch — be honest about the seams)

Adding a domain (WAN/NAC/routing) is **additive**: a new `ir/entities_<domain>.py`, a new collection
on `IR` + `add_*`/dup-check on `IRBuilder`, a new `ir/indexes` module, and a new `representations/`
view. It is **not literally zero-change** to the `IR` container — that container grows by one
collection per domain — but the change is mechanical and bounded:

- **Existing entities and existing checks are never modified or broken.** A WAN check asks the IR
  for `Tunnel`/`BgpPeer` facts a wired-only ingest didn't populate; it self-gates to
  `INSUFFICIENT_DATA` via `requires()` when absent.
- **`diff_ir` extends by one line** — entity kinds are a declared registry, not hard-coded in the
  diff body, so a new kind is one entry.

This honest framing (vs "zero change") is deliberate: a generic untyped entity registry was
considered and rejected — it would trade the typed `ir.ports`/`ir.links` accessors (which the checks
and indexes rely on) for dynamic lookups, a bad trade for the small cost of a per-domain collection.

### IR versioning & capability negotiation

So growth (Aruba, L3, NAC) is additive and never silently breaks a check, every IR snapshot carries:

```python
class IR:
    ir_version: str                # semver of the IR schema; engine rejects an incompatible major
    capabilities: set[IRCapability]   # what THIS instance actually populated, e.g.
                                   #   {wired.l2, stp.state, clients.active, l3.exits}
                                   #   (a wired-only ingest does NOT include {wan, nac, l3.routes})
    ...
```

- A check's `requires()` is matched against `ir.capabilities`. Missing capability →
  `INSUFFICIENT_DATA` (per gating order) — **never a crash, never a false `PASS`**.
- New domains **add** capabilities; existing checks declare only the capabilities they use, so they
  are unaffected by additions. New checks declare the new capabilities and self-gate where absent.
- `ir_version` lets the engine fail fast if an adapter emits an IR shape it can't consume, rather
  than producing garbage findings. Capabilities are the *runtime* contract; `ir_version` is the
  *schema* contract.

---

## The check-plugin contract

A check is a self-contained, self-registering plugin. The contract *is* the no-silent-OK guarantee.

```python
class Check(Protocol):
    id: str                  # "wired.l2.loop"
    title: str
    domain: str              # "wired.l2" — groups in the verdict
    default_severity: Severity

    def requires(self) -> list[Capability]:
        # capabilities this check needs (M1: IRCapability subset — STP_STATE, CLIENTS_ACTIVE, ...)
        ...
    def applies_to(self, diff: IRDiff) -> bool:
        # cheap predicate over the neutral change set: does this touch what I reason about?
        ...
    def run(self, ctx: CheckContext) -> CheckResult:
        ...
```

`CheckContext` provides exactly: `baseline` and `proposed` (two `AnalysisContext`s — each wraps an
immutable IR and lazily memoizes its representations + analysis results), the neutral `IRDiff`, and a
logger bound to `(run_id, check_id)`. **No raw vendor payload, nothing vendor-specific.** A check
reads analysis from the context (`ctx.proposed.vlan_components(30)`) — never rebuilding shared work —
and reads `IRDiff` to know *what kind* of change occurred. Severity is assigned here and only here;
the analysis it consumed carried confidence but no severity.

### Result is evidence, never a bare boolean

Two distinct vocabularies: **Status** (did the check reach a conclusion?) and **Severity** (how bad
is an individual finding?). They are linked but not the same field.

```python
Status   = PASS | WARN | FAIL | NOT_APPLICABLE | INSUFFICIENT_DATA | CHECK_ERROR  # check-level outcome
Severity = INFO | WARNING | ERROR | CRITICAL                                       # per-finding

class Finding:
    source: "adapter" | "check"           # L0/L1 adapter validation, or an L2/L3 check
    category: "network" | "operational"   # network = predicted breakage; operational = the twin itself had trouble
    code: str                             # stable machine code, e.g. "l2.blackhole.vlan_isolated"
    severity: Severity
    confidence: Confidence
    message: str                          # human-readable
    affected_entities: list[str]          # IR entity ids
    evidence: dict                        # facts inspected + reasoning data (what made this fire)
    remediation: str | None               # suggested fix, for the agent

class CheckResult:
    check_id: str
    status: Status
    findings: list[Finding]
    coverage: Coverage             # breadth: what I evaluated vs. couldn't
    confidence: Confidence         # soundness: how much I trust the conclusion
    reasoning: str
```

Two important separations:

- **Status vs Severity.** A `PASS` check may still emit `INFO` findings (e.g. a benign VLAN
  expansion); `WARN` ⇔ worst finding is `WARNING`; `FAIL` ⇔ a `network` finding at `ERROR`/`CRITICAL`.
  **A check emits `FAIL` only at HIGH confidence** — when not confident enough to assert breakage it
  downgrades to `WARN` or `INSUFFICIENT_DATA`, never a silent stretch to `FAIL` or `PASS`. This keeps
  `FAIL → UNSAFE` an always-confident assertion (and resolves the boundary-uplink case below: an
  only-*inferred* exit yields `WARN`, not `FAIL`).
- **Network vs operational.** `Severity.ERROR`/`CRITICAL` describe *predicted network breakage* and
  drive `UNSAFE`. A check that **crashes** is a different thing — status `CHECK_ERROR`, emitting an
  `operational`-category finding. **Operational findings never drive `UNSAFE`; they drive `REVIEW`**
  (the twin couldn't evaluate, so a human/agent must look — it is not evidence the *network* breaks).

### The six check statuses are the whole anti-silent-OK machine

| status | meaning | verdict treatment |
|---|---|---|
| `PASS` | evaluated, clean | green |
| `WARN`/`FAIL` | evaluated, found something | severity-coded with findings |
| `NOT_APPLICABLE` | delta doesn't touch my domain | legitimately silent |
| `INSUFFICIENT_DATA` | delta **does** touch my domain but I lacked IR facts to judge | **surfaced as a coverage gap — never folded into "OK"** |
| `CHECK_ERROR` | the check crashed (operational, not network) | isolated; emits an `operational` finding → `REVIEW`, **never** `UNSAFE` |

The `INSUFFICIENT_DATA` vs `NOT_APPLICABLE` split is what makes "why did it say OK" answerable.

### Coverage vs Confidence — two distinct axes

- **Coverage** = *did I look at it?* (breadth: "evaluated 412/418 trunk links").
- **Confidence** = *how much do I trust what I concluded?* (soundness).

```python
class Confidence:
    level: HIGH | MEDIUM | LOW
    reasons: list[str]   # ["link wan-core↔dist1 one-sided LLDP", "STP root prediction is heuristic"]
```

**Canonical fact→confidence table (single source of truth — every component uses this):**

The axis is **authority/corroboration**, not "config vs live." A device's report about *itself* is
authoritative ground truth; a single-source claim about a *relationship or another device* is weak.

| Provenance of a fact | Confidence |
|---|---|
| Explicitly configured; two-sided LLDP; operator/template-**designated**; **OR authoritative device self-report** — a device reporting *its own* state (STP mode/state, its own associated clients with their VLANs, its own port status) | `HIGH` |
| **Config-inferred but unconfirmed** (e.g. a neighbor's role guessed from model/name, no corroboration) | `MEDIUM` |
| **Single-source claim about a cross-device relationship** (one-sided LLDP link; an uncorroborated inference about another device) | `LOW` |
| Absent | → drives `INSUFFICIENT_DATA` (no confidence assigned) |

So **known STP state** (the switch reporting its own ports) and **observed wireless clients** (the AP
reporting its own associations) are `HIGH` — making GS3/GS7 honestly `UNSAFE`/`HIGH`. A one-sided
LLDP link — a claim about a relationship the other end didn't confirm — stays `LOW`. (Note: observed
clients are HIGH-*confidence* about who is connected; the *coverage* gap for not-yet-connected
clients is a separate `partial`-coverage matter, per the blackhole AP-membership rule.)

**Composition is deterministic:** a derived fact's (and a finding's) confidence =
`MIN(confidence of every fact it relied on, confidence of the inference method)`. Method confidence:
exact `networkx` cycle search = `HIGH`; documented heuristic = `MEDIUM`. So a conclusion built on a
one-sided LLDP link is `LOW` *regardless* of how exact the algorithm is — the weakest input governs.
This rule is the only one; no component may assign confidence by any other means.

Categorical + reason-backed, **never a float** (false precision undermines explainability).

### Verdict aggregation

The verdict aggregates **three independent axes**, so "all clear" can only mean *evaluated,
covered, AND high-confidence*:

```
verdict = {
  decision,            # SAFE | REVIEW | UNSAFE | UNKNOWN   ← the agent-facing contract
  decision_reasons,    # machine + human reasons (e.g. ["UNSUPPORTED object_type: switchtemplate"])
  overall_severity,    # max Severity across findings (INFO|WARNING|ERROR|CRITICAL)

  findings: [ Finding, ... ],        # flat, agent-facing — every Finding from every source
  check_results: [ CheckResult, ... ],   # per-check detail: status, coverage, confidence, reasoning

  coverage:  { "wired.l2": {evaluated, partial, insufficient, not_applicable}, ... },
  confidence_summary: { high: n, medium: n, low: n, reasons: [...] },

  state_meta: {                      # freshness — so the agent can reason about stale evidence
    state_acquired_at,               # when the on-demand fetch ran
    sources: [ { source: "mist", region, fetched_at, age_seconds }, ... ],
    fetch_failures: [ { source, object, error }, ... ],   # partial fetches → lower coverage
  },

  ir_diff,
  trace_ref,
}
```

`findings` is the flat list an agent consumes; `check_results` is the per-check audit detail. A
`Finding` always names its `source` and stable `code`, so the two views never disagree.

#### `decision` — the single field an agent acts on

Agents must not have to re-derive safety from severity × coverage × confidence themselves (they'd
do it inconsistently). The engine collapses everything into one **`decision`** with deterministic,
precedence-ordered rules (first match wins):

| Decision | Meaning for the agent | Triggered when |
|---|---|---|
| **`UNKNOWN`** | "I could not simulate this — do not apply, do not assume safe." | Any op is `UNSUPPORTED` (type/field/fan-out); a structurally-fatal L0 violation short-circuited the run; **or the state fetch failed completely** (no usable baseline). |
| **`UNSAFE`** | "This will break something — do not apply." | Any **`network`-category** finding at `ERROR`/`CRITICAL` severity (a check returned `FAIL`). Operational findings never trigger this. |
| **`REVIEW`** | "Possible issue or a blind spot — a human/agent must look before applying." | Any `WARN` finding; **or** any applicable check returned `INSUFFICIENT_DATA` or `CHECK_ERROR`; **or** any finding carries `LOW`/`MEDIUM` confidence; **or** coverage is `partial`/`insufficient` on an applicable domain (including a **relevant** partial fetch). |
| **`SAFE`** | "Fully evaluated, fully covered, high confidence, clean." | All applicable checks `PASS`, **no** finding above `INFO`, coverage complete on every applicable domain, all confidence `HIGH`. |

Precedence is strict: `UNKNOWN` > `UNSAFE` > `REVIEW` > `SAFE`. The key invariant: **a blind spot
(`INSUFFICIENT_DATA`, partial coverage, non-HIGH confidence, or a crashed check) can never resolve to
`SAFE`** — it floors at `REVIEW`. `decision_reasons` always lists the specific drivers.

**Partial-fetch rules (how `state_meta.fetch_failures` maps to a decision):**
- **Complete failure** (no usable baseline state) → `UNKNOWN`.
- **Relevant partial failure** — a fetch failed for a device/object that an applicable check needs
  → that check returns `INSUFFICIENT_DATA` → `REVIEW`. The failure is named in `decision_reasons`.
- **Irrelevant partial failure** — a fetch failed for something outside every applicable check's
  scope → recorded in `state_meta.fetch_failures` for transparency, but **does not** lower the
  decision (a check that didn't need it still has full coverage).

> Note: the separate **apply module** (out of scope here) is the only consumer allowed to act on
> `SAFE` automatically, and only behind whatever policy gate it defines. The twin only *reports*.

### Registration & isolation (the "don't break the rest" rule)

- Checks live in `checks/wired/` and self-register (decorator/entry-point). Adding one is dropping
  in a file; the engine discovers it.
- The engine runs each check **in isolation**: a raised exception → `status: CHECK_ERROR` + an
  `operational` finding, logged with its `check_id`, run continues. One bad check cannot take down
  the run or another check, and a crash never masquerades as network breakage (`REVIEW`, not `UNSAFE`).

**Gating order (strict — this ordering is itself a contract):** for each check the engine does,
in this sequence:

1. Compute `IRDiff` (`IR` vs `IR'`).
2. **`applies_to(diff)`** — if `False` → **`NOT_APPLICABLE`**, stop. *This is checked
   first*, so a cosmetic change (e.g. a `notes` edit) that touches nothing a check cares about is
   correctly `NOT_APPLICABLE`, **not** `INSUFFICIENT_DATA`.
3. Only for applicable checks: **`requires()`** vs the IR's capabilities — if unmet →
   **`INSUFFICIENT_DATA`**, do not run. A blind spot only counts against the verdict when the
   check actually *applies*.
4. Otherwise run `run(ctx)`.

---

## Validation taxonomy

All validation, regardless of layer, produces the same `Finding`/severity/coverage/confidence and
flows into one verdict. Layers differ by *what* they read and *where* they live:

| Layer | Validates | Reads | Lives in | Form | Milestone |
|---|---|---|---|---|---|
| **L0 — Schema** | payload conforms to Mist OAS (types, enums, required, machine-readable `if/then`) | delta payload | adapter (`validate`) | OAS-driven validator | **M1 (thin)** |
| **L1 — Semantic** | config rules not in raw schema; prose conditionals; value consistency | payload + config | adapter (`validate`) + `rules/` | declarative rules | deferred |
| **L2 — Topology** | loop / blackhole / segmentation / client impact | **IR only** | `checks/` | code plugins | **M1** |
| **L3 — Requirement/reachability** | config triggers a requirement, asserted against the modeled network | **IR only** | `checks/` + `rules/` | declarative rules | deferred |

**Placement rule:** L0/L1 are vendor-specific and read raw payload/config, so they live in the
**adapter**, never in the neutral `checks/` layer. L2/L3 read only the IR and are **vendor-neutral**.

**L0 (M1, thin):** structural validation against the Mist OAS — types, enums, `required`, and
conditionals that the OAS encodes machine-readably (`if/then/else`, `dependentRequired`).
Deterministic → HIGH confidence; coverage bounded by what the OAS encodes. Runs first; a
structurally-fatal payload short-circuits the run with a clear verdict before `apply`.

**Declarative rule layer (designed-for now, populated later):** L1 and L3 are a **rule catalog that
will grow large**, and most rules are declarative — e.g.
`when tunnel.ipsec then require reachable(ap → me.cluster_ips, udp[500,4500])`. They are expressed
as **data over IR/payload primitives**, not bespoke code per rule. M1 ships the rule-engine seam
and (optionally) one example L3 rule that correctly self-reports `INSUFFICIENT_DATA` because the IR
does not yet populate WAN/Mist-Edge/security/reachability facts. L2 graph logic stays as code
plugins; only the rule-shaped layers are declarative.

### Requirement-derivation rules (named pattern)

A first-class check/rule pattern for L3: **config fires a trigger → derives a requirement →
asserts it against the IR.** Steps: (1) match a config condition (`tunnel.mode == ipsec`),
(2) derive a concrete requirement (`reachable(ap, me.cluster_ips, [udp/500, udp/4500])`),
(3) evaluate it against modeled reachability/policy facts. When the IR lacks the facts to
evaluate step 3, the rule returns `INSUFFICIENT_DATA` per the check contract — never a fake pass.
This generalizes across many Mist features ("X requires reachability to Y on port Z").

## The Mist adapter

The vendor-specific heavy lifting, and the part the prior project struggled most with.

### validate (L0/L1 — payload validation, pre-IR)

Validates each delta payload against the Mist OAS before the IR is built. M1 implements the **thin
structural** subset (types, enums, `required`, machine-readable conditionals). Structurally-fatal
violations short-circuit with a clear verdict; semantic violations become findings and the run
continues. Emits findings into the shared verdict (source = adapter). L1 prose-conditional rules
are deferred to the declarative `rules/` layer.

### ingest (raw → IR)

Reads Mist config + live state for the scope and builds the baseline IR. **Both the baseline IR and
the proposed IR are built from raw config run through *our* `compiler`** — *not* from Mist's
`getSiteSettingDerived`. Mist's derived output is used **only** as the oracle in the equivalence
test (below); it never feeds a live simulation, because there is no derived-API equivalent for a
*proposed* state and we need baseline and proposed to be produced by the identical code path.

- **Config (raw, pre-derive):** switch config, raw site setting, templates in the inheritance
  chain, port profiles/usages, networks, VLANs.
- **Live state (from device stats):** `if_stat` (port up/down/speed), `lldp_stat`/`clients[]`
  (neighbors → links), `module_stat` (VC links, PoE), wired+wireless client list with `vlan_id`,
  AP→switch-port attachment via AP LLDP.
- Builds `Device`/`Port`/`Link`/`Vlan`/`L3Intf`/`Client` with provenance + confidence per fact.

### compiler (effective per-device config)

Derives switch effective config through the inheritance chain
(network template → site template → site setting → device), resolving `{{ vars }}` from
`site_setting.vars`. (Derivation semantics are known/owned by the team.)

**Validation gate (the foundation test):** with a **no-op delta**, our compiled effective config
must equal Mist's `getSiteSettingDerived` for the same site, across many real sites from the
available read-only orgs. This is the oracle for *current* state; there is no oracle for a
*proposed* state, so this equivalence is the contract the whole simulation rests on.

**Comparison rules (so "equal" is unambiguous):**
- **Canonical form** before compare: recursively sort object keys and sort arrays whose order is
  not semantically meaningful (e.g. VLAN lists) by a stable key; normalize types (e.g. `"100"` vs
  `100` where Mist is loose).
- **Defaults:** apply Mist's documented default for an absent field on both sides before compare,
  so "field omitted" and "field set to its default" are treated as equal.
- **Absent vs empty:** treat absent, `null`, and empty (`[]`/`{}`) as equal *only* where Mist
  itself does; otherwise flag — document each such field.
- **Ignored fields:** exclude read-only/server-generated fields (timestamps, `_*` metadata,
  derived counters, stats) via an explicit ignore-list; every ignored field is enumerated, not
  blanket-skipped.
- **"Near-100%":** target is **100% on every in-scope field after the rules above.** Any residual
  diff must be an *explicitly catalogued* known-divergence with a reason; an uncatalogued diff
  **fails the gate**. The metric is "sites with zero uncatalogued field diffs / total sites."

**If we cannot reach this, the foundation is not ready — do not build checks on top.**

### apply (raw + δ → raw')

Applies the delta to the **raw** Mist config in memory per the **Delta semantics** defined in
Scope: each `update` op **replaces** the whole raw object identified by `(object_type, object_id)`
with `payload` (Mist `PUT` semantics — full replacement, not a merge/patch). The result `raw'` is
re-ingested into the proposed IR. No Mist API writes. `apply` does object-level replacement only;
**template-inheritance derivation is the `compiler`'s job**, run afterward on the changed raw
state. Unit tests cover, per supported `object_type`, that the right raw object is replaced and
that the subsequent re-derive reflects the change.

---

## L2 topology check semantics (M1 contracts)

These are binding definitions, not hints — each check's model, severity, and confidence behavior.
**Graph preparation (shared):** before any analysis, the per-VLAN graph is normalized so that
**LAG/MCLAG members collapse to a single logical edge** and **VC fabric is device-internal** (a VC
is one logical node, not a user-visible L2 path). This is what prevents intentional redundancy
from being misread as a cycle. Only "newly introduced by the delta" conditions (present in `IR'`
but not `IR`, per `IRDiff`) are attributed to the change; pre-existing conditions are reported as
context, not as caused by the delta.

### `l2.loop` — a cycle is *not* a loop by itself

Operates on the normalized per-VLAN logical graph. For each cycle found:

| Condition on the cycle's ports | Result | Confidence |
|---|---|---|
| All ports have STP running (RSTP/MSTP/VSTP) | `PASS` — protected redundancy, not a loop | HIGH (STP state known) |
| Any port has STP **disabled** | `FAIL` — unprotected redundant path = loop risk | HIGH |
| STP state **unknown** for any cycle port | `WARN` — "potential loop, STP state unverified" | **LOW** → floors decision to `REVIEW` |

So the finding is "cycle **+ STP disabled/unknown**," never "cycle exists." STP-state
availability from Mist live data is the gating fact (see Open items); when absent, the check
degrades to LOW-confidence `WARN`, not a silent pass.

### `l2.blackhole` — and the VLAN-exit contract (promoted from open item)

A blackhole = a connected component of a VLAN's per-VLAN graph that **contains members** but **has
no path to that VLAN's exit**. Membership has two sources with different bases (because M1 compiles
switch config but **not** WLAN config):

- **Switched side — configuration-based.** A component has members if it contains any access port
  configured for the VLAN or any downstream device/trunk carrying it — **even with zero active
  clients** (a configured-but-empty port still breaks tomorrow). This is switch config we compile;
  it needs no client data.
- **AP / wireless side — observation-based.** Since we don't compile the SSID→VLAN mapping, an AP
  contributes VLAN membership only via its **currently-observed wireless clients' VLANs** (from live
  client data). Not-yet-connected clients on an SSID mapped to that VLAN are a **known coverage gap**:
  the AP-uplink's VLAN coverage is marked `partial`, flooring that case to `REVIEW`, never `SAFE`.
  *(Cheap future upgrade: ingest static SSID→VLAN read-only to make this configuration-based too;
  dynamic/RADIUS-assigned VLANs stay observation-based.)*

The separate `client.impact` check is active-client-only. The **exit** is determined by this
precedence — a core M1 contract, because the check is meaningless without it:

1. **In-scope IRB/SVI** — an `L3Intf(role=irb|svi)` for the VLAN exists on a compiled (switch)
   device and is reachable → that is the exit. **HIGH** confidence.
2. **Boundary uplink** — no in-scope IRB, but the VLAN is carried on a port leading to an
   out-of-scope upstream device (gateway/core not compiled in M1). Confidence follows the **canonical
   fact→confidence table** by how the uplink was identified:
   - **Designated** (operator/template-marked) **or confirmed two-sided LLDP** to a known
     gateway/core → **HIGH**.
   - **Config-inferred role** (guessed from model/name, no link evidence) → **MEDIUM**.
   - **One-sided LLDP only** → **LOW**.
3. **Neither** — no in-scope IRB and no identifiable boundary uplink for the VLAN → the exit
   cannot be located → **`INSUFFICIENT_DATA`** for that VLAN (never `PASS`).

`FAIL` fires when a component had a path to the exit in `IR` and loses it in `IR'` **and** the exit
was HIGH-confidence (rule 1, or rule 2-designated/confirmed). With a MEDIUM or LOW exit the loss is
reported as **`WARN`** → `REVIEW`, per the "FAIL only at HIGH confidence" rule — never a confident
`UNSAFE` built on an unconfirmed exit.

### `l2.vlan_segmentation` — structural broadcast-domain change, no intent needed

Purely structural (topology alone, no policy/intent). It compares a VLAN's per-VLAN graph
partition between `IR` and `IR'`:

- **Split** — a single connected component fragments into ≥2 components → status `WARN` with a
  `WARNING`-severity finding ("broadcast domain partitioned"). HIGH confidence.
- **Expansion/contraction** — the VLAN now reaches new devices/ports, or stops reaching some
  (without a full split) → status `PASS` with an `INFO`-severity finding. HIGH confidence.

It deliberately does **not** judge whether the change is *allowed* (that needs intent we don't
have). It is distinct from `l2.blackhole`: segmentation = "the domain's shape changed";
blackhole = "a piece can't reach its exit." Both may fire on the same delta.

### `client.impact` — who is affected, right now

Enrichment check over the entities the delta touched (`IRDiff`). Enumerates **currently-connected**
clients (wired + wireless) whose connectivity changes, classified by impact type:
`disconnect` (access-port VLAN change), `blackhole` (VLAN segment loses its exit), or
`vlan_move`. Emits the client list (mac / vlan / attachment), contributes it to the related
loop/blackhole findings, and raises `WARN` when ≥1 active client is affected. **HIGH** confidence
for observed clients, with the explicit *currently-connected-only* caveat below.

---

## Observability (first-class)

The verdict must always answer "how/why did it return OK/NOK." Mechanisms:

- **Structured trace per run** (correlation `run_id`): scope resolution → raw fetch → IR build →
  delta apply → IR re-derive → per-check execution, each stage logging inputs, outputs, timing.
- **Per-check evidence record:** which IR facts inspected, what was found, reasoning, coverage,
  confidence (incl. a `PASS` carries evidence too).
- **Replay store:** a **local, file-based run-artifact** capturing `(raw snapshot, ChangePlan,
  verdict)` per run so any verdict is reproducible offline and golden-scenario fixtures are captured
  from real data. This is a **debug/test artifact, not product state** — it is *not* the deferred
  `SnapshotProvider` state backend (that's the on-demand-vs-snapshot data source, still deferred).
  Captured runs are the regression-test substrate for GS1–GS8.
- **Structured logging** throughout, bound to `(run_id, check_id)`.

### Replay redaction (mandatory before storing real org data)

Raw snapshots from real orgs contain identifiers and secrets. The `ReplayStore` redacts on write —
**capturing an un-redacted fixture is a defect, not an option:**

- **Deterministic pseudonymization** for relationship-bearing identifiers (device MACs, IPs,
  hostnames, site/org ids): replace each with a stable hash (same input → same token within a
  fixture) so **topology and graph relationships are preserved** while real values are not.
- **Strip secrets outright** (do not hash): PSKs, RADIUS/shared secrets, API tokens, certificates,
  SNMP communities — null them out; checks must never depend on them.
- **Preserve structure**: VLAN ids, port names, subnet *shapes* (re-mapped into documentation ranges
  if needed) — enough for the checks to run identically on the fixture.
- A redaction allow/deny manifest is versioned with the store; an unredacted field appearing in a
  committed fixture fails CI.

---

## Acceptance criteria — golden scenarios

These are simultaneously the **definition of done** and the **test suite**. Each is run against a
real org's data (read-only). The pairs are deliberate: a twin that cries wolf is as dead as one
that misses things.

| # | Delta | Expected `decision` + verdict |
|---|---|---|
| **GS1** | Remove VLAN 30 from trunk `ge-0/0/1` — the *only* uplink carrying VLAN 30 to downstream switch B (VLAN-30 exit is an in-scope IRB or designated uplink → HIGH-confidence exit) | **`UNSAFE`** — `FAIL` `l2.blackhole`: VLAN 30 segment on B isolated; `client.impact` names active VLAN-30 clients on B; **HIGH** confidence. *Variant: if the exit were only inferred (MEDIUM) → `REVIEW`/`WARN`* |
| **GS2** | Remove VLAN 30 from a trunk where a *second* trunk still carries it (HIGH-confidence exit) | **`SAFE`** — `PASS` (at most `INFO` findings): VLAN 30 still reaches via redundant path. *Proves graph reasoning, not "a trunk changed → panic"* |
| **GS3** | Add/enable a second trunk between two switches creating a redundant L2 path on ports with **STP disabled** (LAG/VC normalized out first) | **`UNSAFE`** — `FAIL` `l2.loop` (unprotected cycle), **HIGH**. *Variant: STP state unknown → **`REVIEW`** / `WARN` at LOW confidence, not FAIL* |
| **GS4** | Change an access port's VLAN from 10→20 on a port with active clients | **`REVIEW`** — `WARN` `client.impact`: N clients on VLAN 10 affected |
| **GS5** | Change a description / cosmetic field only | **`SAFE`** — `PASS`, full coverage, HIGH confidence. *Proves no false positives* |
| **GS6** | A change touching a link/device the data doesn't fully cover | **`REVIEW`** — `INSUFFICIENT_DATA` surfaced, *not* a green pass. *Proves the no-silent-OK machinery* |
| **GS7** | Remove VLAN 30 from the trunk feeding AP `ap-floor2`'s switch port (assumes ≥1 **observed** VLAN-30 wireless client via that AP) | **`UNSAFE`** — `FAIL` `l2.blackhole`: observed VLAN-30 wireless clients via that AP isolated; `client.impact` names affected **wireless** clients; HIGH. *Variant: zero observed clients → `REVIEW` (AP-side VLAN coverage `partial`; future clients unknown)* |
| **GS8** | A delta op with an `UNSUPPORTED` `object_type` (e.g. `switchtemplate`) or one that fans out beyond the single site | **`UNKNOWN`** — `UNSUPPORTED` reason; **never** a green verdict. *Proves the honest-boundary gate* |

**Caveat (stated, not a bug):** client impact is reported for **currently-connected** clients — a
moment-in-time read. The verdict is valid "as of now," consistent with the on-demand model.

---

## Testing strategy

- **Compiler equivalence (foundation gate):** no-op delta → our derive == `getSiteSettingDerived`
  across many real sites.
- **L0 schema validation:** payloads with type/enum/required/conditional violations → correct
  findings; a valid payload → no L0 findings; a structurally-fatal payload → short-circuit verdict.
- **Two-source verdict:** assert findings from both `adapter.validate` and the check registry land
  in one verdict with correct aggregation.
- **Unit tests per check:** synthetic IRs with known issues; assert status, severity, findings,
  coverage, and confidence (including the `INSUFFICIENT_DATA` and no-false-positive cases).
- **Unit tests for `apply`:** full-object replacement per supported `object_type` (right object
  replaced; subsequent re-derive reflects the change).
- **Scope/field gate:** unsupported `object_type` → `UNKNOWN`; a supported object whose payload
  also changes an out-of-scope field (e.g. `dhcpd_config`) → `UNKNOWN`, not `SAFE`.
- **Golden-scenario integration tests (GS1–GS8):** real org data via replay fixtures; assert the
  full verdict (including `decision`).
- **Isolation test:** a deliberately-crashing check yields `CHECK_ERROR` + an `operational` finding,
  resolves to `REVIEW` (not `UNSAFE`), and does not affect the run or other checks.
- **Partial-fetch test:** relevant partial failure → affected check `INSUFFICIENT_DATA` → `REVIEW`;
  irrelevant partial failure → recorded in `state_meta`, decision unchanged; total failure → `UNKNOWN`.
- **Capability negotiation:** a check requiring a capability the IR lacks → `INSUFFICIENT_DATA`, not
  a crash or false `PASS`.
- **Replay redaction:** committed fixtures contain no un-redacted MAC/IP/hostname/secret;
  pseudonymization is stable within a fixture (relationships preserved).

---

## Build sequence (Milestone 1)

Each plan below is a separate spec→plan→implement slice (Plan 1–5).

1. **Plan 1 — IR core + indexes + representations** (`ir/`, `representations/`):
   entities (incl. `Port.stp_meta`) with `FactMeta`, validating `IRBuilder` (dup-id + dangling-ref
   rejection for *every* entity), `ir_version` + `capabilities`, `IRDiff`, `ir/indexes.py`, and the
   L2 + per-VLAN representation builders. Pure, no I/O. *(`contracts/` is created when first needed
   in Plan 2/3; analysis + checks are Plan 4.)*
2. **Plan 2 — StateProvider + ingester registry + Mist switch-ingester + compiler + equivalence
   gate + capability wiring.** `MistApiProvider`; `ingest/registry.py` + `ingest/switch.py`;
   `compile/switch.py` + the **equivalence gate** against `getSiteSettingDerived` (*do not proceed
   until it passes*); `engine/capability_check.py`.
3. **Plan 3 — scope gates + L0 validation + apply + derived-impact gate.** `scope/{object,field,
   derived}_gate.py` + `allowlist.py`; `adapters/mist/validate/`; `adapters/mist/apply/` (ordered
   rolling state). Built so validation/gates precede `apply`.
4. **Plan 4 — analysis + checks + verdict/decision.** `analysis/{context,cycles,vlan_reachability,
   exits}.py`; `checks/{base,registry}` + the four `wired/` checks; `verdict/` (finding/coverage/
   confidence/decision/assembly), gating order, isolation (`CHECK_ERROR`→operational), two-source findings.
5. **Plan 5 — drivers + observability + golden scenarios.** `drivers/{cli,mcp_server,render}.py`;
   `observability/` (trace + replay store **with redaction**); **GS1–GS8 green** against real org data.

---

## Open items to resolve during implementation

*(Delta semantics, the VLAN-exit precedence, and the loop model are now binding contracts above,
not open questions.)*

- **STP state availability** from Mist live data — the gating fact for `l2.loop` confidence
  (HIGH when known vs LOW/`REVIEW` when unknown). Confirm which Mist stat exposes per-port STP
  state; if unavailable, GS3 lands on the LOW-confidence path by design.
- **Boundary-uplink identification** — how to reliably mark a port as "leads to an out-of-scope
  upstream gateway/core" (LLDP neighbor role, chassis-id heuristics, or a designated-uplink hint).
- The minimal set of Mist API calls required (config, derived, device stats, client list) and how
  the `ScopeResolver` decides single-site vs fan-out for the `UNSUPPORTED` gate.
- **OAS conditional coverage (drives L1 scope):** how much of "xyz required if abc==true" is
  machine-readable in Mist's OAS vs prose-only. Gates how much L1 can be OAS-driven vs
  hand-authored declarative rules — deliberately kept off the M1 critical path.
- Source/format of the Mist OAS used by L0 (bundled spec version vs fetched), and how schema
  drift is handled when Mist updates the API.
- **Exact leaf paths for the field allowlist** (raw and effective) pinned against the Mist OAS — the
  in-scope tables are named subtrees; implementation must enumerate the precise leaves and their
  out-of-scope siblings (especially under `networks.*` and `port_config.*`).
