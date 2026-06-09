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

An AI agent proposes a change as a JSON delta:

```json
[
  {"action": "update", "order": 0, "object_type": "switchtemplate", "object_id": "xxxx", "payload": { /* Mist API schema */ }},
  {"action": "update", "order": 1, "object_type": "gatewaytemplate", "object_id": "yyyy", "payload": { /* Mist API schema */ }}
]
```

The twin **simulates** it against the current network state and returns a **verdict document**:
per-check evidence, overall severity, a coverage map, a confidence summary, and the IR diff the
change produced. The twin has **no side effects** — applying changes is a separate module, out of
scope here.

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
- **Fully-managed networks only.** All forwarding devices are Mist/Junos with reliable
  two-sided LLDP. We do not attempt to model what we cannot see.
- **On-demand data.** Each simulate call fetches current Mist state for the affected scope,
  builds the IR fresh, applies the delta in memory, runs checks, discards.
- **Drivers:** CLI (for testing) + MCP tool (for the agent loop).
- **~4 topology checks (L2):** `l2.loop`, `l2.blackhole`, `l2.vlan_segmentation`, `client.impact`.
- **Thin schema validation (L0):** basic structural validation of the delta payload against the
  Mist OAS (types, enums, required, machine-readable conditionals), plus the two-source verdict
  plumbing. Deterministic, HIGH confidence.

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
 [{action,order,  │  1. ScopeResolver     what objects/sites does δ touch?   │   (severity +
  object_type,    │  2. Adapter.validate  L0/L1 payload validation (vendor)  │    per-check evidence
  object_id,      │       └─ structurally-fatal → short-circuit verdict      │    + coverage map
  payload}]       │  3. StateProvider     fetch raw vendor state for scope   │    + confidence summary
                  │  4. Adapter.ingest    raw → IR  (baseline)               │    + IR diff + trace)
                  │  5. Adapter.apply     raw + δ → raw'                      │
                  │  6. Adapter.ingest    raw' → IR' (proposed)              │
                  │  7. IRDiff            IR vs IR'                           │
                  │  8. CheckRegistry     run L2/L3 checks on (IR, IR', δ)    │
                  │  9. VerdictBuilder    aggregate findings from BOTH        │
                  │                       sources (validate + checks)        │
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
  Port      device_id, name, mode(access|trunk), speed, poe, profile, native_vlan, tagged_vlans
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

- **L2 graph** — devices+ports as nodes, physical/LAG/VC links as edges → reachability,
  connected components.
- **Per-VLAN graph** — subgraph restricted to links that *carry* that VLAN → **loop detection**
  (cycle finding) and **blackhole detection** (a segment with VLAN members but no path to the
  VLAN's exit/gateway).
- **L3 graph** (minimal in M1) — VLAN→exit (IRB/uplink) used by the blackhole check.

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

```python
class CheckResult:
    status: PASS | WARN | FAIL | NOT_APPLICABLE | INSUFFICIENT_DATA | ERROR
    findings: list[Finding]      # severity, message, affected_entity_ids, detail
    coverage: Coverage           # what I evaluated vs. couldn't, and why  (breadth)
    confidence: Confidence       # how much I trust the conclusion         (soundness)
    reasoning: str               # human-readable "here's how I concluded this"
```

### The five statuses are the whole anti-silent-OK machine

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
  overall_severity,
  checks: [ CheckResult, ... ],
  coverage:  { "wired.l2": {evaluated, partial, insufficient, not_applicable}, ... },
  confidence_summary: { high: n, medium: n, low: n, with reasons },
  ir_diff,
  trace_ref,
}
```

### Registration & isolation (the "don't break the rest" rule)

- Checks live in `checks/wired/` and self-register (decorator/entry-point). Adding one is dropping
  in a file; the engine discovers it.
- The engine runs each check **in isolation**: a raised exception → `status: ERROR`, logged with
  its `check_id`, run continues. One bad check cannot take down the run or another check.
- A check whose `requires()` the current IR can't satisfy is auto-marked `INSUFFICIENT_DATA`
  **before** it runs — it never executes blind.

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

Reads Mist config + live state for the scope and builds the baseline IR:

- **Config:** switch config, site setting (derived), port profiles/usages, networks, VLANs.
- **Live state (from device stats):** `if_stat` (port up/down/speed), `lldp_stat`/`clients[]`
  (neighbors → links), `module_stat` (VC links, PoE), wired+wireless client list with `vlan_id`,
  AP→switch-port attachment via AP LLDP.
- Builds `Device`/`Port`/`Link`/`Vlan`/`L3Intf`/`Client` with provenance + confidence per fact.

### compiler (effective per-device config)

Derives switch effective config through the inheritance chain
(network template → site template → site setting → device), resolving `{{ vars }}` from
`site_setting.vars`. (Derivation semantics are known/owned by the team.)

**Validation gate (the foundation test):** with a **no-op delta**, our compiled effective config
must equal Mist's `getSiteSettingDerived` for the same site. We assert this equivalence across
many real sites from the available read-only orgs. This is the oracle for *current* state; there
is no oracle for a *proposed* state, so this equivalence is the contract the whole simulation
rests on. **If we cannot reach near-100% equivalence, the foundation is not ready.**

### apply (raw + δ → raw')

Applies the delta to the **raw** Mist config in memory (PUT = replace / merge per object-type
semantics — to be enumerated per object type during implementation), producing `raw'`, which is
then re-ingested into the proposed IR. No Mist API writes. PUT/merge semantics per `object_type`
are part of the implementation and must be covered by unit tests.

---

## Observability (first-class)

The verdict must always answer "how/why did it return OK/NOK." Mechanisms:

- **Structured trace per run** (correlation `run_id`): scope resolution → raw fetch → IR build →
  delta apply → IR re-derive → per-check execution, each stage logging inputs, outputs, timing.
- **Per-check evidence record:** which IR facts inspected, what was found, reasoning, coverage,
  confidence (incl. a `PASS` carries evidence too).
- **Replay store:** persist `(raw snapshot, delta)` per run so any verdict is reproducible
  offline. This is the regression-test substrate.
- **Structured logging** throughout, bound to `(run_id, check_id)`.

---

## Acceptance criteria — golden scenarios

These are simultaneously the **definition of done** and the **test suite**. Each is run against a
real org's data (read-only). The pairs are deliberate: a twin that cries wolf is as dead as one
that misses things.

| # | Delta | Expected verdict |
|---|---|---|
| **GS1** | Remove VLAN 30 from trunk `ge-0/0/1` — the *only* uplink carrying VLAN 30 to downstream switch B | **FAIL** `l2.blackhole`: VLAN 30 segment on B isolated; `client.impact` names active VLAN-30 clients on B; **HIGH** confidence |
| **GS2** | Remove VLAN 30 from a trunk where a *second* trunk still carries it | **PASS** (INFO): VLAN 30 still reaches via redundant path. *Proves graph reasoning, not "a trunk changed → panic"* |
| **GS3** | Add/enable a second trunk between two switches creating a redundant L2 path on ports with STP disabled | **FAIL** `l2.loop`: cycle in VLAN graph; HIGH if STP state known |
| **GS4** | Change an access port's VLAN from 10→20 on a port with active clients | **WARN** `client.impact`: N clients on VLAN 10 affected |
| **GS5** | Change a description / cosmetic field only | **PASS**, full coverage, HIGH confidence. *Proves no false positives* |
| **GS6** | A change touching a link/device the data doesn't fully cover | **INSUFFICIENT_DATA**, surfaced — *not* a green pass. *Proves the no-silent-OK machinery* |
| **GS7** | Remove VLAN 30 from the trunk feeding AP `ap-floor2`'s switch port | **FAIL** `l2.blackhole`: wireless clients on VLAN 30 via that AP isolated; `client.impact` names affected **wireless** clients; HIGH confidence |

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
- **Unit tests for `apply`:** PUT/merge semantics per object type.
- **Golden-scenario integration tests (GS1–GS7):** real org data via replay fixtures; assert the
  full verdict.
- **Isolation test:** a deliberately-crashing check yields `ERROR` and does not affect the run or
  other checks.

---

## Build sequence (Milestone 1)

1. **IR model + graphs** (`ir/`) — entities, provenance/confidence, L2 + per-VLAN graph builders.
2. **StateProvider + MistApiProvider** — fetch config + live state for a single site.
3. **Mist ingest** (current state → IR) + **compiler equivalence gate** against
   `getSiteSettingDerived`. *Do not proceed past this until the gate passes.*
4. **apply** (raw + δ → raw') with per-object-type PUT/merge tests.
5. **Check engine + registry + verdict/coverage/confidence aggregation** with isolation, and the
   **two-source verdict** (adapter `validate` findings + check findings).
6. **Thin L0 schema validation** (`adapters/mist/validate.py`) driven by the Mist OAS, plus the
   `rules/` engine seam (no L1/L3 rules populated; optionally one self-`INSUFFICIENT_DATA` example).
7. **The four L2 checks:** `l2.loop`, `l2.blackhole`, `l2.vlan_segmentation`, `client.impact`
   (Wi-Fi-aware via live wireless clients).
8. **Drivers:** CLI, then MCP tool.
9. **Observability:** trace + evidence + replay store (wired in from step 5 onward).
10. **Golden scenarios GS1–GS7 green** against real org data.

---

## Open items to resolve during implementation

- Exact PUT/merge semantics per Mist `object_type` for `apply`.
- The precise definition of a VLAN "exit" for blackhole reasoning when the gateway/IRB is on a
  gateway (out of M1 compile scope) — likely model the upstream uplink port as the exit.
- STP state availability from Mist live data (drives loop-check confidence: HIGH vs MEDIUM).
- The minimal set of Mist API calls required (config, derived, device stats, client list).
- **OAS conditional coverage (drives L1 scope):** how much of "xyz required if abc==true" is
  machine-readable in Mist's OAS vs prose-only. This investigation gates how much L1 can be
  OAS-driven vs hand-authored declarative rules — deliberately kept off the M1 critical path.
- Source/format of the Mist OAS used by L0 (bundled spec version vs fetched), and how schema
  drift is handled when Mist updates the API.
