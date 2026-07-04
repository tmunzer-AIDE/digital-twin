# STP telemetry escalation + self-loop detection — Design

**Status:** PROPOSED
**Date:** 2026-07-04
**Author:** brainstormed with the repo owner

## Problem

PR #43's live investigation proved Mist populates rich per-port STP telemetry
(`stp_state`: forwarding/blocking; `stp_role`: root/designated/backup/
alternate/disabled-bpdu-inconsistent — verified on EX4000@25.4R1) that the twin
mostly ignores:

- `stp_role` is not ingested at all. In particular, **`stp_role: "root"`
  identifies the CURRENT root port** — the live election result — which is
  exactly the fact `wired.stp.policy.root_protect_risk` has to infer from a
  graph election it must often abstain on (external/unprovable root).
- Physical **self-loops** (a cable looped back into the same switch) have a
  clean observed signature — the LLDP `neighbor_mac` equals the device's own
  chassis MAC — but `ingest/lldp.py` deliberately skips same-device claims
  (line ~110) and `build_l2_graph` never sees them, so `wired.l2.loop` is
  structurally blind to the one loop class RSTP contains silently. A config
  delta that disables STP protections (`stp_disable`, `bpdu_filter`) on a
  self-looped port converts that contained loop into a broadcast storm — and
  today the twin would say nothing specific.

Both gaps are fixable from data the twin already fetches, escalate-only
(GS27 pattern): observation sharpens or escalates findings, never suppresses,
never demotes, never earns SAFE.

## Live-verified facts (2026-07-04, TM-LAB SWB-3 `2093390b3580`)

- `xe-0/1/3` (backbone to SWB-2): `stp_role: "root"`, `stp_state: "forwarding"`.
- Self-loop `ge-0/0/8 ↔ ge-0/0/9` (usage `test_pvstp`): `designated/forwarding`
  ↔ `backup/blocking`; both rows carry `neighbor_mac: "2093390b3580"` == own
  chassis MAC (also visible in device-stats `clients[]` with `source: "lldp"`).
- Second pair `ge-0/0/10 ↔ ge-0/0/11` (usage `test_stp`):
  `disabled-bpdu-inconsistent` / `blocking` — BPDU protection triggered.
- Non-participating ports carry `stp_state: ""` / `stp_role: ""` —
  present-but-empty (the #43 falsy-guard convention applies to BOTH fields).
- CAUTION: MCP-shaped stats rows FILTER these fields out; the SDK payload the
  twin ingests carries them. Never treat MCP rows as evidence of API absence.

## Design

### Ingest — two new observed Port facts (one pass, `ingest/lldp.py`)

1. **`Port.stp_role: str | None`** — read in `_apply_stp` beside `stp_state`:
   same falsy/empty-string guard (a row applies if EITHER field is non-empty;
   each field is set only when non-empty), same `stp_meta` OBSERVED live-fact
   treatment, same diff behavior as `stp_state` (compared; baseline and
   proposed share the same fetch, so no spurious diffs). No new capability —
   `STP_STATE` is earned exactly as today (≥1 row with non-empty `stp_state`;
   a role-only row also counts as STP observation and earns it).
2. **`Port.self_loop_peer: str | None`** — the full port id of the OTHER end
   when a port-stats row's `neighbor_mac` equals the SAME device's chassis
   MAC. Set on both ports of the pair (each row names its peer via
   `neighbor_port_desc`; pair them within the device). OBSERVED and
   **diff-ignored** (`_IGNORED_BY_KIND["port"]`, like `is_uplink`) — an
   observation must never be a config diff. The existing same-device skip in
   `_claims`/link-emission stays: self-loops still mint NO `Link` (the L2
   graph correctly never sees them); the fact lives on the ports.
   Chassis-MAC matching only — do NOT match on `neighbor_system_name` alone
   (hostnames collide; MAC is the robust key). VC nuance: match against the
   chassis/member MACs the LLDP ingest already resolves for AP-uplink ties
   (reuse that resolution; a VC member MAC seeing another member of the SAME
   VC is a VC-internal path, not a self-loop — out of scope, do not flag).

### `wired.stp.policy.root_protect_risk` — observed-role route

A second, independent evidence route (no new finding code): the delta enables
`stp_no_root_port` (False/absent→True; tokens excluded as always) on a port
whose **baseline observed `stp_role == "root"`** →

- **ERROR / HIGH** — the observation IS the election result; this route fires
  even where the graph election is unprovable (external root), which is
  precisely where the graph route must degrade to WARNING + note.
- Evidence adds `observed_role: "root"` and
  `severity_reason: "port is the observed root port"`; `election_confidence`
  reports `"observed"` for this route.
- Precedence/interaction: escalate-only. If BOTH routes conclude, keep the
  stronger result and union the evidence (graph `only_path` + observed role).
  The observed route never suppresses the graph route's WARNING/notes when
  the role is NOT "root" — absence of the role observation changes nothing.
- Disable-direction (True→False) stays floor-only regardless of role.
- Staleness honesty: the observation is as fresh as the fetch (state_meta
  timestamp); no extra machinery — same trust level as every OBSERVED fact
  the twin already escalates on (admin_disable precedent).

### `wired.l2.loop.self_loop` — new code on the EXISTING loop check

`wired.l2.loop`'s mission is "a cycle STP is not protecting"; a self-loop is
the degenerate cycle its graph can never carry, detected from `self_loop_peer`
instead. Delta-conditioned tiers (the #42 P3 relevance lesson baked in):

| situation | finding |
|---|---|
| delta enables `stp_disable` or `bpdu_filter` on a self-looped port (either end of the pair) | **ERROR / HIGH → UNSAFE** — converts a contained, STP-blocked loop into a storm; confidence HIGH by construction (the chassis observes ITSELF: two-sided by definition) |
| delta otherwise touches a self-looped port (any config change on it or its pair) | **INFO context** — "physical self-loop observed on `<port>` ↔ `<peer>`" |
| self-loop exists, delta unrelated to those ports | **silent** — ambient facts don't ride along on every verdict |

- INFO never satisfies any floor (established Spec-2 rule; this check has no
  floor of its own — the INFO is pure context).
- No delta-created case exists: `apply_plan` never changes port_stats, so
  `self_loop_peer` is identical in baseline and proposed. The check reads the
  baseline observation and the CONFIG delta only.
- `applies_to`: the existing loop check's gate widens to also apply when a
  port diff touches a self-looped port's protection-relevant fields — keep it
  precise (`changed_fields` style, Spec-2 precedent), not a blanket
  `touches("port")` if the current gate is narrower. (Grounding: read
  `l2_loop.applies_to` first; it may already be broad enough.)
- Trigger-knob grounding (plan task 1): "protection-disabling" = the config
  leaves that ingest maps onto `Port.stp_enabled=False` / `Port.stp_edge` /
  `Port.bpdu_filter` — known: `stp_disable` (in `_STP_USAGE_ATTRS`); verify
  how `bpdu_filter` is configured in Mist (own leaf vs derived from
  `stp_edge`) and pin the exact set in the plan. If a knob turns out not to
  be independently configurable, drop it from the trigger set rather than
  guessing.
- Evidence: `ports` (the pair), `observed_states` (state/role both ends when
  present), the protection knob(s) the delta changed; `caused_by` via
  delta_index on the touched port.

### Never-false-SAFE / never-false-UNSAFE

- Escalate-only throughout: both routes ADD severity on top of contracts that
  already floor REVIEW (`stp.policy`'s floor covers the `stp_no_root_port`
  change; `stp_disable`/`bpdu_filter` are modeled STP usage attrs already in
  scope — verify in the plan which check carries their floor today and that
  the new ERROR only strengthens it). Absent/empty telemetry changes nothing.
- False-UNSAFE guardrails: the observed-root route requires the literal role
  string `"root"` (no fuzzy matching); the self-loop ERROR requires the
  chassis-MAC self-match (no hostname heuristics) AND a protection-disabling
  delta on that specific port pair. Both are pinned by negative tests
  (role `"designated"` → no escalation; unrelated port's `stp_disable` → no
  self-loop finding).

## Files touched

- `src/digital_twin/ir/entities.py` — `Port.stp_role`, `Port.self_loop_peer`.
- `src/digital_twin/ir/diff.py` — `self_loop_peer` into `_IGNORED_BY_KIND["port"]`
  (stp_role mirrors stp_state: compared).
- `src/digital_twin/adapters/mist/ingest/lldp.py` — `_apply_stp` reads role;
  new self-loop pass beside the existing claim handling.
- `src/digital_twin/checks/wired/stp_policy.py` — observed-role route.
- `src/digital_twin/checks/wired/l2_loop.py` — `.self_loop` code + gate widening.
- Tests: ingest pins, both check matrices, e2e, never-SAFE guard extension.
- README: no count change (new CODE on an existing check, not a new check);
  update the two checks' one-line descriptions only if wording goes stale.

## Testing requirements

- **Ingest:** role read + `""` absent (both fields, mirroring #43's pin);
  self-loop pair minted from a chassis-MAC-match fixture shaped like the live
  rows; NO self-loop from a normal two-device link; NO Link minted for the
  pair; `self_loop_peer` diff-ignored (config-identical IRs with/without the
  observation produce no port diff).
- **Observed-root route:** ERROR/HIGH on observed root + enable, INCLUDING an
  external-root topology where the graph route alone yields WARNING+note (the
  motivating case); `designated`/`backup`/empty role → graph route behavior
  unchanged (negative pins); both-routes case unions evidence; disable
  direction → floor.
- **Self-loop:** ERROR/HIGH on `stp_disable` and on `bpdu_filter` (each,
  either end of the pair); INFO on an unrelated knob change on the pair;
  silent on a delta elsewhere; decision-level UNSAFE for the ERROR.
- **Never-SAFE guard:** extend Spec-2's parametrized guard to the new routes'
  fixtures.

## Live verification (read-only — RUN WHILE THE LAB LOOPS ARE STILL CABLED)

Against TM-LAB SWB-3 via the twin CLI (needs GC1-capable creds) or replay of
a fetched fixture:
1. Simulate `stp_no_root_port: true` on the `backbone` usage → `xe-0/1/3` is
   the observed root port → `root_protect_risk` ERROR → UNSAFE, evidence
   `observed_role: "root"`.
2. Simulate `stp_disable: true` on the `test_pvstp` usage → `ge-0/0/8↔9` is a
   live self-loop → `l2.loop.self_loop` ERROR → UNSAFE.
3. Confirm ingest: fetched IR shows `stp_role` on participating ports, `""`
   ports as None, and `self_loop_peer` on both loop pairs.

## Scope and deferred

In scope: the above. Deferred: reachability awareness of observed-blocking
links (blackhole/isolation confidence — a separate, larger spec); the full
STP tree/convergence engine (now viable — per-port state/role ground truth
exists — but still a program, not a slice); VC-internal loop semantics;
`stp_role`-based root-DIRECTION triangulation for the tree engine.
