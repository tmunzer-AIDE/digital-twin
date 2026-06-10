# Network Digital Twin

A **simulate-before-apply safety gate** for [Juniper Mist](https://www.mist.com) networks.

An AI agent (or a human) proposes a configuration change as a JSON **ChangePlan**.
The twin fetches the live network state (read-only), builds a vendor-neutral model,
applies the change **in memory**, re-derives the model, runs topology checks, and
returns a **verdict** — before anything touches the network.

```text
decision: UNSAFE
severity: ERROR
  reason: wired.l2.blackhole.exit_lost: vlan 30: member segment loses its path to the irb exit
  check wired.l2.loop: pass (coverage=complete)
  check wired.l2.blackhole: fail (coverage=complete)
  check wired.l2.vlan_segmentation: warn (coverage=complete)
  check wired.client.impact: warn (coverage=complete)
  finding [error] wired.l2.blackhole.exit_lost: vlan 30: member segment loses its path to the irb exit
  finding [warning] wired.client.impact.active_clients: 3 currently-connected client(s) affected by the delta
  state: api.mist.com @ 2026-06-10T05:07:44+00:00 (age 2s)
  trace: 7c1b9e2f40aa
```

The twin **only simulates** — applying changes is a separate, deferred module.

## The verdict contract

The single field an agent acts on is `decision`, with strict precedence
(`UNKNOWN > UNSAFE > REVIEW > SAFE`):

| Decision | Meaning | Exit code |
|---|---|---|
| `UNKNOWN` | Could not simulate this (unsupported scope, fetch failure, fatal payload). Do not apply, do not assume safe. | 30 |
| `UNSAFE` | This will break something — a network finding at ERROR/CRITICAL with HIGH confidence. | 20 |
| `REVIEW` | Possible issue **or a blind spot** (warning, missing data, partial coverage, non-HIGH confidence). A human/agent must look. | 10 |
| `SAFE` | Fully evaluated, fully covered, high confidence, clean. | 0 |

The core invariant: **a blind spot can never resolve to SAFE.** Every conclusion
carries coverage (*did I look at it?*) and confidence (*how much do I trust it?*)
as separate axes, and anything below "complete + HIGH" floors the decision to
REVIEW. Findings carry evidence, affected entities, and stable machine codes.

## Quick start

Requires Python 3.14 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync

# environment (read-only Mist API access)
export MIST_HOST=api.mist.com          # or api.eu.mist.com, ...
export MIST_APITOKEN=...
```

### CLI

```bash
cat > plan.json <<'EOF'
{
  "source": "mist",
  "scope": {"org_id": "<org_id>", "site_id": "<site_id>"},
  "ops": [
    {"action": "update", "order": 0, "object_type": "site_setting",
     "object_id": "<site_id>", "payload": { ...full site setting... }}
  ]
}
EOF

uv run digital-twin --plan plan.json            # human summary, decision exit code
uv run digital-twin --plan plan.json --json     # full verdict document
uv run digital-twin --plan plan.json --replay-store runs/   # capture (redacted) replay
uv run digital-twin --plan plan.json --replay-fixture fx.json  # offline, against a fixture
```

### MCP server

```bash
uv run python -m digital_twin.drivers.mcp_server
```

Exposes one tool, `simulate_change(change_plan) -> verdict document`. The tool
never throws to the agent — internal errors return a normal `UNKNOWN` verdict.

### ChangePlan format

A `ChangeOp.payload` is the **complete new object** (Mist `PUT` semantics — full
replacement, never a merge-patch). `order` defines a strict total order; ops apply
against a rolling state. One op per object.

**M1 supports** `object_type: site_setting` (the in-scope site) and
`object_type: device` (switches only), with a default-deny, leaf-tightened field
allowlist: `networks.*.vlan_id`, `port_usages.*.{mode,port_network,networks,all_networks}`,
`port_config.*` / `local_port_config.*` / `port_config_overwrite.*` (modeled leaves),
`vars.*`, `name`, `notes`. Anything else — including a `vars` edit that *ripples*
into an out-of-scope effective field after template compilation — returns `UNKNOWN`,
never a silently-wrong verdict.

## How it works

```text
ChangePlan ─▶ 1 envelope + object gate     (shape, M1 whitelist, single site)
              2 L0 payload validation      (against the committed Mist OAS)
              3 fetch                      (mistapi, read-only, on-demand)
              4 field gate                 (changed raw leaves vs allowlist, per op,
                                            against the rolling pre-op state)
              5 ingest baseline            (compile templates+site+device → IR)
              6 apply                      (full-object replace, in memory)
              7 ingest proposed            (same code path → IR')
              8 derived-impact gate        (full effective config diff, default-deny)
              9 diff + checks              (registry: gating order + crash isolation)
             10 verdict                    (findings × coverage × confidence → decision)
```

- **IR**: a vendor-neutral typed model (devices, ports, links, VLANs, L3 exits,
  clients) where every fact carries provenance and categorical confidence
  (HIGH/MEDIUM/LOW — never a float). One-sided LLDP stays LOW; a device's report
  about itself is HIGH. Derived conclusions take the MIN of their inputs.
- **Compiler**: re-implements Mist's inheritance (network template → site setting →
  device, `{{vars}}` resolved once, `switch_matching` rules evaluated). Validated
  against Mist's own `getSiteSettingDerived` on real sites — the *equivalence gate*
  the whole simulation rests on (`tools/equivalence_gate.py`).
- **Checks (M1)**: `wired.l2.loop` (a cycle is only a loop without STP),
  `wired.l2.blackhole` (a member segment that loses its VLAN exit),
  `wired.l2.vlan_segmentation` (broadcast-domain shape change),
  `wired.client.impact` (currently-connected wired + wireless clients affected).
  Checks consume only the IR — never raw vendor payloads — so new vendors plug in
  at the adapter seam.
- **Capabilities**: ingesters *earn* capabilities from data they actually produced;
  a check whose requirements aren't met reports `INSUFFICIENT_DATA` (→ REVIEW),
  never a fake pass.

## Project layout

```
src/digital_twin/
├── contracts/        ChangePlan, Finding, Rejection (pure DTOs)
├── ir/               vendor-neutral model + diff + confidence/provenance
├── representations/  L2 multigraph, per-VLAN graphs (pure views)
├── analysis/         cycles, VLAN reachability, exit resolution (memoized)
├── checks/           the four wired checks + registry (the ONLY layer with severity)
├── verdict/          decision precedence, coverage/confidence rollups, assembly
├── scope/            envelope / object / field / derived gates + allowlist data
├── providers/        Mist API fetch (single-site + org-batched multi-site)
├── adapters/mist/    validate (L0/OAS), compile, ingest, apply + facade
├── engine/           the 10-stage pipeline (orchestration only)
├── observability/    trace, structured logging, redacting replay store
└── drivers/          CLI, MCP server, rendering

docs/superpowers/specs/   the converged design spec
docs/superpowers/plans/   the five implementation plans (as-built)
tools/                    probe_fetch, equivalence_gate, capture_replay
tests/golden/             GS1–GS8 acceptance scenarios + redacted real-org fixture
```

## Development

```bash
uv run pytest -q          # full offline suite (live tests excluded by default)
uv run ruff check .
uv run mypy               # strict

# live gates (need MIST_* env + DT_GATE_ORG_ID + DT_GATE_SITE_IDS)
uv run python tools/probe_fetch.py <org_id> <site_id>    # pin real SDK shapes
uv run python tools/equivalence_gate.py                  # compiler vs getSiteSettingDerived
uv run pytest -m live -q

# refresh the golden fixture (redacted on write)
uv run python tools/capture_replay.py <site_id> tests/golden/fixtures/site.json
```

**Golden scenarios (GS1–GS8)** are the acceptance suite and the definition of done:
blackhole on single-uplink removal → UNSAFE; redundant removal → SAFE; unprotected
new cycle → REVIEW/UNSAFE; client VLAN move → REVIEW; cosmetic change → SAFE (no
false positives); missing data → REVIEW (no silent OK); wireless client isolation →
UNSAFE; unsupported object → UNKNOWN. They run offline against a **redacted**
fixture captured from a real org.

**Replay fixtures are redacted on write** — deterministic pseudonymization for
MACs/IPs/UUIDs/names (topology-preserving), wholesale stripping for secrets,
credential command lines, URL credential params and JWTs. A hygiene CI test fails
on any un-redacted identifier or secret-shaped value in a committed fixture.

## Scope (Milestone 1) and roadmap

M1 simulates **one site, switch L2**, with Wi-Fi-aware client impact (observed
wireless clients). Deliberately deferred behind existing seams: the declarative
L1/L3 rule engine, snapshot-based state backend, multi-site/org-template
simulation, the apply module, and additional vendor adapters (the `VendorAdapter`
protocol and the compositional ingester registry are the extension points).
