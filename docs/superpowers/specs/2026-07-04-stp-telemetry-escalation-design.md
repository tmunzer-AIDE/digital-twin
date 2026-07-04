# STP telemetry escalation + self-loop detection — Design

**Status:** Implemented (2026-07-04; plan docs/superpowers/plans/2026-07-04-stp-telemetry-escalation.md)
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
  reporting MAC — but the fact is discarded end to end today: `_emit_links`
  mints a same-device `Link` that `build_l2_graph` silently drops at
  `na == nb`, so `wired.l2.loop` is structurally blind to the one loop class
  RSTP contains silently. A config delta that disables STP protection
  (`stp_disable`) on a self-looped port converts that contained loop into a
  broadcast storm — and
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
2. **`Port.self_loop_peer: str | None`** plus
   **`Port.self_loop_reciprocal: bool`** — when a port-stats row's
   `neighbor_mac` equals the SAME device's chassis MAC, record the claimed
   peer port (via `neighbor_port_desc`). **Reciprocity is evidence-tiering
   (review P1-2):** `self_loop_reciprocal=True` ONLY when BOTH rows exist and
   name each other (A says B and B says A) — that is the genuine two-sided
   observation; a single self-claiming row sets `self_loop_peer` on that port
   alone with `self_loop_reciprocal=False` (the named peer end is NOT
   synthesized). Both fields OBSERVED and **diff-ignored**
   (`_IGNORED_BY_KIND["port"]`, like `is_uplink`) — an observation must never
   be a config diff.
   **Link emission (review P2-3):** current `_emit_links` DOES mint
   same-device links (the L2 graph silently drops them at `na == nb`). This
   slice makes the contract explicit: after capturing the self-loop facts,
   `_emit_links` SKIPS same-device pairs — self-loops mint NO `Link`, the
   fact lives on the ports only, and a test pins `IR.links` containing no
   same-device link.
   **Match rule, v1 (review P2 round 2 — no phantom helpers):** a self-loop
   claim is `row["neighbor_mac"] == row["mac"]` — the row's OWN reporting
   device MAC, the same key the ingest already uses for `device_id`. Nothing
   else: no `neighbor_system_name` fallback (hostnames collide), no VC
   member-MAC map (none exists in `lldp.py` today, and none is built here).
   VC consequence, explicit: a VC member observing ANOTHER member of the same
   chassis has `neighbor_mac != row["mac"]` and is naturally NOT matched — no
   suppression machinery needed; cross-member VC observations remain whatever
   they are today. If a platform ever reports self-loops under a different
   member MAC, that is a future extension with its own live evidence.

### `wired.stp.policy.root_protect_risk` — observed-role route

A second, independent evidence route (no new finding code): the delta enables
`stp_no_root_port` (False/absent→True; tokens excluded as always) on a port
whose **baseline observed `stp_role == "root"`** →

- **ERROR / HIGH** — the observation IS the election result; this route fires
  even where the graph election is unprovable (external root), which is
  precisely where the graph route must degrade to WARNING + note.
- **Proposed-port liveness guard (review P1-1, tightened P1 round 2):** the
  ERROR requires the PROPOSED port to still participate in STP — the guard
  excludes EXACTLY `disabled` and `stp_disable` (which also drives
  `bpdu_filter` — BPDUs stop entirely). **`stp_edge` is NOT in the guard**:
  an edge port self-heals on BPDU receipt (the existing `Port.stp_edge`
  semantics), so root-protect on an observed root port remains a real
  blocking risk even if the same delta flips stp_edge — the ERROR stands.
  Do not widen the guard without live proof that a knob makes root-protect
  unreachable. A
  multi-attribute change that enables `stp_no_root_port` AND removes the port
  from STP participation cannot "block the root port" (there is no root port
  left to protect); the observed-role route stays silent there and the harm
  is carried by the check that owns the removing attribute (`admin_disable`
  for the disable; `wired.stp.edge_on_uplink` for a bpdu-filtered
  inter-switch link; the `stp.policy` floor otherwise).
  **The guard is ROUTE-INDEPENDENT (review round 3): it is a precondition of
  `root_protect_risk` ERROR generally, not just the observed-role shortcut.**
  The L2 graph only drops ADMIN-disabled links — a `bpdu_filter`'d link keeps
  its edge (`l2_graph.py` ~line 112) — so without the shared guard, a
  combined `stp_no_root_port=true` + `stp_disable=true` delta would still
  produce a graph-route ERROR on a port that no longer processes BPDUs.
  Pinned by negative tests asserting NO `root_protect_risk` ERROR from
  EITHER route for: enable root-protect + `disabled=true` in one delta, and
  enable root-protect + `stp_disable=true` in one delta (the graph-route
  variant is the Spec-2 hole this slice closes in passing).
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
| delta disables STP protection on a self-looped port (either end), pair **reciprocal** | **ERROR / HIGH → UNSAFE** — converts a contained, STP-blocked loop into a storm; HIGH is EARNED by the two reciprocal rows, not assumed (review P1-2) |
| same delta, self-loop evidence **one-sided** (single self-claiming row) | **WARNING / MEDIUM** — a stale/wrong `neighbor_port_desc` on one row must not manufacture UNSAFE; ERROR iff HIGH, as everywhere |
| delta otherwise touches a self-looped port (any config change on it or its claimed pair) | **INFO context** — "physical self-loop observed on `<port>` ↔ `<peer>`" |
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
- Trigger-knob grounding (RESOLVED, corrected review round 3): `bpdu_filter`
  is NOT an independent Mist leaf — ingest maps the `stp_disable` config leaf
  onto **`Port.bpdu_filter` ONLY** (switch.py, ~line 983). `Port.stp_enabled`
  is pure OBSERVED telemetry (set from `stp_state` rows in lldp.py) and is
  NEVER driven by config — a check keyed on `stp_enabled` changes would MISS
  the actual config delta. **The `.self_loop` trigger is therefore pinned as:
  the port diff shows `Port.bpdu_filter` False→True** (i.e. the `stp_disable`
  leaf was enabled), on either end of the pair. No other knob without OAS
  evidence.
- Evidence: `ports` (the pair), `observed_states` (state/role both ends when
  present), the protection knob(s) the delta changed; `caused_by` via
  delta_index on the touched port.

### Never-false-SAFE / never-false-UNSAFE

- The observed-root route is escalate-only atop an existing floor:
  `stp.policy`'s floor already covers every `stp_no_root_port` change, so
  the route can only strengthen, never relax.
- **`.self_loop` is a NEW detection path, honestly stated (review round 4):
  there is NO generic `stp_disable` floor today.** `stp_edge_on_uplink`
  covers only modeled switch-to-switch links, and `l2_loop` ranks cycles
  from observed `stp_enabled` on GRAPH cycles — a self-loop has neither, so
  a `stp_disable` delta on a self-looped (or otherwise isolated) port can
  resolve SAFE on main today. This slice closes that: a previously-invisible
  harmful change becomes ERROR/UNSAFE, justified by the reciprocal chassis
  self-match + `Port.bpdu_filter` False→True evidence — a strict verdict
  TIGHTENING (SAFE→UNSAFE), the safe direction. The one-sided tier
  (WARNING/MEDIUM → REVIEW) likewise tightens a today-SAFE case.
  Absent/empty telemetry changes nothing (those cases keep today's behavior).
- False-UNSAFE guardrails: the observed-root route requires the literal role
  string `"root"` (no fuzzy matching); the self-loop ERROR requires the
  chassis-MAC self-match (no hostname heuristics) AND a protection-disabling
  delta on that specific port pair. Both are pinned by negative tests
  (role `"designated"` → no escalation; unrelated port's `stp_disable` → no
  self-loop finding).

## Files touched

- `src/digital_twin/ir/entities.py` — `Port.stp_role`, `Port.self_loop_peer`,
  `Port.self_loop_reciprocal`.
- `src/digital_twin/ir/diff.py` — `self_loop_peer` + `self_loop_reciprocal`
  into `_IGNORED_BY_KIND["port"]` (stp_role mirrors stp_state: compared).
- `src/digital_twin/adapters/mist/ingest/lldp.py` — `_apply_stp` reads role;
  new self-loop pass beside the existing claim handling; `_emit_links` gains
  the explicit same-device skip (P2-3).
- `src/digital_twin/checks/wired/stp_policy.py` — observed-role route.
- `src/digital_twin/checks/wired/l2_loop.py` — `.self_loop` code + gate widening.
- Tests: ingest pins, both check matrices, e2e, never-SAFE guard extension.
- README: no count change (new CODE on an existing check, not a new check);
  update the two checks' one-line descriptions only if wording goes stale.

## Testing requirements

- **Ingest:** role read + `""` absent (both fields, mirroring #43's pin);
  reciprocal pair (both rows name each other) → `self_loop_peer` both ends +
  `self_loop_reciprocal=True`; single self-claiming row → peer set on that
  port only, `reciprocal=False`, named end NOT synthesized; NO self-loop from
  a normal two-device link; **`IR.links` contains no same-device link**
  (P2-3 — this pins the NEW emission skip, not current behavior); both fields
  diff-ignored (config-identical IRs with/without the observation produce no
  port diff).
- **Observed-root route:** ERROR/HIGH on observed root + enable, INCLUDING an
  external-root topology where the graph route alone yields WARNING+note (the
  motivating case); **liveness-guard negative (P1-1): enable root-protect AND
  disable the port (or stp_disable it) in one delta → NO observed-root ERROR**
  (harm carried by admin_disable/floor); `designated`/`backup`/empty role →
  graph route behavior unchanged (negative pins); both-routes case unions
  evidence; disable direction → floor.
- **Self-loop:** reciprocal + `stp_disable` → ERROR/HIGH (either end);
  **one-sided + `stp_disable` → WARNING/MEDIUM, never ERROR (P1-2)**; INFO on
  an unrelated knob change on the pair; silent on a delta elsewhere;
  decision-level UNSAFE for the reciprocal ERROR.
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
