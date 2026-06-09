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

An AI agent proposes a change as a JSON delta. **Product-vision example** (multi-object, org
templates — *not* all supported in M1; see *Supported delta types*):

```json
[
  {"action": "update", "order": 0, "object_type": "switchtemplate", "object_id": "xxxx", "payload": { /* Mist API schema */ }},
  {"action": "update", "order": 1, "object_type": "gatewaytemplate", "object_id": "yyyy", "payload": { /* Mist API schema */ }}
]
```

**M1-valid example** (single site, switch-relevant — what the MVP actually simulates):

```json
[
  {"action": "update", "order": 0, "object_type": "site_setting", "object_id": "<site_id>", "payload": { /* full site setting: networks, port_usages, ... */ }},
  {"action": "update", "order": 1, "object_type": "device", "object_id": "<switch_device_id>", "payload": { /* full switch device config: port_config, ... */ }}
]
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

**Field-level gate (because a delta is a *full-object* replacement).** Since `payload` replaces the
whole object, a single op can change fields M1 cannot reason about. `ScopeResolver` diffs the
payload against current raw state and inspects **which fields actually changed**:

- **In-scope (simulated):** `site_setting.networks`, `site_setting.port_usages`,
  switch-relevant VLAN/port fields; `device.port_config` and switch port/VLAN fields.
- **Out-of-scope → `UNKNOWN`:** any change touching `dhcpd_config` (DHCP — explicitly out of
  scope), WLAN/RF fields, gateway/WAN fields, NAC, or any field not in the in-scope set. The twin
  must **not** return `SAFE` on the strength of L2 analysis alone when the same payload also
  changed something it didn't evaluate.
- A changed field that is purely cosmetic (e.g. `notes`, `name`) is in-scope and harmless.

**Gating outcomes:**
- Unwhitelisted `object_type`, or a switch `device` that is actually a gateway/AP → `UNKNOWN`.
- A supported object whose **changed fields** include any out-of-scope field → `UNKNOWN`
  (`decision_reasons` names the offending field).
- A supported op that `ScopeResolver` finds would impact **more than the one in-scope site**
  (fan-out) → `UNKNOWN`.

All `UNKNOWN` outcomes carry an `UNSUPPORTED` reason and are **never a green verdict**.

### Delta semantics (resolve before `apply` and L0)

A delta op is **a full-object replacement**, matching Mist's `PUT` semantics: `payload` is the
*complete* new object, not a JSON-merge-patch or partial. Consequences:
- `apply` **replaces** the raw object in state with `payload` (it does not merge fields).
- L0 `required`/conditional validation runs against the **full** object.
- **Do not conflate this with inheritance merge.** Object-replacement (`apply`) and
  template-inheritance derivation (`compiler`) are separate steps: `apply` swaps the raw object,
  then `compiler` re-derives the effective config from the changed raw state.
- `action` values in M1: `update` (replace existing object). `create`/`delete` are out of M1.

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

`simulate(delta) → verdict`, no side effects on Mist. Internally, a fixed pipeline where every
stage is a swappable seam:

```
                  ┌─────────────────────────────────────────────────────────┐
   delta JSON ──▶ │                    SIMULATION ENGINE                     │ ──▶ verdict doc
 [{action,order,  │  1. ScopeResolver     supported? single-site? else       │   (decision +
  object_type,    │       └─ UNSUPPORTED / fan-out → decision: UNKNOWN        │    severity +
  object_id,      │  2. Adapter.validate  L0/L1 payload validation (vendor)  │    per-check evidence
  payload}]       │       └─ structurally-fatal → short-circuit verdict      │    + coverage map
                  │  3. StateProvider     fetch raw vendor state for scope   │    + confidence summary
                  │  4. Adapter.ingest    raw → IR  (baseline)               │    + IR diff + trace)
                  │  5. Adapter.apply     raw + δ → raw'                      │
                  │  6. Adapter.ingest    raw' → IR' (proposed)              │
                  │  7. IRDiff            IR vs IR'                           │
                  │  8. CheckRegistry     run L2/L3 checks on (IR, IR', δ)    │
                  │  9. VerdictBuilder    aggregate findings → decision       │
                  └─────────────────────────────────────────────────────────┘
```

The verdict has **two finding sources**, both using the same `Finding`/severity/confidence model:
the vendor adapter's payload validation (L0/L1, step 2) and the neutral check registry (L2/L3,
step 8). This keeps schema/semantic validation — which is vendor-specific and reads raw payload —
out of the neutral check layer, while still surfacing it in one unified verdict.

### Seams (this is where modularity lives)

| Seam | Responsibility | Milestone 1 impl | Swappable to |
|---|---|---|---|
| `StateProvider` | raw vendor state for a scope | `MistApiProvider` (on-demand) | `SnapshotProvider` |
| `VendorAdapter` | `validate(δ)`, `ingest(raw)→IR`, `apply(raw,δ)→raw'` | `MistAdapter` | `ArubaAdapter` |
| `Check` | inspect `(IR, IR', δ)` → evidence | wired/Wi-Fi-aware checks | any new "test scope" |
| `Driver` | drive the engine | `cli` + `mcp` | `http` / `ui` |

### Two hard rules that keep it modular

1. **Checks only ever see the IR** (+ the IR diff + the delta). They never import vendor code.
   This is what lets a second vendor reuse every check, and lets a new check be added without
   touching the engine.
2. **Vendor specifics live only in the adapter.** `ingest`, the config compiler, and `apply` are
   the *only* places that know Mist's JSON shape and inheritance rules.

### Compositional ingest (cross-vendor enabler, cheap now)

Ingest is compositional, not monolithic. Each adapter contributes entities into the same IR; a
`merge/reconcile` step stitches them on **vendor-neutral identity** (MAC, LLDP system-name,
subnet — never a vendor `object_id`). In Milestone 1 there is one adapter, so reconcile is a
pass-through. The delta envelope carries an implicit `source: "mist"` so a delta-router can later
route each op to the owning adapter's `apply`. This is the only "future" work done now, and it is
one field plus a no-op step.

### Module layout

```
digital_twin/
├── engine/          # pipeline orchestration, run lifecycle, trace
├── ir/              # vendor-neutral model: devices, ports, links, vlans, l3, clients (+ graphs)
├── providers/       # StateProvider interface + MistApiProvider
├── adapters/mist/   # validate (L0/L1 OAS), ingest (raw→IR), compiler (inheritance), apply (raw+δ→raw')
├── checks/          # Check interface + registry + wired/ checks (L2/L3 plugins)
├── rules/           # declarative rule engine (L1/L3 "when <cond> then <require>") + rule catalog
├── verdict/         # evidence model, coverage, confidence, severity aggregation, IR diff
├── drivers/         # cli.py, mcp_server.py
└── observability/   # structured logging, run trace, replay store
```

The central lesson from the prior project: keep `ingest`/`compiler`/`apply` (vendor-specific,
the hard 80%) **strictly separated** from `checks` (vendor-neutral, the valuable part).

### Stack

Python. The difficulty is the network modeling; `networkx` (graph/VLAN/loop analysis) and
`netaddr` (IP/subnet) are the best fit, plus a solid MCP Python SDK and existing Mist tooling.

---

## The Intermediate Representation (IR)

A **typed domain model** (source of truth) plus **derived graphs** (computed views the checks
consume). Snapshots are **immutable**: `ingest` yields a frozen baseline IR, `apply`+`ingest`
yields a frozen proposed IR. Nothing mutates in place — a run is reproducible from `(raw, δ)`.

### Entities (Milestone 1 — namespaced so new domains bolt on without touching these)

```
core domain (Milestone 1):
  Device    id, role(switch|gateway|ap|mistedge), model, site, vc_members
  Port      device_id, name, mode(access|trunk), speed, poe, profile, native_vlan, tagged_vlans,
            stp_enabled, stp_mode(rstp|mstp|vstp|none), stp_state, stp_provenance(observed|config|unknown)
  Link      a_port, b_port, kind(physical|lag|mclag|vc), source(lldp|config), bidirectional
  Vlan      vlan_id, name, scope(site|org)
  L3Intf    device_id, vlan_id|port, subnet, ip, role(irb|svi|wan|loopback)   # minimal: VLAN "exit"
  Client    mac, attach(port|ap), vlan, ip, kind(wired|wireless), active

future domains (entity types only, added later, zero change to the above):
  wan/    Tunnel, Peer, Path        policy/  Acl, FwRule, Service
  nac/    AuthPolicy, AuthServer    svc/     DhcpServer, RadSec
  routing/ OspfAdj, BgpPeer, StpInfo, IpsecSa
```

### Derived graphs (built once per IR snapshot, cached on it)

- **L2 graph** — devices+ports as nodes, physical links as edges, with **LAG/MCLAG collapsed to a
  single logical edge and VC treated as device-internal** → reachability, connected components.
- **Per-VLAN graph** — subgraph restricted to links that *carry* that VLAN → input to **loop** and
  **blackhole** detection. Exact models, severity, and confidence behavior are binding contracts
  in *L2 topology check semantics (M1 contracts)* — a graph cycle is **not** a loop by itself.
- **L3 graph** (minimal in M1) — VLAN→exit, where "exit" follows the precedence contract in the
  blackhole section (in-scope IRB → boundary uplink → else `INSUFFICIENT_DATA`).

### Three properties that make the IR trustworthy and diffable

1. **Every fact carries provenance + confidence.** A `Link` records `source: lldp` and whether it
   was seen from both sides (`bidirectional`). Checks read this and downgrade their own confidence
   when they lean on a weak fact.
2. **Stable identity.** Every entity `id` derives from stable keys (`device.mac + port.name`,
   subnet, LLDP system-name), so baseline and proposed entities line up and `IRDiff` is a clean
   structural delta. This same property enables future cross-vendor reconciliation.
3. **Immutable snapshots.** Frozen IRs make runs reproducible and replayable.

### Extensibility

Adding WAN/NAC later adds new entity types and new derived graphs; it never modifies core
entities or existing checks. A WAN check simply asks the IR for `Tunnel`/`BgpPeer` facts that a
wired-only ingest does not populate.

---

## The check-plugin contract

A check is a self-contained, self-registering plugin. The contract *is* the no-silent-OK guarantee.

```python
class Check(Protocol):
    id: str                  # "wired.l2.loop"
    title: str
    domain: str              # "wired.l2" — groups in the verdict
    default_severity: Severity

    def requires(self) -> list[IRCapability]:
        # IR facts this check needs, e.g. NEEDS_BIDIRECTIONAL_LINKS, NEEDS_ACTIVE_CLIENTS
        ...
    def applies_to(self, delta: Delta, diff: IRDiff) -> bool:
        # cheap predicate: does this change even touch what I reason about?
        ...
    def run(self, ctx: CheckContext) -> CheckResult:
        ...
```

`CheckContext` provides exactly: `baseline_ir`, `proposed_ir`, `diff`, `delta`, and a logger
bound to `(run_id, check_id)`. Nothing vendor-specific.

### Result is evidence, never a bare boolean

Two distinct vocabularies: **Status** (did the check reach a conclusion?) and **Severity** (how bad
is an individual finding?). They are linked but not the same field.

```python
Status   = PASS | WARN | FAIL | NOT_APPLICABLE | INSUFFICIENT_DATA | ERROR   # check-level outcome
Severity = INFO | WARNING | ERROR | CRITICAL                                  # per-finding

class Finding:
    source: "adapter" | "check"    # L0/L1 adapter validation, or an L2/L3 check
    code: str                      # stable machine code, e.g. "l2.blackhole.vlan_isolated"
    severity: Severity
    confidence: Confidence
    message: str                   # human-readable
    affected_entities: list[str]   # IR entity ids
    evidence: dict                 # facts inspected + reasoning data (what made this fire)
    remediation: str | None        # suggested fix, for the agent

class CheckResult:
    check_id: str
    status: Status
    findings: list[Finding]
    coverage: Coverage             # breadth: what I evaluated vs. couldn't
    confidence: Confidence         # soundness: how much I trust the conclusion
    reasoning: str
```

Status and finding severity are linked but distinct: a `PASS` check may still emit `INFO` findings
(e.g. a benign VLAN expansion); `WARN` ⇔ worst finding is `WARNING`; `FAIL` ⇔ a finding at
`ERROR`/`CRITICAL`. **A check emits `FAIL` only at HIGH confidence** — when it is not confident
enough to assert breakage it downgrades to `WARN` or `INSUFFICIENT_DATA`, never a silent stretch to
`FAIL` or `PASS`. This is what keeps `FAIL → UNSAFE` an always-confident assertion (and resolves the
boundary-uplink case below: an only-*inferred* exit yields `WARN`, not `FAIL`).

### The six check statuses are the whole anti-silent-OK machine

| status | meaning | verdict treatment |
|---|---|---|
| `PASS` | evaluated, clean | green |
| `WARN`/`FAIL` | evaluated, found something | severity-coded with findings |
| `NOT_APPLICABLE` | delta doesn't touch my domain | legitimately silent |
| `INSUFFICIENT_DATA` | delta **does** touch my domain but I lacked IR facts to judge | **surfaced as a coverage gap — never folded into "OK"** |
| `ERROR` | the check crashed | isolated, surfaced as a gap |

The `INSUFFICIENT_DATA` vs `NOT_APPLICABLE` split is what makes "why did it say OK" answerable.

### Coverage vs Confidence — two distinct axes

- **Coverage** = *did I look at it?* (breadth: "evaluated 412/418 trunk links").
- **Confidence** = *how much do I trust what I concluded?* (soundness).

```python
class Confidence:
    level: HIGH | MEDIUM | LOW
    reasons: list[str]   # ["link wan-core↔dist1 one-sided LLDP", "STP root prediction is heuristic"]
```

Confidence composes from two sources and a **finding's confidence is bounded by the weakest fact
it relied on**:

1. **Fact-level** (IR provenance): two-sided LLDP `HIGH`; one-sided `LOW`; inferred `MEDIUM`.
2. **Inference-level** (method): exact `networkx` cycle search `HIGH`; heuristic `MEDIUM`.

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
| **`UNKNOWN`** | "I could not simulate this — do not apply, do not assume safe." | Any op is `UNSUPPORTED`; a structurally-fatal L0 violation short-circuited the run; or scope/state could not be fetched. |
| **`UNSAFE`** | "This will break something — do not apply." | Any finding at `error`/`critical` severity, i.e. any check returned `FAIL`. |
| **`REVIEW`** | "Possible issue or a blind spot — a human/agent must look before applying." | Any `WARN` finding; **or** any applicable check returned `INSUFFICIENT_DATA`/`ERROR`; **or** any finding (incl. would-be-PASS) carries `LOW`/`MEDIUM` confidence; **or** coverage is `partial`/`insufficient` on an applicable domain. |
| **`SAFE`** | "Fully evaluated, fully covered, high confidence, clean." | All applicable checks `PASS`, **no** L0/L1 findings above `info`, coverage complete on every applicable domain, all confidence `HIGH`. |

Precedence is strict: `UNKNOWN` > `UNSAFE` > `REVIEW` > `SAFE`. The key invariant: **a blind spot
(`INSUFFICIENT_DATA`, partial coverage, or non-HIGH confidence) can never resolve to `SAFE`** — it
floors at `REVIEW`. `decision_reasons` always lists the specific drivers so the agent can branch
(e.g. `REVIEW` + "low confidence: one-sided LLDP link" → gather more data vs. ask the user).

> Note: the separate **apply module** (out of scope here) is the only consumer allowed to act on
> `SAFE` automatically, and only behind whatever policy gate it defines. The twin only *reports*.

### Registration & isolation (the "don't break the rest" rule)

- Checks live in `checks/wired/` and self-register (decorator/entry-point). Adding one is dropping
  in a file; the engine discovers it.
- The engine runs each check **in isolation**: a raised exception → `status: ERROR`, logged with
  its `check_id`, run continues. One bad check cannot take down the run or another check.

**Gating order (strict — this ordering is itself a contract):** for each check the engine does,
in this sequence:

1. Compute `IRDiff` (`IR` vs `IR'`).
2. **`applies_to(delta, diff)`** — if `False` → **`NOT_APPLICABLE`**, stop. *This is checked
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
no path to that VLAN's exit**. **Membership is configuration-based, not client-based** — a
component counts as having members if it contains any: access port configured for the VLAN, AP
uplink whose AP serves an SSID on the VLAN, or downstream device/trunk carrying the VLAN — **even
with zero currently-active clients.** (A port with no client today still breaks tomorrow.) The
separate `client.impact` check is the only one that is active-client-only; blackhole fires on
configured topology regardless of who is connected. The **exit** is determined by this precedence —
a core M1 contract, because the check is meaningless without it:

1. **In-scope IRB/SVI** — an `L3Intf(role=irb|svi)` for the VLAN exists on a compiled (switch)
   device and is reachable → that is the exit. **HIGH** confidence.
2. **Boundary uplink** — no in-scope IRB, but the VLAN is carried on a port leading to an
   out-of-scope upstream device (gateway/core not compiled in M1). Confidence depends on *how* the
   uplink was identified:
   - **Designated** (operator/template-marked uplink) **or confirmed two-sided LLDP** to a device
     whose role is known gateway/core → **HIGH** confidence.
   - **Inferred** heuristically (one-sided LLDP, role guessed) → **MEDIUM** confidence.
3. **Neither** — no in-scope IRB and no identifiable boundary uplink for the VLAN → the exit
   cannot be located → **`INSUFFICIENT_DATA`** for that VLAN (never `PASS`).

`FAIL` fires when a component had a path to the exit in `IR` and loses it in `IR'` **and** the exit
was HIGH-confidence (rule 1, or rule 2-designated/confirmed). With only a **MEDIUM**-confidence
(inferred) exit, the loss is reported as **`WARN`** → `REVIEW`, per the "FAIL only at HIGH
confidence" rule — never a confident `UNSAFE` built on a guessed exit.

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
- **Replay store:** a **local, file-based run-artifact** capturing `(raw snapshot, delta, verdict)`
  per run so any verdict is reproducible offline and golden-scenario fixtures are captured from real
  data. This is a **debug/test artifact, not product state** — it is *not* the deferred
  `SnapshotProvider` state backend (that's the on-demand-vs-snapshot data source, still deferred).
  Captured runs are the regression-test substrate for GS1–GS8.
- **Structured logging** throughout, bound to `(run_id, check_id)`.

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
| **GS7** | Remove VLAN 30 from the trunk feeding AP `ap-floor2`'s switch port | **`UNSAFE`** — `FAIL` `l2.blackhole`: wireless clients on VLAN 30 via that AP isolated; `client.impact` names affected **wireless** clients; HIGH confidence |
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
  full verdict.
- **Isolation test:** a deliberately-crashing check yields `ERROR` and does not affect the run or
  other checks.

---

## Build sequence (Milestone 1)

1. **IR model + graphs** (`ir/`) — entities (incl. `Port` STP fields), provenance/confidence,
   L2 + per-VLAN graph builders.
2. **StateProvider + MistApiProvider** — fetch config + live state for a single site, with
   `state_meta` (acquired-at, per-source age, fetch failures, region).
3. **Mist ingest** (raw → IR via our `compiler`) + **compiler equivalence gate** against
   `getSiteSettingDerived` using the comparison rules. *Do not proceed past this until it passes.*
4. **ScopeResolver + thin L0 validation** — object-type **and** field-level gate
   (`UNSUPPORTED`→`UNKNOWN`), plus OAS-driven L0 (`adapters/mist/validate.py`) and the `rules/`
   engine seam. Built **before** `apply` because validation runs before apply in the pipeline.
5. **apply** (raw + δ → raw', **full-object replacement**) with per-`object_type` tests.
6. **Check engine + registry + verdict** — gating order, isolation, two-source findings, and the
   `decision` (SAFE|REVIEW|UNSAFE|UNKNOWN) aggregation.
7. **The four L2 checks:** `l2.loop`, `l2.blackhole`, `l2.vlan_segmentation`, `client.impact`
   (Wi-Fi-aware via live wireless clients).
8. **Drivers:** CLI, then MCP tool.
9. **Observability:** trace + evidence + replay store (wired in from step 6 onward).
10. **Golden scenarios GS1–GS8 green** against real org data.

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
